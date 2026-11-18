"""KH 7.1 spaces and flows over the legacy stores (app/spaces, app/flows)."""
from __future__ import annotations

import pytest

from app import projects

DEPLOY = "Deploy the billing service to DEV with cf push from the release branch"


def _add(client, text, project, scope="knowledge", **meta):
    r = client.post("/vector/add", json={"text": text, "project": project, "scope": scope, "metadata": meta})
    assert r.status_code == 200, r.text
    return r.json()["vector_id"]


@pytest.fixture
def space(client, fresh_store):
    projects.get_or_create_project("alpha", aliases=["alpha billing"], keywords=["billing"],
                                   descriptor="Alpha - the billing service")
    projects.get_or_create_project("lesson-learned")
    ids = {
        "deploy": _add(client, DEPLOY, "alpha", kind="how-to", name="deploy billing to dev",
                       keys=["billing deploy", "cf push"]),
        "other": _add(client, "The billing service stores invoices in HANA tables", "alpha", kind="fact"),
        "finding": _add(client, "Login page times out on QA", "alpha", scope="findings"),
        "shared": _add(client, "Push with cf: set CF_HOME per session so cf target stays put", "lesson-learned",
                       kind="lesson"),
    }
    return ids


def test_spaces_list_and_match(client, space):
    j = client.get("/spaces").json()
    ids = [x["id"] for x in j["items"]]
    assert "alpha" in ids and j["total"] == len(j["items"])
    assert set(j["items"][0]) == {"id", "name", "type", "desc", "vis", "recent"}
    m = client.get("/spaces/match", params={"text": "fix the alpha billing invoices"}).json()
    assert m["best_space"] == "alpha" and m["ambiguous"] is False
    assert {"space": "alpha", "term": "alpha billing", "on": "alias"} in m["items"]


def test_scopes_access_layout(client, space):
    j = client.get("/spaces/alpha/scopes").json()
    by = {s["scope"]: s for s in j["scopes"]}
    assert by["knowledges"]["rows"] == 2 and by["knowledges"]["alias"] == "knowledge"
    assert by["findings"]["rows"] == 1 and by["index"]["system"] is True
    a = client.get("/spaces/alpha/access").json()
    assert a["frame"] == "legacy" and a["scopes"]["knowledges"]["read"] is True
    lay = client.get("/projects/alpha/layout").json()
    assert lay["project"] == "alpha" and lay["save_kinds"]["lesson"] == {"scope": "lesson"}
    assert client.get("/spaces/nope-zz/scopes").status_code == 404


def test_rows_expand_index(client, space):
    rows = client.get("/spaces/alpha/scopes/knowledge/rows").json()
    assert rows["scope"] == "knowledges" and rows["total"] == 2
    one = client.get(f"/spaces/alpha/rows/{space['deploy']}").json()
    assert one["id"] == f"alpha/knowledges/{space['deploy']}" and one["name"] == "deploy billing to dev"
    assert one["keys"] == ["billing deploy", "cf push"] and one["history"] == []
    q = client.get(f"/spaces/alpha/rows/alpha/findings/{space['finding']}").json()
    assert q["type"] == "finding"
    assert client.get("/spaces/alpha/rows/nope").status_code == 404
    idx = client.get("/spaces/alpha/index", params={"key": "CF PUSH"}).json()
    assert [x["id"] for x in idx["items"]] == [space["deploy"]]


def test_map_and_search(client, space):
    j = client.post("/spaces/alpha/map", json={"text": DEPLOY}).json()
    assert j["status"] == "ok" and f"alpha/knowledges/{space['deploy']}" in j["ids"]
    assert "[best]" in j["text"] and j["text"].rstrip().endswith('"none found" = nothing there')
    miss = client.post("/spaces/alpha/map", json={"text": "zzqx unrelated wording"}).json()
    s = client.post("/spaces/alpha/scopes/findings/search", json={"text": "Login page times out on QA"}).json()
    assert s["items"][0]["id"] == space["finding"]
    assert miss["text"].count("none found") >= 7


