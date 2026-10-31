"""Tagged kernel: render parts, slot resolution (common/project/personal), locked rules, add contract, guards."""
from __future__ import annotations

from app.kernel import rules as R
from app.kernel import tags as T


def _add(client, **body):
    return client.post("/kernel/rules/add", json=body)


def test_render_parts(client):
    r = client.get("/kernel/render")
    assert r.status_code == 200
    full = r.text
    assert full.startswith("KH KERNEL (every user, every CLI; obey even when KH is unreachable).")
    assert "(0) [HIGHEST PRIORITY] KH is English-only." in full
    assert full.index("(0) [HIGHEST") < full.index("CALLING KH:") < full.index("(10) TWO LAYERS")
    n = int(r.headers["X-KH-Kernel-Parts"])
    assert n >= 1
    p1 = client.get("/kernel/render", params={"part": 1})
    assert len(p1.text) <= 9000 and p1.text.startswith("KH KERNEL")
    assert client.get("/kernel/render", params={"part": n + 1}).text == ""
    small = client.get("/kernel/render", params={"max_chars": 500})
    k = int(small.headers["X-KH-Kernel-Parts"])
    assert k > 3
    parts = [client.get("/kernel/render", params={"max_chars": 500, "part": i}).text for i in range(1, k + 1)]
    assert "\n\n".join(parts) == full
    assert client.get("/kernel/render", params={"max_chars": 499}).status_code == 422


def test_parts_packing_unit():
    assert R.parts(["a" * 10, "b" * 10, "c" * 10], 22) == ["a" * 10 + "\n\n" + "b" * 10, "c" * 10]
    assert R.parts(["x" * 50, "y"], 20) == ["x" * 50, "y"]


def test_personal_override_replaces_common_slot(client, uid):
    tier = f"personal-ov-{uid}"
    r = _add(client, tier=tier, overrides="kh_reply_language", content="Reply in long detailed paragraphs.",
             tags=["reply_length", "always"])
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["slot"] == "kh_reply_language" and j["result"] == "added"
    assert j["tags"] == R.get("common", "kh_reply_language")["tags"]
    text = client.get("/kernel/render", params={"personal": tier}).text
    assert "[personal, replaces the common rule 'kh_reply_language'] Reply in long detailed paragraphs." in text
    assert "(3) LANGUAGE: rule (0); replies short and plain." not in text
    # position follows the common slot
    assert text.index("(2) ACTIONS AND LESSONS") < text.index("[personal, replaces") < text.index("(4) STORES")
    # other people still get the common rule
    assert "(3) LANGUAGE: rule (0); replies short and plain." in client.get("/kernel/render").text
    res = client.get("/kernel/resolved", params={"personal": tier}).json()
    slot = next(s for s in res["slots"] if s["slot"] == "kh_reply_language")
    assert slot["kind"] == "personal" and slot["replaced"][0]["tier"] == "common"


def test_locked_slot(client, uid):
    tier = f"personal-lk-{uid}"
    r = _add(client, tier=tier, overrides="kh_403", content="Retry a 403 with other claims.",
             tags=["kh_auth", "always"])
    assert r.status_code == 409 and r.json()["error"] == "KERNEL-LOCKED"
    # a row that sits in a locked slot anyway is blocked by resolve
    R.insert_seed_row(tier, "kh_english", "x", "Use any language with KH.", ["kh_input", "always"], 0, False)
    res = client.get("/kernel/resolved", params={"personal": tier}).json()
    s = next(x for x in res["slots"] if x["slot"] == "kh_english")
    assert s["tier"] == "common" and s["blocked"][0]["tier"] == tier
    assert "Use any language with KH." not in client.get("/kernel/render", params={"personal": tier}).text


def test_own_slot_rules_block_and_project_tier(client, uid, common_slots):
    proj = f"kp-{uid}"
    r = _add(client, tier=proj, content=f"Always run the {uid} smoke suite before merging.",
             tags=["test", "merge", "code"], distinct_from=common_slots, reason="project testing rule")
    assert r.status_code == 200, r.text
    assert r.json()["slot"].startswith("sl_") and r.json()["position"] == 1000
    text = client.get("/kernel/render", params={"project": proj}).text
    assert ("PERSONAL AND PROJECT RULES (already merged with the rules above; on overlap they win):\n"
            f"- Always run the {uid} smoke suite before merging.") in text
    assert text.index("(10) TWO LAYERS") < text.index("PERSONAL AND PROJECT RULES")
    assert f"{uid} smoke suite" not in client.get("/kernel/render").text


