"""§6.5 principals: service principals, EdDSA tokens, ns client, /v1/token, /v1/me, grants."""
from __future__ import annotations

import jwt
import pytest

from app import auth, sqldb
from app.caps import core, principals
from tests.auth_helpers import bearer, enable_signin, make_token
from tests.caps.conftest import EVENT_MANIFEST, attach, new_ns, new_project


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def test_create_list_patch_token(client):
    p1, p2 = new_project(), new_project()
    ns, _, _ = attach(client, projects=[p1, p2])
    r = client.post(f"/v1/{ns}/principals", json={"role": "agent", "projects": [p1]})
    assert r.status_code == 201
    pr = r.json()
    assert pr["principal_id"].startswith("p_") and len(pr["principal_id"]) == 14
    assert pr["role"] == "agent" and pr["state"] == "active" and pr["projects"] == [p1] and pr["rev"] == 1
    assert client.post(f"/v1/{ns}/principals", json={"role": "boss"}).status_code == 422
    assert client.post(f"/v1/{ns}/principals", json={"role": "agent", "principal_id": "client"}).status_code == 422
    assert client.post(f"/v1/{ns}/principals", json={"role": "agent", "projects": ["zz"]}).status_code == 422
    d = client.post(f"/v1/{ns}/principals", json={"role": "judge", "principal_id": "j1"})
    assert d.status_code == 201
    assert client.post(f"/v1/{ns}/principals", json={"role": "judge", "principal_id": "j1"}).status_code == 409
    lst = client.get(f"/v1/{ns}/principals", params={"role": "judge"}).json()["principals"]
    assert [x["principal_id"] for x in lst] == ["j1"]
    t = client.post(f"/v1/{ns}/principals/j1/token")
    assert t.status_code == 200
    tb = t.json()
    assert tb["token_type"] == "Bearer" and tb["expires_in"] == 900 and tb["principal_id"] == "j1"
    claims = jwt.decode(tb["access_token"], options={"verify_signature": False})
    head = jwt.get_unverified_header(tb["access_token"])
    assert head["alg"] == "EdDSA" and head["typ"] == "kh+jwt" and head["kid"].startswith("ed-")
    assert claims["iss"] == "kh-principals" and claims["aud"] == "kh-v1" and claims["sub"] == f"{ns}:j1"
    assert claims["ns"] == ns and claims["pid"] == "j1" and claims["role"] == "judge" and claims["jti"]
    assert claims["exp"] - claims["iat"] == 900
    s = client.patch(f"/v1/{ns}/principals/j1", json={"state": "suspended"}, headers={"If-Match": '"1"'})
    assert s.status_code == 200 and s.json()["state"] == "suspended" and s.headers["ETag"] == '"2"'
    assert client.patch(f"/v1/{ns}/principals/j1", json={"state": "active"},
                        headers={"If-Match": '"1"'}).status_code == 409
    assert client.post(f"/v1/{ns}/principals/j1/token").status_code == 409
    assert client.patch(f"/v1/{ns}/principals/j1", json={"state": "revoked"}).json()["state"] == "revoked"
    r = client.patch(f"/v1/{ns}/principals/j1", json={"state": "active"})
    assert r.status_code == 409 and r.json()["error"] == "invalid_transition"
    assert client.get(f"/v1/{ns}/principals/nope").status_code == 404


def test_active_cap(monkeypatch, client):
    ns, _, _ = attach(client)
    monkeypatch.setenv("KH_PRINCIPALS_MAX", "2")
    assert client.post(f"/v1/{ns}/principals", json={"role": "agent", "principal_id": "a1"}).status_code == 201
    assert client.post(f"/v1/{ns}/principals", json={"role": "agent", "principal_id": "a2"}).status_code == 201
    r = client.post(f"/v1/{ns}/principals", json={"role": "agent", "principal_id": "a3"})
    assert r.status_code == 429 and r.json()["error"] == "quota"
    client.patch(f"/v1/{ns}/principals/a1", json={"state": "suspended"})
    assert client.post(f"/v1/{ns}/principals", json={"role": "agent", "principal_id": "a3"}).status_code == 201
    assert client.patch(f"/v1/{ns}/principals/a1", json={"state": "active"}).status_code == 429


