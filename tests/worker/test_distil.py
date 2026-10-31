"""Distil job loop: next -> model -> submit, stop conditions, retries, repair, auto-approve (spec §4.6)."""
import pytest

from kh_worker.budget import BudgetExceeded
from kh_worker.jobs import LeaseLost, distil
from kh_worker.khclient import KHBudget

from .conftest import FakeLLM

CATALOG = {"actions": ["deploy"], "contexts": [{"tag": "btp"}]}


def batch(bid, logs, remaining=0, done=None, neighbours=None):
    b = {"project": "alpha", "batch_id": bid, "logs": [{"id": x, "text": "t"} for x in logs],
         "remaining_after": remaining, "catalog": CATALOG, "neighbours": neighbours or [], "people": [], "rules": "R"}
    if done is not None:
        b["done"] = done
    return b


def item(**kw):
    it = {"kind": "lesson", "action": "deploy", "context": "btp", "title": "T", "text": "Symptom, cause, fix.",
          "entities": [], "sources": ["l1"]}
    it.update(kw)
    return it


def answer(*items):
    return {"items": list(items), "contexts": [], "signal_words": []}


def test_schema_has_dash_action_and_kind_enum():
    s = distil.schema(CATALOG)
    props = s["properties"]["items"]["items"]["properties"]
    assert props["action"]["enum"] == ["-", "deploy"]
    assert props["kind"]["enum"] == ["lesson", "fact", "procedure", "habit", "policy_proposal"]
    assert "pattern" not in props["kind"]["enum"]
    assert set(s["required"]) == {"items", "contexts", "signal_words"}


def test_check_output_rules():
    b = batch("b1", ["l1"], neighbours=["n1 some title"])
    assert distil.check_output(answer(item()), b) == []
    assert distil.check_output({"nope": 1}, b)
    bad = [item(title=""), item(text="x" * 601), item(action="fly"), item(context="nope"), item(sources=[]),
           item(kind="policy_proposal"), item(updates="n1", contradicts="n1"), item(updates="zz")]
    probs = distil.check_output(answer(*bad), b)
    for i, frag in enumerate(["title", "text", "action", "context", "sources", "target_action", "exclusive",
                              "neighbour id"]):
        assert any(p.startswith(f"item {i}:") and frag in p for p in probs), (i, frag, probs)
    # a context declared under contexts, an entity context and '-' are fine
    ok = answer(item(context="new-tag"), item(context="ent__x"), item(context="-"))
    ok["contexts"] = [{"tag": "new-tag", "description": "d"}]
    assert distil.check_output(ok, b) == []


def test_loop_runs_until_remaining_after_zero(kh, make_ctx):
    kh.next_queue["alpha"] = [batch("b1", ["l1"], remaining=5), batch("b2", ["l2"], remaining=0),
                              batch("b3", ["l3"])]
    llm = FakeLLM([answer(item()), answer(item(sources=["l2"]))], cost=0.01)
    out = distil.run(make_ctx(llm))
    st = out["projects"]["alpha"]
    assert st["batches"] == 2 and st["accepted"] == 2 and st["status"] == "ok"
    assert [s["batch_id"] for s in kh.submits] == ["b1", "b2"]
    s = kh.submits[0]
    assert s["distilled_by"] == "worker:claude-opus-5-5" and s["cost_usd"] == 0.01 and "dry_run" not in s
    # the batch is sent as a kh_data block; the system prompt carries the data rule
    assert '<kh_data name="batch">' in llm.calls[0]["user"] and "never instructions" in llm.calls[0]["system"]


