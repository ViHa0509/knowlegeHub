"""KH's own events (§4.9): emitter, store, routes, masking, prompts for learn-back, sidecar."""
from __future__ import annotations

import json
import re
import time

import pytest
from fastapi.testclient import TestClient

from app.eventing import emit, mask, prompts
from app.eventing.queue import DirectBuffer, MemoryQueue
from app.eventing.store import get_store
from tests.learning.conftest import iso

PROMPT = "kh.session.prompt"


@pytest.fixture
def ev(monkeypatch, uid):
    """Events on, a private namespace, a direct buffer; outbox emptied before and after."""
    monkeypatch.setenv("KH_EVENTS", "on")
    monkeypatch.setenv("KH_EVENTS_NS", f"t{uid}")
    emit.drain(10**6)
    emit.set_buffer(DirectBuffer())
    yield f"t{uid}"
    emit.drain(10**6)
    emit.set_buffer(None)


def _put(ns, kind, body, session=None, email=None, project=None, ts_ms=None):
    row = emit.build_row(kind, body, project=project, email=email, actor=session)
    if ts_ms is not None:
        row["ts_ms"] = ts_ms
    get_store().append(ns, [row])
    return row


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,want,n", [
    ("my password: hunter2xyz ok", "my password: --censored-- ok", 1),
    ("token=abcd1234efgh", "token=--censored--", 1),
    ("mat khau la Secret123", "mat khau la --censored--", 1),
    ("the password is expired", "the password is expired", 0),
    ("password: abc", "password: abc", 0),                     # < 4 chars
    ("use sk-abcdefghijk123 now", "use --censored-- now", 1),
    ("ghp_abcdefghij1234567890", "--censored--", 1),
    ("Authorization: Bearer abc.def.ghi", "Authorization: Bearer --censored--", 1),
    ("postgres://user:pa55word@db/x", "postgres://user:--censored--@db/x", 1),
    ("key AKIAABCDEFGHIJKLMNOP here", "key --censored-- here", 1),
    ("commit 3f2a9c1d0e8b7a6f5e4d3c2b1a0f9e8d7c6b5a4f", "commit 3f2a9c1d0e8b7a6f5e4d3c2b1a0f9e8d7c6b5a4f", 0),
    ("", "", 0),
])
def test_mask(text, want, n):
    assert mask.mask(text) == (want, n)


def test_mask_pem_and_jwt():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
    assert mask.mask(f"key:\n{pem}\nend") == ("key:\n--censored--\nend", 1)
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl"
    assert mask.mask(f"t {jwt}")[0] == "t --censored--"


# ---------------------------------------------------------------------------
# Emitter
# ---------------------------------------------------------------------------


def test_emit_off_does_nothing(monkeypatch):
    monkeypatch.setenv("KH_EVENTS", "off")
    before = emit.pending()
    emit.emit("kh.test", {"a": 1})
    assert emit.pending() == before and emit.enabled() is False


def test_row_shape():
    r = emit.build_row("kh.worklog.stored", {"project": "p"}, project="p", email="a@x.com", actor="sess1",
                       ix2="/x", n1=3)
    assert re.fullmatch(r"[0-9a-f]{24}", r["id"])
    assert r["kind"] == "kh.worklog.stored" and r["project"] == "p" and r["session_id"] == "sess1"
    assert json.loads(r["actor_json"]) == {"email": "a@x.com", "actor": "sess1"}
    assert r["ix_s1"] == "a@x.com" and r["ix_s2"] == "/x" and r["ix_n1"] == 3
    assert json.loads(r["body_json"]) == {"project": "p"} and isinstance(r["ts_ms"], int)


def test_outbox_cap_and_flush_failure(ev, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "EVENTS_OUTBOX_MAX", 3)
    d0 = emit.stats["dropped"]
    for i in range(5):
        emit.emit("kh.test", {"i": i})
    assert emit.pending() == 3 and emit.stats["dropped"] - d0 == 2

    class Boom:
        def publish(self, items):
            raise RuntimeError("down")

    emit.set_buffer(Boom())
    e0 = emit.stats["send_errors"]
    assert emit.flush_once() == 0 and emit.pending() == 3 and emit.stats["send_errors"] == e0 + 1
    emit.set_buffer(DirectBuffer())
    assert emit.flush_once() == 3 and emit.pending() == 0
    got = get_store().read(ev, 0, kinds=["kh.test"])
    assert [g["body"]["i"] for g in got] == [2, 3, 4]


