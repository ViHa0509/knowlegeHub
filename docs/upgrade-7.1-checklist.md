# Checklist: from this rebuild (v6.5) to a KH v7.1.0 build

Compared on 2026-11-17 against the KH_Setup installer (KH v7.1.0 exe built 2026-11-17, kh-helper 1.33.0),
installed side by side in `C:\KH71` (ports KH 9001, Keycloak 8181/9181, PostgreSQL 5434) and synced from Prod.
Sources of the comparison: the module list and route strings inside `KnowledgeHub.exe` (names only, no code),
the kh-helper 1.33.0 wheel, the installer scripts and seeds, and the KH changelog rows `chg_knowledge-hub_7.0`
and `chg_kh-helper_1.30.0`. There is no 7.x spec: every item below needs its behaviour specified first
(from the 7.1 install, Prod, or the original repo `NghiepHoang91/RWorkspace`).

Status today: the rebuild serves 301 operations; 7.1 bundles 140 app modules, 63 of them absent here.
The 7.1 OpenAPI page answers 500 (see item 12), so the route list below comes from strings in the exe.

## 0. Decisions to make first

- [ ] **Language contract.** 7.x is multilingual (bge-m3, 1024 dims): stored text stays in the person's
      language and Prod plan texts dropped "in English". The rebuild contract is English-only with
      bge-small-en-v1.5 (384 dims). A 7.1 build means changing `docs/rebuild-contract.md`, CLAUDE.md, the
      kernel rule `kh_english`, and the embedder default (`KH_EMBEDDING_MODEL`, `app/config.py`).
- [x] **Calibrated thresholds** (2026-11-18: `app/thresholds.py` with the bge-m3 values the 7.1 build answers). Step floor 0.68 and turn-lesson floor 0.6 were calibrated for
      bge-small; 7.1 has a `thresholds` module and `/thresholds` routes. Re-calibrate for bge-m3.
- [ ] **Spec source.** Decide whether 7.x behaviour may be read from the original repo, or only observed
      from a running 7.1 (black box). The current repo premise is "no original code".

## 1. Embeddings and models

- [ ] bge-m3 adapter as the default model, `paraphrase-multilingual-MiniLM-L12-v2` also shipped
      (`app/embeddings/*`; 7.1 ships both under `_internal/models`).
- [ ] `app.embedding_admin`: `/home/embedding`, `/home/embedding/models`, `.../models/{id}/bench|check|delete`,
      `/home/embedding/upload|convert|retry|rollback`, `/home/embedding/maintenance/off`, and a maintenance
      gate middleware that holds requests during a model switch.
