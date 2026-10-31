import hashlib

from app import vector_store as vs


def test_health_and_version(client):
    assert client.get("/health").json() == {"status": "ok", "model": "kh-hash-384"}
    v = client.get("/version").json()
    assert v["version"] == "v6.5" and v["source"] in ("file:VERSION", "default")


def test_id_is_vec_sha256_prefix(client):
    text = "The billing service retries failed charges three times."
    r = client.post("/vector/add", json={"text": text, "project": "idproj"})
    assert r.status_code == 200
    body = r.json()
    assert body == {"status": "ok", "vector_id": "vec_" + hashlib.sha256(text.encode()).hexdigest()[:12],
                    "dimension": 384}
    # same text -> same id -> overwrite, not a second row
    client.post("/vector/add", json={"text": text, "project": "idproj"})
    assert vs.get_existing("kh_know_idproj").count() == 1


def test_add_query_scroll_delete_roundtrip(client, fresh_store):
    texts = ["Deploys run through the blue green pipeline on Fridays.",
             "The cache layer uses redis with a ten minute ttl for every session key.",
             "Invoices are archived after ninety days."]
    ids = []
    for i, t in enumerate(texts):
        r = client.post("/vector/add", json={"text": t, "project": "rt", "metadata": {"topic": f"t{i}", "ts": 1000 + i}})
        assert r.status_code == 200
        ids.append(r.json()["vector_id"])

    q = client.post("/vector/query", json={"text": "redis cache ttl", "project": "rt", "scope": "knowledge", "k": 2})
    hits = q.json()["hits"]
    assert hits[0]["vector_id"] == ids[1]
    assert set(hits[0]) == {"vector_id", "score", "source_text", "summary", "truncated", "text_len", "piece_at"}
    assert 0 < hits[0]["score"] <= 1.0

    # min_score filter and max_chars clipping
    q = client.post("/vector/query", json={"text": "redis cache ttl", "project": "rt", "scope": "knowledge",
                                           "k": 3, "min_score": 0.99})
    assert q.json()["hits"] == []
    q = client.post("/vector/query", json={"text": "redis cache ttl", "project": "rt", "scope": "knowledge",
                                           "k": 1, "max_chars": 50})
    h = q.json()["hits"][0]
    assert h["truncated"] is True and len(h["source_text"]) == 50 and h["text_len"] > 50

    s = client.post("/vector/scroll", json={"scope": "knowledge", "project": "rt", "limit": 2}).json()
    assert s["matched"] == 3 and s["count"] == 2 and s["limit"] == 2
    s2 = client.post("/vector/scroll", json={"scope": "knowledge", "project": "rt", "limit": 2, "offset": 2}).json()
    assert s2["count"] == 1
    newest = client.post("/vector/scroll", json={"scope": "knowledge", "project": "rt", "order": "newest"}).json()
    assert [h["vector_id"] for h in newest["hits"]] == list(reversed(ids))
    by_ids = client.post("/vector/scroll", json={"scope": "knowledge", "project": "rt",
                                                 "ids": [ids[2], "nope", ids[0]]}).json()
    assert [h["vector_id"] for h in by_ids["hits"]] == [ids[2], ids[0]] and by_ids["matched"] == 2
    filt = client.post("/vector/scroll", json={"scope": "knowledge", "project": "rt",
                                               "where": {"topic": {"$eq": "t0"}}}).json()
    assert [h["vector_id"] for h in filt["hits"]] == [ids[0]]

    d = client.post("/vector/delete", json={"ids": [ids[0], "missing"], "dry_run": True}).json()
    assert d["found"] == {ids[0]: "kh_know_rt"} and d["not_found"] == ["missing"] and d["deleted"] == 0
    d = client.request("DELETE", "/vector/delete", json={"ids": [ids[0]], "scope": "knowledge", "project": "rt"}).json()
    assert d["deleted"] == 1 and d["dry_run"] is False
    s = client.post("/vector/scroll", json={"scope": "knowledge", "project": "rt"}).json()
    assert s["matched"] == 2


def test_metadata_stamps_and_disagree(client):
    r = client.post("/vector/add", json={"text": "stamp me", "project": "stampp"}, headers={"X-KH-Actor": "cli-1"})
    rid = r.json()["vector_id"]
    row = vs.get_ids_vt(vs.get_existing("kh_know_stampp"), [rid])[0]
    m = row["metadata"]
    assert m["knowledge_type"] == "quick" and m["scope"] == "knowledge" and m["project"] == "stampp"
    assert m["actor"] == "cli-1" and m["source_text"] == "stamp me"
    assert m["emb_dim"] == 384 and m["emb_ver"] == "v6.5" and m["emb_model"]
    bad = client.post("/vector/add", json={"text": "x", "project": "a", "metadata": {"project": "b"}})
    assert bad.status_code == 422
    assert bad.json()["detail"] == "project 'a' and metadata.project 'b' disagree; give one"
    bad = client.post("/vector/add", json={"text": "x", "project": "a", "metadata": {"source_id": "c"}})
    assert bad.json()["detail"] == "project 'a' and metadata.source_id 'c' disagree; give one"


