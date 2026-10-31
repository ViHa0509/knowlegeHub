# Rebuild contract (shared by every builder)

The spec is `docs/spec/KH-6.5-co-che-prod.md`. This file fixes the decisions every builder must follow so that
parts written in parallel fit together. When the spec and this file disagree, this file wins; record any other
deviation from the spec in `docs/deviations.md` (one bullet per deviation: what, why).

## Layout

Mirror the spec's paths (they are relative to the original `Slaves/KnowledgeHub/`, which is this repo root):

```
main.py                     FastAPI app, lifespan, core routes of spec §1.14, attaches route groups
VERSION                     "v6.5"
requirements.txt            runtime deps (heavy ones optional, see below)
app/config.py               all env parsing helpers + §1.4 vars; other sections add their own constants
                            in their own modules using app.config helpers (_env, _bool, _flag, _int)
app/sqldb.py, app/hana_conn.py, app/docstore.py            §1.5
app/vector_store.py, app/vector_hana.py, app/vector_sqlite.py   §1.7 (+ local backend, see below)
app/chunker.py, app/chunks.py, app/hotlog.py, app/retriever.py  §1.8-1.10
app/embeddings/                                             §1.11
app/graph_db/                                               §1.12 (minimal)
app/projects.py, app/projects_store.py                      §1.6 + §2.8 data layer
app/auth.py, app/auth_rules.json, app/personal.py, ...      §2
app/step.py, app/turn.py, app/policy*.py, app/kernel/, ...  §3
app/distill.py, app/learnback.py, app/promote.py, app/feedback.py, app/sync.py, app/home.py,
app/static/home.html, app/eventing/, worker/kh_worker/      §4
seeds/                                                      seed content (Appendix A)
tools/kh-helper/                                            §5 client package (pip-installable, own tests)
tests/<area>/                                               pytest, one folder per area
```

## Route groups

`main.py` holds a list `ROUTE_GROUPS` of module names in the spec's §1.2 step 7 order. For each name it imports
`app.<name>` and calls `attach(app)`; a module that does not exist yet is skipped with a warning (so parts can
land independently). Every route module exposes `attach(app) -> None` and may create its own tables there
(with `sqldb.create_table` / `sqldb.migrate`). Background jobs start from an optional `start(app)` called in the
lifespan; stop from optional `stop()`.

Group names (module `app/<name>.py` or package `app/<name>/`): orchestrator, projects_routes, policy,
policy_actions, routing, feedback, messages, artifacts, gallery, revisions, hooks, personalization, entities,
turn, step, distill, promote, kernel, usage, sync, home, learnback, codegraph, caps, session_project, downloads,
eventing, auth_routes. Unneeded/legacy ones may simply not exist.

## Auth seam (owned by §2, stub by §1)

`app/auth.py` exposes, with these exact names (§2.4 "Module helpers"):
`install(app)`, `current() -> Principal | None`, `current_actor() -> str | None`, `visible_projects(perm="project.read") -> set[str] | None`
(None = all), `filter_projects(ids)`, `may(project, perm) -> bool` (True when auth is off), `acting_key()`,
`acting_visible(meta)`, `row_visible(meta, id) -> bool`, `decide(...)`, class `Principal` (§2.4 members),
`personal_id(email)` (may live in `app/personal.py`, re-exported). In open mode (no `KH_AUTH_ISSUER`)
`current()` is None and every check passes; `install` adds only `ActorMiddleware` (reads `X-KH-Actor`).
The §1 builder writes a working open-mode stub with these names; the §2 builder replaces it with the full
implementation and must keep every name and signature.

## Storage backends

- Relational: `KH_STORE_BACKEND=sqlite` (default) or `hana`. Implement both per §1.5; tests use SQLite.
- Vectors: `KH_VECTOR_BACKEND` = `sqlite` (NEW default, `app/vector_sqlite.py`: the HANA adapter's design on
  SQLite: tables `KH_VEC_COLLECTIONS`/`KH_VEC_ROWS` in the same `hooks.db`, embeddings as float32 blobs, cosine in
  numpy, full Chroma `where`/`where_document` semantics in Python) | `hana` (§1.7.1) | `chroma` (only if
  `chromadb` is installed). All three expose the same Chroma-style client/collection API.
- Graph: `GRAPH_BACKEND` = `hana` or a new `sqlite` adapter (same tables as HANA, plain SQL, no OpenCypher);
  Kùzu is not required.
- Optional imports (`hdbcli`, `sentence_transformers`, `chromadb`, `nltk`, `anthropic`) are imported lazily; the
  app must start and pass tests without them.

## Embeddings

`KH_EMBEDDER` = `local` | `remote` | `auto` per §1.11, plus NEW `hash`: a deterministic, dependency-free
384-dim embedder (lower-cased word tokens + character trigrams hashed into 384 buckets, L2-normalized) used by
tests and by machines without the model. Same adapter contract (`load, encode, encode_query, name, dim`).
`get_embedder()` returns the configured singleton. Tests always set `KH_EMBEDDER=hash`.

## Test harness

`tests/conftest.py` (owned by §1) sets, before importing `main`: `KH_EMBEDDER=hash`, a temp `KH_HOOKS_DB`,
temp `KH_PROJECTS_PATH`, `KH_VECTOR_BACKEND=sqlite`, `KH_STORE_BACKEND=sqlite`, `GRAPH_BACKEND=sqlite`, crons off
(`KH_FEEDBACK_CRON=false`, `KH_MESSAGE_CRON=false`, `KH_CAPS_CRONS=false`), and provides a `client` fixture
(FastAPI `TestClient` with lifespan). Auth tests build signed-in clients by setting `KH_AUTH_ISSUER` and
monkeypatching the JWKS fetch with a locally generated RSA key (helper fixture `make_token(email, role, ...)`,
owned by §2 in `tests/auth/conftest.py`).

Run: `.venv/Scripts/python -m pytest -q` (all) or `.venv/Scripts/python -m pytest tests/<area> -q`.

## Conventions

- Python 3.12, FastAPI + pydantic v2, stdlib `logging`, blocking work via `asyncio.to_thread` in async routes.
- Exact constants, status codes, error texts and response shapes come from the spec. Cite nothing; just match.
- English only in code, comments, stored text and seeds.
- Do not edit files owned by another builder except to add a name to an import list; if you need a change in
  another area, write it in `docs/integration-notes.md` instead.
