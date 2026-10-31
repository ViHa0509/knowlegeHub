"""§6.12 packs: versions, freeze, addenda, quotes + re-anchoring, assumptions, duplicate questions."""
from __future__ import annotations

from urllib.parse import quote as urlq

from app.caps import packs
from tests.caps.conftest import attach

DOC1 = "The upload limit is 10 MB per file.\n\nFiles above the limit are rejected. Admins can raise it."
DOC2 = "Intro paragraph added in version two.\n\n" + DOC1


def _env(client, manifest=None):
    ns, projects, _ = attach(client, manifest=manifest)
    return ns, projects[0]


def _events(client, ns, kind):
    return [e for e in client.get(f"/v1/{ns}/events", params={"limit": 1000}).json()["events"] if e["kind"] == kind]


def _new(client, ns, pack="rfp", parts=None, **kw):
    body = {"parts": parts if parts is not None else [{"path": "spec.md", "text": DOC1}], **kw}
    return client.post(f"/v1/{ns}/packs/{pack}/versions", json=body)


def test_helpers():
    assert packs.normalize("a  \r\nb\t\r\n\n\x00") == "a\nb"
    assert packs.parse_label("v3.2") == (3, 2) and packs.parse_label("v4") == (4, 0)
    assert packs.contrast("Is the limit above 10 MB?", "Is the limit below 10 MB?").startswith("opposite")
    assert packs.contrast("Can we upload 10 files?", "Can we upload 12 files?") == "numbers"
    assert packs.contrast("Can we upload ten files?", "Can we upload 10 files?") is None
    assert packs.contrast("Is login required?", "Isn't login required?") == "negation"
    assert packs.contrast("Does it enable caching?", "Does it disable caching?").startswith("opposite")
    assert packs.contrast("What is the upload limit?", "What is the limit for uploads?") is None
    spans = packs.chunk_spans("word " * 700)
    assert all(e - s <= packs.CHUNK_CHARS for s, e in spans) and len(spans) >= 3


def test_versions_freeze_and_read(client):
    ns, project = _env(client)
    r = _new(client, ns, title="RFP 1")
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["label"] == "v1.0" and v["state"] == "draft" and v["parts_n"] == 1 and v["project"] == project
    assert v["sha256"] == packs.version_sha({("spec.md", 1): (packs.core.sha256_text(packs.normalize(DOC1)),
                                                              len(DOC1))})
    p = client.patch(f"/v1/{ns}/packs/rfp/versions/v1.0", headers={"If-Match": '"1"'},
                     json={"parts": [{"path": "annex.md", "page": 2, "text": "Annex text."}]})
    assert p.status_code == 200 and p.json()["parts_n"] == 2 and p.json()["rev"] == 2
    stale = client.patch(f"/v1/{ns}/packs/rfp/versions/v1.0", headers={"If-Match": '"1"'}, json={"remove": ["x"]})
    assert stale.status_code == 409 and stale.json()["error"] == "write_conflict"
    p2 = client.patch(f"/v1/{ns}/packs/rfp/versions/v1.0", json={"remove": [{"path": "annex.md"}]})
    assert p2.json()["parts_n"] == 1
    f = client.post(f"/v1/{ns}/packs/rfp/versions/v1.0:freeze")
    assert f.status_code == 200, f.text
    fv = f.json()
    assert fv["state"] == "frozen" and fv["quotes"] >= 1 and fv["index_state"] == "done" and fv["superseded"] is None
    again = client.post(f"/v1/{ns}/packs/rfp/versions/v1.0:freeze")
    assert again.status_code == 409 and again.json()["error"] == "pack_immutable"
    imm = client.patch(f"/v1/{ns}/packs/rfp/versions/v1.0", json={"remove": ["spec.md"]})
    assert imm.status_code == 409 and imm.json()["error"] == "pack_immutable"
    g = client.get(f"/v1/{ns}/packs/rfp", params={"text": "true"}).json()
    assert g["latest_version"] == "v1.0" and g["title"] == "RFP 1" and g["parts"][0]["text"] == packs.normalize(DOC1)
    assert "text" not in client.get(f"/v1/{ns}/packs/rfp").json()["parts"][0]
    assert [x["pack_id"] for x in client.get(f"/v1/{ns}/packs").json()["packs"]] == ["rfp"]
    assert [x["label"] for x in client.get(f"/v1/{ns}/packs/rfp/versions").json()["versions"]] == ["v1.0"]
    assert client.get(f"/v1/{ns}/packs/nope").status_code == 404
    assert client.get(f"/v1/{ns}/packs/rfp", params={"version": "v9.0"}).status_code == 404
    assert _events(client, ns, "kh.pack_frozen")[-1]["body"]["version"] == "v1.0"
    assert _events(client, ns, "kh.pack_version")
    # validation
    assert _new(client, ns, "bad id").status_code == 422
    assert _new(client, ns, "x", parts=[{"path": "a#b", "text": "t"}]).status_code == 422
    assert _new(client, ns, "x", parts=[{"path": "a", "page": -1, "text": "t"}]).status_code == 422
    assert _new(client, ns, "x", parts=[{"path": "a", "text": "t"}, {"path": "a", "text": "u"}]).status_code == 422
    assert client.post(f"/v1/{ns}/packs/rfp/versions/v1.0:fly").status_code == 405
    # reindex
    ri = client.post(f"/v1/{ns}/packs/rfp/versions/v1.0:reindex")
    assert ri.status_code == 200 and ri.json()["index_state"] == "done"


