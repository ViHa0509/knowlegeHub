"""Distillation pipeline (§4.2-4.3): consent, next, submit, review, rekey, patterns, signals, cursor, serving."""
from __future__ import annotations

import json
import re

import pytest

from app import distill
from app import vector_store as vs
from tests.learning.conftest import ACTION, item


def _meta(project, rid):
    h = vs.get_existing(vs.part_collection_name("knowledge", project))
    rows = vs.get_ids_vt(h, [rid])
    return rows[0]["metadata"] if rows else None


def _text(project, rid):
    h = vs.get_existing(vs.part_collection_name("knowledge", project))
    return vs.get_ids_vt(h, [rid])[0]["source_text"]


# ---------------------------------------------------------------------------
# Consent and shapes
# ---------------------------------------------------------------------------


def test_consent_gates(client, uid, monkeypatch):
    monkeypatch.delenv("KH_DISTILL_PROJECTS", raising=False)
    p = f"nc-{uid}"
    detail = f"project '{p}' has not consented to distillation (KH_DISTILL_PROJECTS)"
    r = client.get("/distill/next", params={"project": p})
    assert r.status_code == 403 and r.json()["detail"] == detail
    r = client.post("/distill/submit", json={"project": p, "batch_id": "db_x", "items": []})
    assert r.status_code == 403
    assert client.get("/distill/patterns", params={"project": p}).status_code == 403
    assert client.post("/distill/cursor/reset", json={"project": p}).status_code == 403
    # cursor read is not consent-gated; it reports the flag
    j = client.get("/distill/cursor", params={"project": p}).json()
    assert j == {"project": p, "consented": False, "last_ts": 0.0, "topics": 0, "contexts": []}


def test_next_empty_shape_and_catalog(client, proj):
    r = client.get("/distill/next", params={"project": proj})
    assert r.status_code == 200
    j = r.json()
    assert set(j) >= {"project", "batch_id", "logs", "passed_without_signal", "confirmed_in_code", "remaining_after",
                      "done", "neighbours", "people", "catalog", "rules"}
    assert j["batch_id"] is None and j["logs"] == [] and j["done"] is True and j["remaining_after"] == 0
    assert j["catalog"]["kinds"] == ["lesson", "fact", "procedure", "habit", "policy_proposal"]
    assert ACTION in j["catalog"]["actions"] and j["catalog"]["contexts"] == []
    assert isinstance(j["rules"], str) and "English only" in j["rules"]


@pytest.mark.parametrize("limit", [0, 101])
def test_next_limit_bounds(client, proj, limit):
    assert client.get("/distill/next", params={"project": proj, "limit": limit}).status_code == 422


def test_defaults():
    assert distill.batch_default() == 20 and distill.scan_max() == 400
    assert distill.confirm_floor() == 0.85 and distill.same_floor() == 0.95
    assert distill.log_chars() == 2000 and distill.neighbour_chars() == 6000
    assert distill.title_chars() == 120 and distill.item_chars() == 600
    assert distill.shared_project() == "lesson-learned"
    words = {s["word"]: s["kind"] for s in distill.signals()}
    assert words.get("decided") == "decision" and words.get("failed") == "failure"
    assert words.get("quyet dinh") == "decision" and words.get("runbook") == "procedure"
    assert {"decision", "failure", "newcomer", "procedure", "correction"} <= set(words.values())


# ---------------------------------------------------------------------------
# Step A: next
# ---------------------------------------------------------------------------


