"""POST /context/step: tiers, floor, budget, open threads, time steps, bench calls (§3.3)."""
from __future__ import annotations

from app import step


def test_empty_pack_shape(client):
    r = client.post("/context/step", json={"text": "zzqx nothing matches this qqzz"})
    assert r.status_code == 200
    j = r.json()
    assert set(j) >= {"project", "time_step", "since", "tiers", "floor", "budget_chars", "used_chars", "cut_at_budget",
                      "count", "best_score", "items", "open_threads"}
    assert j["floor"] == 0.68 and j["budget_chars"] == 6000 and j["count"] == 0 and "keys" not in j


def test_knowledge_floor_and_log_skipped(client, add_row, uid):
    p = f"st-{uid}"
    text = f"how to rotate the {uid} signing certificate on the gateway"
    kid = add_row(text, project=p)
    add_row(f"completely unrelated cooking recipe {uid} pasta tomato basil", project=p)
    lid = add_row(text + " done", scope="log", project=p)
    j = client.post("/context/step", json={"text": text, "project": p}).json()
    ids = [i["id"] for i in j["items"]]
    assert kid in ids and lid not in ids and len(ids) == 1
    assert j["items"][0]["tier"] == "knowledge" and j["items"][0]["score"] >= 0.99
    assert j["best_score"] >= 0.99
    t = {x["kind"]: x for x in j["open_threads"]}
    assert t["log-skipped"]["count"] == 1
    assert t["log-skipped"]["hint"] == ("repeat the step with include_log=true for history, or when knowledge seems "
                                        "stale")
    j2 = client.post("/context/step", json={"text": text, "project": p, "include_log": True}).json()
    assert {i["tier"] for i in j2["items"]} == {"knowledge", "log"} and j2["tiers"] == ["knowledge", "log"]
    assert any(i.get("ts") for i in j2["items"] if i["tier"] == "log")


def test_budget_cut_and_threads(client, add_row, uid):
    p = f"bud-{uid}"
    base = f"budget topic {uid} alpha"
    for i in range(4):
        add_row(base + " " + ("lorem ipsum " * 60) + f" row{i}", project=p)
    j = client.post("/context/step", json={"text": base + " " + ("lorem ipsum " * 60), "project": p,
                                           "budget_chars": 700}).json()
    assert j["used_chars"] <= 700 and j["cut_at_budget"] is True
    assert j["items"][0]["truncated"] is True
    cut = next(t for t in j["open_threads"] if t["kind"] == "cut-at-budget")
    assert cut["count"] >= 1 and len(cut["ids"]) <= 5
    assert cut["hint"] == "rows left out at the budget; read one with POST /vector/scroll {ids} if needed"


def test_pack_unit():
    pk = step._Pack(500)
    assert pk.add("knowledge", {"id": "a", "score": 1, "text": "x" * 450})
    assert not pk.add("knowledge", {"id": "a", "score": 1, "text": "dup"})
    assert not pk.add("log", {"id": "b", "score": 1, "text": "y" * 100})  # room 50 < 200: dropped, pack full
    assert pk.full and pk.dropped == [{"tier": "log", "id": "b", "score": 1}]
    assert not pk.add("log", {"id": "c", "score": 0.9, "text": "z"})
    assert [d["id"] for d in pk.dropped] == ["b", "c"]
    pk2 = step._Pack(1000, ["log"])
    assert not pk2.add("knowledge", {"id": "k", "text": "x"}) and pk2.dropped == []
    pk3 = step._Pack(500)
    pk3.add("knowledge", {"id": "a", "text": "x" * 250})
    pk3.add("knowledge", {"id": "b", "text": "x" * 400})
    assert pk3.items[1]["truncated"] and len(pk3.items[1]["text"]) == 250 and pk3.items[1]["text_len"] == 400


def test_entity_tier_and_refs(client, add_row, uid):
    p = f"se-{uid}"
    client.post("/entities", json={"project": p, "name": f"Gizmo{uid}", "type": "system", "role": "payments",
                                   "relations": [{"rel": "calls", "target": f"Widget{uid}"}]})
    client.post("/entities", json={"project": p, "name": f"Widget{uid}", "type": "system"})
    text = f"why does Gizmo{uid} fail on deploy"
    add_row(text + " see ABC-123 and PR #42", project=p)
    j = client.post("/context/step", json={"text": text, "project": p}).json()
    first = j["items"][0]
    assert first["tier"] == "entity" and first["score"] is None and first["text"].startswith(f"Gizmo{uid} (system,")
    kinds = {t["kind"]: t for t in j["open_threads"]}
    assert set(kinds["reference-not-looked-up"]["refs"]) == {"ABC-123", "PR #42"}
    assert f"Widget{uid}" in kinds["entity-not-loaded"]["names"]


def test_time_step(client, add_row, uid):
    p = f"ts-{uid}"
    add_row(f"worked on the {uid} importer", scope="log", project=p)
    j = client.post("/context/step", json={"text": "what changed recently", "project": p}).json()
    assert j["time_step"] is True and j["since"] is None
    assert [i["tier"] for i in j["items"]] == ["log-recent"]
    from datetime import datetime, timezone

    until = datetime(2026, 10, 31, tzinfo=timezone.utc).timestamp()
    assert step.time_window("status on 2026-10-30 please") == (True, "2026-10-30T00:00:00Z", until)
    assert step.time_window("nothing about time")[0] is False
    assert step.time_window("hom qua lam gi")[0] is True


def test_bench_validation(client):
    r = client.post("/context/step", json={"text": "x", "only_tiers": ["bogus"]})
    assert r.status_code == 422
    r = client.post("/context/step", json={"text": "x", "overrides": {"KH_NOPE": 1}})
    assert r.status_code == 422
    r = client.post("/context/step", json={"text": "x", "overrides": {"KH_STEP_FLOOR": "abc"}})
    assert r.status_code == 422
    r = client.post("/context/step", json={"text": "x", "only_tiers": ["distilled"],
                                           "with_items": [{"id": "cand1", "text": "candidate text"}],
                                           "overrides": {"KH_STEP_FLOOR": 0.9}})
    j = r.json()
    assert j["floor"] == 0.9 and j["items"][0]["key"] == "injected" and j["items"][0]["injected"] is True
    assert client.post("/context/step", json={"text": ""}).status_code == 422
    assert client.post("/context/step", json={"text": "x", "budget_chars": 100}).status_code == 422


def test_signed_in_permission(monkeypatch, client, uid):
    try:
        from tests.auth_helpers import bearer, enable_signin, make_token
    except Exception:  # pragma: no cover
        import pytest

        pytest.skip("auth helpers not available")
    enable_signin(monkeypatch)
    h = bearer(make_token(f"u{uid}@x.com", projects={f"mine-{uid}": "editable"}))
    r = client.post("/context/step", json={"text": "hello", "project": f"other-{uid}"}, headers=h)
    assert r.status_code == 403
    r = client.post("/context/step", json={"text": "hello", "project": f"mine-{uid}"}, headers=h)
    assert r.status_code == 200
    r = client.post("/context/step", json={"text": "hello", "overrides": {"KH_STEP_FLOOR": 0.5}}, headers=h)
    assert r.status_code == 403
