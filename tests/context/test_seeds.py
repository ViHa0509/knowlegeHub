"""Appendix A seeds: 14 common kernel rules + 14 every-project actions, verbatim, never overwriting edits."""
from __future__ import annotations

import re
from pathlib import Path

from app import seeding, sqldb
from app.kernel import rules as R
from app.policy_actions import store

SPEC = Path(__file__).resolve().parents[2] / "docs" / "spec" / "KH-6.5-co-che-prod.md"


def _spec_lines(start: str, end: str) -> list[str]:
    lines = SPEC.read_text(encoding="utf-8").split("\n")
    i = next(n for n, l in enumerate(lines) if l.startswith(start))
    j = next(n for n, l in enumerate(lines) if n > i and l.startswith(end))
    return lines[i:j]


def test_kernel_seed_count_and_verbatim(client):
    rows = {r["slot"]: r for r in R.list_rules("common")}
    seeded = [r for r in rows.values() if r["updated_by"] == "seed"]
    assert len(seeded) == 14
    spec_rows = 0
    for line in _spec_lines("### A.1", "### A.2"):
        m = re.match(r"\| (-?\d+) \| `(\w+)` \| (yes|no) \| ([^|]+) \| (.+) \|$", line)
        if not m:
            continue
        spec_rows += 1
        r = rows[m.group(2)]
        assert r["content"] == m.group(5).strip()
        assert r["position"] == int(m.group(1))
        assert r["locked"] == (m.group(3) == "yes")
        assert r["tags"] == [t.strip() for t in m.group(4).split(",")]
    assert spec_rows == 14
    assert rows["kh_403"]["content"] == "A 403 from KH is final; never work around it."
    assert rows["kh_header"]["position"] == -1 and rows["kh_header"]["locked"]


def test_policy_seed_count_and_verbatim(client):
    acts = [a for a in store.list_actions(store.EVERY) if a["project"] == store.EVERY]
    assert len(acts) == 14
    assert all(a["priority"] == 100 for a in acts)
    labels = {a["action"] for a in acts}
    assert {"talk with user", "save knowledge", "store worklog", "write message to people"} <= labels
    rules = [r for r in store.all_rules() if r["project"] == store.EVERY]
    assert len(rules) == 59
    plan = store.plan("talk with user", None)
    assert [r["sub_action"] for r in plan] == ["reply", "no side effects", "unclear task"]
    assert plan[1]["content"] == "No KH writes and no tools for pure small talk; store no work log."
    ts = store.plan("troubleshoot problem", None)
    assert [(r["seq"], r["sub_action"]) for r in ts] == [(1, "lessons first"), (3, "answer")]
    assert "prod has no seq 2" not in ts[1]["content"]
    by = {a["action"]: a for a in acts}
    assert by["store worklog"]["source_policy"] == "helper-auto-worklog-2026-10-19"
    assert by["switch work mode"]["source_policy"] == ""
    assert by["talk with user"]["description"] == ("Greetings, thanks, small talk, questions about you or KH, or a "
                                                   "message too unclear to map to any task.")


def test_every_plan_step_matches_spec(client):
    cur = None
    n = 0
    for line in _spec_lines("### A.2", "### A.3"):
        m = re.match(r"^\*\*(.+?)\*\* — ", line)
        if m:
            cur = m.group(1)
            continue
        m = re.match(r"^(\d+)\. ([^:]+): (.+)$", line)
        if m and cur:
            r = store.get_rule(cur, m.group(2).strip(), None)
            assert r is not None, (cur, m.group(2))
            want = re.sub(r" \(prod has no seq 2 for this action; gaps in seq are fine\)$", "", m.group(3).strip())
            assert r["content"] == want
            assert r["seq"] == int(m.group(1))
            n += 1
    assert n == 59


def test_seeding_never_overwrites_edits(client):
    orig = store.get_rule("talk with user", "reply", None)
    try:
        store.set_rule(store.EVERY, "talk with user", "reply", "EDITED", orig["seq"])
        k = R.get("common", "kh_versions")
        with sqldb.connect() as c:
            c.execute("UPDATE kernel_rule SET content=? WHERE id=?", ("EDITED KERNEL", k["id"]))
        out = seeding.seed_all()
        assert "skipped" in out["policy_actions"]
        assert store.get_rule("talk with user", "reply", None)["content"] == "EDITED"
        assert R.get("common", "kh_versions")["content"] == "EDITED KERNEL"
    finally:
        store.set_rule(store.EVERY, "talk with user", "reply", orig["content"], orig["seq"])
        with sqldb.connect() as c:
            c.execute("UPDATE kernel_rule SET content=? WHERE id=?", (k["content"], k["id"]))


def test_hook_seeds_present(client):
    from app import hooks

    names = {(r["hook_type"], r["name"]) for r in hooks.list_rules()}
    assert ("PreToolUseTools", "pre-action-check") in names
    assert ("UserPromptSubmit", "step-wise") in names
    pre = [r for r in hooks.list_rules("PreToolUse") if r["name"] == "pre-action-check"][0]
    assert len(pre["content"]) == 661