def test_scope_routing_to_project_collections(client):
    client.post("/vector/add", json={"text": "finding one", "scope": "findings", "project": "route1"})
    client.post("/vector/add", json={"text": "lesson one", "scope": "lesson", "project": "route1"})
    client.post("/vector/add", json={"text": "policy one", "scope": "policy", "project": "route1"})
    client.post("/vector/add", json={"text": "no project knowledge row"})
    client.post("/vector/add", json={"text": "meta alias row", "project": "kh-meta"})
    client.post("/vector/add", json={"text": "unscoped row", "scope": ""})
    client.post("/vector/add", json={"text": "my prefs", "scope": "personalization", "project": "personal-a-b-c"})
    client.post("/vector/add", json={"text": "common prefs", "scope": "personalization"})
    names = vs.list_collection_names()
    for n in ("kh_find_route1", "kh_lesson_route1", "kh_policy_route1", "kh_know_orchestrator",
              "kh_personalization_personal-a-b-c"):
        assert n in names
    assert vs.get_ids_vt(vs.shared_collection(),
                         ["vec_" + hashlib.sha256(b"unscoped row").hexdigest()[:12]])
    orch = vs.get_existing("kh_know_orchestrator")
    docs = {r["source_text"] for r in vs.get_vt(orch)}
    assert {"no project knowledge row", "meta alias row", "common prefs"} <= docs
    # pure routing helpers
    assert vs.scope_collection_name("", "x") is None
    assert vs.scope_collection_name("knowledge", None) == "kh_know_orchestrator"
    assert vs.scope_collection_name("log", "p") == "kh_log_p"
    assert vs.scope_collection_name("findings", "p") == "kh_find_p"
    assert vs.scope_collection_name("entity", "p") == "kh_entity_p"
    assert vs.scope_collection_name("self-info", "p") == "kh_know_orchestrator"
    assert vs.scope_collection_name("personalization", "personal-x") == "kh_personalization_personal-x"
    # no-project read fans out over every existing part of the scope
    q = client.post("/vector/query", json={"text": "finding one", "scope": "findings", "k": 5}).json()
    assert any(h["source_text"] == "finding one" for h in q["hits"])


def test_long_row_gets_chunks_and_query_finds_piece(client):
    filler = " ".join(f"alpha{i} beta gamma" for i in range(150))
    text = filler + " The secret zebra keyword lives deep inside this long document. " + filler
    r = client.post("/vector/add", json={"text": text, "project": "chunky"})
    rid = r.json()["vector_id"]
    ch = vs.get_existing("kh_chunk_chunky")
    rows = vs.get_vt(ch)
    assert rows and all(x["metadata"]["chunk_of"] == rid for x in rows)
    assert [x["vector_id"] for x in rows][0] == f"{rid}#c1"
    re_ = client.post("/vector/rechunk", json={"scope": "knowledge", "project": "chunky"}).json()
    assert re_["dry_run"] is True and re_["long_rows"] == 1
    assert client.post("/vector/rechunk", json={}).status_code == 400
    d = client.post("/vector/delete", json={"ids": [rid]}).json()
    assert d["deleted"] == 1
    assert vs.get_vt(ch) == []


def test_topics_and_backfill(client, fresh_store):
    for i, t in enumerate(["a", "a", "b"]):
        client.post("/vector/add", json={"text": f"topic row {i}", "project": "tp", "metadata": {"topic": t}})
    client.post("/vector/add", json={"text": "topic row none", "project": "tp"})
    t = client.get("/vector/topics", params={"scope": "knowledge", "project": "tp"}).json()
    assert t["topics"] == [{"value": "a", "count": 2}, {"value": "b", "count": 1}]
    assert t["missing_field"] == 1 and t["total_scanned"] == 4 and t["distinct"] == 2
    client.post("/vector/add", json={"text": "legacy row topic:deploy-notes here", "scope": ""})
    b = client.post("/vector/backfill-metadata", json={}).json()
    assert b["mismatched"] == 1 and b["applied"] == 0
    b = client.post("/vector/backfill-metadata", json={"apply": True}).json()
    assert b["applied"] == 1 and b["changes"][0]["to"] == "deploy-notes"


def test_vector_chunk_route(client):
    r = client.post("/vector/chunk", json={"text": "Cats purr softly. Cats sleep a lot. Rockets need fuel to launch."})
    out = r.json()
    assert r.status_code == 200 and out and all(len(c["embedding"]) == 384 for c in out)


def test_knowledge_delete(client):
    r = client.post("/vector/add", json={"text": "to be removed", "project": "kd"})
    rid = r.json()["vector_id"]
    d = client.request("DELETE", "/knowledge", json={"ids": [rid, "ghost"], "project": "kd"}).json()
    assert d["vectors_deleted"] == 1 and d["not_found"] == ["ghost"] and d["requested"] == 2
    assert client.request("DELETE", "/knowledge/orphans").json() == {"documents_deleted": 0}


def test_graph_add_query(client):
    body = {"entities": [{"core_id": "svc_billing", "label": "Billing", "type": "System"},
                         {"core_id": "svc_platform", "label": "Platform", "type": "System"},
                         {"core_id": "act_charge", "label": "Charge card", "type": "Action"}],
            "edges": [{"source_core_id": "svc_billing", "relation": "IS_PART_OF", "target_core_id": "svc_platform"},
                      {"source_core_id": "act_charge", "relation": "PERFORMED_BY", "target_core_id": "svc_billing"}]}
    r = client.post("/graph/add", json=body).json()
    assert r["entities_inserted"] == 3 and r["edges_inserted"] == 2 and r["vectors_generated"] == 3
    q = client.post("/graph/query", json={"core_ids": ["svc_platform"]}).json()
    ids = {n["core_id"] for n in q["nodes"]}
    assert {"svc_platform", "svc_billing", "act_charge"} <= ids
    assert len(q["edges"]) == 2
    bad = client.post("/graph/add", json={"edges": [{"source_core_id": "a", "relation": "lower", "target_core_id": "b"}]})
    assert bad.status_code == 422


def test_vision_unavailable(client):
    assert client.post("/vision/locate-ui").status_code == 503
