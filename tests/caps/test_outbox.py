"""§6.14 outbox: cursors from the manifest, fan-out, signed delivery with retries (HTTP mocked), SSRF guard,
secrets, pull/ack, detach/reattach, retry, gc."""
from __future__ import annotations

import json

import pytest

from app import sqldb
from app.caps import outbox
from tests.caps.conftest import EVENT_MANIFEST, attach

HOOK = "https://hooks.example.com/kh"


def _manifest(subs):
    return dict(EVENT_MANIFEST, subscriptions=subs)


@pytest.fixture
def sent(monkeypatch):
    """Mock HTTP: records calls; per-url status in `sent.status` (default 200); unsafe urls go through the guard."""
    calls = []

    def fake(url, body, headers, timeout, hosts_allow=None):
        if "hooks.example.com" not in url:
            outbox.resolve_target(url, hosts_allow)  # raises UnsafeTarget for private targets
        calls.append({"url": url, "body": body, "headers": headers})
        return fake.status

    fake.status = 200
    monkeypatch.setattr(outbox, "http_post", fake)
    calls_obj = type("Sent", (), {})()
    calls_obj.calls = calls
    calls_obj.fake = fake
    return calls_obj


def _ev(client, ns, n=1, kind="step.done"):
    for i in range(n):
        r = client.post(f"/v1/{ns}/events", json={"kind": kind, "body": {"n": i}})
        assert r.status_code == 200, r.text


def _rows(ns, sub_id=None):
    with sqldb.connect() as c:
        q = "SELECT * FROM kh_outbox WHERE ns=?" + (" AND sub_id=?" if sub_id else "") + " ORDER BY event_seq"
        return [dict(zip(r.keys(), tuple(r))) for r in c.execute(q, (ns, sub_id) if sub_id else (ns,)).fetchall()]


def test_kind_filter_and_signature():
    assert outbox.kind_matches(["*"], "x.y")
    assert outbox.kind_matches(["step.*"], "step.done")
    assert not outbox.kind_matches(["step.*"], "steps.done")
    assert outbox.kind_matches(["run.start"], "run.start") and not outbox.kind_matches(["run.start"], "run.end")
    body = b'{"a":1}'
    sig = outbox.sign("s3cret", 1000, body)
    assert sig.startswith("sha256=") and len(sig) == 7 + 64
    assert outbox.verify("s3cret", body, "1000", sig, now_ms=1000 + 299_000)
    assert not outbox.verify("s3cret", body, "1000", sig, now_ms=1000 + 301_000)
    assert not outbox.verify("other", body, "1000", sig, now_ms=1000)
    assert [outbox.backoff_ms(a) for a in (1, 2, 3)] == [1000, 2000, 4000]
    assert outbox.backoff_ms(30) == 3_600_000


def test_ssrf_guard(monkeypatch):
    for url in ("http://hooks.example.com/x", "https://127.0.0.1/x", "https://10.1.2.3/x", "https://169.254.169.254/",
                "https://100.64.0.1/", "https://[::ffff:10.0.0.1]/", "https://0.0.0.0/", "https://224.0.0.1/"):
        with pytest.raises(outbox.UnsafeTarget):
            outbox.resolve_target(url)
    monkeypatch.setenv("KH_OUTBOX_ALLOW_LOCAL", "1")
    assert outbox.resolve_target("http://127.0.0.1:9/x")[3] == "127.0.0.1"
    with pytest.raises(outbox.UnsafeTarget):
        outbox.resolve_target("https://8.8.8.8/x", ["hooks.example.com"])
    assert outbox.resolve_target("https://8.8.8.8/p?q=1")[4] == "/p?q=1"


