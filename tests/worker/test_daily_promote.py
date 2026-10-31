"""Daily and promote jobs (spec §4.6)."""
import pytest

from kh_worker.jobs import daily, promote
from kh_worker.khclient import KHBudget, KHError

from .conftest import FakeLLM


def day(d, **kw):
    return {"day": d, "logs": [{"id": "l1", "text": "x"}], "listing": [], "tz": "Asia/Ho_Chi_Minh", "rules": "R",
            "rev": 1, "built_from_ms": 1000, **kw}


ANS = {"done": "Shipped X.", "decided": "", "broken_or_open": "Y fails.", "who": "Dev", "source_ids": ["l1"]}


# -- daily ------------------------------------------------------------------------------------------------


def test_compose_four_parts_none_and_cap():
    t = daily.compose(ANS)
    assert t == "Done: Shipped X.\nDecided: none\nBroken or open: Y fails.\nWho: Dev"
    long = daily.compose({"done": "a" * 2000, "decided": "b" * 300, "broken_or_open": "c", "who": "PO"})
    assert len(long) <= daily.CAP == 1200
    assert "Decided: " + "b" * 300 in long and long.startswith("Done: aaa") and "...\nDecided" in long


def test_daily_empty(kh, make_ctx):
    llm = FakeLLM()
    assert daily.run(make_ctx(llm))["status"] == "empty" and llm.batches == []


def test_daily_one_batch_for_all_projects_and_submit_body(kh, make_ctx):
    kh.daily_days = {"alpha": [day("2026-10-29"), day("2026-10-30")], "beta": [day("2026-10-30")]}
    llm = FakeLLM([ANS, ANS, ANS], cost=0.01)
    out = daily.run(make_ctx(llm, projects=["alpha", "beta"], mode="dry"))
    assert len(llm.batches) == 1 and len(llm.batches[0]) == 3 and llm.calls == []
    assert out == {"status": "ok", "days": 3, "served": 3, "deferred": 0, "written": 3, "skipped": 0}
    b = kh.daily_submits[0]
    assert b["project"] == "alpha" and b["day"] == "2026-10-29" and b["source_ids"] == ["l1"]
    assert b["rev"] == 1 and b["built_from_ms"] == 1000 and b["model"] == "claude-opus-5-5"
    assert b["cost_usd"] == 0.01 and b["dry_run"] is True and b["text"].startswith("Done: ")
    assert llm.batches[0][0]["max_tokens"] == 4000 and '<kh_data name="day">' in llm.batches[0][0]["user"]


def test_daily_defers_what_does_not_fit_the_run_budget(kh, make_ctx):
    kh.daily_days = {"alpha": [day(f"2026-10-2{i}") for i in range(5)]}
    llm = FakeLLM([ANS] * 5)
    # one day's batch estimate is ~0.04 (4000 output tokens x 20/MTok x 0.5); 0.1 fits two
    out = daily.run(make_ctx(llm, cap=0.1))
    assert out["served"] == 2 and out["deferred"] == 3 and len(kh.daily_submits) == 2


def test_daily_retries_failed_answers_once_in_second_batch(kh, make_ctx):
    kh.daily_days = {"alpha": [day("d1"), day("d2")]}
    llm = FakeLLM([ANS, RuntimeError("x"), RuntimeError("y")])
    out = daily.run(make_ctx(llm))
    assert len(llm.batches) == 2 and len(llm.batches[1]) == 1
    assert out["written"] == 1 and out["skipped"] == 1


def test_daily_retry_refused_by_budget_keeps_paid_answers(kh, make_ctx):
    kh.daily_days = {"alpha": [day("d1"), day("d2")]}
    llm = FakeLLM([ANS, RuntimeError("x")], cost=0.06)
    out = daily.run(make_ctx(llm, cap=0.09))  # both fit once (~0.08); after 0.06 spent the retry (~0.04) does not
    assert out["written"] == 1 and out["skipped"] == 1 and len(llm.batches) == 1


