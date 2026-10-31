"""Contract details aligned with the original local KH build (older v6.5, 262 operations).

Where the spec is silent, the rebuild follows the original's query/body declarations; see the
"Verified against the original local build" section of docs/deviations.md.
"""
from __future__ import annotations

import hashlib

from app import auth_routes
from tests.auth_helpers import ISSUER

# sha256 of the original /auth/help text served by the local build (KH http://127.0.0.1:9000,
# issuer http://localhost:8180/realms/knowledge-hub, helper wheel 1.14.0).
ORIG_HELP_SHA = "ca1bf2e7e19231a5236b293721292fda784ba454d2f244b7295e03b9c5e7acc0"


def _params(client, path, method="get"):
    op = client.get("/openapi.json").json()["paths"][path][method]
    return {p["name"]: p for p in op.get("parameters", [])}


def test_query_params_match_original(client):
    assert "module" in _params(client, "/artifacts")
    assert _params(client, "/sync/given")["limit"]["schema"]["default"] == 30
    assert _params(client, "/sync/jobs")["limit"]["schema"]["default"] == 30
    assert "entities" in _params(client, "/turn/context")
    home = _params(client, "/home/items")
    assert home["part"]["required"] is True and home["limit"]["schema"]["default"] == 10
    assert _params(client, "/messages/channel")["project"]["required"] is True
    assert _params(client, "/orchestrator/project_match")["text"]["required"] is True
    assert _params(client, "/policy/list")["project"]["schema"]["default"] == "orchestrator"
    rk = _params(client, "/distill/rekey")["limit"]["schema"]
    assert rk["default"] == 20 and rk["maximum"] == 100
    assert _params(client, "/v1/{ns}/metrics")["series"]["required"] is True
    assert _params(client, "/v1/{ns}/quotes/{quote_id}")["pack"]["required"] is False
    assert "confirm" in _params(client, "/v1/admin/ns/{ns}:purge", "post")
    assert _params(client, "/v1/admin/ns:import", "post")["rebuild"]["schema"]["default"] == "async"


def test_required_params_are_enforced(client):
    assert client.get("/orchestrator/project_match").status_code == 422
    assert client.get("/messages/channel").status_code == 422
    assert client.get("/home/items", params={"project": "orchestrator"}).status_code == 422
    assert client.post("/graph/add", json={"edges": []}).status_code == 422
    assert client.post("/graph/add", json={"entities": []}).status_code == 200


def test_delete_policy_takes_a_raw_object(client):
    assert client.request("DELETE", "/policy", json={}).status_code == 422
    r = client.post("/policy/add", json={"intent": "align test intent", "body": "do it", "policy_id": "pol_align"})
    assert r.status_code == 200, r.text
    assert any(p["policy_id"] == "pol_align" for p in client.get("/policy/list").json()["policies"])
    r = client.request("DELETE", "/policy", json={"policy_id": "pol_align"})
    assert r.status_code == 200 and r.json()["deleted"] == 1


def test_turn_context_entities_param(client):
    assert client.get("/turn/context", params={"text": "hello", "entities": ["a", "b"]}).status_code == 200
    assert client.get("/turn/context", params={"text": "hello", "entities[]": ["a"]}).status_code == 200


def test_step_budget_and_k_default_from_config(client):
    r = client.post("/context/step", json={"text": "how do I align the build", "budget_chars": None, "k": None,
                                          "entities": None})
    assert r.status_code == 200, r.text
    assert r.json()["budget_chars"] == 6000


def test_artifacts_module_filter(client):
    import base64

    b = base64.b64encode(b"x").decode()
    assert client.post("/artifacts", json={"name": "align-a.txt", "content_b64": b, "module": "m-a"}).status_code == 200
    assert client.post("/artifacts", json={"name": "align-b.txt", "content_b64": b, "module": "m-b"}).status_code == 200
    names = [a["name"] for a in client.get("/artifacts", params={"module": "m-a"}).json()["artifacts"]]
    assert names == ["align-a.txt"]
    assert {"align-a.txt", "align-b.txt"} <= {a["name"] for a in client.get("/artifacts").json()["artifacts"]}


def test_auth_help_seed_is_the_original_text():
    seed = auth_routes.SEED_PATH.read_text(encoding="utf-8")
    text = (seed.replace("{source}", "http://127.0.0.1:9000/auth/download/kh_helper-1.14.0-py3-none-any.whl")
            .replace("{kh_url}", "http://127.0.0.1:9000")
            .replace("{issuer}", "http://localhost:8180/realms/knowledge-hub"))
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == ORIG_HELP_SHA


def test_auth_help_served_from_seed(signed_client, monkeypatch):
    monkeypatch.setattr(auth_routes, "guide_text", lambda: None)
    base = "https://kh.example"
    text = signed_client.get("/auth/help", headers={"x-forwarded-proto": "https",
                                                    "x-forwarded-host": "kh.example"}).text
    source = auth_routes.helper_link(base)[0] or f"{base}/auth/download/{auth_routes.HELPER_MODULE}"
    seed = auth_routes.SEED_PATH.read_text(encoding="utf-8")
    assert text == seed.replace("{source}", source).replace("{kh_url}", base).replace("{issuer}", ISSUER)
    assert text.startswith("KNOWLEDGE HUB - HOW TO CONNECT\n") and text.endswith(f"Identity service: {ISSUER}\n")


def test_401_body_is_compact(signed_client):
    r = signed_client.get("/projects")
    assert r.status_code == 401
    assert r.content == b'{"detail":"sign-in required","help":"/auth/help"}'