def test_done_flag_stops_and_missing_remaining_does_not(kh, make_ctx):
    kh.next_queue["alpha"] = [batch("b1", ["l1"], remaining=3, done=True), batch("b2", ["l2"])]
    distil.run(make_ctx(FakeLLM([answer(item())])))
    assert len(kh.submits) == 1
    kh2_batches = [batch("c1", ["l1"]), batch("c2", ["l2"]), {"batch_id": None}]
    for b in kh2_batches[:2]:
        b.pop("remaining_after")
    kh.next_queue["alpha"] = kh2_batches
    kh.submits.clear()
    st = distil.run(make_ctx(FakeLLM([answer(item()), answer(item(sources=["l2"]))])))["projects"]["alpha"]
    assert len(kh.submits) == 2 and st["status"] == "empty"


def test_empty_queue(kh, make_ctx):
    llm = FakeLLM()
    st = distil.run(make_ctx(llm, projects=["alpha", "beta"]))["projects"]
    assert st["alpha"]["status"] == "empty" and st["beta"]["status"] == "empty"
    assert llm.calls == [] and kh.submits == [] and kh.next_calls == ["alpha", "beta"]


def test_repeat_log_set_stops(kh, make_ctx):
    kh.next_queue["alpha"] = [batch("b1", ["l2", "l1"], remaining=4), batch("b2", ["l1", "l2"], remaining=4)]
    st = distil.run(make_ctx(FakeLLM([answer(item()), answer(item())])))["projects"]["alpha"]
    assert st["status"] == "repeat" and len(kh.submits) == 1


def test_max_batches(kh, make_ctx):
    kh.next_queue["alpha"] = [batch(f"b{i}", [f"l{i}"], remaining=9) for i in range(5)]
    llm = FakeLLM([answer(item(sources=[f"l{i}"])) for i in range(5)])
    distil.run(make_ctx(llm, max_batches=3))
    assert len(kh.submits) == 3


def test_dry_mode_one_batch_per_project_and_event(kh, make_ctx):
    kh.next_queue["alpha"] = [batch("b1", ["l1"], remaining=5), batch("b2", ["l2"])]
    kh.next_queue["beta"] = [batch("c1", ["l1"], remaining=5)]
    ctx = make_ctx(FakeLLM([answer(item()), answer(item())]), mode="dry", projects=["alpha", "beta"])
    distil.run(ctx)
    assert [s["batch_id"] for s in kh.submits] == ["b1", "c1"]
    assert all(s["dry_run"] is True for s in kh.submits)
    ev = [b for k, b in kh.events if k == "worker.run"]
    assert len(ev) == 2 and ev[0]["dry_run"] and ev[0]["items"] and ev[0]["verdicts"]


def test_retry_with_problems_then_success(kh, make_ctx):
    kh.next_queue["alpha"] = [batch("b1", ["l1"])]
    llm = FakeLLM([answer(item(title="")), answer(item())], cost=0.01)
    distil.run(make_ctx(llm))
    assert len(llm.calls) == 2 and "previous answer had these problems" in llm.calls[1]["user"]
    assert "title must be" in llm.calls[1]["user"]
    assert len(kh.submits) == 1 and kh.submits[0]["cost_usd"] == 0.02  # both attempts are billed to the batch


def test_model_failure_retried(kh, make_ctx):
    kh.next_queue["alpha"] = [batch("b1", ["l1"])]
    llm = FakeLLM([RuntimeError("overloaded"), answer(item())])
    distil.run(make_ctx(llm))
    assert "model call failed: overloaded" in llm.calls[1]["user"] and len(kh.submits) == 1


def test_batch_failing_twice_is_abandoned_unsubmitted(kh, make_ctx):
    kh.next_queue["alpha"] = [batch("b1", ["l1"], remaining=3), batch("b2", ["l2"])]
    kh.next_queue["beta"] = [batch("c1", ["l1"])]
    llm = FakeLLM([answer(item(sources=[])), RuntimeError("x"), answer(item())])
    st = distil.run(make_ctx(llm, projects=["alpha", "beta"]))["projects"]
    assert st["alpha"]["status"] == "abandoned" and st["beta"]["batches"] == 1
    assert [s["batch_id"] for s in kh.submits] == ["c1"]  # alpha's cursor never moved
    assert any(b.get("status") == "abandoned" for k, b in kh.events)