def test_service_tokens_signed_in(monkeypatch, client):
    p1, p2 = new_project(), new_project()
    ns, _, _ = attach(client, projects=[p1, p2], manifest=EVENT_MANIFEST)
    orch = client.post(f"/v1/{ns}/principals", json={"role": "orchestrator", "principal_id": "o1"}).json()
    assert orch["role"] == "orchestrator"
    otok = client.post(f"/v1/{ns}/principals/o1/token").json()["access_token"]
    enable_signin(monkeypatch)
    me = client.get("/v1/me", headers=_h(otok))
    assert me.status_code == 200
    assert me.json()["kind"] == "service" and me.json()["role"] == "orchestrator" and me.json()["ns"] == ns
    # orchestrator creates agents, not orchestrators; only within its own projects
    assert client.post(f"/v1/{ns}/principals", json={"role": "orchestrator"}, headers=_h(otok)).status_code == 403
    a = client.post(f"/v1/{ns}/principals", json={"role": "agent", "principal_id": "ag", "projects": [p1]},
                    headers=_h(otok))
    assert a.status_code == 201 and a.json()["created_by"] == "o1"
    atok = client.post(f"/v1/{ns}/principals/ag/token", headers=_h(otok)).json()["access_token"]
    # agent: append yes, read no
    assert client.post(f"/v1/{ns}/events", json={"kind": "step.done", "body": {"n": 1}},
                       headers=_h(atok)).status_code == 200
    r = client.get(f"/v1/{ns}/events", headers=_h(atok))
    assert r.status_code == 403 and r.json()["error"] == "forbidden"
    assert client.get(f"/v1/{ns}/principals", headers=_h(atok)).status_code == 403
    # project grant narrows
    assert client.post(f"/v1/{ns}/events", json={"kind": "step.done", "project": p2, "body": {"n": 1}},
                       headers=_h(atok)).status_code == 422
    # other namespace / non-/v1 routes refused
    ns2, _, _ = attach(client, headers=bearer(make_token("boss@x.com", role="maintainer")))
    assert client.get(f"/v1/{ns2}/events", headers=_h(otok)).status_code == 403
    assert client.get("/projects", headers=_h(otok)).status_code == 403
    # suspend -> 401 within cache window (cache cleared by PATCH on this instance)
    client.patch(f"/v1/{ns}/principals/ag", json={"state": "suspended"}, headers=_h(otok))
    r = client.post(f"/v1/{ns}/events", json={"kind": "step.done", "body": {"n": 1}}, headers=_h(atok))
    assert r.status_code == 401
    # tampered / wrong-audience tokens
    bad = jwt.encode({"iss": "kh-principals", "aud": "kh-v1", "ns": ns, "pid": "o1", "role": "orchestrator",
                      "exp": 9999999999}, "secret", algorithm="HS256")
    assert client.get("/v1/me", headers=_h(bad)).status_code == 401


def test_client_credentials(monkeypatch, client):
    p = new_project()
    ns = new_ns()
    enable_signin(monkeypatch)
    owner = bearer(make_token("own@x.com", projects={p: "owner"}))
    att = client.put(f"/v1/admin/ns/{ns}", json={"projects": [p]}, headers=owner)
    assert att.status_code == 201
    cl = att.json()["client"]
    r = client.post("/v1/token", data={"grant_type": "client_credentials", "client_id": cl["client_id"],
                                       "client_secret": cl["client_secret"]})
    assert r.status_code == 200
    tok = r.json()
    assert tok["principal_id"] == "client" and tok["role"] == "admin" and tok["token_type"] == "Bearer"
    claims = jwt.decode(tok["access_token"], options={"verify_signature": False})
    assert claims["obo"] == "own@x.com" and claims["pid"] == "client"
    j = client.post("/v1/token", json={"client_id": cl["client_id"], "client_secret": cl["client_secret"]})
    assert j.status_code == 200
    assert client.post("/v1/token", json={"client_id": cl["client_id"], "client_secret": "x"}).status_code == 401
    assert client.post("/v1/token", json={"grant_type": "password"}).status_code == 422
    me = client.get("/v1/me", headers=_h(tok["access_token"])).json()
    assert me["kind"] == "client" and me["role"] == "admin" and me["on_behalf_of"] == "own@x.com"
    assert me["projects"] == [p]
    # client is the ns admin: manifest + principals
    assert client.put(f"/v1/{ns}/manifest", json=EVENT_MANIFEST, headers=_h(tok["access_token"])).status_code == 200
    assert client.post(f"/v1/{ns}/principals", json={"role": "orchestrator"},
                       headers=_h(tok["access_token"])).status_code == 201
    # but cannot manage namespaces
    assert client.get(f"/v1/admin/ns/{ns}", headers=_h(tok["access_token"])).status_code == 403
    # rotate: old secret dies
    rot = client.post(f"/v1/admin/ns/{ns}:client", headers=owner)
    assert rot.status_code == 201 and rot.json()["rotated"] is True
    assert client.post("/v1/token", json={"client_id": cl["client_id"],
                                          "client_secret": cl["client_secret"]}).status_code == 401
    # detach disables client + suspends principals; reattach re-enables client only
    client.post(f"/v1/admin/ns/{ns}:detach", headers=owner)
    assert client.get("/v1/me", headers=_h(tok["access_token"])).status_code == 401
    with sqldb.connect() as c:
        states = {r[0] for r in c.execute("SELECT state FROM kh_principal WHERE ns=?", (ns,)).fetchall()}
    assert states == {"suspended"}
    client.post(f"/v1/admin/ns/{ns}:reattach", headers=owner)
    assert client.get("/v1/me", headers=_h(tok["access_token"])).status_code == 200


