"""§6.8 redaction: packs, selection, scan/verify, scrub on write paths."""
from __future__ import annotations

from app.caps import redact
from tests.caps.conftest import EVENT_MANIFEST, attach

OWN_PACK = {
    "description": "ticket ids are secret here",
    "patterns": [{"name": "ticket", "kind": "secret", "mode": "mask", "regex": r"\bTCK-\d{4,}\b"},
                 {"name": "badge", "kind": "pii", "mode": "refuse", "regex": r"\bBADGE#\d{6}\b"}],
    "vectors": [{"text": "see TCK-12345 now", "findings": ["ticket"], "masked": "see [REDACTED:ticket] now",
                 "refused": False},
                {"text": "badge BADGE#123456", "findings": ["badge"], "refused": True},
                {"text": "nothing", "findings": []}],
}


def test_default_pack_and_scanner():
    sc = redact.default_scanner()
    masked, found, refused = sc.mask("mail me at jane.doe@example.com, password=Hunter2Secret!")
    assert masked == "mail me at [REDACTED:email], [REDACTED:assigned_secret]"
    assert [f["name"] for f in found] == ["email", "assigned_secret"] and refused is False
    d = [p for p in redact.file_packs() if p["id"] == "kh-default"][0]
    assert len(d["vectors"]) == 22 and all(p["mode"] == "mask" for p in d["patterns"])
    conf = redact.conformance({"ref": "kh-default@1", **d})
    assert conf["failures"] == [] and conf["passed"] == 22
    # merging: overlapping spans fold into the first (longest at the same start)
    sc2 = redact.Scanner([{"ref": "x@1", "patterns": [
        {"name": "a", "regex": r"abc"}, {"name": "b", "regex": r"bcd"}]}])
    m, f, _ = sc2.mask("xabcdx")
    assert m == "x[REDACTED:a]x" and [x["name"] for x in f] == ["a"]
    assert redact.entropy("aaaa") == 0.0 and round(redact.entropy("abcd"), 3) == 2.0


def test_kh_packs_routes(client):
    r = client.get("/v1/redaction/packs")
    assert r.status_code == 200
    refs = [p["ref"] for p in r.json()["packs"]]
    assert "kh-default@1" in refs and "spec" in r.json()
    one = client.get("/v1/redaction/packs/kh-default").json()
    assert one["ref"] == "kh-default@1" and one["owner"] == "kh" and len(one["patterns"]) == 11
    assert client.get("/v1/redaction/packs/kh-nope").status_code == 404


def test_selection_scan_verify_and_own_pack(client):
    ns, _, _ = attach(client)
    sel = client.get(f"/v1/{ns}/redaction")
    assert sel.status_code == 200
    assert sel.json()["source"] == "default" and sel.json()["effective"][0]["ref"] == "kh-default@1"
    # publish an own pack
    bad = client.put(f"/v1/{ns}/redaction/packs/kh-mine@1", json=OWN_PACK)
    assert bad.status_code == 422
    nover = client.put(f"/v1/{ns}/redaction/packs/mine", json=OWN_PACK)
    assert nover.status_code == 422
    put = client.put(f"/v1/{ns}/redaction/packs/mine@1", json=OWN_PACK)
    assert put.status_code == 201, put.text
    assert put.json()["ref"] == "mine@1" and put.json()["conformance"]["passed"] == 3
    again = client.put(f"/v1/{ns}/redaction/packs/mine@1", json=OWN_PACK)
    assert again.status_code == 200 and again.json()["created"] is False
    other = {**OWN_PACK, "vectors": OWN_PACK["vectors"] + [{"text": "x", "findings": []}]}
    imm = client.put(f"/v1/{ns}/redaction/packs/mine@1", json=other)
    assert imm.status_code == 409 and imm.json()["error"] == "pack_immutable"
    failing = {**OWN_PACK, "vectors": [{"text": "TCK-1234", "findings": []}, *OWN_PACK["vectors"][1:]]}
    assert client.put(f"/v1/{ns}/redaction/packs/mine@2", json=failing).status_code == 422
    unexercised = {**OWN_PACK, "vectors": [OWN_PACK["vectors"][0]]}
    assert client.put(f"/v1/{ns}/redaction/packs/mine@2", json=unexercised).status_code == 422
    nested = {"patterns": [{"name": "n", "regex": r"(a+)+b"}], "vectors": [{"text": "aab", "findings": ["n"]}]}
    assert client.put(f"/v1/{ns}/redaction/packs/nest@1", json=nested).status_code == 422
    empty = {"patterns": [{"name": "e", "regex": r"a*"}], "vectors": [{"text": "a", "findings": ["e"]}]}
    assert client.put(f"/v1/{ns}/redaction/packs/empty@1", json=empty).status_code == 422
    lst = client.get(f"/v1/{ns}/redaction/packs").json()["packs"]
    assert {"kh-default@1", "mine@1"} <= {p["ref"] for p in lst}
    assert client.get(f"/v1/{ns}/redaction/packs/mine").json()["owner"] == ns
    # select it
    s = client.put(f"/v1/{ns}/redaction", json={"packs": ["mine@1", "ghost"]})
    assert s.status_code == 200, s.text
    body = s.json()
    assert body["source"] == "api" and body["rev"] == 1 and body["missing"] == ["ghost"]
    assert [e["ref"] for e in body["effective"]] == ["kh-default@1", "mine@1"]
    conflict = client.put(f"/v1/{ns}/redaction", json={"packs": ["mine"]}, headers={"If-Match": '"9"'})
    assert conflict.status_code == 409
    assert client.put(f"/v1/{ns}/redaction", json={"packs": ["mine"]}, headers={"If-Match": '"1"'}).json()["rev"] == 2
    assert client.put(f"/v1/{ns}/redaction", json={"packs": ["a"] * 9}).status_code == 422
    sc = client.post(f"/v1/{ns}/redaction:scan", json={"text": "TCK-9999 by bob@example.org BADGE#654321"}).json()
    assert sc["masked"] == "[REDACTED:ticket] by [REDACTED:email] [REDACTED:badge]"
    assert sc["refused"] is True and sc["packs"] == ["kh-default@1", "mine@1"]
    assert [f["name"] for f in sc["findings"]] == ["ticket", "email", "badge"]
    v = client.post(f"/v1/{ns}/redaction:verify", json={}).json()
    assert v["all_passed"] is True and [p["ref"] for p in v["packs"]] == ["kh-default@1", "mine@1"]
    d = client.delete(f"/v1/{ns}/redaction")
    assert d.status_code == 200 and d.json()["source"] == "default"
    big = client.post(f"/v1/{ns}/redaction:scan", json={"text": "x" * (256 * 1024 + 1)})
    assert big.status_code == 413


