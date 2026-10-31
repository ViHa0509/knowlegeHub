import time
from datetime import datetime, timezone

import pytest

from app import config, hotlog
from app import vector_store as vs


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(client, text, project, ts, **meta):
    r = client.post("/vector/add", json={"text": text, "scope": "log", "project": project,
                                         "metadata": {"scope": "log", "ts": ts, "topic": "t", **meta}})
    assert r.status_code == 200, r.text
    return r.json()["vector_id"]


def _hot_ids(project=None):
    where = {"project": {"$eq": project}} if project else None
    return set(hotlog.collection().get(where=where, include=[])["ids"])


def _cold_ids(project):
    h = vs.get_existing(f"kh_log_{project}")
    return set(h.get(include=[])["ids"]) if h is not None else set()


def test_parse_ts():
    assert hotlog.parse_ts(1700000000) == 1700000000.0
    assert hotlog.parse_ts("1700000000") == 1700000000.0
    assert hotlog.parse_ts("2026-10-31T00:00:00Z") == datetime(2026, 10, 31, tzinfo=timezone.utc).timestamp()
    assert hotlog.parse_ts("2026-10-31T00:00:00") == datetime(2026, 10, 31, tzinfo=timezone.utc).timestamp()
    assert hotlog.parse_ts("garbage") == 0.0
    assert hotlog.parse_ts(None) == 0.0


def test_log_goes_to_hot_window_tagged(client, fresh_store):
    rid = _log(client, "did a thing", "hotp", _iso(time.time()))
    assert rid in _hot_ids("hotp")
    assert not vs.collection_exists("kh_log_hotp")
    # no project -> tagged orchestrator (sign-in off), never untagged
    rid2 = _log(client, "untagged log", None, _iso(time.time()))
    assert rid2 in _hot_ids(vs.ORCHESTRATOR)


def test_age_spill(client, fresh_store):
    old = _log(client, "ancient work", "agep", _iso(time.time() - 30 * 86400))
    new = _log(client, "fresh work", "agep", _iso(time.time()))
    nots = _log(client, "no ts work", "agep", "not a time")
    assert old not in _hot_ids() and old in _cold_ids("agep")
    assert new in _hot_ids() and nots in _hot_ids()  # unparseable ts is never age-spilled


def test_depth_spill(client, fresh_store, monkeypatch):
    monkeypatch.setattr(config, "HOTLOG_MAX_PER_SLOT", 3)
    now = time.time()
    ids = [_log(client, f"depth row {i}", "depthp", _iso(now - 100 + i)) for i in range(5)]
    assert _hot_ids("depthp") == set(ids[2:])
    assert _cold_ids("depthp") == set(ids[:2])


def test_slot_lru_spill(client, fresh_store, monkeypatch):
    monkeypatch.setattr(config, "HOTLOG_SLOTS", 2)
    now = time.time()
    a = _log(client, "slot a", "slota", _iso(now - 300))
    b = _log(client, "slot b", "slotb", _iso(now - 200))
    c = _log(client, "slot c", "slotc", _iso(now - 100))
    hot = _hot_ids()
    assert a not in hot and {b, c} <= hot
    assert a in _cold_ids("slota")


def test_window_off_drains(client, fresh_store, monkeypatch):
    rid = _log(client, "drain me", "drainp", _iso(time.time()))
    monkeypatch.setattr(config, "HOTLOG_ENABLED", False)
    out = hotlog.enforce()
    assert out["enabled"] is False and out["spilled"] == {"drainp": 1}
    assert rid in _cold_ids("drainp")
    # reads ignore the flag: the spilled row is still found by project
    r = client.get("/log/recent", params={"project": "drainp"}).json()
    assert [h["vector_id"] for h in r["hits"]] == [rid]


def test_log_recent_ordering_and_filters(client, fresh_store, monkeypatch):
    monkeypatch.setattr(config, "HOTLOG_MAX_PER_SLOT", 2)
    now = time.time()
    ids = [_log(client, f"recent row {i}", "recp", _iso(now - 1000 + i * 100)) for i in range(4)]
    # two newest are hot, two oldest spilled; the project read merges both lanes newest first
    r = client.get("/log/recent", params={"project": "recp", "limit": 10}).json()
    assert [h["vector_id"] for h in r["hits"]] == list(reversed(ids))
    assert r["matched"] == 4 and r["count"] == 4
    r = client.get("/log/recent", params={"project": "recp", "limit": 2}).json()
    assert [h["vector_id"] for h in r["hits"]] == [ids[3], ids[2]] and r["matched"] == 4
    r = client.get("/log/recent", params={"project": "recp", "since": _iso(now - 850), "until": _iso(now - 750)}).json()
    assert [h["vector_id"] for h in r["hits"]] == [ids[2]]
    # no project: window lane only
    r = client.get("/log/recent").json()
    assert [h["vector_id"] for h in r["hits"]] == [ids[3], ids[2]]
    r = client.get("/log/recent", params={"project": "recp", "max_chars": 5}).json()
    assert all(h["truncated"] and len(h["source_text"]) == 5 for h in r["hits"])
    assert client.get("/log/recent", params={"since": "garbage"}).status_code == 400


def test_log_window_by_and_backfill(client, fresh_store):
    now = time.time()
    a = _log(client, "window row a", "winp", _iso(now - 10), author="Jane@Example.com")
    _log(client, "window row b", "winq", _iso(now - 5))
    w = client.get("/log/window").json()
    assert w["collection"] == "kh_hotlog" and w["slots_used"] == 2
    assert [s["project"] for s in w["slots"]] == ["winq", "winp"]
    assert w["caps"] == {"slots": config.HOTLOG_SLOTS, "max_per_slot": config.HOTLOG_MAX_PER_SLOT,
                         "max_age_days": config.HOTLOG_MAX_AGE_DAYS}
    by = client.get("/log/by", params={"author": "Jane@Example.com"}).json()
    assert by["hits"] == [] or all(h["metadata"].get("author") == "jane@example.com" for h in by["hits"])
    bf = client.post("/log/backfill-author", json={"author": "bob@example.com", "projects": ["winq"]}).json()
    assert bf["missing"] == 1 and bf["applied"] == 0
    bf = client.post("/log/backfill-author", json={"author": "bob@example.com", "projects": ["winq"],
                                                   "apply": True}).json()
    assert bf["applied"] == 1
    by = client.get("/log/by", params={"author": "BOB@example.com"}).json()
    assert len(by["hits"]) == 1
    assert a  # row a keeps its caller-given author in open mode


@pytest.mark.parametrize("scope", ["log"])
def test_delete_knowledge_log_scope_clears_hot_rows(client, fresh_store, scope):
    rid = _log(client, "hot delete", "hdel", _iso(time.time()))
    d = client.request("DELETE", "/knowledge", json={"ids": [rid], "project": "hdel", "data_scope": scope,
                                                     "scope": "vector"}).json()
    assert d["vectors_deleted"] == 1 and rid not in _hot_ids()