def test_quotes_reanchor_and_resolve(client):
    ns, _ = _env(client)
    assert _new(client, ns, freeze=True).json()["state"] == "frozen"
    off = packs.normalize(DOC1).index("Files above")
    q = client.post(f"/v1/{ns}/packs/rfp/quotes", json={"path": "spec.md", "text": "Files above the limit"})
    assert q.status_code == 201, q.text
    qv = q.json()
    assert qv["quote_id"] == f"spec.md#1#{off}" and qv["version"] == "v1.0" and qv["origin"] == "cite"
    same = client.post(f"/v1/{ns}/packs/rfp/quotes", json={"path": "spec.md", "offset": off, "length": 21})
    assert same.status_code == 200 and same.json()["quote_id"] == qv["quote_id"]
    longer = client.post(f"/v1/{ns}/packs/rfp/quotes", json={"path": "spec.md", "offset": off, "length": 25})
    assert longer.status_code == 201 and longer.json()["quote_id"] == f"spec.md#1#{off}~25"
    gone = client.post(f"/v1/{ns}/packs/rfp/quotes", json={"path": "spec.md", "text": "Admins can raise it."})
    assert gone.status_code == 201
    assert client.post(f"/v1/{ns}/packs/rfp/quotes", json={"path": "spec.md", "text": "absent"}).status_code == 404
    assert client.post(f"/v1/{ns}/packs/rfp/quotes", json={"path": "spec.md", "offset": 0,
                                                            "length": 9999}).status_code == 422
    lst = client.get(f"/v1/{ns}/packs/rfp/quotes", params={"path": "spec.md"}).json()["quotes"]
    assert {qv["quote_id"], longer.json()["quote_id"]} <= {x["quote_id"] for x in lst}

    # v2: base v1.0, text moved (re-anchor) and one sentence removed (orphan)
    doc2 = DOC2.replace(" Admins can raise it.", "")
    v2 = client.post(f"/v1/{ns}/packs/rfp/versions", json={"base": "v1.0", "parts": [{"path": "spec.md",
                                                                                      "text": doc2}]})
    assert v2.json()["label"] == "v2.0" and v2.json()["base_label"] == "v1.0"
    fz = client.post(f"/v1/{ns}/packs/rfp/versions/v2.0:freeze").json()
    assert fz["superseded"] == "v1.0" and fz["orphaned"] >= 1 and fz["reanchored"] >= 2
    vers = {x["label"]: x["state"] for x in client.get(f"/v1/{ns}/packs/rfp/versions").json()["versions"]}
    assert vers == {"v1.0": "superseded", "v2.0": "frozen"}

    res = client.get(f"/v1/{ns}/quotes/{urlq(qv['quote_id'], safe='')}", params={"pack": "rfp", "version": "v1.0"})
    assert res.status_code == 200, res.text
    rb = res.json()
    new_off = packs.normalize(doc2).index("Files above")
    assert rb["status"] == "reanchored" and rb["hops"] == 1 and rb["latest_version"] == "v2.0"
    assert rb["latest"]["quote_id"] == f"spec.md#1#{new_off}" and rb["latest"]["text"] == "Files above the limit"
    orphan = client.get(f"/v1/{ns}/quotes/{urlq(gone.json()['quote_id'], safe='')}", params={"pack": "rfp"}).json()
    assert orphan["status"] == "orphaned" and orphan["orphaned_at"] == "v2.0" and orphan["latest"] is None
    cur = client.get(f"/v1/{ns}/quotes/{urlq(rb['latest']['quote_id'], safe='')}", params={"pack": "rfp"}).json()
    assert cur["status"] == "current" and cur["hops"] == 0
    assert client.get(f"/v1/{ns}/quotes/nope", params={"pack": "rfp"}).status_code == 404

    # addendum v2.1 overlays v2.0; an addendum of the older v1.0 cannot freeze after v2.0
    ad = client.post(f"/v1/{ns}/packs/rfp/versions/v2.0/addenda",
                     json={"parts": [{"path": "faq.md", "text": "Q and A."}], "freeze": True})
    assert ad.status_code == 201 and ad.json()["label"] == "v2.1" and ad.json()["state"] == "frozen"
    assert ad.json()["parts_n"] == 2
    old = client.post(f"/v1/{ns}/packs/rfp/versions/v1.0/addenda", json={"parts": [{"path": "z", "text": "z"}]})
    assert old.json()["label"] == "v1.1"
    vo = client.post(f"/v1/{ns}/packs/rfp/versions/v1.1:freeze")
    assert vo.status_code == 409 and vo.json()["error"] == "version_order"
    two_hops = client.get(f"/v1/{ns}/quotes/{urlq(qv['quote_id'], safe='')}",
                          params={"pack": "rfp", "version": "v1.0"}).json()
    assert two_hops["hops"] == 2 and two_hops["status"] == "reanchored" and two_hops["latest_version"] == "v2.1"


