"""Policy action table: menu, plan with tier overrides, errors, lint, bulk (§3.7.2)."""
from __future__ import annotations

from app import sqldb
from app.policy_actions import store


def test_actions_shape_and_order(client):
    r = client.get("/policy/actions")
    assert r.status_code == 200
    j = r.json()
    assert j["project"] == "every project" and j["requested_project"] is None
    assert j["count"] == len(j["actions"]) >= 14
    a = j["actions"][0]
    assert set(a) >= {"project", "action", "description", "source_policy", "priority", "updated_at", "sub_actions"}
    keys = [(-x["priority"], x["action"]) for x in j["actions"]]
    assert keys == sorted(keys)
    sm = next(x for x in j["actions"] if x["action"] == "save knowledge")
    assert sm["sub_actions"] == ["where", "store", "shared", "confirm"]
    assert j["table"]["db"] == "sqlite"


def test_actions_raw_menu_text(client):
    t = client.get("/policy/actions/raw").text
    assert "  - ACTION 'save knowledge' [project: every project] - when: The person asks to save" in t
    assert "    sub-actions in order: where -> store -> shared -> confirm" in t
    assert t.endswith("  - If no action fits, answer none and proceed. Never invent a label.")


def test_plan_project_override_and_fetch_count(client, uid):
    p = f"pa-{uid}"
    r = client.post("/policy/rule/set", json={"project": p, "action": "save knowledge", "sub_action": "store",
                                              "content": "Project-specific store step.", "seq": 2})
    assert r.status_code == 200, r.text
    r = client.post("/policy/rule/set", json={"project": p, "action": "save knowledge", "sub_action": "extra",
                                              "content": "One more step.", "seq": 9})
    plan = client.get("/policy/plan", params={"action": "save knowledge", "project": p}).json()
    assert plan["project"] == p and plan["count"] == 5
    by = {x["sub_action"]: x for x in plan["rules"]}
    assert by["store"]["content"] == "Project-specific store step." and by["store"]["project"] == p
    assert by["where"]["project"] == "every project"
    assert [x["sub_action"] for x in plan["rules"]][-1] == "extra"
    # every-project plan unchanged
    base = client.get("/policy/plan", params={"action": "save knowledge"}).json()
    assert {x["sub_action"]: x for x in base["rules"]}["store"]["project"] == "every project"
    with sqldb.connect() as c:
        n = c.execute("SELECT fetches FROM policy_usage WHERE project=? AND action=?",
                      (p, "save knowledge")).fetchone()[0]
    assert n == 1


def test_unknown_action_and_rule(client):
    r = client.get("/policy/plan", params={"action": "fly to the moon"})
    assert r.status_code == 404
    j = r.json()
    assert j["error"] == "UNKNOWN-ACTION" and "talk with user" in j["known_actions"]
    assert j["hint"] == "pick a label from GET /policy/actions, or answer none - never guess"
    r = client.get("/policy/rule", params={"action": "talk with user", "sub_action": "nope"})
    assert r.status_code == 404 and r.json()["error"] == "NO-RULE"
    assert r.json()["sub_actions_of_action"] == ["reply", "no side effects", "unclear task"]
    r = client.post("/policy/rule/set", json={"action": "no such action", "sub_action": "x", "content": "y"})
    assert r.status_code == 404


def test_lint_gate_and_delete(client, uid):
    p = f"lint-{uid}"
    r = client.post("/policy/action/set", json={"project": p, "action": "do thing", "description": "x" * 401})
    assert r.status_code == 422 and r.json()["error"] == "POLICY-LINT"
    r = client.post("/policy/action/set", json={"project": p, "action": "do thing", "description": "x" * 401,
                                                "allow_oversize": True, "reason": "legacy row"})
    assert r.status_code == 200 and r.json()["warnings"]
    r = client.post("/policy/action/set", json={"project": p, "action": "do thing",
                                                "description": "Do the thing for the project."})
    assert r.status_code == 200
    r = client.post("/policy/rule/set", json={"project": p, "action": "do thing", "sub_action": "a",
                                              "content": "y" * 1201})
    assert r.status_code == 422
    client.post("/policy/rule/set", json={"project": p, "action": "do thing", "sub_action": "a", "content": "Go."})
    r = client.post("/policy/action/delete", json={"project": p, "action": "do thing"})
    assert r.status_code == 409
    r = client.post("/policy/action/delete", json={"project": p, "action": "do thing", "cascade": True})
    assert r.status_code == 200 and r.json()["deleted"] == 1
    rep = client.get("/policy/lint").json()
    assert set(rep) >= {"limits", "oversize_descriptions", "oversize_rules", "oversize_plans", "hooks", "overlaps",
                        "unused", "ok"}


def test_overlap_warning(client, uid):
    p = f"ov-{uid}"
    client.post("/policy/action/set", json={"project": p, "action": "alpha", "description":
                                            "Deploy the billing service to staging servers"})
    r = client.post("/policy/action/set", json={"project": p, "action": "beta", "description":
                                                "Deploy the billing service to staging servers now"})
    assert any("overlaps 'alpha'" in w for w in r.json()["warnings"])


def test_bulk_undeclared(client, uid):
    p = f"bulk-{uid}"
    r = client.post("/policy/actions/bulk", json={"actions": [], "rules": [
        {"project": p, "action": "ghost", "sub_action": "s", "content": "c"}]})
    assert r.status_code == 400 and r.json()["error"] == "UNDECLARED-ACTION"
    r = client.post("/policy/actions/bulk", json={
        "actions": [{"project": p, "action": "ghost", "description": "A ghost action."}],
        "rules": [{"project": p, "action": "ghost", "sub_action": "s", "content": "c"}]})
    assert r.status_code == 200
    assert store.plan("ghost", p)[0]["content"] == "c"


def test_canonical_alias(client, uid):
    from app import projects

    pid = f"canon-{uid}"
    projects.get_or_create_project(pid, aliases=[f"Canon Alias {uid}"])
    projects.sync_orchestrator_registry()
    r = client.get("/policy/actions", params={"project": f"Canon Alias {uid}"})
    assert r.json()["project"] == pid


def test_projects_listing(client):
    j = client.get("/policy/actions/projects").json()
    assert "every project" in j["projects"]
