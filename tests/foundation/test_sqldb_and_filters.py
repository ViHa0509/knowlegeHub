import pytest

from app import projects, sqldb
from app.vector_filter import matches_document, matches_where, sql_filter
from app.vector_sqlite import SqliteVectorClient


# ---------------------------------------------------------------------------
# sqldb
# ---------------------------------------------------------------------------


def test_counters():
    with sqldb.connect() as c:
        a = sqldb.counter_add(c, "t:counter", 1)
        b = sqldb.counter_add(c, "t:counter", 5)
        assert b == a + 5
        assert sqldb.counter_get(c, "t:counter") == b
        assert sqldb.counter_get(c, "t:none") == 0


def test_migrate_runs_once_in_order():
    calls = []

    def mk(v):
        def step(c):
            calls.append(v)
            c.execute("CREATE TABLE IF NOT EXISTS t_mig (x INTEGER)")
        return step

    with sqldb.connect() as c:
        assert sqldb.migrate(c, "testmod", [(2, mk(2)), (1, mk(1))]) == [1, 2]
        assert sqldb.migrate(c, "testmod", [(1, mk(1)), (2, mk(2)), (3, mk(3))]) == [3]
    assert calls == [1, 2, 3]


def test_leases_and_fencing():
    with sqldb.connect() as c:
        l1 = sqldb.lease_acquire(c, "t", "job", "h1", 60_000)
        assert l1 and l1["epoch"] == 1
        assert sqldb.lease_acquire(c, "t", "job", "h2", 60_000) is None  # held
        again = sqldb.lease_acquire(c, "t", "job", "h1", 60_000)  # renew by same holder keeps epoch
        assert again["epoch"] == 1
        assert sqldb.lease_renew(c, "t", "job", "h1", 1, 60_000)
        assert not sqldb.lease_renew(c, "t", "job", "h1", 99, 60_000)
        fence = sqldb.fence_sql(c)
        assert c.execute(f"SELECT 1 WHERE {fence}", ("t", "job", 1)).fetchone() is not None
        assert sqldb.lease_release(c, "t", "job", "h1")
        assert c.execute(f"SELECT 1 WHERE {fence}", ("t", "job", 1)).fetchone() is None
        l2 = sqldb.lease_acquire(c, "t", "job", "h2", 60_000)  # expired -> new holder, epoch+1
        assert l2["epoch"] == 2
        sqldb.lease_bump_all(c, "t")
        assert sqldb.lease_get(c, "t", "job") == {"holder": None, "epoch": 3, "until_ms": l2["until_ms"]}
        # zero ttl lease is immediately takeable
        sqldb.lease_acquire(c, "t", "short", "h1", -1)
        assert sqldb.lease_acquire(c, "t", "short", "h2", 1000)["epoch"] == 2


def test_run_under_lease():
    assert sqldb.run_under_lease("cron.test", 30, lambda: 42) == 42
    with sqldb.connect() as c:
        c.execute("UPDATE kh_lease SET holder='someone-else' WHERE ns='_kh' AND name='cron.test'")
    assert sqldb.run_under_lease("cron.test", 30, lambda: 42) is None


def test_upsert_insert_new_and_columns():
    with sqldb.connect() as c:
        sqldb.create_table(c, "t_up", "CREATE TABLE IF NOT EXISTS t_up (k TEXT PRIMARY KEY, v TEXT)", "")
        sqldb.upsert(c, "t_up", ["k"], {"k": "a", "v": "1"})
        sqldb.upsert(c, "t_up", ["k"], {"k": "a", "v": "2"})
        assert c.execute("SELECT v FROM t_up WHERE k='a'").fetchone()["v"] == "2"
        assert sqldb.insert_new(c, "t_up", {"k": "b", "v": "x"}) is True
        assert sqldb.insert_new(c, "t_up", {"k": "b", "v": "y"}) is False
        assert sqldb.add_column(c, "t_up", "extra", "TEXT") is True
        assert sqldb.add_column(c, "t_up", "extra", "TEXT") is False
        sqldb.create_index(c, "ix_t_up_v", "t_up", ["v"])
        assert sqldb.now_ms(c) > 1_600_000_000_000


# ---------------------------------------------------------------------------
# projects data layer
# ---------------------------------------------------------------------------


def test_projects_data_layer(client):
    with pytest.raises(ValueError):
        projects.validate_id("  ")
    with pytest.raises(ValueError):
        projects.validate_id("GraphIfy__x")
    assert projects.validate_id(" kh-meta ") == "orchestrator"
    b = projects.bundle_of("p1")
    assert (b["knowledge"], b["log"], b["findings"], b["policy"], b["vector"]) == (
        "kh_know_p1", "kh_log_p1", "kh_find_p1", "kh_policy_p1", "kh_src_p1")
    e = projects.get_or_create_project("dl-proj", aliases=["DL"], keywords=["data"], descriptor="first")
    e2 = projects.get_or_create_project("dl-proj", aliases=["Data Layer", "DL"], source="AI-SDLC")
    assert e2["aliases"] == ["DL", "Data Layer"] and e2["descriptor"] == "first"
    assert e2["created_at"] == e["created_at"] and e2["source"] == "AI-SDLC"
    assert projects.read_counter("dl-proj") == {"request_total": 0, "issues": {}}
    assert projects.get_project("orchestrator")["role"] == "manage"
    s = projects.sync_orchestrator_registry()
    assert s["total"] >= 2 and s["written"] >= 1
    assert projects.sync_orchestrator_registry()["written"] == 0
    rid, text, meta = projects.registry_row(projects.get_project("dl-proj"))
    assert rid == "project_registry__dl-proj" and text.startswith("PROJECT dl-proj (role project). first")
    assert meta["kind"] == "project-registry" and meta["terms"].split("|")[0] == "data layer"
    client.post("/vector/add", json={"text": "doomed", "project": "dl-proj"})
    with pytest.raises(ValueError):
        projects.delete_project("orchestrator", confirm=True)
    prev = projects.delete_project("dl-proj")
    assert prev["preview"] is True and "kh_know_dl-proj" in prev["collections"]
    rep = projects.delete_project("dl-proj", confirm=True)
    assert "kh_know_dl-proj" in rep["collections_dropped"] and rep["registry_row_deleted"] is True
    assert projects.get_project("dl-proj") is None


