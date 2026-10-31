# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A rebuild of the production Knowledge Hub (KH) v6.5 from a written specification, `docs/spec/KH-6.5-co-che-prod.md` ("co che prod" = production mechanism, ~4,800 lines). The spec was written so that a Claude session **without access to the original KH code** can build an equivalent KH: same behaviour toward agents and users, same contracts. It describes what each part does, with exact constants, shapes and algorithms; it does not copy code.

- The original source is `github.com/NghiepHoang91/RWorkspace`, folder `Slaves/KnowledgeHub/`, commit `41f756b`. Every `path:line` citation in the spec points at that commit, not at files here.
- All 296 operations of the spec's Appendix B exist (`tools/route_coverage.py`).
- `docs/rebuild-contract.md` fixes the decisions every builder follows (layout, route groups, auth seam, backends, test harness). When it and the spec disagree, the contract wins.
- `docs/deviations.md` lists every place the rebuild differs from the spec or fills a spec UNKNOWN. Add a bullet when you do either.
- `docs/integration-notes.md` documents the helper APIs each area exposes to the others.

## Commands

All from the repo root, with the project venv (`.venv`, Python 3.12):

- Install: `python -m venv .venv` then `.venv/Scripts/python -m pip install -r requirements.txt` and `.venv/Scripts/python -m pip install -e tools/kh-helper`
- All tests: `.venv/Scripts/python -m pytest -q` (server + worker + kh-helper, about 2.5 min)
- One area: `.venv/Scripts/python -m pytest -q tests/<area>` (areas: foundation, auth, context, learning, routes_extra, legacy, worker, caps) or `tools/kh-helper/tests`
- One test: `.venv/Scripts/python -m pytest -q tests/caps/test_rules.py::test_bulk`
- Route coverage against the spec: `.venv/Scripts/python tools/route_coverage.py`
- Run the server locally: `.venv/Scripts/python -m uvicorn main:app --port 8000` (open mode, SQLite in `hooks.db`)

Tests never touch the network: `tests/conftest.py` sets `KH_EMBEDDER=hash` (a deterministic 384-dim embedder), temp SQLite stores and crons off. LLM calls are always faked.

## Reading the spec efficiently

The spec is too large to read in one go. Find the section first, then read only that range:

- `grep -n '^## \|^### ' docs/spec/KH-6.5-co-che-prod.md` lists every section with its line number.
- §0 (lines ~1-115) is the entry point: the section map, the glossary, the system diagram, the prod state verified on 2026-10-31, **one user turn end to end (§0.6)**, the rebuild order (§0.7) and the consolidated UNKNOWNs (§0.8).
- Every major section ends with its own gaps/UNKNOWN list, and most have a re-implementation checklist.
- The `kh-spec` skill in `.claude/skills/` covers this workflow.

Markers used throughout: **LIVE** (used on prod), **LOCAL** (desktop/Docker only), **OFF** (exists but disabled on prod), **DEAD** (defined but unused), **LEGACY**, **UNKNOWN** (could not be determined). Do not invent values for UNKNOWNs silently: fill them and record them in `docs/deviations.md`.

## Architecture (big picture)

- **Server**: one Python 3.12 FastAPI app (`main.py`, uvicorn, single process). It runs no LLM for serving: it is deterministic storage, permissions and retrieval; the calling agent does all the reasoning. LLMs are used only in background learning jobs (learn-back, `worker/kh_worker`) and in the client's work-log summarizer.
- **Route groups**: `main.ROUTE_GROUPS` imports `app.<name>` and calls `attach(app)` (optional `start(app)` / `stop()` for crons). A missing module is skipped with a warning.
- **Storage**: relational via `app/sqldb.py` with dual DDL (SQLite default, HANA on prod); vectors via `app/vector_store.py` (SQLite backend by default, HANA, Chroma optional); graph via `app/graph_db/`. Embeddings come from `bge-small-en-v1.5` (384-dim). Queries are encoded **without** the bge query instruction (use `encode`, never `encode_query`), or calibrated thresholds stop matching (step floor 0.68, turn lesson floor 0.6).
- **Isolation unit = project**: every row has `metadata.project`. Each (project, scope) pair is its own collection (`kh_know_<id>`, `kh_log_<id>`, `kh_<scope>_<id>`). Special projects: `orchestrator` (shared registry; refuses logs/findings), `lesson-learned`, `market`, and one `personal-<email-slug>` per user (spec §1.6, §2.8-2.9).
- **Identity**: Keycloak JWTs; `app/auth.py` maps routes to permissions with `app/auth_rules.json` and per-project access levels (§2). Without `KH_AUTH_ISSUER` the server runs in open mode.
- **Context engine (the product)**: `GET /turn/context`, `POST /context/step`, the policy action table with plans, and the tagged kernel (common / project / personal tiers resolved by `slot`; `locked` rules cannot be overridden; tool guards) (§3). Seeds in `seeds/` come from spec Appendix A.
- **Learning loop**: worklogs → distill/learn-back into keyed lessons → promote personal rules to the common tier (§4, `app/distill.py`, `app/learnback.py`, `app/promote.py`, `worker/kh_worker`).
- **Client (`tools/kh-helper`)**: pip package with the `kh` CLI, Claude Code hooks, the MCP server `knowledge-hub`, and the background work log (§5).
- **`/v1` capability modules** (`app/caps/`, §6): namespaces for external multi-agent consumers. `app/caps/core.py` is the shared plumbing; each module declares `NAME, VERSION, TABLES, init, attach` and optional `CRONS`, `purge_ns`, `rebuild_ns`. Register `:verb` routes before plain `/{id}` routes, and call `core.emit` only after closing a `sqldb.connect()` block.

## Contract to preserve

- KH is English-only. Everything sent to or stored in KH is English, and clients translate before every call. Code, comments, seeds and stored text are English.
