# kh-helper 1.19.3

The Knowledge Hub client. It is the only thing on the client that holds credentials: the assistant asks the
helper to make a call and gets back the JSON answer; it never sees a password, refresh token or access token.

Three ways in:

- the `kh` CLI (`kh call METHOD PATH [JSON]`, `login`, `status`, `setup`, ...);
- Claude Code hooks (`kh hook <KEY>`: SessionStart (3 parts), UserPromptSubmit, PreToolUse, PostToolUse, Stop,
  PostCompact (3 parts));
- the MCP stdio server `knowledge-hub` (`kh mcp`, 15 tools).

## Install (documented layout)

```
python -m venv <PROFILE>/kh-helper          # PROFILE = $CLAUDE_CONFIG_DIR or ~/.claude
<PROFILE>/kh-helper/Scripts/python -m pip install kh_helper-1.19.3-py3-none-any.whl
<PROFILE>/kh-helper/Scripts/python -m kh_helper.cli login
<PROFILE>/kh-helper/Scripts/python -m kh_helper.cli setup
```

HOME (where the copy keeps state) is `KH_HELPER_HOME`, else the running venv, else `<PROFILE>/kh-helper`.
The sign-in lasts up to 90 days (Keycloak offline session); the refresh token is kept in the OS keyring.

## Tests

```
.venv/Scripts/python -m pytest tools/kh-helper/tests -q
```

The tests use a fake KH/Keycloak (requests adapter), a memory keyring, temp HOME/CLAUDE_CONFIG_DIR and a fake
`claude` runner; they never touch a real profile or server.

## Windows offline installer (not part of this package)

`deploy/windows/kh-setup/` of the original is out of scope here. It would:

- build `KH_Setup` from an earlier package (embeddable Python 3.12, PostgreSQL 17, Keycloak fork, Java 21 JRE,
  kh-helper wheels), the PyInstaller `KnowledgeHub.exe` and the helper wheel; optionally refresh the seed from
  prod (`/hooks/list`, `/policy/actions`, `/policy/plan`);
- (A) `install-kh.cmd`: copy the payload, create the `kh-postgres` service and Keycloak DB, configure Keycloak
  (realm `knowledge-hub`, admin client, agent admin, the installing person with a one-time password, tenants),
  write `kh\.env`, register the `KH Keycloak` / `KH Server` scheduled tasks, tray app and firewall rules, then
  seed `lesson-learned`, `market`, `kh-tools`, the hook rules and the action table;
- (B) `setup-kh-helper.cmd --kh-url http://<host>:9000`: find or install Python >= 3.10, create
  `<profile>\kh-helper`, install the helper offline, check `/health`, `kh login`, `kh setup`, and verify
  `kh me`, `/hooks/list` and `/policy/actions`.