# ---------------------------------------------------------------------------
# where / where_document semantics
# ---------------------------------------------------------------------------

META = {"scope": "log", "project": "p", "n": 5, "f": 1.5, "flag": True, "tags": "a"}


@pytest.mark.parametrize("where,expected", [
    ({}, True),
    ({"scope": "log"}, True),
    ({"scope": {"$eq": "log"}}, True),
    ({"scope": {"$ne": "log"}}, False),
    ({"missing": {"$ne": "x"}}, False),          # a missing field never matches
    ({"missing": {"$nin": ["x"]}}, False),
    ({"project": {"$in": ["p", "q"]}}, True),
    ({"project": {"$nin": ["p", "q"]}}, False),
    ({"n": {"$gt": 4}}, True),
    ({"n": {"$gte": 5, "$lt": 6}}, True),
    ({"n": {"$lte": 4}}, False),
    ({"n": {"$gt": "4"}}, False),                 # type mismatch never matches
    ({"n": {"$eq": 5}}, True),
    ({"flag": {"$eq": True}}, True),
    ({"flag": {"$eq": 1}}, False),                # bool is not int
    ({"$and": [{"scope": "log"}, {"n": {"$gt": 1}}]}, True),
    ({"$and": [{"scope": "log"}, {"n": {"$gt": 9}}]}, False),
    ({"$or": [{"scope": "x"}, {"n": {"$lt": 9}}]}, True),
    ({"$or": [{"scope": "x"}, {"n": {"$lt": 1}}]}, False),
])
def test_matches_where(where, expected):
    assert matches_where(META, where) is expected


def test_matches_document():
    doc = "Alpha beta Gamma"
    assert matches_document(doc, {"$contains": "beta"})
    assert not matches_document(doc, {"$contains": "gamma"})  # case-sensitive
    assert matches_document(doc, {"$not_contains": "delta"})
    assert matches_document(doc, {"$and": [{"$contains": "Alpha"}, {"$contains": "Gamma"}]})
    assert matches_document(doc, {"$or": [{"$contains": "zzz"}, {"$contains": "beta"}]})


def test_sql_filter_translation():
    jv = lambda k: f"J({k})"  # noqa: E731
    sql, params, exact = sql_filter({"scope": "log", "project": {"$in": ["a", "b"]}}, jv)
    assert sql == " AND J(scope) = ? AND J(project) IN (?, ?)" and params == ["log", "a", "b"] and exact
    sql, params, exact = sql_filter({"$and": [{"scope": {"$eq": "log"}}, {"n": {"$gt": 1}}]}, jv)
    assert params == ["log"] and not exact
    assert sql_filter({"$or": [{"a": "b"}]}, jv)[2] is False
    assert sql_filter({"bad-key": "x"}, jv)[2] is False


def test_sqlite_client_where_and_query(tmp_path):
    cl = SqliteVectorClient(str(tmp_path / "v.db"))
    col = cl.get_or_create_collection("c1")
    col.upsert(ids=["a", "b", "c", "d"],
               embeddings=[[1, 0, 0], [0, 1, 0], [0.9, 0.1, 0], [0, 0, 1]],
               documents=["apple pie", "banana split", "apple tart", "cherry"],
               metadatas=[{"kind": "x", "n": 1}, {"kind": "y", "n": 2}, {"kind": "x", "n": 3}, {"n": 4}])
    assert col.count() == 4
    assert col.get(where={"kind": "x"})["ids"] == ["a", "c"]
    assert col.get(where={"n": {"$gte": 2}}, limit=2)["ids"] == ["b", "c"]
    assert col.get(where={"kind": {"$ne": "x"}})["ids"] == ["b"]  # d has no kind -> no match
    assert col.get(where_document={"$contains": "apple"})["ids"] == ["a", "c"]
    assert col.get(ids=["c", "a", "zz"])["ids"] == ["c", "a"]
    assert col.get(limit=2, offset=1)["ids"] == ["b", "c"]
    res = col.query(query_embeddings=[[1, 0, 0]], n_results=2)
    assert res["ids"][0] == ["a", "c"] and res["distances"][0][0] == pytest.approx(0.0, abs=1e-6)
    res = col.query(query_embeddings=[[1, 0, 0]], n_results=5, where={"kind": {"$in": ["y"]}})
    assert res["ids"][0] == ["b"]
    col.update(ids=["a", "zz"], metadatas=[{"kind": "z"}, {"kind": "q"}])  # update never inserts
    assert col.get(ids=["a"])["metadatas"][0] == {"kind": "z"} and col.count() == 4
    col.add(ids=["a"], embeddings=[[0, 1, 0]], documents=["ignored"])  # add skips existing ids
    assert col.get(ids=["a"])["documents"] == ["apple pie"]
    got = col.get(ids=["b"], include=["embeddings"])
    assert got["embeddings"][0] == pytest.approx([0, 1, 0]) and got["documents"] is None
    assert col.delete(where={"n": {"$gt": 3}}) == ["d"]
    assert [c.name for c in cl.list_collections()] == ["c1"]
    cl.delete_collection("c1")
    with pytest.raises(ValueError):
        cl.delete_collection("c1")
    with pytest.raises(ValueError):
        cl.get_collection("c1")