def test_next_why_signals_topics_and_passed(client, proj, add_log, monkeypatch):
    a = add_log(proj, "we decided to pin the parser version", topic="build")
    b = add_log(proj, "routine check of nothing in particular", topic="build")
    c = add_log(proj, "plain note without any keyword at all", topic="infra")
    j = client.get("/distill/next", params={"project": proj}).json()
    by = {lg["id"]: lg for lg in j["logs"]}
    assert by[a]["why"] == ["word:decided", "new-topic"]
    assert b not in by and j["passed_without_signal"] == 1
    assert by[c]["why"] == ["new-topic"]
    assert [lg["id"] for lg in j["logs"]] == [a, c]          # (ts, id) order
    lg = by[a]
    assert set(lg) == {"id", "ts", "topic", "why", "text", "prompts"} and lg["topic"] == "build"
    assert j["batch_id"].startswith("db_") and len(j["batch_id"]) == 35
    assert j["done"] is True and j["remaining_after"] == 0
    # sampling: 100 % picks every unsignalled row
    monkeypatch.setenv("KH_DISTILL_SAMPLE_PCT", "100")
    j2 = client.get("/distill/next", params={"project": proj}).json()
    assert {lg["id"]: lg["why"] for lg in j2["logs"]}[b] == ["sample"]


def test_next_limit_and_remaining(client, proj, add_log):
    ids = [add_log(proj, f"we decided step {i}", topic="same") for i in range(5)]
    j = client.get("/distill/next", params={"project": proj, "limit": 2}).json()
    assert [lg["id"] for lg in j["logs"]] == ids[:2]
    assert j["remaining_after"] == 3 and j["done"] is False


def test_log_text_cut(client, proj, add_log):
    add_log(proj, "we decided " + "x" * 2500)
    lg = client.get("/distill/next", params={"project": proj}).json()["logs"][0]
    assert len(lg["text"]) == 2000


def test_cursor_moves_only_on_submit(client, proj, add_log, distil):
    a = add_log(proj, "we decided to use blue green deploys")
    j1 = client.get("/distill/next", params={"project": proj}).json()
    j2 = client.get("/distill/next", params={"project": proj}).json()
    assert [x["id"] for x in j1["logs"]] == [x["id"] for x in j2["logs"]] == [a]
    assert j1["batch_id"] != j2["batch_id"]
    r = client.post("/distill/submit", json={"project": proj, "batch_id": j2["batch_id"], "items": [],
                                             "distilled_by": "test"})
    assert r.status_code == 200
    s = r.json()
    assert s["batch_open"] is False and s["results"] == [] and s["cursor"] > 0
    cur = client.get("/distill/cursor", params={"project": proj}).json()
    assert cur["consented"] is True and cur["last_ts"] == s["cursor"] and cur["topics"] == 1
    j3 = client.get("/distill/next", params={"project": proj}).json()
    assert j3["logs"] == [] and j3["batch_id"] is None
    # the closed batch cannot be resent
    r = client.post("/distill/submit", json={"project": proj, "batch_id": j2["batch_id"], "items": []})
    assert r.status_code == 409 and r.json()["detail"] == "unknown or already submitted batch"
    # a new row with a later ts is served; reset re-serves everything
    b = add_log(proj, "we decided again")
    assert [x["id"] for x in client.get("/distill/next", params={"project": proj}).json()["logs"]] == [b]
    rr = client.post("/distill/cursor/reset", json={"project": proj})
    assert rr.json() == {"project": proj, "reset": True}
    assert client.post("/distill/cursor/reset", json={"project": proj}).json()["reset"] is False
    ids = [x["id"] for x in client.get("/distill/next", params={"project": proj}).json()["logs"]]
    assert ids == [a, b]


def test_follows_signal(client, proj, add_log):
    a = add_log(proj, "the build failed on the parser", topic="t1")
    b = add_log(proj, f"follows {a}: retried with a clean cache", topic="t1")
    j = client.get("/distill/next", params={"project": proj}).json()
    assert {x["id"]: x["why"] for x in j["logs"]}[b] == ["follows-signal"]


def test_confirmed_in_code(client, proj, add_log, distil):
    a = add_log(proj, "the deploy failed because the cache was stale", topic="deploy")
    title, text = "Stale cache breaks deploys", "The deploy failed because the cache was stale; clear it first."
    _nj, r = distil(proj, lambda nj: [item([a], title, text)])
    iid = r.json()["results"][0]["id"]
    client.post("/distill/approve", json={"project": proj, "ids": [iid]})
    # same topic (not new) + signal word present in the item + embedding identical -> confirms, never shown
    b = add_log(proj, f"{title}. {text}", topic="deploy")
    j = client.get("/distill/next", params={"project": proj}).json()
    assert j["logs"] == [] and j["confirmed_in_code"] == {iid: 1}
    s = client.post("/distill/submit", json={"project": proj, "batch_id": j["batch_id"], "items": []}).json()
    assert s["confirmed_in_code"] == {iid: 1}
    m = _meta(proj, iid)
    assert json.loads(m["sources_json"]) == [a, b] and m["evidence_count"] == 2


