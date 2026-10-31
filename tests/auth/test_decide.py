"""§2.4-2.6: rule matching, decide() for every kind, Principal predicates, levels, visibility helpers."""
from __future__ import annotations

import pytest

from app import auth
from tests.auth_helpers import LEVELS

P = auth.Principal


def member(email="m@x.com", **projects):
    return P(email=email, role="member", projects={k: set(LEVELS[v]) for k, v in projects.items()})


def maint(email="boss@x.com"):
    return P(email=email, role="maintainer", kh_permissions={"project.create"},
             every_project=set(LEVELS["owner"]))


def rule(method, path):
    r, params = auth.match_rule(method, path)
    assert r is not None, f"no rule for {method} {path}"
    return r, params


def test_rules_file_loads_and_templates_match():
    r, params = rule("GET", "/entities/alpha/billing")
    assert r["template"] == "/entities/{project}/{name}" and params == {"project": "alpha", "name": "billing"}
    r, params = rule("POST", "/v1/billing/items/abc:support")
    assert r["kind"] == "ns" and params["ns"] == "billing" and params["rest"] == "items/abc:support"
    assert rule("GET", "/projects/registry")[0]["kind"] == "authenticated"
    assert rule("GET", "/projects/alpha")[0]["kind"] == "project"
    assert rule("DELETE", "/vector/delete")[0]["permission"] == "project.write"
    assert rule("POST", "/vector/delete")[0]["permission"] == "project.write"
    assert rule("GET", "/events:stats")[0]["kind"] == "maintainer"
    assert rule("GET", "/distill/next")[0].get("worker") is True
    assert rule("POST", "/improve/propose")[0].get("agents") is True
    assert auth.match_rule("GET", "/no/such/route") == (None, {})
    assert auth.match_rule("PATCH", "/projects") == (None, {})


@pytest.mark.parametrize("method,path,kind", [
    ("GET", "/health", "public"), ("GET", "/auth/download/kh-helper", "public"), ("POST", "/v1/token", "public"),
    ("GET", "/gallery/designs/3/thumb", "public"), ("POST", "/home/login/poll", "public"),
    ("GET", "/turn/context", "authenticated"), ("POST", "/context/step", "authenticated"),
    ("GET", "/kernel/render", "authenticated"), ("GET", "/session/project", "authenticated"),
    ("DELETE", "/messages/msg_1", "authenticated"), ("PUT", "/projects/x/visibility", "authenticated"),
    ("GET", "/v1/admin/ns/x", "authenticated"), ("POST", "/projects", "kh"),
    ("GET", "/policy/plan", "project"), ("GET", "/kernel/pretool", "project"), ("POST", "/feedback/tick", "project"),
    ("PUT", "/auth/help", "maintainer"), ("POST", "/messages/announce", "maintainer"),
    ("POST", "/session/sweep", "maintainer"), ("GET", "/home/members", "maintainer"),
    ("POST", "/orchestrator/fact", "maintainer"), ("GET", "/sync/grants", "maintainer"),
    ("POST", "/distill/approve", "maintainer"), ("GET", "/downloads", "maintainer"),
])
def test_route_kinds(method, path, kind):
    assert rule(method, path)[0]["kind"] == kind


def test_decide_open_mode_allows_everything():
    assert auth.decide(None, None) is None
    assert auth.decide(rule("POST", "/messages/announce")[0], None) is None


def test_decide_public_authenticated_unknown():
    m = member()
    assert auth.decide(rule("GET", "/health")[0], m) is None
    assert auth.decide(rule("GET", "/turn/context")[0], m) is None
    assert auth.decide(None, m) == "route not open to members"
    assert auth.decide(None, maint()) is None


def test_decide_maintainer_and_kh():
    r = rule("POST", "/messages/sweep")[0]
    assert auth.decide(r, member()) == "maintainers only"
    assert auth.decide(r, maint()) is None
    r = rule("POST", "/projects")[0]
    assert auth.decide(r, member()) == "missing permission project.create"
    assert auth.decide(r, P(email="c@x.com", kh_permissions={"project.create"})) is None


def test_decide_project_kind_levels():
    r, params = rule("GET", "/projects/alpha")
    assert auth.decide(r, member(alpha="readonly"), params) is None
    assert auth.decide(r, member(beta="owner"), params) == "missing permission project.read on this project"
    r, params = rule("DELETE", "/projects/alpha")
    assert auth.decide(r, member(alpha="editable"), params) == "missing permission project.delete on this project"
    assert auth.decide(r, member(alpha="owner"), params) is None
    assert auth.decide(r, maint(), params) is None
    w = rule("POST", "/keys/add")[0]
    assert auth.decide(w, member(alpha="readonly"), {}, {}, {"project": "alpha"}) == \
        "missing permission project.write on this project"
    assert auth.decide(w, member(alpha="editable"), {}, {}, {"project": "alpha"}) is None


