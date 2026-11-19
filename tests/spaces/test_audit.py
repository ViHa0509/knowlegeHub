"""Change audit: record(), /home/audit, export, rule history, and the write paths that feed it."""
from __future__ import annotations

import csv
import io
import json

from app import audit, projects, sqldb


def _policy(client, project, action, sub, content):
    r = client.post("/policy/rule/set", json={"project": project, "action": action, "sub_action": sub,
                                              "content": content})
    assert r.status_code == 200, r.text
    return r


def _setup_action(client, project, action):
    r = client.post("/policy/action/set", json={"project": project, "action": action,
                                                "description": "Do the " + action + " thing for the team"})
    assert r.status_code == 200, r.text


def test_policy_history(client, fresh_store):
    projects.get_or_create_project("alpha")
    _setup_action(client, "alpha", "ship release notes")
    _policy(client, "alpha", "ship release notes", "draft", "Write the notes from the merged changes only.")
    _policy(client, "alpha", "ship release notes", "draft", "Write the notes from merged changes, newest first.")
    key = "alpha|ship release notes|draft"
    h = client.get("/home/rules/history", params={"kind": "policy", "key": key}).json()
    assert h["kind"] == "policy" and h["key"] == key and len(h["changes"]) == 2
    first, second = h["changes"]
    assert first["detail"]["before"] is None and first["action"] == "rule.policy.add"
    assert second["action"] == "rule.policy.update" and second["detail"]["before"] == first["detail"]["after"]
    assert first["id"].startswith("au_") and len(first["id"]) == 25 and first["actor"] == "local"
    d = client.post("/policy/rule/delete", json={"project": "alpha", "action": "ship release notes",
                                                 "sub_action": "draft"}).json()
    assert d["deleted"] == 1
    h = client.get("/home/rules/history", params={"kind": "policy", "key": key}).json()
    assert [c["action"] for c in h["changes"]] == ["rule.policy.add", "rule.policy.update", "rule.policy.delete"]
    assert h["changes"][-1]["detail"]["after"] is None


def test_history_requires_params_and_empty(client):
    assert client.get("/home/rules/history", params={"kind": "policy"}).status_code == 422
    assert client.get("/home/rules/history", params={"kind": "policy", "key": "x"}).json() == \
        {"kind": "policy", "key": "x", "changes": []}


def test_audit_failure_is_harmless(client, fresh_store, monkeypatch):
    projects.get_or_create_project("beta")
    _setup_action(client, "beta", "plan the sprint")

    real = sqldb.upsert

    def flaky(c, table, keys, row):
        if table == "kh_audit":
            raise RuntimeError("audit down")
        return real(c, table, keys, row)

    monkeypatch.setattr(sqldb, "upsert", flaky)
    audit.record("rule.policy.add", "beta", "t")  # must not raise
    r = client.post("/policy/rule/set", json={"project": "beta", "action": "plan the sprint", "sub_action": "draft",
                                              "content": "List the goals first."})
    assert r.status_code == 200
    monkeypatch.undo()
    got = client.get("/policy/plan", params={"action": "plan the sprint", "project": "beta"})
    assert got.status_code == 200 and "draft" in got.text
    assert client.get("/home/rules/history", params={"kind": "policy", "key": "beta|plan the sprint|draft"}
                      ).json()["changes"] == []


def test_filters_and_paging(client):
    audit.record("distill.apply", "gamma", "gamma|k1", note="x")
    audit.record("home.items.delete", "gamma", "gamma|knowledge", ids=["a"])
    audit.record("retention.logs", "every project", "logs", total=0)
    j = client.get("/home/audit", params={"action": "distill.", "project": "gamma"}).json()
    assert j["total"] == 1 and j["rows"][0]["action"] == "distill.apply" and j["rows"][0]["detail"] == {"note": "x"}
    allr = client.get("/home/audit", params={"limit": 2}).json()
    assert len(allr["rows"]) == 2 and allr["total"] >= 3
    assert allr["rows"][0]["ts"] >= allr["rows"][1]["ts"] and allr["rows"][0]["id"] > allr["rows"][1]["id"]
    page2 = client.get("/home/audit", params={"limit": 2, "offset": 2}).json()
    assert page2["rows"][0]["id"] < allr["rows"][1]["id"]
    today = allr["rows"][0]["ts"][:10]
    assert client.get("/home/audit", params={"since": today, "until": today}).json()["total"] >= 3
    assert client.get("/home/audit", params={"since": "2999-01-01"}).json() == {"total": 0, "rows": []}
    assert client.get("/home/audit", params={"until": "2000-01-01"}).json() == {"total": 0, "rows": []}


def test_export_formats(client):
    audit.record("home.part.delete", "delta", "delta|findings", deleted=3)
    r = client.get("/home/audit/export", params={"fmt": "csv", "project": "delta", "limit": 2})
    assert r.headers["content-type"].startswith("text/csv") and "kh-audit-" in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == ["ts", "actor", "action", "project", "target", "detail"]
    assert rows[1][2] == "home.part.delete" and json.loads(rows[1][5]) == {"deleted": 3}
    r = client.get("/home/audit/export", params={"fmt": "json", "project": "delta"})
    assert r.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(x) for x in r.text.splitlines()]
    assert lines and lines[0]["target"] == "delta|findings" and lines[0]["id"].startswith("au_")


def test_other_write_paths_are_audited(client, fresh_store):
    projects.get_or_create_project("eps")
    # kernel rule add -> update -> delete (a personal override of a common slot)
    tier = "personal-audit-eps"
    body = {"tier": tier, "overrides": "kh_reply_language", "content": "Reply in long detailed paragraphs.",
            "tags": ["reply_length", "always"]}
    r = client.post("/kernel/rules/add", json=body)
    assert r.status_code == 200, r.text
    r2 = client.post("/kernel/rules/add", json={**body, "content": "Reply in two long paragraphs."})
    assert r2.status_code == 200 and r2.json()["result"] == "updated"
    assert client.post("/kernel/rules/delete", json={"id": r.json()["id"]}).json() == {"deleted": 1}
    ch = client.get("/home/rules/history", params={"kind": "kernel", "key": "kh_reply_language"}).json()["changes"]
    ch = [c for c in ch if c["project"] == tier]  # other tests may touch the same common slot
    assert [c["action"] for c in ch] == ["rule.kernel.add", "rule.kernel.update", "rule.kernel.delete"]
    assert ch[0]["project"] == tier and ch[1]["detail"]["before"] == "Reply in long detailed paragraphs."
    # threshold
    assert client.put("/thresholds", json={"name": "KH_MAP_K", "value": 11}).status_code == 200
    client.delete("/thresholds/KH_MAP_K")
    rows = client.get("/home/audit", params={"action": "threshold."}).json()["rows"]
    assert {"threshold.set", "threshold.clear"} <= {r["action"] for r in rows}
    # /home deletes
    r = client.post("/home/parts/delete", json={"project": "eps", "part": "knowledge", "confirm": "eps"})
    assert r.status_code == 200
    r = client.post("/home/items/delete", json={"project": "eps", "part": "knowledge", "ids": ["nope"]})
    assert r.status_code == 200
    acts = {r["action"] for r in client.get("/home/audit", params={"action": "home.", "project": "eps"}).json()["rows"]}
    assert acts == {"home.part.delete", "home.items.delete"}
