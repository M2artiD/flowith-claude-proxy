# Project Memory

Current baseline (2026-07-16): the Anthropic proxy on port 8787 compacts oversized Fable contexts and makes one configured non-Fable fallback request if Fable still returns no content before any delta is delivered. The Flowith GPT-5.6 Codex proxy on port 8788 independently enforces real tool execution across direct actions, mandatory/tool-directed and Chinese object-fronted phrasing, colloquial continuations, and failed-tool recovery, while explanation-only and negated turns remain tool-optional. Botcf and Hermes port 8789 remain outside these changes.

- [Codex GPT-5.6 tool repair](memory/codex-5-6-tool-repair-2026-07-15.md)
- [Codex GPT-5.6 provider troubleshooting](memory/codex-gpt-5.6-fix-2026-07-11.md)
- [Fable context watchdog](memory/fable-context-watchdog-2026-07-15.md)
- [Hermes stream duplicate repair](memory/hermes-stream-duplicate-repair-2026-07-15.md)
- [Current working notes](memory/working.md)

## Workspace-local skills

- [LLM proxy streaming](.codex/skills/llm-proxy-streaming/SKILL.md)
- [Hermes proxy compatibility](.codex/skills/hermes-proxy-compat/SKILL.md)

These skills are intentionally gitignored by the current project policy and exist only in this workspace unless that policy changes.