def test_decide_default_project_hint():
    r = rule("POST", "/vector/add")[0]
    reason = auth.decide(r, member(alpha="owner"), {}, {}, {"text": "x", "scope": "knowledge"})
    assert reason == ("missing permission project.write on this project (the request names no project, so it "
                      "touched 'orchestrator'; name it in body.metadata.project or body.metadata.source_id or "
                      "body.project)")
    # orchestrator is readable by every member (system:read) but not writable
    q = rule("POST", "/vector/query")[0]
    m = P(email="m@x.com", projects={"orchestrator": {"project.read"}})
    assert auth.decide(q, m, {}, {}, {"text": "x"}) is None


def test_decide_project_from_order_and_log_permission():
    r = rule("POST", "/vector/add")[0]
    m = member(alpha="readonly", beta="editable")
    # metadata.project wins over body.project
    assert auth.decide(r, m, {}, {}, {"metadata": {"project": "beta"}, "project": "alpha"}) is None
    # readonly cannot log; editable can
    assert auth.decide(r, m, {}, {}, {"scope": "log", "metadata": {"project": "alpha"}}) == \
        "missing permission project.log on this project"
    assert auth.decide(r, m, {}, {}, {"scope": "log", "metadata": {"project": "beta"}}) is None
    # source_id is a project source too
    assert auth.decide(r, m, {}, {}, {"metadata": {"source_id": "beta"}}) is None


def test_decide_personal_redirection():
    r = rule("POST", "/vector/add")[0]
    m = member()
    for scope in ("log", "findings", "lesson", "personalization"):
        assert auth.decide(r, m, {}, {}, {"scope": scope, "text": "x"}) is None, scope
    q = rule("POST", "/vector/query")[0]
    assert auth.decide(q, m, {}, {}, {"scope": "personalization", "text": "x"}) is None
    # project kind with neither source nor default -> a project must be named
    c = rule("GET", "/findings/list")[0]
    assert auth.decide(c, m, {}, {}) == "a project must be named"
    assert auth.decide(c, maint(), {}, {}) is None
    assert auth.decide(c, m, {}, {"project": m.personal_project}) is None


def test_decide_service_tokens():
    ns = P(email="client:alpha", ns="alpha", svc_role="admin", principal_id="client")
    r, params = rule("GET", "/v1/alpha/items")
    assert auth.decide(r, ns, params) is None
    r, params = rule("GET", "/v1/beta/items")
    assert auth.decide(r, ns, params) == "this token belongs to another namespace"
    assert auth.decide(rule("GET", "/v1/capabilities")[0], ns, {}) is None
    assert auth.decide(rule("GET", "/projects")[0], ns, {}) == "service tokens may call /v1 routes only"
    assert auth.decide(None, ns, {}) == "service tokens may call /v1 routes only"
    # a person may call namespace routes (the route checks owner/maintainer itself)
    assert auth.decide(r, member(), params) is None


def test_principal_predicates_and_personal_access():
    m = member("Jane@X.com".lower(), alpha="owner", beta="readonly")
    assert m.personal_project == "personal-jane-x-com"
    assert m.can("personal-jane-x-com", "project.delete")
    assert not m.can("personal-other-x-com", "project.read")
    assert m.is_owner("alpha") and not m.is_owner("beta")
    assert m.can("beta", "project.read") and not m.can("beta", "project.log")
    assert m.visible_projects() == {"alpha", "beta", "personal-jane-x-com"}
    assert m.visible_projects("project.write") == {"alpha", "personal-jane-x-com"}
    boss = maint()
    assert boss.visible_projects() is None and boss.can("personal-other-x-com", "project.read")
    assert not m.is_technical and P(email="t@x.com", kh_permissions={"kh.act_for_user"}).is_technical


def _with(principal):
    return auth._current.set(principal)


def test_module_helpers_visible_and_row_visible():
    assert auth.may("anything", "project.write") and auth.visible_projects() is None
    m = member("m@x.com", alpha="readonly")
    tok = _with(m)
    try:
        assert auth.may("alpha", "project.read") and not auth.may("alpha", "project.write")
        assert not auth.may(None, "project.read")
        assert auth.filter_projects(["alpha", "beta", "personal-m-x-com"]) == ["alpha", "personal-m-x-com"]
        assert auth.row_visible({"kind": "project-registry", "project_id": "alpha"}, "project_registry__alpha")
        assert not auth.row_visible({"kind": "project-registry", "project_id": "beta"}, "project_registry__beta")
        assert not auth.row_visible({}, "project_registry__beta")
        assert auth.row_visible({"scope": "log", "acting_user": "m@x.com"})
        assert not auth.row_visible({"scope": "log", "acting_user": "other@x.com"})
        assert auth.acting_key() == "m@x.com"
    finally:
        auth._current.reset(tok)
    t = P(email="bot@x.com", kh_permissions={"kh.act_for_user"}, acting_user="other@x.com")
    tok = _with(t)
    try:
        assert auth.acting_visible({"acting_user": "other@x.com"})
        assert not auth.acting_visible({"acting_user": "third@x.com"})
        assert auth.acting_key() == "other@x.com"
    finally:
        auth._current.reset(tok)
    tok = _with(maint())
    try:
        assert auth.row_visible({"acting_user": "third@x.com"})
    finally:
        auth._current.reset(tok)