def test_full_retrieve_flow(client, space):
    r = client.post("/flows/task.full-retrieve/run",
                    json={"input": {"text": DEPLOY, "actions": "Push with cf: set CF_HOME per session so cf target "
                                                               "stays put; sign in to SAP BTP", "space": "alpha"}})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["flow"] == "task.full-retrieve" and j["status"] == "done" and j["trace_id"].startswith("run_")
    t = j["text"]
    assert t.startswith("Open with kh_expand(id) the rows you use.")
    assert f"lesson-learned/knowledges/{space['shared']} (knowledge, for 'Push with cf" in t
    assert "- NO LESSON for 'sign in to SAP BTP'" in t
    assert f"{space['deploy']} | alpha/knowledges" in t
    runs = client.get("/flows/runs", params={"flow": "task.full-retrieve"}).json()["runs"]
    assert runs[0]["run_id"] == j["trace_id"] and runs[0]["input"]["space"] == "alpha"
    one = client.get(f"/flows/runs/{j['trace_id']}").json()
    assert one["result"]["text"] == t
    assert client.post(f"/flows/runs/{j['trace_id']}/answer", json={"answer": "x"}).status_code == 409


def test_flows_listing_manifest_hooks(client, space):
    j = client.get("/flows").json()
    ids = {f["id"] for f in j["flows"]}
    assert {"task.full-retrieve", "sys.on_prompt_submit", "save.log"} <= ids and j["rev"]
    m = client.get("/helper/manifest").json()
    assert {t["name"] for t in m["tools"]} >= {"kh_map", "kh_expand", "kh_find", "kh_walk", "kh_flow"}
    assert m["hooks"]["UserPromptSubmit"]["run"] == "/hooks/UserPromptSubmit/run" and m["flows_on"] is True
    h = client.post("/hooks/UserPromptSubmit/run", json={"input": {"text": "fix the alpha billing invoices"}}).json()
    assert h["flow"] == "sys.on_prompt_submit" and h["project"] == "alpha"
    assert h["text"].startswith("KH TURN CONTEXT") and "SPACE ABOUT AND OPEN FINDINGS:" in h["text"]
    s = client.post("/hooks/SessionStart/run", json={"input": {"part": 1}}).json()
    assert s["flow"] == "sys.on_session_start" and isinstance(s["parts"], int)
    assert client.post("/hooks/Stop/run", json={"input": {}}).json()["text"] == ""
    assert client.post("/hooks/Nope/run", json={}).status_code == 404
    nr = client.post("/flows/save.spec/run", json={"input": {"text": "x"}}).json()
    assert nr["status"] == "failed" and "no runner" in nr["message"]
    assert client.get("/project-types").json()["types"][0]["name"] == "Project"


def test_about_put_get(client, space):
    assert client.get("/projects/alpha/about").status_code == 404
    r = client.put("/projects/alpha/about", json={"text": "Alpha billing service. Repo: git@example:alpha.git"})
    assert r.status_code == 200 and r.json()["id"] == "about__alpha"
    a = client.get("/projects/alpha/about").json()
    assert a["text"].startswith("Alpha billing service.")
    assert client.get("/projects/alpha/layout").json()["about"].startswith("Alpha billing")
    sc = {s["scope"]: s["rows"] for s in client.get("/spaces/alpha/scopes").json()["scopes"]}
    assert sc["about"] == 1


def test_save_flows(client, space):
    k = client.post("/flows/save.knowledge/run", json={"input": {"text": "Alpha uses port 8443 on DEV",
                                                                  "kind": "fact", "space": "alpha"}}).json()
    assert k["status"] == "done" and k["text"].startswith("saved 1 row: alpha/knowledges/")
    les = client.post("/flows/save.knowledge/run", json={"input": {"text": "Never push alpha on Fridays",
                                                                    "kind": "lesson", "space": "alpha"}}).json()
    assert "/lesson-learns/" in les["text"]
    lg = client.post("/flows/save.log/run", json={"input": {"text": "2026-11-18 fixed alpha login", "space": "alpha"}})
    assert lg.json()["status"] == "done" and lg.json()["text"].startswith("saved log ")
    assert client.post("/flows/save.log/run", json={"input": {"text": " "}}).json()["status"] == "failed"
