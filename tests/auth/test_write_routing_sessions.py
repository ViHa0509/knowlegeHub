"""§2.3 step 5-8, §2.9, §2.11: acting tokens, sync-key reads, write routing, session binding."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app import auth, hotlog, projects, sqldb
from app import vector_store as vs
from tests.auth_helpers import OTHER_KEY, bearer, jwk_of, make_token

TECH = dict(permissions=["kh.act_for_user"])


def _hot_row(vid):
    rows = vs.get_ids_vt(hotlog.collection(), [vid]) if hotlog.enabled() else []
    return rows[0]["metadata"] if rows else None


def _log(client, headers, text, **body):
    return client.post("/vector/add", headers=headers, json={"text": text, "scope": "log", **body})


# --- write routing (§2.9) ---------------------------------------------------------------------------------


def test_member_log_without_project_lands_in_personal(signed_client, uid):
    h = bearer(make_token(f"w{uid}@x.com"), actor="s1")
    r = _log(signed_client, h, f"worked on the parser {uid}", metadata={"author": "forged@x.com", "actor": "zz"})
    assert r.status_code == 200, r.text
    meta = _hot_row(r.json()["vector_id"])
    assert meta["project"] == f"personal-w{uid}-x-com"
    assert meta["author"] == f"w{uid}@x.com" and meta["actor"] == "s1"
    assert projects.exists(f"personal-w{uid}-x-com")


def test_orchestrator_refuses_logs_and_findings(signed_client, uid):
    boss = bearer(make_token(f"boss{uid}@x.com", role="maintainer"))
    for scope in ("log", "findings"):
        r = signed_client.post("/vector/add", headers=boss,
                               json={"text": f"x {scope} {uid}", "scope": scope, "project": "orchestrator"})
        assert r.status_code == 409
        assert r.json()["detail"].startswith("the orchestrator keeps no worklogs or findings")
    # a member is stopped earlier by the permission check
    r = signed_client.post("/vector/add", headers=bearer(make_token(f"m{uid}@x.com")),
                           json={"text": "y", "scope": "log", "project": "orchestrator"})
    assert r.status_code == 403 and r.json()["detail"] == "missing permission project.log on this project"


def test_member_knowledge_write_needs_project_write(signed_client, uid):
    p = f"wr{uid}"
    projects.get_or_create_project(p)
    ro = bearer(make_token(f"ro{uid}@x.com", projects={p: "readonly"}))
    ed = bearer(make_token(f"ed{uid}@x.com", projects={p: "editable"}))
    body = {"text": f"fact {uid}", "metadata": {"project": p}}
    assert signed_client.post("/vector/add", headers=ro, json=body).status_code == 403
    assert signed_client.post("/vector/add", headers=ed, json=body).status_code == 200
    assert vs.get_existing(f"kh_know_{p}").count() == 1
    # personalization naming no project goes to the caller's personal collection
    r = signed_client.post("/vector/add", headers=ed, json={"text": f"pref {uid}", "scope": "personalization"})
    assert r.status_code == 200
    assert vs.get_existing(f"kh_personalization_personal-ed{uid}-x-com").count() == 1
    # top-level project disagreeing with metadata -> 422
    r = signed_client.post("/vector/add", headers=ed, json={"text": "z", "project": p, "metadata": {"project": "o"}})
    assert r.status_code == 403 or r.status_code == 422


# --- session binding (§2.11) -------------------------------------------------------------------------------


def test_session_routes_need_signin_and_actor(client, signed_client, uid):
    h = bearer(make_token(f"s{uid}@x.com"))
    r = signed_client.get("/session/project", headers=h)
    assert r.status_code == 400 and r.json()["detail"] == "name the session in the x-kh-actor header"


def test_session_routes_open_mode(client):
    r = client.get("/session/project", headers={"X-KH-Actor": "a1"})
    assert r.status_code == 400 and r.json()["detail"] == "session binding needs sign-in"


def test_bind_get_ttl_and_rewrite(signed_client, uid):
    p = f"bound{uid}"
    projects.get_or_create_project(p, aliases=[f"bnd{uid}"])
    projects.sync_orchestrator_registry()
    email = f"b{uid}@x.com"
    h = bearer(make_token(email, projects={p: "editable", f"other{uid}": "readonly"}), actor="cli-1")
    assert signed_client.get("/session/project", headers=h).json() == {"actor": "cli-1", "binding": None,
                                                                         "ttl_hours": 72.0}
    assert signed_client.put("/session/project", headers=h, json={"project": "nope-" + uid}).status_code == 404
    projects.get_or_create_project(f"other{uid}")
    r = signed_client.put("/session/project", headers=h, json={"project": f"other{uid}"})
    assert r.status_code == 403
    r = signed_client.put("/session/project", headers=h, json={"project": p})
    assert r.status_code == 200
    b = r.json()
    assert b["previous"] is None and b["binding"]["project"] == p and b["binding"]["source"] == "asked"
    got = signed_client.get("/session/project", headers=h).json()["binding"]
    assert got["project"] == p and got["actor"] == "cli-1"

    # a log write with the actor is rewritten to the bound project before the permission check
    r = _log(signed_client, h, f"bound log {uid}", project=f"personal-b{uid}-x-com",
             metadata={"source_id": "zzz", "project": f"personal-b{uid}-x-com"})
    assert r.status_code == 200, r.text
    meta = _hot_row(r.json()["vector_id"])
    assert meta["project"] == p and meta["project_from"] == "session" and "source_id" not in meta
    # knowledge writes and other sessions are not rewritten
    h2 = bearer(make_token(email, projects={p: "editable"}), actor="cli-2")
    r = _log(signed_client, h2, f"unbound log {uid}")
    assert _hot_row(r.json()["vector_id"])["project"] == f"personal-b{uid}-x-com"

    # 72 h idle TTL
    owner = f"personal-b{uid}-x-com"
    old = (datetime.now(timezone.utc) - timedelta(hours=73)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with sqldb.connect() as c:
        c.execute("UPDATE kh_session_binding SET last_seen=? WHERE owner=?", (old, owner))
    assert signed_client.get("/session/project", headers=h).json()["binding"] is None
    r = _log(signed_client, h, f"after ttl {uid}")
    assert _hot_row(r.json()["vector_id"])["project"] == owner

    # sweep (maintainer) and delete
    boss = bearer(make_token(f"boss{uid}@x.com", role="maintainer"))
    assert signed_client.post("/session/sweep", headers=h, json={}).status_code == 403
    assert signed_client.post("/session/sweep", headers=boss, json={"max_idle_hours": 0.5}).status_code == 400
    assert signed_client.post("/session/sweep", headers=boss, json={"max_idle_hours": "x"}).status_code == 400
    r = signed_client.post("/session/sweep", headers=boss, json={"max_idle_hours": 72})
    assert r.status_code == 200 and r.json()["deleted"] >= 1 and "idle_before" in r.json()
    signed_client.put("/session/project", headers=h, json={"project": f"bnd{uid}"})  # alias canonicalised
    assert signed_client.get("/session/project", headers=h).json()["binding"]["project"] == p
    assert signed_client.delete("/session/project", headers=h).json() == {"actor": "cli-1", "unbound": True}
    assert signed_client.delete("/session/project", headers=h).json() == {"actor": "cli-1", "unbound": False}


def test_detect_binds_on_id_or_alias_only(signed_client, uid):
    from app import session_project

    p = f"det{uid}"
    signed_client.post("/projects", headers=bearer(make_token(f"boss{uid}@x.com", role="maintainer")),
                       json={"project_id": p, "aliases": [f"detal{uid}"], "keywords": [f"detkw{uid}"]})
    pr = auth.Principal(email=f"d{uid}@x.com", projects={p: {"project.read", "project.log"}})
    tok = auth._current.set(pr)
    try:
        assert session_project.detect(pr, "a1", f"look at detkw{uid} please") is None
        b = session_project.detect(pr, "a1", f"work on detal{uid} today")
        assert b["project"] == p and b["source"] == "detected"
        ro = auth.Principal(email=f"r{uid}@x.com", projects={p: {"project.read"}})
        assert session_project.detect(ro, "a1", f"work on {p}") is None
    finally:
        auth._current.reset(tok)


# --- acting tokens (§2.3 step 5) -----------------------------------------------------------------------------


def test_acting_token_own_realm(signed_client, uid):
    person = f"p{uid}@x.com"
    tech = bearer(make_token(f"bot{uid}@x.com", **TECH), actor="bot")
    acting = make_token(person)
    r = _log(signed_client, {**tech, "X-KH-Acting-Token": "Bearer " + acting}, f"acted {uid}")
    assert r.status_code == 200, r.text
    assert _hot_row(r.json()["vector_id"])["acting_user"] == person
    # e-mail not verified
    bad = make_token(person, email_verified=False)
    r = _log(signed_client, {**tech, "X-KH-Acting-Token": bad}, f"acted2 {uid}")
    assert r.status_code == 401 and r.json() == {"detail": "x-kh-acting-token: e-mail not verified"}
    r = _log(signed_client, {**tech, "X-KH-Acting-Token": "garbage"}, f"acted3 {uid}")
    assert r.status_code == 401 and r.json() == {"detail": "x-kh-acting-token: malformed token"}
    # ignored for callers without kh.act_for_user
    plain = bearer(make_token(f"plain{uid}@x.com"))
    r = _log(signed_client, {**plain, "X-KH-Acting-Token": acting}, f"acted4 {uid}")
    assert r.status_code == 200 and "acting_user" not in _hot_row(r.json()["vector_id"])


def test_acting_token_foreign_issuer(monkeypatch, signin, signed_client, uid):
    other = "https://idp.other.test"
    monkeypatch.setenv("KH_ACTING_ISSUERS", json.dumps([
        {"issuer": other, "audience": "other-aud", "technical_users": [f"bot{uid}@x.com"]},
        {"broken": True}]))
    signin["docs"][other + "/protocol/openid-connect/certs"] = {"keys": [jwk_of(OTHER_KEY, "other")]}
    tech = bearer(make_token(f"bot{uid}@x.com", **TECH))
    good = make_token(f"q{uid}@x.com", iss=other, aud="other-aud", _key=OTHER_KEY, _kid="other")
    r = _log(signed_client, {**tech, "X-KH-Acting-Token": good}, f"foreign {uid}")
    assert r.status_code == 200, r.text
    assert _hot_row(r.json()["vector_id"])["acting_user"] == f"q{uid}@x.com"
    r = _log(signed_client, {**tech, "X-KH-Acting-Token": make_token("q@x.com", iss="https://nope.test")}, "n")
    assert r.json() == {"detail": "x-kh-acting-token: issuer not trusted"}
    tech2 = bearer(make_token(f"bot2{uid}@x.com", **TECH))
    r = _log(signed_client, {**tech2, "X-KH-Acting-Token": good}, "n2")
    assert r.json() == {"detail": "x-kh-acting-token: this technical user may not act for that issuer"}
    unverified = make_token("q@x.com", iss=other, aud="other-aud", _key=OTHER_KEY, _kid="other", email_verified=None)
    r = _log(signed_client, {**tech, "X-KH-Acting-Token": unverified}, "n3")
    assert r.json() == {"detail": "x-kh-acting-token: e-mail not verified"}


# --- sync-key readers (§2.3 step 6) ---------------------------------------------------------------------------


def test_with_key_reads_adds_project_read(signed_client, uid):
    p = f"synced{uid}"
    projects.get_or_create_project(p)
    with sqldb.connect() as c:
        sqldb.create_table(c, "sync_grants",
                           "CREATE TABLE IF NOT EXISTS sync_grants (email TEXT, project TEXT, source TEXT, kid TEXT, "
                           "job_id TEXT, granted_at TEXT, PRIMARY KEY (email, project))", "")
        c.execute("INSERT INTO sync_grants (email, project, source, kid, job_id, granted_at) VALUES (?,?,?,?,?,?)",
                  (f"k{uid}@x.com", p, "peer", "kid1", "job1", "2026-10-31T00:00:00Z"))
    h = bearer(make_token(f"k{uid}@x.com"))
    assert signed_client.get(f"/projects/{p}", headers=h).status_code == 200
    assert signed_client.get(f"/projects/{p}", headers=bearer(make_token(f"n{uid}@x.com"))).status_code == 403