def test_daily_repair_on_leak_refused_and_skip_conflict(kh, make_ctx):
    kh.daily_days = {"alpha": [day("d1"), day("d2")]}
    kh.daily_answers = [KHError(400, {"error": "leak_refused", "detail": "names customer Z"}), {"ok": True},
                        KHError(409, {"error": "write_conflict"})]
    fixed = dict(ANS, done="Shipped X (fixed).", source_ids=["l1", "l2"])
    llm = FakeLLM([ANS, ANS, fixed], cost=0.01)
    out = daily.run(make_ctx(llm))
    assert len(llm.calls) == 1 and "KH refused" in llm.calls[0]["user"] and "names customer Z" in llm.calls[0]["user"]
    assert kh.daily_submits[1]["text"].startswith("Done: Shipped X (fixed).")
    assert kh.daily_submits[1]["source_ids"] == ["l1", "l2"] and kh.daily_submits[1]["cost_usd"] == 0.02
    assert out["written"] == 1 and out["skipped"] == 1 and len(kh.daily_submits) == 3


def test_daily_next_errors(kh, make_ctx):
    kh.daily_days = {"alpha": RuntimeError("down"), "beta": [day("d1")]}
    out = daily.run(make_ctx(FakeLLM([ANS]), projects=["alpha", "beta"]))
    assert out["written"] == 1
    kh.daily_days = {"alpha": KHBudget(429, {"error": "budget"})}
    with pytest.raises(KHBudget):
        daily.run(make_ctx(FakeLLM()))


# -- promote ----------------------------------------------------------------------------------------------

ROWS = [{"id": "r1", "owner": "a@x"}, {"id": "r2", "owner": "b@x"}, {"id": "r3", "owner": "a@x"}]
DATA = {"rows": ROWS, "common": [{"action": "deploy"}, {"action": "review"}], "kernel": "K", "proposals": [],
        "rules": {"min_people": 2, "text_max": 600}}


def pitem(**kw):
    it = {"title": "T", "text": "Step text.", "target_action": "deploy", "target_sub_action": "check",
          "sources": ["r1", "r2"], "why": "w"}
    it.update(kw)
    return it


def test_promote_schema_and_check():
    s = promote.schema(DATA["common"])
    assert s["properties"]["items"]["items"]["properties"]["target_action"]["enum"] == ["deploy", "review"]
    ok = promote.check([pitem(), pitem(sources=["r1", "r3"]), pitem(sources=["r9", "r2"]), pitem(title=""),
                        pitem(text="x" * 601), pitem(sources=[])], DATA)
    assert ok == [pitem()]  # owners counted, not rows


def test_promote_empty_and_none(kh, make_ctx):
    kh.promote_data = {"rows": []}
    llm = FakeLLM()
    assert promote.run(make_ctx(llm))["status"] == "empty" and llm.calls == []
    kh.promote_data = DATA
    assert promote.run(make_ctx(FakeLLM([{"items": [pitem(sources=["r1", "r3"])]}])))["status"] == "none"
    assert kh.promote_submits == []


def test_promote_submit_dry_and_never_approves(kh, make_ctx):
    kh.promote_data = DATA
    llm = FakeLLM([{"items": [pitem()]}])
    out = promote.run(make_ctx(llm, mode="dry"))
    assert len(llm.calls) == 1 and out["submitted"] == 1 and out["dry_run"] is True
    b = kh.promote_submits[0]
    assert b["dry_run"] is True and b["distilled_by"] == "worker:claude-opus-5-5"
    for name in ("rows", "common", "kernel", "proposals", "rules"):
        assert f'<kh_data name="{name}">' in llm.calls[0]["user"]
    assert kh.approves == []


def test_promote_repairs_refused_items_once(kh, make_ctx):
    kh.promote_data = DATA
    kh.promote_answers = [{"dry_run": False, "results": [{"n": 0, "result": "accepted", "id": "p0"},
                                                         {"n": 1, "result": "rejected", "reason": "bad step"}]},
                          {"dry_run": False, "results": [{"n": 0, "result": "accepted", "id": "p1"}]}]
    llm = FakeLLM([{"items": [pitem(), pitem(title="B")]}, {"items": [pitem(title="B2")]}])
    out = promote.run(make_ctx(llm))
    assert len(llm.calls) == 2 and "bad step" in llm.calls[1]["user"]
    assert [i["title"] for i in kh.promote_submits[1]["items"]] == ["B2"]
    assert [r["id"] for r in out["results"]] == ["p0", "p1"]