def test_assumptions(client):
    ns, _ = _env(client, manifest={"settings": {"packs": {"require_if_match": False}}})
    _new(client, ns, freeze=True)
    qid = client.post(f"/v1/{ns}/packs/rfp/quotes", json={"path": "spec.md", "text": "10 MB"}).json()["quote_id"]
    a = client.post(f"/v1/{ns}/packs/rfp/assumptions", json={"text": "Limit applies per file", "quote_id": qid})
    assert a.status_code == 201 and a.json()["code"] == "ASSUMED-1" and a.json()["quote_ver"] == "v1.0"
    b = client.post(f"/v1/{ns}/packs/rfp/assumptions", json={"text": "Second"})
    assert b.json()["code"] == "ASSUMED-2"
    assert client.post(f"/v1/{ns}/packs/rfp/assumptions", json={"text": "x", "quote_id": "nope"}).status_code == 422
    c = client.patch(f"/v1/{ns}/packs/rfp/assumptions/ASSUMED-1", json={"status": "confirmed"})
    assert c.status_code == 200 and c.json()["status"] == "confirmed"
    t = client.patch(f"/v1/{ns}/packs/rfp/assumptions/ASSUMED-1", json={"text": "Limit applies per upload"})
    assert t.json()["status"] == "open"  # content change drops to open
    s = client.patch(f"/v1/{ns}/packs/rfp/assumptions/ASSUMED-1", json={"superseded_by": "ASSUMED-2"})
    assert s.json()["status"] == "superseded" and s.json()["superseded_by"] == "ASSUMED-2"
    fin = client.patch(f"/v1/{ns}/packs/rfp/assumptions/ASSUMED-1", json={"status": "open"})
    assert fin.status_code == 409 and fin.json()["error"] == "assumption_final"
    conflict = client.patch(f"/v1/{ns}/packs/rfp/assumptions/ASSUMED-2", json={"note": "n"},
                            headers={"If-Match": '"7"'})
    assert conflict.status_code == 409
    lst = client.get(f"/v1/{ns}/packs/rfp/assumptions", params={"status": "open"}).json()["assumptions"]
    assert [x["code"] for x in lst] == ["ASSUMED-2"]
    assert client.get(f"/v1/{ns}/packs/rfp/assumptions/ASSUMED-9").status_code == 404
    assert client.get(f"/v1/{ns}/packs/rfp/assumptions/ASSUMED-2").headers["ETag"] == '"1"'
    assert {e["body"]["status"] for e in _events(client, ns, "kh.assumption")} >= {"open", "confirmed", "superseded"}


