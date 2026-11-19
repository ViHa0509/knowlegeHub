# KH 7.1 home UI routes

## Why

The rebuild now serves the KH 7.1 home page (`app/static/home.html`, `flows.js`, copied from the 7.1 build, commit 13c9783). Several of its tabs call routes the rebuild does not have, so they show 404s: Review, Embedding, Audit, the Flow Config editor (events, nodes, expression check, save), Space Data (gaps, learned links) and log retention.

## What changes

- `GET /home/config` answers `base` = this KH's own URL when sign-in is off (7.1 does), so the header reads "This KH: <url> · v7.1.0-dev".
- New `app/home_admin.py`: `GET /home/embedding` (read only), the embedding write routes answer 501, `GET /home/review` + `POST /home/review/apply`, `POST /home/retention/logs`.
- New `app/audit.py`: an append-only audit table, written by rule, policy, review, /home delete and retention changes; `GET /home/audit`, `GET /home/audit/export`, `GET /home/rules/history`.
- `app/flows`: `GET /flows/events`, `GET /flows/nodes` (catalogs seeded from 7.1: `seeds/flow_events.json`, `seeds/flow_nodes.json`), `POST /flows/expr/check`, `GET /flows/scope-events/runs`, `POST /flows/events/{event_id}/fire`, `PUT`/`DELETE /flows/{flow_id}` and `PUT`/`DELETE /project-types/{type_id}` (edits stored in SQLite over the seeds).
- `app/spaces/api.py`: `GET /spaces/{space}/gaps` and `GET /spaces/{space}/links/learned` answer the 7.1 shapes with no items (no gap tracking yet).

Out of scope: converting or downloading embedding models, the generic flow step engine (a saved flow still runs only when a Python runner exists), gap and learned-link learning.

## Impact

Routes, auth rules (`app/auth_rules.json`, done), route groups `home_admin` and `audit` in `main.ROUTE_GROUPS` (done), three new tables (`kh_audit`, `kh_flow_def`, `kh_project_type`), `docs/deviations.md`.
