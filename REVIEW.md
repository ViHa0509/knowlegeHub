# REVIEW.md

How AI reviewers and the human owner review every pull request in this repo.

## Passes (run in this order)

### 1. Spec conformance
- Does the PR do what its OpenSpec change's specs and checked tasks say, and nothing else?
- Is any behavior added that no spec scenario describes? That is new scope: report it.
- Are tasks marked `[x]` backed by code and tests in this PR?

### 2. Correctness
- Wrong results, unhandled errors, off-by-one, missing null/empty cases.
- Concurrency, transactions and partial failure: what happens if a step fails halfway?
- Data migrations: reversible, safe on existing data.

### 3. Security
- Untrusted input reaching queries, shell, file paths, HTML or redirects.
- Secrets in code, logs or test fixtures.
- Authorization and isolation: can one piece of data or one page reach what it should not?

### 4. Tests
- Each touched spec scenario has a test that would fail without the change.
- Tests check behavior, not implementation details; no test was weakened or deleted to pass.
- The verifier report is attached and its verdict is pass.

## Severity

| Level | Meaning |
|---|---|
| blocker | Must be fixed before merge. |
| major | Fix in this PR unless the owner defers it in writing in the PR. |
| minor | Optional improvement. |

Every finding states: **severity**, **file:line**, and a **failure scenario** (concrete input or state -> wrong result).

## Limits

- Report at most **5 minor** findings per review: the most useful ones, plus a line saying how many were omitted.
- Never report:
  - formatting the formatter handles;
  - generated files and lock files;
  - style preferences not written in CLAUDE.md.

## Approval

- Only the human owner approves and merges.
- An agent never approves its own work, including a PR it opened.
- A PR merges only when no blocker is open and the verifier verdict is pass.