def test_grants_never_widen():
    row = {"ns": "abc", "projects": ["a", "b", "c"]}
    with sqldb.connect() as c:
        sqldb.upsert(c, "kh_owner_grant", ["email"], {"email": "g@x.com", "projects_json": '["a","b","z"]',
                                                      "source": "token", "refreshed_ms": 1})
    principals.invalidate()
    assert principals.effective_projects(row, None, None) == ["a", "b", "c"]
    assert principals.effective_projects(row, ["b", "c"], None) == ["b", "c"]
    assert principals.effective_projects(row, None, "g@x.com") == ["a", "b"]
    assert principals.effective_projects(row, ["b", "c"], "g@x.com") == ["b"]
    assert principals.effective_projects(row, None, "unknown@x.com") == []
    people = {"ns": "kh_people", "projects": []}
    assert principals.effective_projects(people, None, None) == ["*"]
    assert principals.effective_projects(people, ["q"], None) == ["q"]


def test_me_person_and_local(monkeypatch, client):
    assert client.get("/v1/me").json()["kind"] == "local"
    enable_signin(monkeypatch)
    p = new_project()
    me = client.get("/v1/me", headers=bearer(make_token("m@x.com", projects={p: "readonly"}))).json()
    assert me["kind"] == "person" and me["role"] == "member" and p in me["projects"]


def test_key_rotation(monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    k1, k2 = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    pem = lambda k: k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,  # noqa: E731
                                    serialization.NoEncryption()).decode()
    pub = lambda k: k.public_key().public_bytes(serialization.Encoding.PEM,  # noqa: E731
                                                serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    monkeypatch.setenv("KH_PRINCIPAL_SIGNING_KEY", pem(k1))
    old_tok, _ = principals.issue("abc", "client", "admin", None)
    assert principals.verify(old_tok)["pid"] == "client"
    monkeypatch.setenv("KH_PRINCIPAL_SIGNING_KEY", pem(k2))
    with pytest.raises(ValueError):
        principals.verify(old_tok)
    monkeypatch.setenv("KH_PRINCIPAL_OLD_KEYS", '["%s"]' % pub(k1).replace("\n", "\\n"))
    assert principals.verify(old_tok)["ns"] == "abc"
    monkeypatch.delenv("KH_PRINCIPAL_SIGNING_KEY")
    monkeypatch.delenv("KH_PRINCIPAL_OLD_KEYS")


def test_auth_delegates_service_tokens(client):
    ns, _, _ = attach(client)
    client.post(f"/v1/{ns}/principals", json={"role": "judge", "principal_id": "jj"})
    tok = client.post(f"/v1/{ns}/principals/jj/token").json()["access_token"]
    p = auth.principal_of(tok)
    assert p.ns == ns and p.svc_role == "judge" and p.principal_id == "jj" and not p.is_person
    core.invalidate_ns(ns)