# ---------------------------------------------------------------------------
# Step B: submit
# ---------------------------------------------------------------------------


def test_submit_accept_and_item_row(client, proj, add_log, distil):
    a = add_log(proj, "we decided to always run migrations before deploy")
    _nj, r = distil(proj, lambda nj: [item([a], "Run migrations first", "Run DB migrations before each deploy.",
                                           context="-")], distilled_by="human:me")
    assert r.status_code == 200
    s = r.json()
    assert set(s) >= {"project", "dry_run", "results", "contexts", "signal_words", "confirmed_in_code", "cursor",
                      "batch_open", "cost_usd"}
    res = s["results"][0]
    key = distill.dkey(proj, "lesson", ACTION, "-", "-")
    assert res == {"n": 0, "result": "accepted", "id": key} and re.fullmatch(r"dk_[0-9a-f]{12}", key)
    m = _meta(proj, key)
    assert m["status"] == "proposed" and m["knowledge_type"] == "proposed" and m["topic"] == "distilled"
    assert m["scope"] == "knowledge" and m["project"] == proj and m["kind"] == "lesson"
    assert m["action"] == ACTION and m["context"] == "-" and m["entity_key"] == "-" and m["dkey"] == key
    assert json.loads(m["sources_json"]) == [a] and m["evidence_count"] == 1
    assert m["distilled_by"] == "human:me" and m["title"] == "Run migrations first"
    for f in ("first_seen", "last_confirmed", "distilled_at", "ts"):
        assert m[f].endswith("Z")
    # pending lists it with sources
    p = client.get("/distill/pending", params={"project": proj}).json()
    assert p["project"] == proj and p["count"] == 1
    assert p["lines"][0] == (f"{key} lesson [{ACTION} / -] Run migrations first: Run DB migrations before each "
                             f"deploy. (sources: {a})")


@pytest.mark.parametrize("bad,reason", [
    ({"kind": "pattern"}, "kind must be one of lesson, fact, procedure, habit, policy_proposal"),
    ({"action": "fly a kite"}, "unknown action 'fly a kite'"),
    ({"context": "nowhere"}, "unknown context 'nowhere'"),
    ({"title": "t" * 121}, "title must be 1..120 characters"),
    ({"title": ""}, "title must be 1..120 characters"),
    ({"text": "x" * 601}, "text must be 1..600 characters"),
    ({"sources": []}, "sources must not be empty"),
    ({"sources": ["vec_nope"]}, "unknown sources: vec_nope"),
    ({"text": "mail ops@example.com for access"}, "leak gate: 'ops@example.com'"),
    ({"text": "set password=abc123xyz in the env"}, "leak gate:"),
    ({"text": "uses key sk-abcdefgh12345 now"}, "leak gate: 'sk-abcdefgh12345'"),
    ({"text": "token eyJhbGciOi.eyJzdWIiOi.sig here"}, "leak gate:"),
    ({"text": "the host 10.1.2.3 was down"}, "leak gate: '10.1.2.3'"),
    ({"text": "api.internal.example.com was slow"}, "leak gate:"),
    ({"entities": ["bob"]}, "entities must be entity card ids (ent__...)"),
    ({"kind": "policy_proposal"}, "policy_proposal needs a known target_action"),
    ({"kind": "policy_proposal", "target_action": ACTION}, "policy_proposal needs a non-empty target_sub_action"),
])
def test_submit_rejections(client, proj, add_log, distil, bad, reason):
    a = add_log(proj, "we decided something worth keeping")

    def items(nj):
        it = item([a], "A good title", "A good body text.")
        it.update(bad)
        return [item([a], "Fine item", "This one is fine and accepted."), it]

    _nj, r = distil(proj, items)
    res = r.json()["results"]
    assert res[0]["result"] == "accepted"
    assert res[1]["n"] == 1 and res[1]["result"] == "rejected" and res[1]["reason"].startswith(reason)
    assert r.json()["batch_open"] is True


