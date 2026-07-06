# Project Agent Rules

## Global Entry

Before starting substantive work, read the shared agent layer when available:

- `C:\Users\qiyan\.agent\GLOBAL_AGENT_RULES.md`
- `C:\Users\qiyan\.agent\SKILL_ROUTING.md`

Then read project-local context if present and relevant to the task:

- `CURRENT_CONTEXT.md`
- `memory/working.md`

Do not claim build, test, fix, proxy compatibility, streaming behavior, paper, evaluation, or delivery success without fresh verification evidence.

## Project Skill Routing

Prefer project-local skills when they match the task, especially:

- `.codex/skills/llm-proxy-streaming/SKILL.md` for SSE, OpenAI-compatible streaming, Responses API, Anthropic Messages, XML/ReAct tool bridges, think-tag filtering, duplicate text, raw XML leaks, or final-chunk truncation.
- `.codex/skills/hermes-proxy-compat/SKILL.md` for Hermes OpenAI-compatible endpoint behavior, `/health`, `/v1/models`, `/v1/chat/completions`, tool-call bridging, or Hermes smoke tests.

Use shared global rules for cross-project boundaries; keep project-specific status and handoff notes in `memory/working.md`, not in the global `.agent` layer.

## Commentary Discipline

Do not send optional commentary at the expense of reasoning quality. Before and after any commentary, preserve sufficient reasoning, inspection, and verification budget. Commentary is only for useful coordination, blockers, or verified progress; it must not cause premature final answers or shallow actions.
