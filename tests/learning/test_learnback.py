"""Learn back: the in-process distil runner (§4.4). The LLM is always a local fake (no network)."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

import pytest

from app import distill, learnback
from app import vector_store as vs
from tests.learning.conftest import ACTION


def _batch_of(user: str) -> dict:
    m = re.search(r'<kh_data name="batch">(.*?)</kh_data>', user, re.S)
    return json.loads(m.group(1)) if m else {}


def _responder(system, user, schema):  # noqa: ARG001
    b = _batch_of(user)
    ids = [lg["id"] for lg in b.get("logs") or []]
    if not ids:
        return {"items": [], "contexts": [], "signal_words": []}
    return {"items": [{"kind": "lesson", "action": ACTION, "context": "-", "title": "Learned in process",
                       "text": "Restart the indexer after a schema change.", "entities": [], "sources": ids}],
            "contexts": [], "signal_words": []}


@pytest.fixture
def lb_on(monkeypatch):
    from kh_worker.llm import DryLLM  # importable because learnback put worker/ on sys.path or conftest

    monkeypatch.setenv("KH_LEARNBACK_LLM", "dry")
    monkeypatch.setattr(learnback, "LLM_FACTORY", lambda kind, mdl: DryLLM(mdl, fixture="", responder=_responder))
    yield
    learnback._running.clear()


@pytest.fixture(autouse=True)
def _worker_path():
    import sys

    p = learnback.worker_path()
    if p and p not in sys.path:
        sys.path.insert(0, p)


def test_disabled_by_default(client, monkeypatch):
    monkeypatch.delenv("KH_LEARNBACK_LLM", raising=False)
    j = client.get("/home/learnback").json()
    assert j["enabled"] is False and j["llm"] is None and j["schedules"] == [] and j["runs"] == []
    assert "KH_LEARNBACK_LLM" in j["reason"] and j["tz"] == "Asia/Ho_Chi_Minh"
    assert set(j) == {"enabled", "reason", "llm", "model", "tz", "running", "schedules", "runs"}
    r = client.post("/home/learnback/run", json={"projects": ["x"]})
    assert r.status_code == 409 and r.json()["detail"].startswith("learn back is off")
    r = client.post("/home/learnback/schedules", json={"projects": ["x"], "kind": "daily", "at": "02:00"})
    assert r.status_code == 409
    monkeypatch.setenv("KH_LEARNBACK_LLM", "gpt")
    assert client.get("/home/learnback").json()["enabled"] is False


def test_defaults():
    assert learnback.tick_s() == 30 and learnback.cap_run() == 5 and learnback.tz_name() == "Asia/Ho_Chi_Minh"


def test_next_time():
    now = datetime(2026, 10, 31, 0, 0, tzinfo=timezone.utc)      # Saturday 07:00 in Ho Chi Minh City
    nt = learnback.next_time
    assert nt("now", None, now) == now
    assert nt("daily", "08:00", now) == datetime(2026, 10, 31, 1, 0, tzinfo=timezone.utc)
    assert nt("daily", "06:00", now) == datetime(2026, 10, 31, 23, 0, tzinfo=timezone.utc)
    assert nt("weekly", "fri 00:00", now) == datetime(2026, 11, 5, 17, 0, tzinfo=timezone.utc)
    assert nt("weekly", "sat 08:00", now) == datetime(2026, 10, 31, 1, 0, tzinfo=timezone.utc)
    assert nt("once", "2026-11-01T09:30", now) == datetime(2026, 11, 1, 2, 30, tzinfo=timezone.utc)
    for kind, at in (("daily", "nope"), ("weekly", "funday 00:00"), ("once", "tomorrow"), ("hourly", "1")):
        with pytest.raises(ValueError):
            nt(kind, at, now)


def test_schedules_routes(lb_on, client):
    r = client.post("/home/learnback/schedules", json={"projects": ["p1", "common"], "kind": "daily", "at": "02:00"})
    assert r.status_code == 200
    s = r.json()
    assert re.fullmatch(r"lbs_[0-9a-f]{10}", s["id"]) and s["kind"] == "daily" and s["enabled"] is True
    assert s["projects"] == ["p1", "common"] and s["next_utc"].endswith("Z")
    j = client.get("/home/learnback").json()
    assert j["enabled"] is True and j["reason"] == "" and j["llm"] == "dry"
    assert s["id"] in [x["id"] for x in j["schedules"]]
    assert client.post("/home/learnback/schedules", json={"projects": ["p1"], "kind": "hourly"}).status_code == 422
    assert client.post("/home/learnback/schedules", json={"projects": [], "kind": "daily",
                                                          "at": "02:00"}).status_code == 422
    assert client.post("/home/learnback/schedules", json={"projects": ["p"], "kind": "daily",
                                                          "at": "25h"}).status_code == 422
    assert client.post(f"/home/learnback/schedules/{s['id']}/delete").json() == {"deleted": s["id"]}
    assert client.post(f"/home/learnback/schedules/{s['id']}/delete").status_code == 404


def test_run_distils_logs_and_findings(lb_on, client, proj, add_log):
    a = add_log(proj, "we decided to restart the indexer after schema changes")
    for _ in range(4):
        client.post("/feedback/issue", json={"project": proj, "issue_id": "idx", "desc": "indexer failed after schema change"})
    client.post("/feedback/sweep")
    out = learnback.start_run([proj, "common"], "tester@x.com", wait=True)
    assert re.fullmatch(r"lbr_[0-9a-f]{10}", out["id"]) and out["state"] == "done"
    run = next(r for r in learnback.runs() if r["id"] == out["id"])
    assert run["state"] == "done" and run["created_by"] == "tester@x.com" and run["trigger"] == "now"
    summ = run["summary"]
    assert summ["status"] == "ok" and summ["llm"] == "dry"
    assert summ["projects"][proj]["accepted"] == 1
    assert summ["promote"] == {"status": "skipped", "reason": "promote is off"}
    assert "budget" in summ and isinstance(summ["usage"], list), summ["usage"]
    pend = distill.pending(proj)
    assert pend["count"] == 1
    it = distill.items_of(proj, ("proposed",))[0]
    assert set(it["sources"]) == {a, "finding__idx"}          # findings are distilled too
    assert it["metadata"]["distilled_by"].startswith("worker:")
    j = client.get("/home/learnback").json()
    assert out["id"] in [r["id"] for r in j["runs"]] and j["running"] == []


def test_one_run_at_a_time(lb_on, client):
    learnback._running["lbr_busy"] = {"id": "lbr_busy"}
    r = client.post("/home/learnback/run", json={"projects": ["p"]})
    assert r.status_code == 409 and r.json()["detail"] == "a learn back run is already going"
    learnback._running.clear()
    assert client.post("/home/learnback/run", json={"projects": []}).status_code == 422


def test_run_route_background(lb_on, client, proj, add_log):
    add_log(proj, "we decided background runs work")
    r = client.post("/home/learnback/run", json={"projects": [proj]})
    assert r.status_code == 200 and r.json()["state"] == "running"
    rid = r.json()["id"]
    end = time.time() + 20
    while learnback._running and time.time() < end:
        time.sleep(0.05)
    run = next(x for x in learnback.runs() if x["id"] == rid)
    assert run["state"] == "done"


def test_tick_fires_due_once_schedule(lb_on, client, proj, add_log, monkeypatch):
    add_log(proj, "we decided schedules fire")
    now = datetime.now(timezone.utc)
    s = learnback.add_schedule([proj], "now", None, "sched@x.com")
    fired = learnback.tick(wait=True)
    assert len(fired) == 1
    after = next(x for x in learnback.schedules() if x["id"] == s["id"])
    assert after["enabled"] is False and after["last_run"]
    assert learnback.tick(wait=True) == []
    learnback.delete_schedule(s["id"])
    # a daily schedule computes its next time instead of disabling
    d = learnback.add_schedule([proj], "daily", "00:00", "sched@x.com")
    monkeypatch.setattr(learnback, "CLOCK", lambda: now.replace(year=now.year + 1))
    assert len(learnback.tick(wait=True)) == 1
    d2 = next(x for x in learnback.schedules() if x["id"] == d["id"])
    assert d2["enabled"] is True and d2["next_utc"] > d["next_utc"]
    learnback.delete_schedule(d["id"])


def test_running_rows_interrupted_on_init():
    from app import sqldb

    with learnback._conn() as c:
        sqldb.insert_new(c, "learnback_run", {"id": "lbr_stale00001", "trigger_kind": "now", "projects": "[]",
                                              "state": "running", "started": "2026-01-01T00:00:00Z",
                                              "finished": None, "created_by": "x", "summary": None})
    learnback._ready.clear()
    with learnback._conn() as c:
        st = c.execute("SELECT state FROM learnback_run WHERE id='lbr_stale00001'").fetchone()[0]
    assert st == "interrupted"


def test_learnback_routes_maintainer_only(client, monkeypatch):
    from tests.auth_helpers import bearer, enable_signin, make_token

    enable_signin(monkeypatch)
    assert client.get("/home/learnback", headers=bearer(make_token("m@x.com"))).status_code == 403
    assert client.get("/home/learnback",
                      headers=bearer(make_token("b@x.com", role="maintainer"))).status_code == 200


def test_findings_lane_unused_when_absent(proj):
    assert vs.get_existing(vs.part_collection_name("findings", proj)) is None
    kh = learnback._LocalKH("x")
    j = kh.distill_next(proj)
    assert j["batch_id"] is None
