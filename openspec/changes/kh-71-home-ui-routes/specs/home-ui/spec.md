# Spec Delta

## Purpose
The KH 7.1 home page served by the rebuild finds every route its tabs call.

## ADDED Requirements

### Requirement: Home config names this KH
`GET /home/config` SHALL answer `base` as the issuer when sign-in is on, else this KH's own base URL.

#### Scenario: Open mode
- **WHEN** no `KH_AUTH_ISSUER` is set and the page loads
- **THEN** `base` is the URL the request reached, without a trailing slash

### Requirement: Embedding tab is read only
`GET /home/embedding` SHALL answer the active model and backend in the 7.1 shape; every embedding write route SHALL answer 501 with a detail that says how to switch models instead.

#### Scenario: Write refused
- **WHEN** a maintainer posts to `/home/embedding/convert`
- **THEN** the answer is 501 and nothing is changed

### Requirement: Changes are audited
Rule, policy, review, /home delete, retention, threshold, finding-resolve, flow and project-type changes SHALL append one audit row each; `GET /home/audit` SHALL list them newest first with filters, `GET /home/audit/export` SHALL export them as csv or json lines, and `GET /home/rules/history` SHALL list the changes of one rule.

#### Scenario: Policy history
- **WHEN** a policy is added and then changed
- **THEN** `GET /home/rules/history?kind=policy&key=<target>` lists two changes, oldest first, the first with `before` null

#### Scenario: Audit failure is harmless
- **WHEN** the audit write fails
- **THEN** the change itself still succeeds

### Requirement: Flow editor routes
`GET /flows/events` and `GET /flows/nodes` SHALL answer the 7.1 catalogs; `POST /flows/expr/check` SHALL check an expression and list what it reads; `PUT /flows/{id}` SHALL store an edited flow that `GET /flows/{id}` and `GET /flows` then answer; `DELETE /flows/{id}` SHALL drop the edit (a seed flow returns to its seed version, a new flow disappears) and SHALL refuse system flows with 409.

#### Scenario: Save and read back
- **WHEN** a maintainer saves a flow with a changed description
- **THEN** `GET /flows/{id}` answers the new description and `GET /flows` has a new `rev`

#### Scenario: Empty expression
- **WHEN** `POST /flows/expr/check {"expr": ""}`
- **THEN** the answer is `{"ok": false, "reads": [], "error": "empty expression"}`

### Requirement: Space data routes
`GET /spaces/{space}/gaps` and `GET /spaces/{space}/links/learned` SHALL answer the 7.1 shapes, with no items while gap tracking is not built, and 404 "no such space" for an unknown space.

#### Scenario: Empty gaps
- **WHEN** a reader asks for the open gaps of a space
- **THEN** the answer is `{space, open: true, total: 0, items: []}`

### Requirement: Log retention
`POST /home/retention/logs` SHALL report that retention is off when `KH_LOG_RETENTION_DAYS` is 0, and otherwise count (dry run) or delete log rows older than that many days.

#### Scenario: Off by default
- **WHEN** `KH_LOG_RETENTION_DAYS` is unset
- **THEN** the answer has `days: 0`, `total: 0` and the 7.1 note