def test_rejected_batch_stays_open_and_resend(client, proj, add_log):
    a = add_log(proj, "we decided on a retry policy")
    nj = client.get("/distill/next", params={"project": proj}).json()
    body = {"project": proj, "batch_id": nj["batch_id"], "distilled_by": "t",
            "items": [item([a], "", "no title")]}
    s1 = client.post("/distill/submit", json=body).json()
    assert s1["batch_open"] is True and s1["results"][0]["result"] == "rejected"
    cur1 = s1["cursor"]
    assert cur1 > 0
    body["items"] = [item([a], "Retry policy", "Retry three times with backoff.")]
    s2 = client.post("/distill/submit", json=body).json()
    assert s2["results"][0]["result"] == "accepted" and s2["batch_open"] is False and s2["cursor"] == cur1


def test_cost_usd_validation_and_closed_batch(client, proj, add_log):
    add_log(proj, "we decided cost tracking")
    nj = client.get("/distill/next", params={"project": proj}).json()
    for bad in (-1, 1001, "abc", True):
        r = client.post("/distill/submit", json={"project": proj, "batch_id": nj["batch_id"], "items": [],
                                                 "cost_usd": bad})
        assert r.status_code == 400 and r.json()["detail"] == "cost_usd must be a number 0..1000"
    r = client.post("/distill/submit", json={"project": proj, "batch_id": nj["batch_id"], "items": [],
                                             "cost_usd": 0.25})
    assert r.json()["cost_usd"] == 0.25 and r.json()["batch_open"] is False
    row = distill._batch(nj["batch_id"])
    assert row["closed"] == 1 and row["cost_usd"] == 0.25 and row["claimed"] == 1
    # other project's batch / unknown id
    r = client.post("/distill/submit", json={"project": proj, "batch_id": "db_unknown", "items": []})
    assert r.status_code == 409


def test_contexts_and_signal_words_in_same_call(client, proj, add_log, distil):
    a = add_log(proj, "we decided the deploy pipeline order")
    _nj, r = distil(proj, lambda nj: [item([a], "Pipeline order", "Build, test, then deploy.",
                                           context="Deploy Pipeline")],
                    contexts=[{"tag": "Deploy Pipeline", "description": "CI/CD"}, {"tag": "x"}],
                    signal_words=["flakyness", {"word": "hotfix", "kind": "failure", "reason": "r"}, "ab",
                                  "a@b.com"])
    s = r.json()
    assert s["contexts"][0] == {"tag": "deploy-pipeline", "result": "added"}
    assert s["contexts"][1]["result"] == "rejected"
    assert s["results"][0]["result"] == "accepted"
    assert _meta(proj, s["results"][0]["id"])["context"] == "deploy-pipeline"
    sw = {x["word"]: x["result"] for x in s["signal_words"]}
    assert sw["flakyness"] == "added" and sw["hotfix"] == "added" and sw["ab"] == "rejected"
    assert s["signal_words"][3]["result"] == "rejected" and "leak gate" in s["signal_words"][3]["reason"]
    words = {w["word"]: w for w in client.get("/distill/signals").json()["words"]}
    assert words["hotfix"]["kind"] == "failure" and words["hotfix"]["source"] == "test"
    cur = client.get("/distill/cursor", params={"project": proj}).json()
    assert cur["contexts"] == [{"tag": "deploy-pipeline", "description": "CI/CD", "source": "test"}]
    client.post("/distill/signals", json={"remove": ["flakyness", "hotfix"]})


