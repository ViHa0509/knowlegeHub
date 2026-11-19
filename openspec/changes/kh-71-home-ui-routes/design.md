# Design

## Reference

7.1 answers are captured (git-ignored) in `docs/ref71/ui/*.json`; the UI code that calls each route is in `app/static/home.html` and `app/static/flows.js` (grep the path). The 7.1 install can be started on port 9001 (`cd /c/KH71/kh && KH_SPACES=on KH_FLOWS=on ./KnowledgeHub.exe`); when probing it, send only GETs and deliberately invalid bodies: a valid write changes its data.

## Conventions (repo)

- One module per route group, `attach(app)`; tables through `app/sqldb.py` with dual DDL (`sqldb.create_table(c, name, sqlite_ddl, hana_ddl)`), as `app/flows/store.py` does.
- English only in code, comments, seeds and stored text. Never `encode_query`.
- Every deviation from 7.1 or filled unknown gets a bullet in `docs/deviations.md`, section "KH 7.1 upgrade".
- Tests in `tests/spaces/` with the `client` and `fresh_store` fixtures (`tests/conftest.py`); the hash embedder, no network.

## Decisions

- **Embedding writes return 501** with detail `embedding admin is not in this build: start KH with KH_EMBEDDING_MODEL=<model> and its own KH_HOOKS_DB`. Converting stored vectors is a data migration; it is out of scope.
- **Audit** (`app/audit.py`): table `kh_audit(id, ts, actor, action, project, target, detail JSON)`, id `au_<22 hex>`, helper `record(action, project, target, before=None, after=None, **extra)` that never raises. Actions use the 7.1 prefixes the UI filters on: `rule.` (`rule.policy.add|update|delete`, `rule.kernel.add|update|delete`), `distill.` (review decisions), `home.` (deletes on /home), `embedding.`, `retention.`. `project` is the project id or `every project` for common rules; `target` for a policy is `<project or 'every project'>|<action label>|<slot>`. Callers wire `record` into existing write paths; a failing audit write never fails the change.
- **Flow edits** are stored in table `kh_flow_def(id, body JSON, updated, actor)` and read over the seed (`app/flows/store.py` `flows()` merges: stored wins, a stored `{"deleted": true}` hides a seed flow). `rev` changes with every save. Project types the same way in `kh_project_type`. `kind: system` flows cannot be deleted (409).
- **Expression check** without Polars installed: empty -> `{ok:false, reads:[], error:"empty expression"}`; otherwise parse with Python `ast` (expression mode), allow only names, attributes, calls, constants, comparisons, bool/bin ops; `reads` = the string arguments of `pl.col("...")` calls; a name in `reads` that is not in `upstream` (for kind `when`) gives `error: reads "<x>" which no upstream step gives`. Record the Polars gap in deviations.
- **Event fire** (`POST /flows/events/kh.daily_0200/fire?flow=`): runs the flow through `run_flow(..., origin="event", event=<id>)` when it has a runner, answers `{queued: true, run_id}`; otherwise `{queued: false, reason}`.
- **Scope-event runs** (`GET /flows/scope-events/runs?type=`): `type` required (422); answers `{type, spaces:[ids of spaces of that type], runs:[], every_s:300, on:<KH_FLOWS on>}`.
- **Gaps / learned links**: `{space, open, total:0, items:[]}` and `{space, total:0, offset, limit, items:[]}`.
- **Review** (`GET /home/review`): `{shared_project:"lesson-learned", projects:[{project, count, items:[...]}], count}` from the pending learn-back/distill proposals the rebuild already keeps (see `app/distill.py`, `app/learnback.py`); `POST /home/review/apply {decisions:[...]}` -> `{results:[...]}` (body shape: read home.html line ~1075). An empty body answers `{results: []}` as 7.1.
- **Retention** (`POST /home/retention/logs {days?, dry_run?}`): `KH_LOG_RETENTION_DAYS` default 0 = off -> the captured note; with days > 0, counts (dry_run) or deletes log rows older than that per project, audit `retention.logs`.
