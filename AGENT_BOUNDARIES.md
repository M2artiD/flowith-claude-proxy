# Agent Boundaries

This file records the local workspace boundary for future agents.

## Committed Fix Surface

The minimal startup fix is limited to:

- `claude-proxy/proxy/config.py`
- `claude-proxy/tests/test_config.py`

Do not remove or rewrite `FLOWITH_REQUIRE_SERVER_KEY` unless the user explicitly asks to change the server-key enforcement behavior. The default must stay compatible with existing client-supplied API key behavior.

## Dirty-Work Handling

The previously dirty files were reviewed and handled in small follow-up commits. Future agents should rely on `git status --short` for the current workspace state rather than this file's old dirty-file list.

If future local changes appear, inspect the diff first and preserve unrelated user or prior-agent changes. Do not delete backup, cache, lock, or generated files unless the user asks to clean them or the file is verified to be a disposable local artifact.
