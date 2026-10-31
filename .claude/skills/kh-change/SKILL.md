---
name: kh-change
description: Checklist for changing or adding code in this KH rebuild (server routes, /v1 caps modules, worker, kh-helper) so it stays consistent with the spec, the rebuild contract and the test suite. Use for any code change in this repo.
---

# Making a change in the KH rebuild

1. **Find the behaviour** in the spec first (skill `kh-spec`), then the owning module:
   - core routes: `main.py`; route groups: `app/<name>.py` listed in `main.ROUTE_GROUPS`
   - auth rules for routes: `app/auth_rules.json` (with sign-in on, an unlisted route is maintainer-only)
   - `/v1`: `app/caps/<module>.py` on top of `app/caps/core.py`
   - worker: `worker/kh_worker/`; client: `tools/kh-helper/kh_helper/`
2. **Reuse the helpers** listed in `docs/integration-notes.md` (sqldb dual DDL, vector store, retriever, auth, projects) instead of writing new ones.
3. **Conventions**
   - Every table gets both SQLite and HANA DDL via `sqldb.create_table` / `sqldb.migrate`.
   - Embeddings: `get_embedder().encode(...)`, never `encode_query`.
   - `/v1` modules: register `:verb` routes before plain `/{id}` routes; call `core.emit` after closing the `sqldb.connect()` block; declare `TABLES` so lifecycle export/import/purge covers them.
   - English only in code, comments, seeds and stored text.
4. **Tests**: add or update tests in `tests/<area>/` (or `tools/kh-helper/tests`). No network; fake LLMs and HTTP.
5. **Verify** before finishing:
   - `.venv/Scripts/python -m pytest -q tests/<area>` for the area you touched
   - `.venv/Scripts/python -m pytest -q` for the whole suite
   - `.venv/Scripts/python tools/route_coverage.py` when routes changed
6. **Record** any spec deviation or filled UNKNOWN in `docs/deviations.md`, and any cross-area API in `docs/integration-notes.md`.