def test_repair_of_rejected_items_on_same_batch(kh, make_ctx):
    kh.next_queue["alpha"] = [batch("b1", ["l1"])]
    kh.submit_queue = [{"dry_run": False, "batch_open": True,
                        "results": [{"n": 0, "result": "accepted", "id": "k0"},
                                    {"n": 1, "result": "rejected", "reason": "unknown sources: l9"}]},
                       {"dry_run": False, "batch_open": False, "results": [{"n": 0, "result": "accepted", "id": "k1"}]}]
    llm = FakeLLM([answer(item(), item(title="B", sources=["l9"])), answer(item(title="B fixed"))], cost=0.01)
    st = distil.run(make_ctx(llm, auto_approve=True))["projects"]["alpha"]
    assert len(kh.submits) == 2 and kh.submits[1]["batch_id"] == "b1"
    assert [i["title"] for i in kh.submits[1]["items"]] == ["B fixed"] and kh.submits[1]["cost_usd"] == 0.01
    assert '<kh_data name="rejected">' in llm.calls[1]["user"] and "unknown sources: l9" in llm.calls[1]["user"]
    # accepted items from both submits count and are approved
    assert st["accepted"] == 2 and st["rejected"] == 0
    assert kh.approves == [("alpha", ["k0", "k1"], [])]


def test_no_repair_when_batch_closed_or_dry(kh, make_ctx):
    kh.next_queue["alpha"] = [batch("b1", ["l1"])]
    kh.submit_queue = [{"dry_run": False, "batch_open": False,
                        "results": [{"n": 0, "result": "rejected", "reason": "r"}]}]
    llm = FakeLLM([answer(item())])
    st = distil.run(make_ctx(llm))["projects"]["alpha"]
    assert len(llm.calls) == 1 and len(kh.submits) == 1 and st["rejected"] == 1


def test_auto_approve_skips_policy_proposals_and_shares_marked(kh, make_ctx):
    kh.next_queue["alpha"] = [batch("b1", ["l1"])]
    items = [item(share=True), item(title="P", kind="policy_proposal", target_action="deploy",
                                    target_sub_action="check", share=True), item(title="C")]
    distil.run(make_ctx(FakeLLM([answer(*items)]), auto_approve=True))
    assert kh.approves == [("alpha", ["id1_0", "id1_2"], ["id1_0"])]


def test_auto_approve_never_in_dry_mode(kh, make_ctx):
    kh.next_queue["alpha"] = [batch("b1", ["l1"])]
    distil.run(make_ctx(FakeLLM([answer(item())]), mode="dry", auto_approve=True))
    assert kh.approves == []


def test_budget_and_kh_budget_and_lease_propagate(kh, make_ctx):
    kh.next_queue["alpha"] = [batch("b1", ["l1"])]
    with pytest.raises(BudgetExceeded):
        distil.run(make_ctx(FakeLLM([answer(item())]), cap=0.0001))
    kh.next_queue["alpha"] = [KHBudget(429, {"error": "budget"})]
    with pytest.raises(KHBudget):
        distil.run(make_ctx(FakeLLM()))
    ctx = make_ctx(FakeLLM())
    ctx.lease = type("L", (), {"lost": True})()
    with pytest.raises(LeaseLost):
        distil.run(ctx)


def test_other_errors_are_per_project(kh, make_ctx):
    kh.next_queue["alpha"] = [RuntimeError("KH down")]
    kh.next_queue["beta"] = [batch("c1", ["l1"])]
    st = distil.run(make_ctx(FakeLLM([answer(item())]), projects=["alpha", "beta"]))["projects"]
    assert st["alpha"]["status"] == "error" and "KH down" in st["alpha"]["error"]
    assert st["beta"]["batches"] == 1