def test_questions(client):
    ns, _ = _env(client)
    _new(client, ns)
    q1 = client.post(f"/v1/{ns}/packs/rfp/questions:lookup", json={"text": "What is the maximum upload size per file?"})
    assert q1.status_code == 201, q1.text
    assert q1.json()["question_id"] == "Q-1" and q1.json()["duplicate_of"] is None and q1.json()["indexed"] is True
    ex = client.post(f"/v1/{ns}/packs/rfp/questions:lookup", json={"text": "what is the MAXIMUM upload size per file"})
    assert ex.status_code == 200 and ex.json()["duplicate_of"] == "Q-1" and ex.json()["match"] == "exact"
    assert ex.json()["score"] == 1.0
    sem = client.post(f"/v1/{ns}/packs/rfp/questions:lookup",
                      json={"text": "What is the maximum upload size per file please?", "threshold": 0.8})
    assert sem.status_code == 200 and sem.json()["duplicate_of"] == "Q-1" and sem.json()["match"] == "semantic"
    opp = client.post(f"/v1/{ns}/packs/rfp/questions:lookup",
                      json={"text": "What is the minimum upload size per file?", "threshold": 0.8})
    assert opp.status_code == 201 and opp.json()["question_id"] == "Q-2"
    assert opp.json()["candidates"] and opp.json()["candidates"][0]["contrast"].startswith("opposite")
    nostore = client.post(f"/v1/{ns}/packs/rfp/questions:lookup", json={"text": "Who signs the contract?",
                                                                       "store": False})
    assert nostore.status_code == 200 and nostore.json()["duplicate_of"] is None
    assert client.post(f"/v1/{ns}/packs/rfp/questions:lookup", json={"text": "x", "threshold": 0.5}).status_code == 422
    g = client.get(f"/v1/{ns}/packs/rfp/questions/Q-1").json()
    assert g["asked_n"] == 3
    ans = client.patch(f"/v1/{ns}/packs/rfp/questions/Q-1", json={"answer": "10 MB"})
    assert ans.json()["status"] == "answered" and ans.json()["answer"] == "10 MB"
    dup = client.post(f"/v1/{ns}/packs/rfp/questions:lookup", json={"text": "What is the maximum upload size per file?"})
    assert dup.json()["answer"] == "10 MB" and dup.json()["status"] == "answered"
    client.patch(f"/v1/{ns}/packs/rfp/questions/Q-1", json={"status": "closed"})
    re = client.post(f"/v1/{ns}/packs/rfp/questions:lookup", json={"text": "What is the maximum upload size per file?"})
    assert re.status_code == 201 and re.json()["reopened"] is True and re.json()["status"] == "open"
    assert [x["question_id"] for x in client.get(f"/v1/{ns}/packs/rfp/questions",
                                                 params={"status": "open"}).json()["questions"]] == ["Q-1", "Q-2"]
    assert client.patch(f"/v1/{ns}/packs/rfp/questions/Q-1", json={"status": "weird"}).status_code == 422
    assert client.get(f"/v1/{ns}/packs/rfp/questions/Q-9").status_code == 404
    assert len(_events(client, ns, "kh.question_duplicate")) == 3


def test_retrieval_provider_and_roles(client, monkeypatch):
    from app.caps import core
    from tests.auth_helpers import enable_signin

    ns, project = _env(client)
    _new(client, ns, freeze=True)
    ctx = core.make_ctx(ns)
    rows = packs.retrieval_provider(ctx, {"project": project, "query": "upload limit per file", "limit": 3})
    assert rows and rows[0]["id"].startswith("rfp@v1.0#spec.md#1#") and "spec.md p1:" in rows[0]["text"]

    def tok(pid, role):
        client.post(f"/v1/{ns}/principals", json={"role": role, "principal_id": pid})
        return {"Authorization": f"Bearer {client.post(f'/v1/{ns}/principals/{pid}/token').json()['access_token']}"}

    agent, orch = tok("ag", "agent"), tok("or", "orchestrator")
    enable_signin(monkeypatch)
    assert client.get(f"/v1/{ns}/packs/rfp", headers=agent).status_code == 200
    assert client.post(f"/v1/{ns}/packs/rfp/versions", json={"parts": []}, headers=agent).status_code == 403
    assert client.post(f"/v1/{ns}/packs/rfp/versions", json={"parts": []}, headers=orch).status_code == 201
    a = client.post(f"/v1/{ns}/packs/rfp/assumptions", json={"text": "t"}, headers=orch).json()["code"]
    no = client.patch(f"/v1/{ns}/packs/rfp/assumptions/{a}", json={"status": "confirmed"}, headers=orch)
    assert no.status_code == 403