def test_fanout_deliver_signed(client, sent):
    ns, _, _ = attach(client, manifest=EVENT_MANIFEST)
    _ev(client, ns, 2)  # before the subscription: never delivered
    r = client.put(f"/v1/{ns}/manifest", json=_manifest([{"sub_id": "w1", "kinds": ["step.*"], "url": HOOK}]),
                   headers={"If-Match": "1"})
    assert r.status_code == 200, r.text
    _ev(client, ns, 3)
    client.post(f"/v1/{ns}/events", json={"kind": "secret.ev", "body": {}})
    assert outbox.cron_fanout() >= 3
    rows = _rows(ns, "w1")
    assert len(rows) == 3 and all(r["state"] == "pending" for r in rows)
    assert rows[0]["delivery_id"] == f"w1:{rows[0]['event_seq']}"
    outbox.cron_fanout()  # repeat is a no-op
    assert len(_rows(ns, "w1")) == 3
    outbox.cron_deliver()
    mine = [c for c in sent.calls if c["headers"]["X-KH-Namespace"] == ns]
    assert len(mine) == 3
    secret = client.get(f"/v1/{ns}/subscriptions/w1/secret").json()["secret"]
    c0 = mine[0]
    h = c0["headers"]
    assert outbox.verify(secret, c0["body"], h["X-KH-Timestamp"], h["X-KH-Signature"])
    payload = json.loads(c0["body"])
    assert set(payload) == {"delivery_id", "ns", "sub_id", "event"}
    assert payload["event"]["kind"] == "step.done" and payload["delivery_id"] == h["X-KH-Delivery"]
    assert b" " not in c0["body"].split(b'"body"')[0]  # compact JSON
    assert all(r["state"] == "done" for r in _rows(ns, "w1"))
    subs = client.get(f"/v1/{ns}/subscriptions").json()["subscriptions"]
    assert subs[0]["deliveries"]["done"] == 3 and subs[0]["secret"] == "kh"


def test_retries_dead_and_retry_route(client, sent):
    ns, _, _ = attach(client, manifest=_manifest([{"sub_id": "w1", "kinds": ["*"], "url": HOOK}]))
    _ev(client, ns, 1)
    outbox.cron_fanout()
    sent.fake.status = 500
    outbox.cron_deliver()
    row = [r for r in _rows(ns, "w1") if r["state"] != "done"][0]
    assert row["state"] == "pending" and row["attempts"] == 1 and row["last_status"] == 500
    with sqldb.connect() as c:
        c.execute("UPDATE kh_outbox SET attempts=11, next_ms=0 WHERE ns=?", (ns,))
    outbox.cron_deliver()
    dead = _rows(ns, "w1")
    assert all(r["state"] == "dead" for r in dead) and dead[0]["attempts"] == 12
    lst = client.get(f"/v1/{ns}/outbox", params={"state": "dead"}).json()["deliveries"]
    did = lst[0]["delivery_id"]
    r = client.post(f"/v1/{ns}/outbox/{did}/retry")
    assert r.status_code == 200 and r.json()["state"] == "pending" and r.json()["attempts"] == 0
    assert client.post(f"/v1/{ns}/outbox/{did}/retry").status_code == 409
    assert client.post(f"/v1/{ns}/outbox/nope:1/retry").status_code == 404
    sent.fake.status = 204
    outbox.cron_deliver()
    assert {r["delivery_id"]: r["state"] for r in _rows(ns, "w1")}[did] == "done"
    # gc keeps fresh done rows, drops old ones
    with sqldb.connect() as c:
        c.execute("UPDATE kh_outbox SET done_ms=1 WHERE ns=?", (ns,))
    outbox.cron_gc()
    assert [r["state"] for r in _rows(ns, "w1")] == ["dead"]  # only done rows are collected


def test_unsafe_target_dead(client, sent, monkeypatch):
    monkeypatch.setenv("KH_OUTBOX_ALLOW_LOCAL", "")
    ns, _, _ = attach(client, manifest=_manifest([{"sub_id": "bad", "kinds": ["*"], "url": "https://10.0.0.5/h"}]))
    _ev(client, ns, 1)
    outbox.cron_fanout()
    outbox.cron_deliver()
    rows = _rows(ns, "bad")
    assert rows[0]["state"] == "dead" and rows[0]["last_error"].startswith("unsafe_target")


def test_secrets(client, monkeypatch):
    monkeypatch.setenv("KH_TEST_HOOK_SECRET", "envsecret")
    ns, _, _ = attach(client, manifest=_manifest([
        {"sub_id": "w1", "kinds": ["*"], "url": HOOK},
        {"sub_id": "w2", "kinds": ["*"], "url": HOOK, "secret_ref": "env:KH_TEST_HOOK_SECRET"}]))
    s1 = client.get(f"/v1/{ns}/subscriptions/w1/secret").json()
    assert s1["secret"].startswith("whsec_")
    s2 = client.post(f"/v1/{ns}/subscriptions/w1/secret/rotate").json()
    assert s2["secret"] != s1["secret"] and s2["rotated"] is True
    assert client.get(f"/v1/{ns}/subscriptions/w1/secret").json()["secret"] == s2["secret"]
    r = client.get(f"/v1/{ns}/subscriptions/w2/secret")
    assert r.status_code == 409 and r.json()["error"] == "secret_external"
    assert client.post(f"/v1/{ns}/subscriptions/w2/secret/rotate").status_code == 409
    assert client.get(f"/v1/{ns}/subscriptions/zz/secret").status_code == 404
    with sqldb.connect() as c:
        assert outbox.secret_for(c, ns, "w2", "env:KH_TEST_HOOK_SECRET") == "envsecret"


