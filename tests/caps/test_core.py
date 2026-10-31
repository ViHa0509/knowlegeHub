"""§6.0 registration/discovery and §6.1 core plumbing."""
from __future__ import annotations

import pytest

from app import caps, sqldb
from app.caps import core
from app.caps.core import CapError
from tests.caps.conftest import attach, new_project


def test_modules_order_and_switch(monkeypatch):
    assert caps.MODULES[:4] == ["registry", "coord", "events", "principals"]
    assert caps.MODULES[-5:] == ["people", "daily", "outcomes", "learn", "improve"]
    assert len(caps.MODULES) == 19
    assert caps.enabled("events", True)
    monkeypatch.setenv("KH_CAP_EVENTS", "off")
    assert not caps.enabled("events", True)
    assert not caps.enabled("people", False)
    monkeypatch.setenv("KH_CAP_PEOPLE", "yes")
    assert caps.enabled("people", False)
    monkeypatch.setenv("KH_CAP_REDACTION", "0")
    assert not caps.enabled("redaction", True)
    assert caps._import("does_not_exist") is None


def test_capabilities_and_openapi(client):
    r = client.get("/v1/capabilities")
    assert r.status_code == 200
    body = r.json()
    assert body["api_versions"] == ["v1"]
    names = [c["name"] for c in body["capabilities"]]
    assert names == sorted(names)
    assert {"registry", "coord", "events", "principals"} <= set(names)
    reg = next(c for c in body["capabilities"] if c["name"] == "registry")
    assert reg["version"] == "1.0.0" and reg["status"] == "beta" and reg["limits"]["types_per_ns"] == 200
    spec = client.get("/v1/openapi.json").json()
    assert spec["paths"] and all(p.startswith("/v1/") for p in spec["paths"])
    assert "/v1/{ns}/events" in spec["paths"] and "/v1/admin/ns/{ns}" in spec["paths"]


def test_ns_ids():
    assert core.ns_valid("abc") and core.ns_valid("a_b9")
    assert not core.ns_valid("ab") and not core.ns_valid("Abc") and not core.ns_valid("9ab")
    assert not core.ns_valid("a" * 33)
    for bad in ("admin", "token", "khfoo", "kh_people"):
        with pytest.raises(CapError) as e:
            core.check_new_ns(bad)
        assert e.value.status == 422


def test_error_shape_and_codes(client):
    r = client.get("/v1/nosuchns1/events")
    assert r.status_code == 404
    assert r.json()["error"] == "ns_not_active"
    e = CapError("stale_epoch", "x", extra={"lease": "l"})
    assert e.status == 409 and e.body() == {"error": "stale_epoch", "detail": "x", "lease": "l"}
    assert core.CODES["approval_required"] == 428 and core.CODES["ns_suspended"] == 423
    assert CapError("quota").status == 429


def test_if_match_parsing():
    assert core.parse_if_match('"7"') == 7
    assert core.parse_if_match('W/"7"') == 7
    assert core.parse_if_match("7") == 7
    assert core.parse_if_match("*") == -1
    assert core.parse_if_match(None) is None and core.parse_if_match(" ") is None
    with pytest.raises(CapError):
        core.parse_if_match("abc")
    assert core.etag(3) == '"3"'


def test_cas_update():
    with sqldb.connect() as c:
        c.execute("CREATE TABLE IF NOT EXISTS t_cas (k TEXT PRIMARY KEY, v TEXT, rev INTEGER NOT NULL)")
        c.execute("DELETE FROM t_cas")
        c.execute("INSERT INTO t_cas VALUES ('a', 'x', 1)")
        assert core.cas_update(c, "t_cas", {"k": "a"}, {"v": "y"}, 1) == 2
        with pytest.raises(CapError) as e:
            core.cas_update(c, "t_cas", {"k": "a"}, {"v": "z"}, 1)
        assert e.value.code == "write_conflict" and e.value.extra["current"] == 2
        assert e.value.headers["ETag"] == '"2"'
        assert core.cas_update(c, "t_cas", {"k": "a"}, {"v": "z"}, -1) == 3
        with pytest.raises(CapError) as e:
            core.cas_update(c, "t_cas", {"k": "zz"}, {"v": "z"}, None)
        assert e.value.code == "not_found"


