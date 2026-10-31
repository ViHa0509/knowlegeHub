---
name: kh-spec
description: Look up how the production Knowledge Hub v6.5 behaves in the spec docs/spec/KH-6.5-co-che-prod.md (routes, constants, error texts, response shapes, UNKNOWNs). Use before changing or adding any KH route or module, or when asked how KH does something.
---

# Reading the KH v6.5 spec

The spec is ~4,800 lines. Never read it whole.

1. List the sections with line numbers:
   `grep -n '^## \|^### ' docs/spec/KH-6.5-co-che-prod.md`
2. Pick the section, then read only that range (Read with offset/limit). Section map:
   - §0 entry point: glossary, system diagram, one user turn end to end (§0.6), UNKNOWNs (§0.8)
   - §1 platform, storage, embeddings, core routes
   - §2 identity, auth rules, projects, messages, sessions
   - §3 context engine: `/turn/context`, `/context/step`, policy actions, kernel
   - §4 worklogs, learning loop, worker, `/home`, sync, downloads, gallery
   - §5 client `kh-helper`: CLI, hooks, MCP tools, work log
   - §6 `/v1` capability modules (`app/caps/`)
   - Appendix A seed content (verbatim), Appendix B full prod route list
3. For a specific route or constant, grep for it: `grep -n 'context/step' docs/spec/KH-6.5-co-che-prod.md`.
4. Check the end of the section for its gaps/UNKNOWN list.

## Rules when implementing from the spec

- Match exact constants, status codes, error texts and response shapes.
- Build LIVE behaviour first; LEGACY/OFF only when asked.
- For an UNKNOWN, choose a sensible value and add one bullet to `docs/deviations.md` (prefix with the section, e.g. `§6 packs`).
- `docs/rebuild-contract.md` overrides the spec where they disagree.
- After adding routes, run `.venv/Scripts/python tools/route_coverage.py` (must stay at 296/296).
