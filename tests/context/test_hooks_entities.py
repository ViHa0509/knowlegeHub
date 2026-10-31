"""Hook rule store (§3.5) and entity cards (§3.6)."""
from __future__ import annotations

import json

from app import entities


def test_hook_rules_crud_and_menu(client, uid):
    ht = f"Custom{uid}"
    r = client.post("/hooks/set", json={"hook_type": ht, "name": "b", "content": "SECOND", "position": 5})
    assert r.status_code == 200
    client.post("/hooks/set", json={"hook_type": ht, "name": "a", "content": "FIRST {menu}", "position": 1})
    client.post("/hooks/set", json={"hook_type": ht, "name": "off", "content": "HIDDEN", "enabled": False})
    raw = client.get("/hooks/raw", params={"hook_type": ht})
    assert raw.headers["content-type"].startswith("text/plain")
    assert raw.text.startswith("FIRST - 'browse market': Only when the person asks to look at 'market'")
    assert raw.text.endswith("\n\nSECOND") and "HIDDEN" not in raw.text
    assert "- 'talk with user': Greetings, thanks, small talk" in raw.text
    g = client.get("/hooks/get", params={"hook_type": ht}).json()
    assert g["count"] == 2 and g["content"] == raw.text
    assert len(client.get("/hooks/list", params={"hook_type": ht}).json()["rules"]) == 3
    assert client.post("/hooks/delete", json={"hook_type": ht, "name": "b"}).json()["deleted"] == 1
    rid = g["rules"][0]["id"]
    assert client.post("/hooks/delete", json={"id": rid}).json()["deleted"] == 1
    assert client.get("/hooks/raw", params={"hook_type": "NoSuchHook"}).text == ""


def test_hook_budget_lint(client, uid):
    r = client.post("/hooks/set", json={"hook_type": "UserPromptSubmit", "name": f"big-{uid}", "content": "x" * 1600})
    assert r.status_code == 422 and r.json()["error"] == "POLICY-LINT"
    r = client.post("/hooks/set", json={"hook_type": "UserPromptSubmit", "name": f"big-{uid}", "content": "x" * 1600,
                                        "allow_oversize": True, "reason": "test"})
    assert r.status_code == 200
    client.post("/hooks/delete", json={"hook_type": "UserPromptSubmit", "name": f"big-{uid}"})


def _transcript(tmp_path, tools):
    rows = [{"type": "user", "message": {"content": "earlier prompt"}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write", "input": {}}]}},
            {"type": "user", "promptId": "p2", "message": {"content": "do the thing"}}]
    for name, inp in tools:
        rows.append({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}})
        rows.append({"type": "user", "toolUseResult": {}, "message": {"content": [{"type": "tool_result"}]}})
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return str(p)


def test_turn_mutated(client, tmp_path):
    ro = _transcript(tmp_path, [("Read", {"file_path": "x"}), ("Bash", {"command": "git status"})])
    r = client.post("/hooks/turn_mutated", json={"transcript_path": ro}).json()
    assert r["mutated"] is False and r["tool_count"] == 2
    # prompt id not in the last slice: widen to the whole transcript (finds the earlier Write)
    r = client.post("/hooks/turn_mutated", json={"transcript_path": ro, "prompt_id": "zzz"}).json()
    assert r["mutated"] is True and r["how"] == "whole-transcript"
    rm = _transcript(tmp_path, [("Bash", {"command": "rm -rf build"})])
    assert client.post("/hooks/turn_mutated", json={"transcript_path": rm}).json()["mutated"] is True
    unk = _transcript(tmp_path, [("FancyNewTool", {})])
    assert client.post("/hooks/turn_mutated", json={"transcript_path": unk}).json()["mutated"] is True
    assert client.post("/hooks/turn_mutated", json={"transcript_path": str(tmp_path / "missing")}).json()["mutated"]


def test_entity_upsert_merge_and_line(client, uid):
    p = f"ent-{uid}"
    r = client.post("/entities", json={"project": p, "name": "Rowan Lee", "type": "person", "role": "lead dev",
                                       "aliases": ["RL"], "relations": [{"rel": "OWNS", "target": "Billing API"}],
                                       "source_log_ids": ["vec_1"]})
    assert r.status_code == 200
    j = r.json()
    assert j["id"] == f"ent__{p}__rowan-lee" and j["updated"] is False
    r = client.post("/entities", json={"project": p, "name": "Rowan Lee", "type": "person", "purpose": "ships",
                                       "aliases": ["rl", "Ro"], "relations": [{"rel": "owns", "target": "billing api"},
                                                                             {"rel": "uses", "target": "HANA"}],
                                       "source_log_ids": ["vec_1", "vec_2"]})
    c = r.json()["card"]
    assert r.json()["updated"] is True
    assert c["aliases"] == ["RL", "Ro"] and len(c["relations"]) == 2 and c["sources"] == ["vec_1", "vec_2"]
    assert c["role"] == "lead dev"
    assert entities.line_of(c) == (f"Rowan Lee (person, {p}) - role: lead dev - purpose: ships - relations: "
                                   "owns Billing API; uses HANA")
    client.post("/entities", json={"project": p, "name": "Billing API", "type": "system",
                                   "relations": [{"rel": "runs on", "target": "HANA"}]})
    client.post("/entities", json={"project": p, "name": "HANA", "type": "system"})
    m = client.get("/entities/match", params={"text": "ask ro about the billing api", "project": p}).json()
    assert m["count"] == 2 and m["cards"][0]["name"] == "Billing API"
    assert m["lines"][1].startswith("Rowan Lee (person,")
    g = client.get(f"/entities/{p}/RL", params={"depth": 2}).json()
    assert g["entity"]["name"] == "Rowan Lee" and {x["name"] for x in g["linked"]} == {"Billing API", "HANA"}
    g1 = client.get(f"/entities/{p}/Billing API", params={"depth": 0}).json()
    assert g1["linked"] == []
    assert client.get(f"/entities/{p}/Nobody").status_code == 404
    assert client.post("/entities", json={"project": p, "name": "X", "type": "alien"}).status_code == 422