def test_split_verb_and_helpers():
    assert core.split_verb("a/b:acquire", ("acquire",)) == ("a/b", "acquire")
    assert core.split_verb("a:b", ("acquire",)) == ("a:b", None)
    assert core.clamp_limit("5000", 100, 1000) == 1000
    assert core.clamp_limit(None, 100, 1000) == 100
    with pytest.raises(CapError):
        core.clamp_limit("x", 1, 2)
    assert core.csv("a, b,,c") == ["a", "b", "c"]
    assert core.parse_ms("1700000000") == 1700000000000
    assert core.parse_ms("2026-01-01T00:00:00Z") == 1767225600000
    i = core.new_id()
    assert len(i) == 24 and int(i, 16) >= 0


def test_index_values_and_validate():
    trow = {"index_fields": {"who": "ix_s1", "n": "ix_n1", "deep.x": "ix_s2", "at": "ix_t1"}}
    v = core.index_values(trow, {"who": "a" * 300, "n": "3.5", "deep": {"x": {"k": 1}}, "at": 5})
    assert v["ix_s1"] == "a" * 256 and v["ix_n1"] == 3.5 and v["ix_s2"] == '{"k":1}' and v["ix_t1"] == 5
    errs = core.validate({"type": "object", "required": ["n"]}, {})
    assert errs and errs[0]["pointer"] == "/"
    assert core.schema_check({"type": "nope"})
    assert core.schema_check({"type": "object"}) == []


def test_roles_matrix():
    assert core.role_allows("admin", "vault", "read")
    assert core.role_allows("agent", "events", "append")
    assert not core.role_allows("agent", "events", "read")
    assert core.role_allows("grader", "vault", "read")
    assert not core.role_allows("judge", "vault", "read")
    assert core.role_allows("confirmer", "items", "withdraw")
    assert core.acl_allows("agent", []) and not core.acl_allows("agent", ["judge"])


def test_idempotency_replay_mismatch(ns_env):
    client, ns, _ = ns_env
    h = {"Idempotency-Key": "k-1"}
    ev = {"kind": "step.done", "body": {"n": 1}}
    r1 = client.post(f"/v1/{ns}/events", json=ev, headers=h)
    assert r1.status_code == 200 and "Idempotent-Replay" not in r1.headers
    r2 = client.post(f"/v1/{ns}/events", json=ev, headers=h)
    assert r2.status_code == 200 and r2.headers["Idempotent-Replay"] == "true"
    assert r2.json() == r1.json()
    r3 = client.post(f"/v1/{ns}/events", json={"kind": "step.done", "body": {"n": 2}}, headers=h)
    assert r3.status_code == 422 and r3.json()["error"] == "idempotency_mismatch"
    # stored 422 errors replay
    bad = {"kind": "step.done", "body": {}}
    b1 = client.post(f"/v1/{ns}/events", json=bad, headers={"Idempotency-Key": "k-2"})
    b2 = client.post(f"/v1/{ns}/events", json=bad, headers={"Idempotency-Key": "k-2"})
    assert b1.status_code == b2.status_code == 422 and b2.headers.get("Idempotent-Replay") == "true"
    kinds = [e["kind"] for e in client.get(f"/v1/{ns}/events", params={"kind": "kh.duplicate"}).json()["events"]]
    assert kinds.count("kh.duplicate") >= 2


