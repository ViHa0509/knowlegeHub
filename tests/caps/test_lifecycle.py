"""§6.15 lifecycle: export (route not swallowed by GET /v1/admin/ns/{ns}), import (byte-identical re-export),
purge (confirm token / force, tombstone), retention sweep, global stop/resume, permissions."""
from __future__ import annotations

import io
import json
import time
import zipfile

from app import sqldb
from app.caps import core, lifecycle
from tests.caps.conftest import EVENT_MANIFEST, attach
from tests.auth_helpers import bearer, enable_signin, make_token

DAY = 86_400_000


def _populate(client, ns):
    for i in range(3):
        r = client.post(f"/v1/{ns}/events", json={"kind": "step.done", "body": {"n": i, "who": "x"}})
        assert r.status_code == 200
    with sqldb.connect() as c:
        sqldb.lease_acquire(c, ns, "job.lease", "me", 60_000)
    from app import vector_store as vs
    from app.embeddings import encode

    coll = vs.get_or_create(f"ns_{ns}_knowledge")
    vs.upsert_rows(coll, ["v1", "v2"], encode(["alpha text", "beta text"]), ["alpha text", "beta text"],
                   [{"ns": ns, "kind": "a"}, {"ns": ns, "kind": "b"}])


def _export(client, ns, **params):
    r = client.get(f"/v1/admin/ns/{ns}:export", params=params)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    return r


def _files(content: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def test_export_format(client):
    ns, _, _ = attach(client, manifest=EVENT_MANIFEST)
    _populate(client, ns)
    r = _export(client, ns)
    assert r.headers["X-KH-Export-Id"] and r.headers["X-KH-Export-Token"]
    files = _files(r.content)
    man = json.loads(files["manifest.json"])
    assert man["format"] == "kh-ns-export/1" and man["ns"] == ns
    assert man["confirm_token"] == r.headers["X-KH-Export-Token"]
    assert man["namespace"]["ns"] == ns and "registry" in man["modules"]
    ev = [json.loads(x) for x in files["tables/kh_event.ndjson"].splitlines()]
    assert [e["seq"] for e in ev] == sorted(e["seq"] for e in ev) and "ns" not in ev[0]
    assert any(e["kind"] == "step.done" for e in ev)
    assert "tables/kh_type.ndjson" in files and "tables/kh_lease.ndjson" in files
    ctr = [json.loads(x) for x in files["tables/kh_counter.ndjson"].splitlines()]
    assert {"name": "ev:{ns}", "n": len(ev)} in ctr
    vec = [json.loads(x) for x in files["vectors/knowledge.ndjson"].splitlines()]
    assert [v["id"] for v in vec] == ["v1", "v2"] and vec[0]["text"] == "alpha text"
    assert "embedding" not in files["vectors/knowledge.ndjson"].decode() and "emb_dim" not in vec[0]["metadata"]
    sums = json.loads(files["checksums.json"])
    import hashlib

    assert sums["tables/kh_event.ndjson"]["sha256"] == hashlib.sha256(files["tables/kh_event.ndjson"]).hexdigest()
    assert sums["tables/kh_event.ndjson"]["lines"] == len(ev)
    # the plain ns route still answers JSON (not swallowed / not swallowing)
    assert client.get(f"/v1/admin/ns/{ns}").json()["ns"] == ns
    with sqldb.connect() as c:
        row = c.execute("SELECT completed_ms, token_sha FROM kh_ns_export WHERE ns=?", (ns,)).fetchone()
    assert row[0] and row[1] == core.sha256_text(r.headers["X-KH-Export-Token"])
    assert client.get("/v1/admin/ns/tnope12345:export").status_code == 404


def test_purge_and_reimport_byte_identical(client):
    ns, _, _ = attach(client, manifest=EVENT_MANIFEST)
    _populate(client, ns)
    # purge refuses an active ns
    assert client.post(f"/v1/admin/ns/{ns}:purge", json={"confirm": "x"}).status_code == 409
    assert client.post(f"/v1/admin/ns/{ns}:detach").status_code == 200
    r1 = _export(client, ns)
    token = r1.headers["X-KH-Export-Token"]
    assert client.post(f"/v1/admin/ns/{ns}:purge", json={}).status_code == 428
    assert client.post(f"/v1/admin/ns/{ns}:purge", json={"confirm": "wrong"}).status_code == 422
    r = client.post(f"/v1/admin/ns/{ns}:purge", params={"confirm": token})  # ?confirm= as in the original build
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["state"] == "purged" and j["deleted"]["kh_event"] >= 3 and j["vectors_dropped"] == [f"ns_{ns}_knowledge"]
    with sqldb.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM kh_event WHERE ns=?", (ns,)).fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM kh_type WHERE ns=?", (ns,)).fetchone()[0] == 0
        assert sqldb.counter_get(c, f"ev:{ns}") == 0
        tomb = c.execute("SELECT state, purge_after_ms FROM kh_ns WHERE ns=?", (ns,)).fetchone()
    assert tomb[0] == "purged" and tomb[1] is None
    assert client.post(f"/v1/admin/ns/{ns}:purge", json={"confirm": token}).status_code == 404
    # import into the same (now clean) id: table files re-export byte-identical
    r = client.post(f"/v1/admin/ns:import?ns={ns}&rebuild=sync", content=r1.content,
                    headers={"Content-Type": "application/zip"})
    assert r.status_code == 201, r.text
    assert r.json()["rebuild"] == "done" and r.json()["rebuilt"]["vectors"] == {"knowledge": 2}
    st = client.get(f"/v1/admin/ns/{ns}").json()
    assert st["state"] == "active"
    r2 = _export(client, ns)
    f1, f2 = _files(r1.content), _files(r2.content)
    tables = [n for n in f1 if n.startswith(("tables/", "_kh/", "vectors/"))]
    assert tables and all(f1[n] == f2[n] for n in tables), [n for n in tables if f1[n] != f2[n]]
    # stream continues gaplessly after import
    r = client.post(f"/v1/{ns}/events", json={"kind": "step.done", "body": {"n": 9}})
    seqs = [e["seq"] for e in client.get(f"/v1/{ns}/events").json()["events"]]
    assert r.json()["accepted"][0]["seq"] == max(seqs) and len(seqs) == len(set(seqs))
    # re-import onto an existing ns is refused; a new id works
    assert client.post(f"/v1/admin/ns:import?ns={ns}", content=r1.content).status_code == 409
    new = "t" + ns[1:-1] + "z"
    r = client.post(f"/v1/admin/ns:import?ns={new}", content=r1.content)  # rebuild defaults to async
    assert r.status_code == 201 and r.json()["rebuild"] == "queued" and r.json()["source_ns"] == ns
    ev = client.get(f"/v1/{new}/events").json()["events"]
    assert [e["body"].get("n") for e in ev if e["kind"] == "step.done"] == [0, 1, 2]


