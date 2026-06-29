# Agent Boundaries

This file records the local workspace boundary for future agents.

## Committed Fix Surface

The minimal startup fix is limited to:

- `claude-proxy/proxy/config.py`
- `claude-proxy/tests/test_config.py`

Do not remove or rewrite `FLOWITH_REQUIRE_SERVER_KEY` unless the user explicitly asks to change the server-key enforcement behavior. The default must stay compatible with existing client-supplied API key behavior.

## Existing Local Work To Leave Alone

The following files had local changes or were untracked before this boundary note was created. Do not modify, format, stage, commit, delete, or revert them unless the user explicitly asks for that specific file or task:

- `README.md`
- `claude-proxy/proxy/codex/router.py`
- `claude-proxy/proxy/server.py`
- `claude-proxy/start-codex.bat`
- `claude-proxy/start-hermes.bat`
- `claude-proxy/start.bat`
- `memory/working.md`
- `start-codex.bat`
- `start-hermes.bat`
- `start.bat`
- `claude-proxy/proxy/server.py.bak`
- `clean.bat`

If a future task requires touching one of these files, inspect the current diff first and preserve unrelated user or prior-agent changes.