def test_idempotency_in_progress(ns_env):
    client, ns, _ = ns_env
    with sqldb.connect() as c:
        now = sqldb.now_ms(c)
        sqldb.insert_new(c, "kh_idem", {"ns": ns, "principal": "local", "idem_key": "busy", "request_sha256": "x",
                                        "state": "pending", "created_ms": now, "updated_ms": now})
    r = client.post(f"/v1/{ns}/events", json={"kind": "step.done", "body": {"n": 1}},
                    headers={"Idempotency-Key": "busy"})
    assert r.status_code == 409 and r.json()["error"] == "in_progress" and r.headers["Retry-After"] == "1"
    with sqldb.connect() as c:  # stale pending row (> 60 s) is taken over
        c.execute("UPDATE kh_idem SET updated_ms=? WHERE ns=? AND idem_key='busy'", (now - 61_000, ns))
    r = client.post(f"/v1/{ns}/events", json={"kind": "step.done", "body": {"n": 1}},
                    headers={"Idempotency-Key": "busy"})
    assert r.status_code == 200


def test_rate_quota(client):
    ns, _, _ = attach(client, manifest={"quotas": [{"name": "requests_per_min_principal", "limit": 2,
                                                    "window_s": 60}]})
    core.reset_buckets()
    codes = [client.get(f"/v1/{ns}/manifest").status_code for _ in range(3)]
    assert codes[:2] == [200, 200] and codes[2] == 429
    r = client.get(f"/v1/{ns}/manifest")
    assert r.json()["error"] == "quota" and int(r.headers["Retry-After"]) >= 1
    core.reset_buckets()


def test_envelope_headers(ns_env):
    client, ns, _ = ns_env
    r = client.post(f"/v1/{ns}/events", json={"kind": "step.done", "body": {"n": 1}},
                    headers={"X-KH-Attempt": "x"})
    assert r.status_code == 422
    r = client.post(f"/v1/{ns}/events", json={"kind": "step.done", "body": {"n": 1}},
                    headers={"X-KH-Lease": "l"})
    assert r.status_code == 422  # lease without epoch
    r = client.post(f"/v1/{ns}/events", json={"kind": "step.done", "body": {"n": 1}},
                    headers={"X-KH-Run": "r1", "X-KH-Unit": "u1", "X-KH-Session": "s1", "X-KH-Attempt": "2"})
    assert r.status_code == 200
    ev = client.get(f"/v1/{ns}/events", params={"run_id": "r1"}).json()["events"][0]
    assert (ev["run_id"], ev["unit_id"], ev["session_id"], ev["attempt"]) == ("r1", "u1", "s1", 2)
    assert ev["actor"]["principal"] == "local" and ev["actor"]["run_id"] == "r1"


def test_require_project(client):
    p1, p2 = new_project(), new_project()
    ns, _, _ = attach(client, projects=[p1, p2])
    ctx = core.make_ctx(ns)
    with pytest.raises(CapError) as e:
        ctx.require_project("")
    assert e.value.status == 422
    assert ctx.require_project(p1) == p1
    with pytest.raises(CapError):
        ctx.require_project("other-project")
    ns1, (only,), _ = attach(client)
    assert core.make_ctx(ns1).require_project(None) == only
    ns0, _, _ = attach(client, projects=[])
    assert core.make_ctx(ns0).require_project("") == ""


def test_body_filters_hook(ns_env):
    client, ns, _ = ns_env

    def mask(ctx, where, body):
        if isinstance(body, dict) and body.get("who") == "secret":
            return {**body, "who": "[REDACTED:test]"}
        return body

    core.BODY_FILTERS.append(mask)
    try:
        r = client.post(f"/v1/{ns}/events", json={"kind": "step.done", "body": {"n": 1, "who": "secret"}})
        assert r.status_code == 200
    finally:
        core.BODY_FILTERS.remove(mask)
    eid = r.json()["accepted"][0]["id"]
    assert client.get(f"/v1/{ns}/events/{eid}").json()["body"]["who"] == "[REDACTED:test]"


def test_crons_listed():
    names = [n for m in caps.LOADED for n, _, _ in getattr(m, "CRONS", [])]
    assert "cron.idempotency_purge" in names and "cron.owner_grants" in names
    assert caps.start_crons() == 0  # KH_CAPS_CRONS=false in tests
