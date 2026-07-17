# Project Memory

Current baseline (2026-07-17): GPT-5.6 Codex 8788 now progressively releases required-tool streams once tool XML appears, allows multi-tool/`update_plan` turns, keeps up to 8 public plan steps, and ships hardened `start-codex.bat` / `clean.bat` (default double-click stops 8787/8788/8789; `--keep-proxy` keeps them) for 8788 reuse and Codex 502 troubleshooting. Prior baseline (2026-07-16): the Anthropic proxy on port 8787 compacts oversized Fable contexts and makes one configured non-Fable fallback request if Fable still returns no content before any delta is delivered. The Flowith GPT-5.6 Codex proxy on port 8788 independently enforces real tool execution across direct actions, mandatory/tool-directed and Chinese object-fronted phrasing, colloquial continuations, and failed-tool recovery, while explanation-only and negated turns remain tool-optional. Hermes port 8789 separately compacts duplicate Responses snapshots and enables a single-answer prompt for semantic recap repetition; its CN Desktop configuration disables separate interim assistant messages. Botcf remains outside these changes.

- [Codex GPT-5.6 tool repair](memory/codex-5-6-tool-repair-2026-07-15.md)
- [Codex GPT-5.6 provider troubleshooting](memory/codex-gpt-5.6-fix-2026-07-11.md)
- [Fable context watchdog](memory/fable-context-watchdog-2026-07-15.md)
- [Hermes stream duplicate repair](memory/hermes-stream-duplicate-repair-2026-07-15.md)
- [Current working notes](memory/working.md)

## Workspace-local skills

- [LLM proxy streaming](.codex/skills/llm-proxy-streaming/SKILL.md)
- [Hermes proxy compatibility](.codex/skills/hermes-proxy-compat/SKILL.md)

These skills are intentionally gitignored by the current project policy and exist only in this workspace unless that policy changes.