def test_signal_hits_counted(client, proj, add_log):
    word = f"zork{proj[-6:]}"
    r = client.post("/distill/signals", json={"add": [{"word": word, "kind": "decision"}]}).json()
    assert r["added"][0]["result"] == "added"
    add_log(proj, f"the {word} happened")
    nj = client.get("/distill/next", params={"project": proj}).json()
    assert nj["logs"][0]["why"][0] == f"word:{word}"
    client.post("/distill/submit", json={"project": proj, "batch_id": nj["batch_id"], "items": []})
    words = {w["word"]: w for w in client.get("/distill/signals").json()["words"]}
    assert words[word]["hits"] == 1
    j = client.get("/distill/signals").json()
    assert j["count"] == len(j["words"])
    assert set(j["words"][0]) == {"word", "kind", "source", "reason", "hits", "added_at"}
    rm = client.post("/distill/signals", json={"remove": [word]}).json()
    assert rm["removed"] == [word]


def test_merge_update_contradict(client, proj, add_log, distil):
    a = add_log(proj, "we decided to tag releases")
    title, text = "Tag every release", "Create a git tag for every production release."
    _nj, r = distil(proj, lambda nj: [item([a], title, text)])
    iid = r.json()["results"][0]["id"]
    # same text, other key, no relation -> merged into the same row (cosine >= 0.95)
    b = add_log(proj, "we decided again to tag releases")
    _nj, r = distil(proj, lambda nj: [item([b], title, text, context="-", action="-")])
    res = r.json()["results"][0]
    assert res["result"] == "merged" and res["id"] == iid
    m = _meta(proj, iid)
    assert json.loads(m["sources_json"]) == [a, b] and m["evidence_count"] == 2
    # updates: rewrite in place
    c = add_log(proj, "we decided tags must be signed")
    _nj, r = distil(proj, lambda nj: [item([c], "Sign release tags", "Create a signed git tag per release.",
                                           updates=iid)])
    assert r.json()["results"][0] == {"n": 0, "result": "updated", "id": iid}
    m = _meta(proj, iid)
    assert m["title"] == "Sign release tags" and json.loads(m["sources_json"]) == [a, b, c]
    assert m["evidence_count"] == 3 and _text(proj, iid) == "Create a signed git tag per release."
    # contradicts: new contested row dkey_2, target contested too
    d = add_log(proj, "we decided tags are not needed")
    _nj, r = distil(proj, lambda nj: [item([d], "Tags are optional", "Releases do not need git tags at all.",
                                           contradicts=iid)])
    res = r.json()["results"][0]
    assert res["result"] == "contested" and res["with"] == [iid] and res["id"] == f"{iid}_2"
    assert _meta(proj, iid)["status"] == "contested" and _meta(proj, res["id"])["status"] == "contested"
    assert _meta(proj, iid)["knowledge_type"] == "quick"
    lines = client.get("/distill/pending", params={"project": proj}).json()["lines"]
    assert any("[contested]" in ln for ln in lines)
    # approving one contested row supersedes the other under the same dkey
    ap = client.post("/distill/approve", json={"project": proj, "ids": [res["id"]]}).json()
    assert ap["results"] == [{"id": res["id"], "result": "active"}]
    old = _meta(proj, iid)
    assert old["status"] == "superseded" and old["superseded_by"] == res["id"] and old["knowledge_type"] == "superseded"
    # updates/contradicts of a non-live target is rejected
    e = add_log(proj, "we decided once more")
    _nj, r = distil(proj, lambda nj: [item([e], "x title", "x body", updates="dk_doesnotexist")])
    assert r.json()["results"][0]["result"] == "rejected"


def test_dry_run_writes_nothing_and_releases(client, proj, add_log, distil):
    a = add_log(proj, "we decided dry runs are useful")
    nj, r = distil(proj, lambda nj: [item([a], "Dry title", "Dry body text.")], dry_run=True,
                   contexts=[{"tag": "dry-ctx"}])
    s = r.json()
    assert s["dry_run"] is True and s["results"][0]["result"] == "accepted" and s["batch_open"] is False
    assert _meta(proj, s["results"][0]["id"]) is None if vs.get_existing(
        vs.part_collection_name("knowledge", proj)) else True
    assert client.get("/distill/cursor", params={"project": proj}).json()["contexts"] == []
    assert distill._batch(nj["batch_id"]) is None
    again = client.get("/distill/next", params={"project": proj}).json()
    assert [x["id"] for x in again["logs"]] == [a]