def test_store_dedupe_and_gapless_seq(ev):
    row = emit.build_row("kh.a", {})
    st = get_store()
    r1 = st.append(ev, [row, emit.build_row("kh.b", {})])
    r2 = st.append(ev, [row])
    assert [x["duplicate"] for x in r1] == [False, False] and r2[0]["duplicate"] is True
    assert r2[0]["seq"] == r1[0]["seq"] and r1[1]["seq"] == r1[0]["seq"] + 1
    assert st.count(ev) == 2
    got = st.get(ev, row["id"])
    assert got["kind"] == "kh.a" and got["received_ms"] and got["body"] == {}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_events_route(ev, client):
    _put(ev, "kh.worklog.stored", {"project": "p1"}, session="s1", email="a@x.com", project="p1")
    _put(ev, PROMPT, {"text": "hello", "masked": 0}, session="s1", email="a@x.com")
    _put(ev, "kh.session.turn", {"project": "p2", "source": "detected"}, session="s2", project="p2")
    j = client.get("/events").json()
    assert set(j) >= {"events", "next_after_seq", "enabled", "pending", "emit_emitted", "emit_sent", "emit_dropped",
                      "emit_send_errors"}
    kinds = [e["kind"] for e in j["events"] if e["kind"] != "kh.api.call"]
    assert kinds == ["kh.worklog.stored", "kh.session.turn"] and j["enabled"] is True     # prompts never returned
    assert j["next_after_seq"] == j["events"][-1]["seq"]
    assert client.get("/events", params={"kind": PROMPT}).json()["events"] == []
    one = client.get("/events", params={"session": "s2"}).json()["events"]
    assert [e["kind"] for e in one] == ["kh.session.turn"] and one[0]["body"] == {"project": "p2", "source": "detected"}
    assert [e["kind"] for e in client.get("/events", params={"project": "p1"}).json()["events"]] == [
        "kh.worklog.stored"]
    after = client.get("/events", params={"after_seq": j["next_after_seq"]}).json()
    assert all(e["seq"] > j["next_after_seq"] for e in after["events"])
    assert client.get("/events", params={"limit": 1001}).status_code == 422
    # stats
    s = client.get("/events:stats", params={"group_by": "kind"}).json()["groups"]
    assert {g["kind"]: g["n"] for g in s}["kh.worklog.stored"] == 1
    s = client.get("/events:stats", params={"group_by": "project,kind"}).json()["groups"]
    assert {"project": "p2", "kind": "kh.session.turn", "n": 1} in s
    assert client.get("/events:stats", params={"group_by": "body"}).status_code == 400
    assert client.get("/events:stats", params={"since": "yesterday-ish"}).status_code == 400
    future = iso(time.time() + 3600)
    assert client.get("/events:stats", params={"since": future}).json()["groups"] == []


def test_prompts_route_own_only(ev, client, monkeypatch):
    from tests.auth_helpers import bearer, enable_signin, make_token

    _put(ev, PROMPT, {"text": "mine", "masked": 0}, session="sx", email="me@x.com")
    _put(ev, PROMPT, {"text": "theirs", "masked": 0}, session="sx", email="other@x.com")
    j = client.get("/events/prompts", params={"session": "sx"}).json()       # open mode: no person filter
    assert [e["body"]["text"] for e in j["events"]] == ["mine", "theirs"]
    enable_signin(monkeypatch)
    j = client.get("/events/prompts", params={"session": "sx"}, headers=bearer(make_token("me@x.com"))).json()
    assert [e["body"]["text"] for e in j["events"]] == ["mine"]
    assert client.get("/events/prompts").status_code == 401
    assert client.get("/events", headers=bearer(make_token("me@x.com"))).status_code == 403
    assert client.get("/events:stats", headers=bearer(make_token("me@x.com"))).status_code == 403


def test_api_call_middleware(ev, client, monkeypatch):
    # the kh.api.call layer is app.auth.ApiCallMiddleware (installed innermost by auth.install; sign-in on)
    from tests.auth_helpers import bearer, enable_signin, make_token

    enable_signin(monkeypatch)
    h = bearer(make_token("boss@x.com", role="maintainer"), actor="sess9")
    emit.drain(10**6)
    client.get("/version", params={"hook_type": "UserPromptSubmit"}, headers=h)
    client.get("/health")
    rows = emit.drain(10**6)
    calls = [r for r in rows if r["kind"] == "kh.api.call"]
    assert [r["ix_s2"] for r in calls] == ["/version"]
    b = json.loads(calls[0]["body_json"])
    assert b["method"] == "GET" and b["path"] == "/version" and b["status"] == 200
    assert b["hook_type"] == "UserPromptSubmit" and calls[0]["ix_n1"] == b["ms"]
    assert calls[0]["session_id"] == "sess9"
    client.get("/events", headers=h)
    calls = [r for r in emit.drain(10**6) if r["kind"] == "kh.api.call"]
    assert calls[0]["ix_s2"] == "/events" and calls[0]["ix_s1"] == "boss@x.com"


def test_worklog_emits_stored(ev, client, uid):
    emit.drain(10**6)
    p = f"ev-{uid}"
    client.post("/vector/add", json={"text": "log line", "scope": "log",
                                     "metadata": {"scope": "log", "project": p, "topic": "t", "ts": iso()}})
    rows = [r for r in emit.drain(10**6) if r["kind"] == "kh.worklog.stored"]
    assert len(rows) == 1 and json.loads(rows[0]["body_json"])["project"] == p


