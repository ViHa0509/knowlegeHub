# Knowledge Hub v7.1 (in progress)

A shared memory and context engine for AI coding agents such as Claude Code.

Knowledge Hub (KH) stores what agents and people learn while they work: knowledge, work logs, findings, policies and lessons. Each item belongs to a project. On every user message, KH hands the agent a compact, permission-checked context block. The server runs **no LLM to answer requests**. It does deterministic storage, permissions and retrieval, and the calling agent does all the reasoning.

It started as a demo for my team: the goal is to help people work together better by managing and sharing context, so teammates and their AI coding agents build on each other's work instead of starting from zero. It was built from a written design specification, which is kept private and not published in this repository.

The code base implements KH v6.5 in full and is being upgraded to KH 7.1. It reports version `v7.1.0-dev`. The 7.1 parts built so far are listed under [KH 7.1 upgrade](#kh-71-upgrade). What is still missing is in [Status and limits](#status-and-limits) and in [`docs/upgrade-7.1-checklist.md`](docs/upgrade-7.1-checklist.md).

## Features

- **Per-turn context.** `GET /turn/context` returns the project match, the action menu, findings, people and things mentioned, and nearby lessons. A Claude Code hook prints it into the model's context for each message.
- **Step-wise retrieval.** `POST /context/step` returns one bounded context pack (6000 characters by default) for each step of work, mixing entities, work logs, knowledge and lessons.
- **Policies and kernel.**
  - An action table maps an intent label (such as `save knowledge`) to an ordered plan.
  - A tagged kernel has three tiers: common, project and personal. Rules are resolved by slot, and locked rules cannot be overridden.
  - Tool guards check tool calls before they run.
- **Project isolation.** Every row belongs to one project. Special projects:
  - `orchestrator`: the shared directory.
  - `lesson-learned`: reviewed lessons that everyone can read.
  - `market`: public posts.
  - One `personal-<email>` workspace per user.
- **Identity and permissions.** Open mode by default: no sign-in, every call allowed, for a local or single-user KH. Optional sign-in: JWTs from an identity service set with `KH_AUTH_ISSUER`, a route-to-permission rule table and per-project access levels. The identity service must also serve KH's identity extension at `<issuer>/kh/...` (projects, members, visibility); no such service is shipped with this repo.
- **Learning loop.** Work logs are distilled into keyed lessons for review. Personal rules held by enough people are promoted to the common tier.
- **Client package.** `kh-helper` provides the `kh` CLI, Claude Code hooks, the `knowledge-hub` MCP server and an automatic background work log.
- **`/v1` capability modules.** Namespaces for external multi-agent systems, with 14 modules:

  | Module | What it provides |
  |---|---|
  | registry | namespaces, manifests, quotas |
  | coord | leases with fencing, idempotency keys, side-effect approvals |
  | events | append-only events with gapless cursors |
  | principals | service principals with signed tokens |
  | records | typed records with revisions |
  | rules | rule graph, scans, gate |
  | redaction | secret and PII masking |
  | items | distilled knowledge items |
  | retrieval | per-namespace context steps |
  | vault | sealed items with canaries |
  | packs | versioned document packs, quotes, Q&A |
  | metrics | metric points, A/B compare, rollback |
  | outbox | signed webhooks |
  | lifecycle | export, import, purge |

- **Home page (`/home`).** A browser page for maintainers with these tabs: projects, lessons and review, sync, embedding, audit, flow config and space data.
- **Sync between KH instances.** One KH (the giver) makes a `khsync1.` key, and another KH (the receiver) connects with it and pulls the projects and parts you choose. Rows already present are matched rather than copied twice.
- **Two storage backends behind one storage layer.** SQLite for local work and demos, SAP HANA Cloud for a shared team instance. Every table has DDL for both, and the layer is designed to extend to PostgreSQL (pgvector).

## Architecture

```mermaid
flowchart LR
    subgraph Client["User machine"]
        CC["Claude Code / other CLIs"]
        H["kh-helper<br/>hooks · MCP server · kh CLI<br/>background work log"]
        CC <--> H
    end
    B["Browser · /home"]
    subgraph Server["KH server (FastAPI, single process)"]
        A["Auth gate<br/>JWT + route rules"]
        CE["Context engine<br/>/turn/context · /context/step<br/>policies · kernel"]
        ST["Storage API<br/>/vector · /log · keys · layers"]
        PP["Projects, messages,<br/>sessions"]
        LL["Learning loop<br/>distill · learn-back · promote"]
        V1["/v1 capability modules"]
        EM["Embedder<br/>bge-small-en-v1.5 (384-dim)<br/>or bge-m3 (1024-dim)"]
    end
    DB[("SQLite (local)<br/>or HANA Cloud (team)<br/>relational · vectors · graph")]
    KC["Identity service<br/>(optional, sign-in on)"]
    W["kh_worker<br/>nightly LLM jobs"]
    H -- "HTTPS + JWT" --> A
    B --> A
    A --> CE & ST & PP & LL & V1
    Server --> DB
    A -. JWKS .-> KC
    W -- "next / submit" --> LL
```

A typical user turn:

1. **Session start.** The SessionStart hook loads the merged kernel and the user's personalization.
2. **User message.** The UserPromptSubmit hook prints the KH TURN CONTEXT block for that message.
3. **Agent work.** The agent picks an action, fetches its plan, and calls `/context/step` for each step. Writes go through `/vector/add` into the owning project.
4. **Before a tool runs.** The PreToolUse hook checks tool guards.
5. **End of the turn.** The Stop hook queues a background job that summarizes the turn into one work log.

## KH 7.1 upgrade

The 7.1 parts built so far:

- **Home UI.** The 7.1 home page is served at `/home` (`app/static/`: `home.html`, `flows.js`, `theme.css`, `flows.css` and fonts under the SIL Open Font License). The rebuild has every route its tabs call.
- **Spaces.** These read routes are built: `/spaces`, `/spaces/match`, scopes, access, index and rows. Open gaps and learned links answer the 7.1 shapes with no items.
- **Flows.**
  - Routes: `/flows` and `/flows/{id}`, `/flows/{id}/run` and the runs list, the event and node catalogs, an expression check, and saving or deleting flows and project types.
  - Built-in flows, each run by its own Python runner: `task.full-retrieve`, hook flows, `save.log` and `save.knowledge`.
  - The helper manifest (`/helper/manifest`).
- **Audit.**
  - Changes to rules, policies, thresholds, flows, project types and review decisions are recorded, as are deletes on `/home`.
  - Routes: `/home/audit`, `/home/audit/export` and `/home/rules/history`.
- **Score thresholds per embedding model** (`/thresholds`), including the bge-m3 values.
- **Messages and findings.** `/messages/send`, `/messages/sent`, `/findings/resolve` and `/personalization/guard_tools`.
- **Sync protocol 2.**
  - The receiver understands KH 7.1 giver keys and answers in the shape the 7.1 page reads.
  - `/sync/dump` and `/sync/import` copy data through a file.
- **Embedding tab.** It is read only: it shows the active model, and every write answers 501. To change models, start KH with another `KH_EMBEDDING_MODEL` and its own `KH_HOOKS_DB`.

Every place the rebuild differs from 7.1 is listed in [`docs/deviations.md`](docs/deviations.md).

## Quick start

Requires Python 3.12.

```bash
git clone https://github.com/ViHa0509/knowlegeHub.git
cd knowlegeHub
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt      # on Linux/macOS: .venv/bin/python
.venv/Scripts/python -m pip install -e tools/kh-helper       # optional: the client
```

Run the server locally. It starts in open mode, with storage in `hooks.db` and the hash embedder:

```bash
KH_EMBEDDER=hash .venv/Scripts/python -m uvicorn main:app --port 8000
```

Then open:

- `http://localhost:8000/home` for the home page
- `http://localhost:8000/docs` for the interactive API reference
- `http://localhost:8000/health` for a health check

Store a piece of knowledge, then search for it:

```bash
curl -X POST localhost:8000/vector/add -H "Content-Type: application/json" \
  -d '{"text":"Deploys run from the release branch only.","scope":"knowledge","metadata":{"scope":"knowledge","project":"demo"}}'

curl -X POST localhost:8000/vector/query -H "Content-Type: application/json" \
  -d '{"text":"which branch do deploys run from","project":"demo","scope":"knowledge"}'
```

The `hash` embedder only matches shared words, so its scores stay low. For real semantic search, and for `/context/step` packs (which drop items scoring below 0.68), install the optional `sentence-transformers` and `torch` packages and use the default `KH_EMBEDDER=local`.

To run with the KH 7.1 model (bge-m3, 1024 dimensions) on its own database, for example one synced from another KH:

```bash
KH_EMBEDDING_MODEL=bge-m3 KH_HOOKS_DB=./hooks-m3.db .venv/Scripts/python -m uvicorn main:app --port 8000
```

Each database stays tied to one model, because stored vectors are not converted. bge-m3 needs several GB of memory.

To copy data from another KH: on the giving KH, create a key under **Sync > Give**. On this KH, paste it under **Sync > Receive**, click **Connect**, tick the projects you want and start the sync. Both KHs must use the same embedding model.

## Configuration

All settings come from environment variables. The main ones:

| Variable | Default | Purpose |
|---|---|---|
| `KH_HOST`, `KH_PORT` | `0.0.0.0`, `8000` | Bind address |
| `KH_STORE_BACKEND` | `sqlite` | Relational store: `sqlite` or `hana` |
| `KH_VECTOR_BACKEND` | `sqlite` | Vector store: `sqlite`, `hana` or `chroma` |
| `GRAPH_BACKEND` | `sqlite` | Graph store: `sqlite` or `hana` |
| `KH_HOOKS_DB` | `./hooks.db` | SQLite file for local storage |
| `KH_EMBEDDER` | `local` | `local` (bge-small-en-v1.5), `remote`, `auto` or `hash` (deterministic, no model) |
| `KH_EMBEDDING_MODEL` | `bge-small-en-v1.5` | Model for the `local` embedder; `bge-m3` is the KH 7.1 model |
| `KH_SPACES`, `KH_FLOWS` | on | The 7.1 spaces and flows (what the helper manifest reports) |
| `KH_LOG_RETENTION_DAYS` | `0` (off) | Delete log rows older than this many days (Home > retention) |
| `KH_AUTH_ISSUER` | unset | Issuer URL of an optional identity service. When unset, sign-in is off (open mode, the default) |
| `HANA_HOST`, `HANA_PORT`, `HANA_USERNAME`, `HANA_PASSWORD`, `HANA_SCHEMA` | unset | HANA Cloud connection |
| `KH_FEEDBACK_CRON`, `KH_MESSAGE_CRON`, `KH_CAPS_CRONS` | on | Background jobs |
| `KH_CAP_<MODULE>` | module default | Switch a `/v1` module on or off |

The full list, with exact defaults, is in [`app/config.py`](app/config.py).

KH is **English-only**. Clients translate everything before sending it, so stored text and embeddings stay consistent. KH 7.1 is multilingual with bge-m3. Whether the rebuild follows it is still open (see the upgrade checklist).

## Project layout

```
main.py                 FastAPI app, lifespan, core routes, route-group loader
app/                    server modules (one route group per module)
  auth.py, auth_rules.json   identity and route permissions
  turn.py, step.py           per-turn context and step packs
  policy_actions/, kernel/   action table, plans, tagged kernel
  vector_*.py, sqldb.py      vector and relational storage (SQLite / HANA)
  distill.py, learnback.py, promote.py   learning loop
  caps/                      /v1 capability modules
  spaces/, flows/            KH 7.1 spaces and flows
  audit.py, home_admin.py    audit log, home page admin routes
  sync.py                    sync between KH instances
  static/                    the KH 7.1 home page
worker/kh_worker/       nightly LLM jobs (distil, promote, daily)
tools/kh-helper/        client: kh CLI, hooks, MCP server, work log
seeds/                  seed kernel rules, actions and tags
tests/                  pytest suites per area
docs/                   build contract, deviations, integration notes, 7.1 upgrade checklist
openspec/               OpenSpec changes (proposal, design, tasks)
.claude/skills/         project skills for Claude Code
```

## Client: kh-helper

```bash
kh login              # sign in (only when the KH has sign-in on)
kh setup              # install the Claude Code hooks and the knowledge-hub MCP server
kh status             # show sign-in and connection state
kh call GET "/turn/context?text=how%20do%20we%20deploy"
```

See [`tools/kh-helper/README.md`](tools/kh-helper/README.md) for details.

## Development

```bash
.venv/Scripts/python -m pytest -q                      # full suite (~4.5 min, no network)
.venv/Scripts/python -m pytest -q tests/caps           # one area
.venv/Scripts/python tools/route_coverage.py           # every route the design lists exists
```

Tests use the deterministic `hash` embedder, temporary SQLite stores and fake LLMs. They need no network, model download or database server.

Before changing code, read [`docs/rebuild-contract.md`](docs/rebuild-contract.md) for the shared conventions. Record any departure from the design in [`docs/deviations.md`](docs/deviations.md).

## Status and limits

- This is a demo for team use, not a hardened product. Where the design leaves a value open, the choice made is listed in [`docs/deviations.md`](docs/deviations.md).
- Not yet built for KH 7.1:
  - the general flow engine (a saved flow runs only when a Python runner exists for it);
  - the spaces v2 project frame, with gap tracking and learned links;
  - switching the embedding model from the home page, and the `/embed` service;
  - the revisions API and log ids;
  - copying member lists during sync;
  - kh-helper 1.33.0 and the Windows installer.
- Not yet built (earlier):
  - a PostgreSQL (pgvector) storage backend;
  - five roadmap `/v1` modules (people, daily, outcomes, learn, improve);
  - the Windows offline installer for `kh-helper`;
  - half of the kernel tag vocabulary (151 of 299 tags are seeded).
- Not covered by tests: sync with a second, separate KH instance (the tests sync a KH with itself), account provisioning in an identity service, and event delivery to a sidecar. These need live services.

## License

[MIT](LICENSE)