def test_relation_needs_live_item_and_merge_threshold(proj):
    # unit: the leak gate and ids
    assert distill.leak("contact me at a@b.io", proj) == "a@b.io"
    assert distill.leak("ghp_ABCDEFGH1234 token", proj) == "ghp_ABCDEFGH1234"
    assert distill.leak("plain words only", proj) is None


def test_own_project_lenient_gate(proj, monkeypatch):
    assert distill.leak("db at 10.0.0.7", proj) == "10.0.0.7"
    monkeypatch.setenv("KH_DISTILL_OWN_PROJECTS", proj)
    assert distill.leak("db at 10.0.0.7", proj) is None
    assert distill.leak("db at 10.0.0.7", proj, share=True) == "10.0.0.7"
    assert distill.leak("mail x@y.com", proj) == "x@y.com"


def test_person_names_refused(client, proj, uid):
    from app import entities

    name = f"Zelda{uid}"
    entities.upsert(proj, name, "person", "Dev", "", [], [], [], None)
    assert distill.leak(f"ask {name} about it", proj) == name.lower()
    cards = client.get("/distill/next", params={"project": proj}).json()["people"]
    assert any(c["name"] == name for c in cards)
    assert set(cards[0]) == {"id", "name", "aliases", "role"}


# ---------------------------------------------------------------------------
# Step C: review
# ---------------------------------------------------------------------------


def test_reject_reopen(client, proj, add_log, distil):
    a = add_log(proj, "we decided something to reject")
    _nj, r = distil(proj, lambda nj: [item([a], "Reject me", "This will be rejected.")])
    iid = r.json()["results"][0]["id"]
    rj = client.post("/distill/reject", json={"project": proj, "ids": [iid, "dk_none"], "reason": "r" * 700}).json()
    assert rj["results"] == [{"id": iid, "result": "withdrawn"}, {"id": "dk_none", "result": "not found"}]
    m = _meta(proj, iid)
    assert m["status"] == "withdrawn" and m["knowledge_type"] == "withdrawn" and len(m["reject_reason"]) == 500
    assert client.get("/distill/pending", params={"project": proj}).json()["count"] == 0
    ro = client.post("/distill/reopen", json={"project": proj, "ids": [iid]}).json()
    assert ro["results"] == [{"id": iid, "result": "proposed"}]
    m = _meta(proj, iid)
    assert m["status"] == "proposed" and "superseded_by" not in m
    # approve -> active, quick; second approve is refused (not reviewable)
    ap = client.post("/distill/approve", json={"project": proj, "ids": [iid]}).json()
    assert ap["results"][0]["result"] == "active"
    assert _meta(proj, iid)["knowledge_type"] == "quick"
    ap2 = client.post("/distill/approve", json={"project": proj, "ids": [iid]}).json()
    assert ap2["results"][0]["result"].startswith("not reviewable")


def test_approve_share_to_lesson_learned(fresh_store, client, proj, add_log, distil):
    from app import projects

    projects.get_or_create_project(proj)
    a = add_log(proj, "we decided to restart the worker after a config change")
    b = add_log(proj, f"we decided {proj} needs its own runbook")
    _nj, r = distil(proj, lambda nj: [
        item([a], "Restart after config change", "Restart the worker after changing its config.",
             context="ent__someone"),
        item([b], "Own runbook", f"The {proj} service needs a runbook.")])
    ids = [x["id"] for x in r.json()["results"]]
    ap = client.post("/distill/approve", json={"project": proj, "ids": ids, "share": ids}).json()
    shared = {s["id"]: s for s in ap["shared"]}
    assert shared[ids[0]]["result"] == "shared"
    assert shared[ids[1]]["result"] == f"refused: not general ('{proj.replace('-', ' ')}')"  # folded name
    sid = shared[ids[0]]["shared_id"]
    m = _meta("lesson-learned", sid)
    assert m["status"] == "active" and m["origin_project"] == proj and m["context"] == "-"
    assert m["entities_json"] == "[]" and m["project"] == "lesson-learned"
    assert sid == distill.dkey("lesson-learned", "lesson", ACTION, "-", "-")
    # sharing the same text again merges into the existing shared row
    c = add_log(proj, "we decided to restart the worker again")
    _nj, r = distil(proj, lambda nj: [item([c], "Restart after config change",
                                           "Restart the worker after changing its config.", updates=ids[0])])
    client.post("/distill/reopen", json={"project": proj, "ids": [ids[0]]})
    ap = client.post("/distill/approve", json={"project": proj, "ids": [ids[0]], "share": [ids[0]]}).json()
    assert ap["shared"][0]["result"] == "merged" and ap["shared"][0]["shared_id"] == sid