def test_pull_ack(client):
    ns, _, _ = attach(client, manifest=EVENT_MANIFEST)
    _ev(client, ns, 2)
    client.put(f"/v1/{ns}/manifest", json=_manifest([{"sub_id": "p1", "kinds": ["step.*"]}]),
               headers={"If-Match": "1"})
    _ev(client, ns, 3)
    client.post(f"/v1/{ns}/events", json={"kind": "run.start", "run_id": "r", "body": {}})
    r = client.get(f"/v1/{ns}/subscriptions/p1/pull")
    j = r.json()
    assert r.status_code == 200, r.text
    assert [e["body"]["n"] for e in j["events"]] == [0, 1, 2] and all(e["kind"] == "step.done" for e in j["events"])
    start = j["acked_seq"]
    assert j["next_after_seq"] > j["events"][-1]["seq"] - 1
    # pull does not move the stored cursor; ack does (monotonic)
    assert client.get(f"/v1/{ns}/subscriptions/p1/pull").json()["acked_seq"] == start
    seq2 = j["events"][1]["seq"]
    assert client.post(f"/v1/{ns}/subscriptions/p1/ack", json={"after_seq": seq2}).json()["acked_seq"] == seq2
    assert client.post(f"/v1/{ns}/subscriptions/p1/ack", json={"after_seq": start}).json()["acked_seq"] == seq2
    j = client.get(f"/v1/{ns}/subscriptions/p1/pull").json()
    assert [e["body"]["n"] for e in j["events"]] == [2]
    assert client.get(f"/v1/{ns}/subscriptions/p1/pull", params={"after_seq": 0, "limit": 1}).json()["events"][0][
        "body"]["n"] == 0
    assert client.post(f"/v1/{ns}/subscriptions/p1/ack", json={"after_seq": 10 ** 9}).status_code == 422
    assert client.post(f"/v1/{ns}/subscriptions/p1/ack", json={}).status_code == 422
    assert client.get(f"/v1/{ns}/subscriptions/zz/pull").status_code == 404
    assert client.get(f"/v1/{ns}/subscriptions/p1/pull", params={"wait_s": 0.3}).status_code == 200


def test_detach_reattach(client, sent):
    ns, _, _ = attach(client, manifest=_manifest([{"sub_id": "w1", "kinds": ["*"], "url": HOOK}]))
    _ev(client, ns, 2)
    outbox.cron_fanout()
    with sqldb.connect() as c:
        c.execute("UPDATE kh_outbox SET state='dead', last_error='HTTP 500' WHERE ns=? AND event_seq=("
                  "SELECT MIN(event_seq) FROM kh_outbox WHERE ns=?)", (ns, ns))
    assert client.post(f"/v1/admin/ns/{ns}:detach").status_code == 200
    rows = _rows(ns)
    assert all(r["state"] == "dead" for r in rows)
    assert sum(r["last_error"] == "detached" for r in rows) == len(rows) - 1
    assert client.post(f"/v1/admin/ns/{ns}:reattach").status_code == 200
    rows = _rows(ns)
    assert sum(r["state"] == "pending" for r in rows) == len(rows) - 1
    assert sum(r["last_error"] == "HTTP 500" for r in rows) == 1


def test_removed_subscription(client):
    ns, _, _ = attach(client, manifest=_manifest([{"sub_id": "w1", "kinds": ["*"], "url": HOOK}]))
    _ev(client, ns, 1)
    outbox.cron_fanout()
    client.put(f"/v1/{ns}/manifest", json=EVENT_MANIFEST, headers={"If-Match": "1"})
    assert all(r["state"] == "dead" and r["last_error"] == "unsubscribed" for r in _rows(ns))
    assert client.get(f"/v1/{ns}/subscriptions").json()["subscriptions"] == []