def test_import_rejects_bad_zip(client):
    assert client.post("/v1/admin/ns:import?ns=tbadzip1", content=b"not a zip").status_code == 422
    ns, _, _ = attach(client, manifest=EVENT_MANIFEST)
    content = _export(client, ns).content
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content)) as src, zipfile.ZipFile(buf, "w") as dst:
        for n in src.namelist():
            data = src.read(n)
            if n == "tables/kh_type.ndjson":
                data += b'{"type":"forged"}\n'
            dst.writestr(n, data)
    r = client.post("/v1/admin/ns:import?ns=tforged1", content=buf.getvalue())
    assert r.status_code == 422 and "checksum" in r.json()["detail"]
    assert client.post("/v1/admin/ns:import?ns=_bad", content=content).status_code == 422


def test_purge_force_and_cron(client):
    ns, _, _ = attach(client, manifest=EVENT_MANIFEST)
    _populate(client, ns)
    client.post(f"/v1/admin/ns/{ns}:detach")
    r = client.post(f"/v1/admin/ns/{ns}:purge?force=true")
    assert r.status_code == 200 and r.json()["state"] == "purged"
    # cron: detached past grace is purged; half-done purge older than 1 h resumes
    ns2, _, _ = attach(client, manifest=EVENT_MANIFEST)
    _populate(client, ns2)
    client.post(f"/v1/admin/ns/{ns2}:detach")
    ns3, _, _ = attach(client, manifest=EVENT_MANIFEST)
    _populate(client, ns3)
    with sqldb.connect() as c:
        c.execute("UPDATE kh_ns SET purge_after_ms=1 WHERE ns=?", (ns2,))
        c.execute("UPDATE kh_ns SET state='purged', purge_after_ms=1 WHERE ns=?", (ns3,))
    core.invalidate_ns()
    done = lifecycle.cron_ns_purge()
    assert ns2 in done and ns3 in done
    with sqldb.connect() as c:
        for n in (ns2, ns3):
            assert c.execute("SELECT COUNT(*) FROM kh_event WHERE ns=?", (n,)).fetchone()[0] == 0
            assert c.execute("SELECT purge_after_ms FROM kh_ns WHERE ns=?", (n,)).fetchone()[0] is None
    # a tombstone can be attached again
    assert client.put(f"/v1/admin/ns/{ns}", json={"projects": []}).status_code == 201