def test_manifest_selection_and_scrub_events(client):
    manifest = {**EVENT_MANIFEST, "redaction": {"packs": ["mine@1"]}}
    ns, _, _ = attach(client, manifest=manifest)
    assert client.put(f"/v1/{ns}/redaction/packs/mine@1", json=OWN_PACK).status_code == 201
    redact.invalidate()
    sel = client.get(f"/v1/{ns}/redaction").json()
    assert sel["source"] == "manifest" and sel["missing"] == []
    r = client.post(f"/v1/{ns}/events", json={"kind": "step.done", "body": {"n": 1, "who": "ann@example.com",
                                                                            "note": "TCK-5555"}})
    assert r.status_code in (200, 201), r.text
    evs = client.get(f"/v1/{ns}/events", params={"kinds": "step.done"}).json()
    rows = evs.get("events") or evs.get("items") or []
    stored = [e for e in rows if e["kind"] == "step.done"][0]["body"]
    assert stored["who"] == "[REDACTED:email]" and stored["note"] == "[REDACTED:ticket]"
    red = client.get(f"/v1/{ns}/events", params={"kinds": "kh.redacted"}).json()
    rrows = [e for e in (red.get("events") or red.get("items") or []) if e["kind"] == "kh.redacted"]
    assert rrows and rrows[0]["body"]["count"] == 2
    assert {f["pointer"] for f in rrows[0]["body"]["findings"]} == {"/who", "/note"}
    assert "ann@example.com" not in str(rrows[0]["body"])
    refused = client.post(f"/v1/{ns}/events", json={"kind": "step.done", "body": {"n": 2, "who": "BADGE#111111"}})
    assert refused.status_code == 422 and refused.json()["error"] == "redaction_refused"


def test_switch_off_is_noop(client, monkeypatch):
    from app import caps

    ns, _, _ = attach(client)
    ctx = __import__("app.caps.core", fromlist=["make_ctx"]).make_ctx(ns)
    assert redact.scrub(ctx, "x", {"a": "jane@example.com"}) == {"a": "[REDACTED:email]"}
    monkeypatch.setattr(caps, "LOADED", [m for m in caps.LOADED if m.NAME != "redaction"])
    assert redact.scrub(ctx, "x", {"a": "jane@example.com"}) == {"a": "jane@example.com"}


def test_canary_hook(client):
    ns, _, _ = attach(client)
    ctx = __import__("app.caps.core", fromlist=["make_ctx"]).make_ctx(ns)

    def fake(ns_, text):
        i = text.find("khc-0123456789abcdef")
        return [] if i < 0 else [{"start": i, "end": i + 20, "vault": "v", "item": "it", "canary_kind": "kh"}]

    redact.register_canary(fake)
    try:
        out = redact.scrub(ctx, "records", {"t": "leak khc-0123456789abcdef here"})
        assert out == {"t": "leak [REDACTED:canary] here"}
        assert redact.scrub(ctx, "vault", {"t": "khc-0123456789abcdef"}) == {"t": "khc-0123456789abcdef"}
    finally:
        redact.CANARY_SCANNERS.remove(fake)