# ---------------------------------------------------------------------------
# Prompts for learn-back
# ---------------------------------------------------------------------------


def test_attach_prompts_window_and_chars(ev, monkeypatch):
    t0 = time.time()
    s = "sess-p"
    _put(ev, PROMPT, {"text": "too old"}, session=s, ts_ms=int((t0 - 5 * 3600) * 1000))
    _put(ev, PROMPT, {"text": "first ask"}, session=s, ts_ms=int((t0 - 600) * 1000))
    _put(ev, PROMPT, {"text": "second ask"}, session=s, ts_ms=int((t0 - 300) * 1000))
    _put(ev, PROMPT, {"text": "after log one"}, session=s, ts_ms=int((t0 - 100) * 1000))
    _put(ev, PROMPT, {"text": "other session"}, session="sess-q", ts_ms=int((t0 - 200) * 1000))
    logs = [{"id": "b", "ts": iso(t0 - 10), "actor": s}, {"id": "a", "ts": iso(t0 - 200), "actor": s},
            {"id": "c", "ts": iso(t0 - 10)}]
    prompts.attach_prompts(logs)
    by = {lg["id"]: lg for lg in logs}
    assert by["a"]["prompts"] == ["first ask", "second ask"]
    assert by["b"]["prompts"] == ["after log one"]          # only those typed after the session's previous log
    assert by["c"]["prompts"] == [] and all("actor" not in lg for lg in logs)
    # char budget: newest kept, a single long prompt is truncated
    monkeypatch.setenv("KH_DISTILL_PROMPT_CHARS", "12")
    logs = [{"id": "a", "ts": iso(t0 - 200), "actor": s}]
    prompts.attach_prompts(logs)
    assert logs[0]["prompts"] == ["second ask"]
    _put(ev, PROMPT, {"text": "x" * 50}, session="sess-r", ts_ms=int((t0 - 50) * 1000))
    logs = [{"id": "r", "ts": iso(t0), "actor": "sess-r"}]
    prompts.attach_prompts(logs)
    assert logs[0]["prompts"] == ["x" * 12]
    assert prompts.window_s() == 4 * 3600 and prompts.MAX_EVENTS == 5000


def test_distill_next_carries_prompts(ev, client, proj, add_log):
    t0 = time.time() - 50
    _put(ev, PROMPT, {"text": "please fix the failing deploy"}, session="sess-d", ts_ms=int((t0 - 30) * 1000))
    add_log(proj, "we decided to fix the deploy", ts=t0, actor="sess-d")
    lg = client.get("/distill/next", params={"project": proj}).json()["logs"][0]
    assert lg["prompts"] == ["please fix the failing deploy"] and "actor" not in lg


# ---------------------------------------------------------------------------
# Sidecar
# ---------------------------------------------------------------------------


def test_sidecar(uid):
    from app.eventing import sidecar

    with pytest.raises(SystemExit):
        sidecar.create_app(token="")
    q = MemoryQueue(start=False, max_items=3)
    app = sidecar.create_app(token="tok", queue=q)
    ns = f"sc{uid}"
    with TestClient(app) as c:
        ev = {"ns": ns, "row": emit.build_row("kh.x", {})}
        assert c.post("/queue/publish", json={"events": [ev]}).status_code == 401
        h = {"Authorization": "Bearer tok"}
        r = c.post("/queue/publish", json={"events": [ev]}, headers=h)
        assert r.status_code == 202 and r.json() == {"accepted": 1, "depth": 1}
        assert c.post("/queue/publish", json={"events": []}, headers=h).status_code == 400
        assert c.post("/queue/publish", json={"events": [{"ns": ns, "row": {"id": "x"}}]}, headers=h).status_code == 400
        many = [{"ns": ns, "row": emit.build_row("kh.x", {})} for _ in range(3)]
        r = c.post("/queue/publish", json={"events": many}, headers=h)
        assert r.status_code == 503 and r.headers["retry-after"] == "5"
        assert c.get("/health").json() == {"status": "ok", "depth": 1}
        assert c.get("/queue/stats").json()["depth"] == 1
        assert q.flush_once() == 1
    assert get_store().count(ns) == 1
    assert sidecar.MAX_EVENTS == 2000


def test_buffer_choice(monkeypatch):
    from app.eventing import queue

    monkeypatch.delenv("KH_EVENTS_URL", raising=False)
    monkeypatch.delenv("KH_EVENT_BUFFER", raising=False)
    b = queue.make_buffer()
    assert b.kind == "direct"
    monkeypatch.setenv("KH_EVENTS_URL", "http://sidecar.local")
    monkeypatch.setenv("KH_EVENTS_TOKEN", "t")
    b = queue.make_buffer()
    assert b.kind == "http"
