# Tasks

## 1. Home admin (`app/home_admin.py`, `app/home.py`, `app/spaces/api.py`)

- [x] 1.1 `GET /home/config` returns `base` = the request's base URL (no trailing slash) when no issuer is set. Verified by: a test asserting `base == "http://testserver"` in open mode.
- [x] 1.2 `GET /home/embedding` with the captured shape (`active`, `embedder`, `backend`, `maintenance`, `notice`, `models`, `jobs`, `backup_path`, `can_switch: false`); every embedding write route the UI calls answers 501 with the design's detail. Verified by: tests on GET shape and one write.
- [x] 1.3 `GET /home/review` and `POST /home/review/apply` over the existing learn-back/distill proposals; decisions are audited (`distill.*`) once task 2.1 exists (call `app.audit.record` guarded by try/except). Verified by: a test with one pending proposal, applied.
- [x] 1.4 `POST /home/retention/logs` as in design. Verified by: tests for off (default) and a dry run with days=1 over an old log row.
- [x] 1.5 `GET /spaces/{space}/gaps` and `GET /spaces/{space}/links/learned` (404 "no such space" as other space routes). Verified by: a test of both shapes.

## 2. Audit (`app/audit.py` + callers)

- [x] 2.1 `app/audit.py`: table, `record()`, `GET /home/audit` (filters `action` prefix, `project`, `since`, `until`, `limit`, `offset`; newest first; `{total, rows}`), `GET /home/audit/export?fmt=csv|json` (csv header `ts,actor,action,project,target,detail`; json = one object per line), `GET /home/rules/history?kind=&key=` (`kind`, `key` required; `{kind, key, changes}` oldest first, `action` starts with `rule.<kind>.` and `target == key`). Verified by: tests.
- [x] 2.2 Wire `record` into policy and kernel rule add/update/delete, /home item and part deletes, threshold PUT/DELETE, `/findings/resolve`. Verified by: a test that adds and changes a policy and reads its history.

## 3. Flow config (`app/flows/`)

- [x] 3.1 `GET /flows/events` from `seeds/flow_events.json`, `GET /flows/nodes` from `seeds/flow_nodes.json`. Verified by: tests on top-level keys.
- [x] 3.2 `POST /flows/expr/check {expr, kind, upstream}` as in design. Verified by: tests for empty, valid `pl.col("a") > 1` with upstream `a`, unknown read, syntax error.
- [x] 3.3 `GET /flows/scope-events/runs?type=` and `POST /flows/events/{event_id}/fire?flow=`. Verified by: tests (422 without type; fire of `task.full-retrieve` queues; fire of a flow without runner answers queued false).
- [x] 3.4 `PUT /flows/{flow_id}` and `DELETE /flows/{flow_id}` stored in `kh_flow_def` over the seed; `PUT`/`DELETE /project-types/{type_id}` in `kh_project_type`; `rev` changes on save; system flows cannot be deleted (409). Audit as `flow.save` / `flow.delete`, `project_type.save` / `project_type.delete`. Verified by: tests (save, read back, list shows it, delete restores the seed version or hides a new one).

## 4. Close

- [x] 4.1 Deviations bullets for every decision in design.md; checklist items in `docs/upgrade-7.1-checklist.md`. Verified by: grep.
- [x] 4.2 Full suite and route coverage pass; the home page loads with no 404 in the server log for the tabs Review, Embedding, Audit, Flow Config, Space Data. Verified by: commands in CLAUDE.md and a headless page load.
