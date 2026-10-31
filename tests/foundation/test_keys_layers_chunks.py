from app import chunks, layers
from app import vector_store as vs
from app.retriever import clip_hit


def test_keys_add_merge_query_delete(client):
    r = client.post("/keys/add", json={"key": "billing-service", "aliases": ["Billing", "billing svc"]}).json()
    assert r == {"key": "billing-service", "aliases": ["Billing", "billing svc"], "dimension": 384}
    r = client.post("/keys/add", json={"key": "billing-service", "aliases": ["BILLING", "invoicer"]}).json()
    assert r["aliases"] == ["Billing", "billing svc", "invoicer"]  # merged, case-insensitive dedupe
    r = client.post("/keys/add", json={"key": "billing-service", "aliases": ["only"], "merge": False}).json()
    assert r["aliases"] == ["only"]
    client.post("/keys/add", json={"key": "auth-service", "aliases": ["login"], "project": "kp"})
    assert vs.collection_exists("kh_keys_kp")
    hits = client.post("/keys/query", json={"text": "billing service", "k": 2}).json()["hits"]
    assert hits[0]["key"] == "billing-service" and hits[0]["aliases"] == ["only"]
    assert set(hits[0]) == {"key", "score", "aliases", "source_text"}
    phits = client.post("/keys/query", json={"text": "login", "project": "kp"}).json()["hits"]
    assert [h["key"] for h in phits] == ["auth-service"]
    d = client.post("/keys/delete", json={"keys": ["billing-service", "nope"]}).json()
    assert d == {"status": "ok", "requested": 2, "deleted": ["billing-service"], "not_found": ["nope"]}


def test_chunk_windows():
    assert chunks.windows("x" * 1800) == []
    text = "".join(chr(65 + (i % 26)) for i in range(3000))
    w = chunks.windows_at(text)
    assert [s for s, _ in w] == [700, 1400, 2100, 2800]
    assert all(len(p) == 800 for _, p in w[:3]) and len(w[3][1]) == 200
    # a short trailing window (< 200 non-space chars) is dropped unless it is the first
    text2 = "y" * 2250  # last window [2100:2250] has 150 chars -> dropped
    assert [s for s, _ in chunks.windows_at(text2)] == [700, 1400]
    text3 = "z" * 700 + " " * 1200  # blank windows: only the first is kept
    assert [s for s, _ in chunks.windows_at(text3)] == [700]


def test_clip_hit_head_plus_piece():
    text = "H" * 1000 + "the zebra lives here and the zebra sleeps" + "T" * 1000
    piece = text[900:1700]
    hit = {"source_text": text, "piece": piece}
    clip_hit(hit, 400, "where does the zebra sleep")
    assert hit["truncated"] is True and hit["text_len"] == len(text)
    assert hit["source_text"].startswith("H" * 160 + " [...] ")
    assert hit["piece_at"] is not None and "zebra" in hit["source_text"]
    plain = {"source_text": text}
    clip_hit(plain, 300, None)
    assert plain["source_text"] == text[:300] and plain["piece_at"] is None
    short = {"source_text": "short"}
    clip_hit(short, 300, None)
    assert short["truncated"] is False


def test_layers_helpers():
    assert layers.slug("Hello, World!").startswith("kb_hello_world_")
    assert layers.slug("Hello", "subj").startswith("subj_hello_")
    assert layers.should_layer(" ".join(["w"] * 25)) and not layers.should_layer("few words")
    hits = [{"score": s} for s in (0.9, 0.85, 0.6, 0.55)]
    assert len(layers.dynamic_k(hits)) == 2  # drop 0.25 > 0.15
    assert len(layers.dynamic_k([{"score": 0.2}])) == 1  # never empty
    assert len(layers.dynamic_k([{"score": 0.5}, {"score": 0.29}])) == 1
    rows = [
        {"vector_id": "a", "metadata": {"subject_key": "s", "lv": 1, "author": "agent:kh", "verified_count": 5}},
        {"vector_id": "b", "metadata": {"subject_key": "s", "lv": 1, "author": "user:jane", "verified_count": 0}},
        {"vector_id": "c", "metadata": {"subject_key": "s", "lv": 2, "author": "agent:x", "ts": "2026-01-01T00:00:00Z"}},
        {"vector_id": "d", "metadata": {"subject_key": "s", "lv": 2, "author": "agent:y", "ts": "2026-02-01T00:00:00Z"}},
    ]
    assert [h["vector_id"] for h in layers.reconcile(rows)] == ["b", "d"]


def test_layers_routes(client):
    r = client.post("/layers/add", json={"text": "Payments settle nightly via the ledger batch.",
                                         "subject_key": "payments", "project": "lay"}).json()
    assert r["records"][0]["id"] == r["tree_id"] + "_lv1" and r["attached_to"] is None
    r2 = client.post("/layers/add", json={"text": "Payments settle every night in a batch job.",
                                          "subject_key": "payments", "author": "user:jane", "project": "lay"}).json()
    assert r2["attached_to"] == r["tree_id"]
    q = client.post("/layers/query", json={"text": "payments settle nightly", "project": "lay", "k": 5}).json()
    assert q["hits"] and set(q["hits"][0]) == {"vector_id", "score", "text", "lv", "tree_id", "parent_id",
                                               "subject_key"}
    g = client.post("/layers/gather", json={"subject_key": "payments", "project": "lay"}).json()
    assert [h["vector_id"] for h in g["hits"]] == [r2["records"][0]["id"]]  # user beats agent
    g = client.post("/layers/gather", json={"subject_key": "payments", "project": "lay", "reconcile": False}).json()
    assert len(g["hits"]) == 2
    # layer rows are invisible to /vector/query (no knowledge_type=quick)
    vq = client.post("/vector/query", json={"text": "payments settle nightly", "project": "lay",
                                            "scope": "knowledge", "k": 5}).json()
    assert vq["hits"] == []
    assert client.post("/layers/query", json={"text": "x", "project": "nolayers"}).json() == {"hits": []}