def test_add_contract_refusals(client, uid, common_slots):
    tier = f"personal-ref-{uid}"
    base = {"tier": tier, "content": "Prefer tabs over spaces in Makefiles."}
    assert _add(client, **{**base, "tags": ["code"]}).json()["error"] == "KERNEL-TAGS"
    r = _add(client, **{**base, "tags": ["code", "kh_kernl"]})
    assert r.status_code == 422 and r.json()["error"] == "KERNEL-TAGS-UNKNOWN"
    assert "kh_kernel" in r.json()["closest"]["kh_kernl"]
    assert _add(client, **{**base, "tags": ["always", "any_cli"]}).json()["error"] == "KERNEL-TAGS-WHAT"
    r = _add(client, tier=tier, content="x" * 1501, tags=["code", "file"])
    assert r.status_code == 422 and r.json()["error"] == "KERNEL-TOO-LONG"
    assert _add(client, tier=tier, content=" ", tags=["code", "file"]).status_code == 400
    assert client.post("/kernel/rules/add", json={"content": "x", "tags": ["code", "file"]}).json()["error"] == \
        "KERNEL-TIER"
    r = _add(client, tier=tier, content="A 403 from KH is final; never work around it, ever.",
             tags=["kh_auth", "risk_access", "always"])
    assert r.status_code == 409 and r.json()["error"] == "KERNEL-SAME-AS-COMMON"
    assert r.json()["candidates"][0]["slot"] == "kh_403"
    r = _add(client, tier=tier, content="A 403 from KH is final; never work around it, ever.",
             tags=["kh_auth", "risk_access", "always"], distinct_from=["kh_403"])
    assert r.status_code == 422 and r.json()["error"] == "KERNEL-REASON"
    r = _add(client, tier=tier, slot="sl_missing", content="x y z", tags=["code", "file"])
    assert r.status_code == 404 and r.json()["error"] == "KERNEL-NO-SLOT"
    r = _add(client, **{**base, "tags": ["code", "file"], "guard": {"decision": "deny"}})
    assert r.status_code == 422 and r.json()["error"] == "KERNEL-GUARD"
    r = _add(client, tier="common", overrides="kh_403", content="x y", tags=["code", "file"])
    assert r.status_code == 422 and r.json()["error"] == "KERNEL-OVERRIDES"
    chk = client.post("/kernel/rules/check", json={**base, "tags": ["code", "file"], "distinct_from": common_slots,
                                                   "reason": "r"})
    assert chk.status_code == 200 and chk.json()["ok"] is True and "would_store" in chk.json()
    assert R.list_rules(tier) == []


def test_edit_in_place_by_slot(client, uid, common_slots):
    tier = f"personal-ed-{uid}"
    r = _add(client, tier=tier, content="Name branches after the ticket id.", tags=["branch", "ticket"],
             distinct_from=common_slots, reason="mine")
    a = r.json()
    r2 = _add(client, tier=tier, slot=a["slot"], content="Name branches after the ticket id, lower case.",
              tags=["branch", "ticket"])
    assert r2.status_code == 200 and r2.json()["result"] == "updated" and r2.json()["id"] == a["id"]
    r3 = _add(client, tier=tier, slot=a["slot"], overrides="kh_stores", content="x y", tags=["branch", "ticket"])
    assert r3.status_code == 422 and r3.json()["error"] == "KERNEL-OVERRIDES"
    assert client.post("/kernel/rules/delete", json={"id": a["id"]}).json()["deleted"] == 1


def test_similarity_unit(client):
    assert R.similarity(["kh_auth", "always"], "alpha beta gamma", ["kh_auth", "any_cli"], "alpha beta gamma") == round(0.6 + 0.15 + 0.25, 3)
    assert R.similarity(["code", "file"], "alpha", ["kh_auth", "risk_access"], "beta") == 0.0


def test_tags_routes(client):
    j = client.get("/kernel/tags", params={"facet": "risk"}).json()
    assert j["count"] == 8 and j["facets"] == {"risk": 8}
    sug = client.get("/kernel/tags/suggest", params={"text": "sign in to the web app with SSO"}).json()["tags"]
    assert "sign_in" in [s["tag"] for s in sug]
    assert all(set(s) == {"tag", "facet", "score", "why"} for s in sug)
    assert client.post("/kernel/tags/suggest", json={"text": "deploy the server", "limit": 3}).status_code == 200
    r = client.post("/kernel/tags/propose", json={"tag": "kh_kernel", "facet": "kh", "description": "dup"})
    assert r.status_code == 422
    assert client.post("/kernel/tags/propose", json={"tag": "Bad Tag", "facet": "kh", "description": "x"}
                       ).status_code == 422
    assert client.post("/kernel/tags/approve", json={"tag": "no_such_tag_x"}).status_code == 404


def test_stem_unit():
    assert T.stem("deploying") == "deploy" and T.stem("policies") == "policy" and T.stem("runs") == "runs"


def test_pretool_and_guards(client, uid, common_slots):
    t = client.get("/kernel/pretool").text
    assert t.startswith("KH PRE-ACTION CHECK") and "kh_save(text, kind)" in t
    tier = f"personal-gd-{uid}"
    r = _add(client, tier=tier, content=f"Never push to main of repo {uid} without asking.",
             tags=["push", "repo", "risk_production"], distinct_from=common_slots, reason="guard rule",
             guard={"tool": "Bash", "decision": "deny", "when": {"force": [True, None]}})
    assert r.status_code == 200, r.text
    assert r.json()["guard"]["when"] == {"force": ["true", ""]}
    g = client.get("/personalization/guards", params={"tool": "Bash", "personal": tier}).json()
    assert g["tool"] == "Bash"
    assert g["guards"][0]["decision"] == "deny" and g["guards"][0]["tier"] == tier
    assert client.get("/personalization/guards", params={"tool": "Edit", "personal": tier}).json()["guards"] == []