def test_approve_policy_proposal_project_tier(client, proj, add_log, distil):
    from app.policy_actions import store

    a = add_log(proj, "we decided to always check the error log first")
    _nj, r = distil(proj, lambda nj: [item([a], "Check logs first", "Read the error log before anything else.",
                                           kind="policy_proposal", target_action=ACTION,
                                           target_sub_action="first-look")])
    iid = r.json()["results"][0]["id"]
    ap = client.post("/distill/approve", json={"project": proj, "ids": [iid]}).json()
    assert ap["results"][0]["result"] == "applied"
    m = _meta(proj, iid)
    assert m["status"] == "superseded" and m["applied_to"] == f"{proj}:{ACTION}/first-look"
    rules = {x["sub_action"]: x for x in store.plan(ACTION, proj)}
    assert rules["first-look"]["content"] == "Read the error log before anything else."


# ---------------------------------------------------------------------------
# Rekey, patterns, serving
# ---------------------------------------------------------------------------


def test_rekey(client, proj, add_know):
    k1 = add_know(proj, "Old lesson about rotating certificates.", category="security")
    k2 = add_know(proj, "Old note that should be skipped.")
    g = client.get("/distill/rekey", params={"project": proj}).json()
    ids = {r["id"] for r in g["rows"]}
    assert {k1, k2} <= ids and g["catalog"]["kinds"][0] == "lesson"
    r = client.post("/distill/rekey", json={
        "project": proj, "contexts": [{"tag": "Certificates"}],
        "keys": [{"id": k1, "action": ACTION, "context": "certificates", "entities": ["ent__x", "nope"]},
                 {"id": k2, "skip": True}, {"id": "vec_missing", "skip": True},
                 ]}).json()
    res = {x["id"]: x["result"] for x in r["results"]}
    assert res == {k1: "keyed", k2: "skipped", "vec_missing": "not found"}
    assert r["contexts"][0]["tag"] == "certificates"
    m1, m2 = _meta(proj, k1), _meta(proj, k2)
    assert m1["action"] == ACTION and m1["context"] == "certificates" and m1["status"] == "active"
    assert m1["entities_json"] == '["ent__x"]' and m1["rekeyed_at"] and m1["rekeyed_by"] == "local"
    assert _text(proj, k1) == "Old lesson about rotating certificates."
    assert m2["action"] == "-" and m2["context"] == "-" and "status" not in m2
    left = {r["id"] for r in client.get("/distill/rekey", params={"project": proj}).json()["rows"]}
    assert k1 not in left and k2 not in left
    # rekeyed rows are served by key
    items, key = distill.served(proj, ACTION, "certificates")
    assert key == "action+context" and [i["id"] for i in items] == [k1]
    items, key = distill.served(proj, ACTION, "other-ctx")
    assert key == "action" and [i["id"] for i in items] == [k1]
    items, key = distill.served(proj, None, "certificates")
    assert key == "context"
    # bad action is rejected, no consent needed
    r = client.post("/distill/rekey", json={"project": proj, "keys": [{"id": k2, "action": "nope"}]}).json()
    assert r["results"][0]["result"] == "rejected"