def test_retention_sweep(client):
    m = dict(EVENT_MANIFEST, retention={"events_days": 10},
             types=[dict(t, retention_days=30) if t["type"] == "step.done" else t for t in EVENT_MANIFEST["types"]])
    ns, _, _ = attach(client, manifest=m)
    now = int(time.time() * 1000)
    old, mid = now - 40 * DAY, now - 20 * DAY
    for ts in (old, old + 1, mid):
        client.post(f"/v1/{ns}/events", json={"kind": "step.done", "ts_ms": ts, "body": {"n": 1}})
    from app.caps import events as ev_mod

    ev_mod.append_system(ns, "kh.custom", {"x": 1})
    with sqldb.connect() as c:
        c.execute("UPDATE kh_event SET ts_ms=? WHERE ns=? AND kind='kh.custom'", (mid, ns))
    swept = lifecycle.cron_retention()
    assert swept[f"{ns}/step.done"] == 2 and swept[f"{ns}/kh.custom"] == 1
    left = [e for e in client.get(f"/v1/{ns}/events").json()["events"] if e["kind"] == "step.done"]
    assert [e["ts_ms"] for e in left] == [mid]
    with sqldb.connect() as c:
        rows = c.execute("SELECT kind, week, n FROM kh_event_week WHERE ns=? ORDER BY kind", (ns,)).fetchall()
    assert ("step.done", lifecycle.week_of(old), 2) in [tuple(r) for r in rows]
    # detached namespaces are skipped
    ns2, _, _ = attach(client, manifest=m)
    client.post(f"/v1/{ns2}/events", json={"kind": "step.done", "ts_ms": old, "body": {"n": 1}})
    client.post(f"/v1/admin/ns/{ns2}:detach")
    assert f"{ns2}/step.done" not in lifecycle.cron_retention()


def test_global_stop_resume(client):
    a, _, _ = attach(client, manifest=EVENT_MANIFEST)
    b, _, _ = attach(client, manifest=EVENT_MANIFEST)
    client.post(f"/v1/admin/ns/{b}:suspend")
    with sqldb.connect() as c:
        sqldb.lease_acquire(c, a, "L", "h", 60_000)
        ep = sqldb.lease_get(c, a, "L")["epoch"]
    r = client.post("/v1/admin/agents:stop")
    assert r.status_code == 200 and a in r.json()["stopped"] and b not in r.json()["stopped"]
    assert client.post(f"/v1/{a}/events", json={"kind": "step.done", "body": {"n": 1}}).status_code == 423
    with sqldb.connect() as c:
        assert sqldb.lease_get(c, a, "L")["epoch"] == ep + 1
    r = client.post("/v1/admin/agents:resume")
    assert a in r.json()["resumed"] and b not in r.json()["resumed"]
    assert client.get(f"/v1/admin/ns/{a}").json()["state"] == "active"
    assert client.get(f"/v1/admin/ns/{b}").json()["state"] == "suspended"
    assert "stopped_from" not in client.get(f"/v1/admin/ns/{a}").json()["config"]
    assert client.post(f"/v1/{a}/events", json={"kind": "step.done", "body": {"n": 1}}).status_code == 200


def test_permissions(monkeypatch, client):
    from tests.caps.conftest import new_project

    p1 = new_project()
    enable_signin(monkeypatch)
    owner = bearer(make_token("owner@x.com", projects={p1: "owner"}))
    other = bearer(make_token("other@x.com", projects={p1: "editable"}))
    maint = bearer(make_token("boss@x.com", role="maintainer"))
    ns, _, _ = attach(client, projects=[p1], headers=owner)
    assert client.get(f"/v1/admin/ns/{ns}:export", headers=other).status_code == 403
    assert client.get(f"/v1/admin/ns/{ns}:export", headers=owner).status_code == 200
    assert client.post("/v1/admin/agents:stop", headers=owner).status_code == 403
    assert client.post("/v1/admin/agents:resume", headers=other).status_code == 403
    client.post(f"/v1/admin/ns/{ns}:detach", headers=owner)
    assert client.post(f"/v1/admin/ns/{ns}:purge?force=true", headers=owner).status_code == 403
    assert client.post(f"/v1/admin/ns/{ns}:purge?force=true", headers=maint).status_code == 200
    assert client.post("/v1/admin/agents:resume", headers=maint).status_code == 200