- [ ] `app.embed_server`: `/embed`, `/embed/info` (embedding as a service).
- [ ] `app.vision` (image input; `KH_PRELOAD_VISION=false` in the installer's `.env`).

## 2. Spaces (v2 project frame)

- [ ] `app.spaces.*` (21 modules): every project gets a v2 frame of scopes, access rules and a kind map;
      old data stays in place.
- [~] Built 2026-11-18 (legacy frame, read side): `/spaces`, `/spaces/match`, `scopes`, `access`, `index`, `rows/{row}`,
      `scopes/{s}/rows[/{row}]`, `scopes/{s}/search`, `map`, `links/walk`, `/projects/{id}/layout|about`. Still missing:
      the v2 frame, adopt/backfill/gaps/cases/learned links, `levels`, `nodes`, `graph-store`.
- [ ] Routes: `/spaces`, `/spaces/match`, `/spaces/served`, `/spaces/{id}/access|adopt|backfill|cases|gaps|
      keep-missing|levels|nodes|nodes/check|nodes/resolve|scopes|index|map`, `/spaces/{id}/gaps/{g}/close`,
      `/spaces/{id}/links/fade|group|housekeep|learned|learned/reject|tree|walk`,
      `/spaces/{id}/scopes/{s}/rows[/{row}]`, `/spaces/{id}/scopes/{s}/search`, `/spaces/{id}/rows/{row}`,
      `/spaces/{id}/graph-store` (external RAGStoraged graph).
- [ ] `app.project_layout` (`GET /projects/{id}/layout`, `save_kinds`), `app.project_card`,
      `GET/PUT /projects/{id}/about`, project types (`/project-types[/{id}]`).
- [ ] Scope names: about, policies, feature_graph, knowledge, log, findings, lessons (kernel rule 4).

## 3. Flows engine

- [ ] `app.flows.*` (14 modules): server-side flows with nodes, blocks, a Polars expression check,
      a scheduler and an event listener.
- [ ] Routes: `/flows`, `/flows/{id}`, `/flows/{id}/run`, `/flows/runs`, `/flows/runs/{run}`,
      `/flows/runs/{run}/answer`, `/flows/match`, `/flows/nodes`, `/flows/options`, `/flows/expr/check`,
      `/flows/events`, `/flows/events/{id}/fire`, `/flows/scope-events/runs`, `/spaces/{id}/events/runs`,
      `/hooks/{id}/run`.
- [~] Built 2026-11-18: `/flows` (list, `?q=` search), `/flows/{id}`, `/flows/{id}/run`, `/flows/runs[/{run}]`,
      `/flows/runs/{run}/answer` (409), `/flows/match`, `/hooks/{id}/run`, `/helper/manifest`, `/project-types`;
      Python runners for task.full-retrieve and the four hook flows. Still missing: the generic step engine.
- [ ] Seed flows (`app.flows.seeds`, `seeds_v2`): `task.full-retrieve` (the task map with LESSONS first),
      `save.spec`, `save.structured`, `save.log`, `save.retro`, plus hook flows.
- [ ] Flow asks that wait for the agent ("IS WAITING FOR YOUR ANSWER") and the answer shape.

## 4. Context, hooks, policy

- [ ] Split `app.hooks` into `api`, `store`, `turn`; the turn context now carries ABOUT, ACTIONS, FINDINGS,
      own lessons, project lessons and a TASK FLOW line, and reads `input.context` (git branch, cwd, user)
      sent by kh-helper 1.33.0 for per-branch variants (`app.spaces.variants`).
- [ ] `app.policy_actions.api` split; `GET /helper/manifest` (what the server asks of the helper).
- [ ] `app.revisions.api`, `app.audit` (`/home/audit`, `/home/audit/export`), `app.log_ids`,
      `/home/retention/logs`, `/home/review`, `/home/review/apply`, `/home/rules/history`.
- [ ] `/findings/resolve` (finding to lesson), `/personalization/guard_tools`, `/step/outcome(s)`.

## 5. Messages and people

- [ ] `/messages/send`, `/messages/sent` (sent, delivered, opened per recipient), `/messages/mirror/check`
      (`app.messages_mirror`), `/people`, `/members/`.

## 6. /v1 capability modules

- [ ] New modules: `daily` (`/daily/next`, `/daily/submit`, `/log/day`, `/log/week`), `improve`
      (`/improve/propose|decide|revert|fallback|proposals|state`), `learn` (`/learn/propose|decide|trials`),
      `outcomes`, `people`, `benchhook`.

## 7. Storage

- [ ] Kùzu graph adapter (`app.graph_db.kuzu_adapter`, `project_graph`); the rebuild maps Kùzu to SQLite.
- [ ] `app.db`, `app.schemas`, `app.extractor`, `app.codegraph.api` (code graph push).
- [ ] Data migration for an existing 384-dim store (7.0 note: "old data kept in place, no re-embedding").

## 8. Sync (protocol 2)

- [ ] The receiver half already accepts 7.1 shapes (uncommitted `app/sync.py` change, see deviations).
      Still missing: `/sync/dump`, `/sync/import`, the member transfer on pull (`members_here`, owners and
      levels per project), `parts.default` / `parts.optional` in the connect answer, and personal-project pull
      (7.1 offers and accepts the signed-in person's own personal project).

## 9. Home UI

- [ ] `app/static`: `flows.css`, `flows.js`, `theme.css`, fonts (Inter, Poppins, Patrick Hand, Dancing Script,
      Delius); theme picker (kawaii scrapbook default, aura starry night, dark slate); `/home/fonts/{f}`;
      `app.gallery` (`/gallery/designs/`); `app.downloads`.

## 10. Auth and catalogue

Decided 2026-11-18: no Keycloak. The local and self-hosted KH runs in open mode; the items below apply only
if sign-in is wanted later. Sign-in still needs an identity service with KH's `/kh/...` extension, or the members,
project registry and visibility moved into KH's own tables.

- [ ] Diff `app/auth_rules.json` against the 7.1 copy (`_internal/app/auth_rules.json`): new routes need rules.
- [ ] Access catalogue from `setup/deploy/kh-auth/catalog.json` and the Postgres auth tables
      (`schema_postgres.sql`: `kh_auth_role`, `_project_level`, `_permission`, `_grant`, ...).
- [ ] `/auth/download`, the kh-helper download routes.

## 11. kh-helper 1.33.0

- [ ] New modules `context.py` (git branch, cwd, user on every hook and flow), `manifest.py`, `ragstore.py`.
- [ ] Fixed tools `kh_graph` and `kh_sent`; the other task tools (`kh_task_full_retrieve`, `kh_expand`,
      `kh_find`, `kh_walk`, `kh_map`, `kh_flow`, `kh_flow_answer`, `kh_flows`, `kh_save_*`) are built at start
      from the server's `GET /helper/manifest`, so they need items 3 and 4. `kh_save` follows `save_kinds`.
- [ ] Keep the local Claude Desktop extension (`kh_login`, `kh_logout`, uncommitted) when merging.
- [ ] KH 7.0 asks for helper 1.30.0 or newer; bump `pyproject.toml`, `__init__.py`, README.

## 12. Packaging and Windows install

- [ ] PyInstaller build to `KnowledgeHub.exe` (onedir, models under `_internal/models`), `VERSION` file.
- [ ] `app.status_tray` and `app.desktop`: `KnowledgeHub.exe --tray` status icon.
- [ ] Installer: `install-kh.cmd`, `setup/install.py`, `local_setup.py`, `seed_kh.py`, `uninstall.py`,
      `deploy/btp/configure_*.py`, bundled Python, JRE 21, Keycloak 26.7.4 (KH fork), PostgreSQL 17.11.
      With no Keycloak, this installer shrinks to: copy KH, write `.env` (no `KH_AUTH_ISSUER`), start task, tray,
      seed the Prod kernel; Java, Keycloak and PostgreSQL drop out (checked: the 7.1 exe serves home, turn context,
      flows, search and writes in open mode). This closes the deviation "Windows offline installer (§5.14) not
      implemented".
- [ ] Fix found in the 7.1 build: `GET /openapi.json` answers 500 because the request body model of
      `POST /home/embedding/upload` is not fully defined (pydantic `class-not-fully-defined`). `/docs` is
      unusable until that model is rebuilt.

## 13. Seeds

- [ ] `seeds/kernel_hooks.seed.json`: take the five 7.1 Prod hook texts (prompt-guard and SessionStart kernel
      need items 2-3 first). Stop/store-log is already taken.
- [ ] `seeds/policy_actions_every_project.json`: 14 actions / 61 rules from Prod (the installer page says
      18 / 89; the shipped file has 14 / 61).

## 14. Tests and docs

- [ ] Tests per new area; `tools/route_coverage.py` against a 7.1 route list instead of spec Appendix B.
- [ ] `docs/deviations.md`, `docs/integration-notes.md`, CLAUDE.md commands and architecture.