def test_patterns(client, proj, add_log):
    add_log(proj, "the build failed again", topic="build")
    add_log(proj, "the build failed twice", topic="build")
    add_log(proj, "deploy failed", topic="deploy")
    add_log(proj, "all good today", topic="misc")
    j = client.get("/distill/patterns", params={"project": proj, "days": 30}).json()
    assert j["days"] == 30
    p = j["projects"][0]
    assert p["project"] == proj and p["logs"] == 4 and p["failure_logs"] == 3 and p["failure_rate"] == 0.75
    assert p["top_failing_topics"][0] == {"topic": "build", "count": 2}
    assert client.get("/distill/patterns", params={"project": proj, "days": 731}).status_code == 422
    # no project: every consented project
    j = client.get("/distill/patterns").json()
    assert [x["project"] for x in j["projects"]] == [proj]


def test_served_exact_first_and_cut():
    a = ([{"id": "1"}], "action+context")
    b = ([{"id": "2"}], "action")
    c = ([{"id": "3"}, {"id": "1"}], "context")
    assert [i["id"] for i in distill.exact_first([a, b, c])] == ["1", "3"]
    assert [i["id"] for i in distill.exact_first([b, c])] == ["2", "3", "1"]
    long = "x" * 700
    assert distill._cut(long) == "x" * 600 + " ...(cut; read the row by id)"
    it = {"id": "dk_1", "kind": "fact", "status": "contested", "action": "a", "context": "c", "title": "T",
          "text": "body", "sources": ["s1"]}
    assert distill.line_of(it) == "dk_1 fact [contested] [a / c] T: body"
    assert distill.line_of(it, True) == "dk_1 fact [contested] [a / c] T: body (sources: s1)"


# ---------------------------------------------------------------------------
# Worker callers
# ---------------------------------------------------------------------------


class _Worker:
    ns = "kh_people"
    svc_role = "distiller"
    principal_id = "worker"
    email = None
    is_maintainer = False


def test_worker_budget_and_distilled_by(client, proj, add_log, monkeypatch):
    with pytest.raises(distill.BudgetError) as e:
        distill.serve_next(proj, 5, caller=_Worker())
    assert e.value.body["error"] == "budget" and e.value.body["cap_usd"] == 60
    monkeypatch.setattr(distill, "WORKER_SPEND_FN", lambda: 61.0)
    with pytest.raises(distill.BudgetError) as e:
        distill.serve_next(proj, 5, caller=_Worker())
    assert e.value.body["month_usd"] == 61.0
    monkeypatch.setattr(distill, "WORKER_SPEND_FN", lambda: 1.0)
    a = add_log(proj, "we decided worker things")
    nj = distill.serve_next(proj, 5, caller=_Worker())
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as he:
        distill.submit({"project": proj, "batch_id": nj["batch_id"], "items": [], "distilled_by": "me"},
                       caller=_Worker())
    assert he.value.status_code == 400
    monkeypatch.setenv("KH_DISTILL_WORKER", "dry")
    s = distill.submit({"project": proj, "batch_id": nj["batch_id"], "distilled_by": "worker:m",
                        "items": [item([a], "Worker item", "Worker body.")]}, caller=_Worker())
    assert s["dry_run"] is True and s["forced_dry"] is True and s["results"][0]["result"] == "accepted"


def test_budget_429_route(client, proj, monkeypatch):
    monkeypatch.setattr(distill, "_principal", lambda: _Worker())
    r = client.get("/distill/next", params={"project": proj})
    assert r.status_code == 429 and r.json()["error"] == "budget" and set(r.json()) == {
        "error", "detail", "month_usd", "cap_usd"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_member_refused_maintainer_allowed(client, proj, monkeypatch):
    from tests.auth_helpers import bearer, enable_signin, make_token

    enable_signin(monkeypatch)
    m = bearer(make_token("mem@x.com", projects={proj: "owner"}))
    for method, path, kw in [("get", "/distill/next", {"params": {"project": proj}}),
                             ("get", "/distill/pending", {"params": {"project": proj}}),
                             ("post", "/distill/approve", {"json": {"project": proj, "ids": []}}),
                             ("get", "/distill/signals", {})]:
        assert getattr(client, method)(path, headers=m, **kw).status_code == 403, path
    mt = bearer(make_token("boss@x.com", role="maintainer"))
    assert client.get("/distill/next", params={"project": proj}, headers=mt).status_code == 200
