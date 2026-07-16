# Changelog

## 2026-07-16

- Tightened the GPT-5.6 action gate without returning to force-tools-on-every-turn behavior: Chinese object-fronted requests such as `这些话为我提炼成...写在当前工作区` and colloquial continuations such as `继续啊` now require execution, while greetings, negated actions, and explanation-only quotations remain tool-optional.
- Added bounded Fable empty-response recovery on the Anthropic path: after context compaction and same-model recovery are exhausted without delivering a delta, the proxy can make one transparent request with a configured non-Fable model; a Fable first-byte idle timeout now returns immediately to this route-level fallback instead of spending another full same-model watchdog window.
- Closed an action-classification gap found by a real Codex CLI probe: mandatory Chinese forms such as `必须实际调用`, delivery phrases such as `写一个...`, and English `use <tool> to <verb>` requests now enter the GPT-5.6 required-tool path instead of accepting a promise plus a premature success marker.
- Tightened GPT-5.6 continuation handling: terse approvals and short failure reports now inherit an active task, tool-output detection survives trailing reasoning/metadata and custom output shapes, no-tool correction allows two buffered retries, and complex tool notes carry a public decision brief with diagnosis, tradeoff, action, and validation instead of mechanical step counting.
- Strengthened GPT-5.6 execution visibility and anti-avoidance on the Codex Responses path: high reasoning requests are forwarded to Flowith thinking controls, explicit public plans can retain up to four numbered steps with adjacent duplicates removed, and required actions or unfinished tool-result progress receive up to two buffered corrections when the model returns prose without a tool call.
- Kept reasoning reporting honest: Flowith `gpt-5.6-sol` currently returns no separate reasoning channel even when `thinking=true`, so the proxy preserves a concise public execution plan instead of fabricating hidden chain-of-thought.

## 2026-07-15

- Prevented Codex from reporting `idle timeout waiting for SSE` during slow Flowith turns: the Responses stream now emits a real `response.in_progress` event every five seconds instead of relying only on SSE comments that Codex does not count as event activity.
- Fixed Fable empty replies and missing tool calls in real Claude Code requests with the full tool catalog: oversized tool descriptions are now bounded, and long-context trimming keeps a continuous recent suffix instead of stitching stale turns around skipped messages.
- Balanced GPT-5.6 tool enforcement: greetings, casual conversation, explanation-only questions, and explicitly negated actions no longer force tools, while terminal/file/browser/build requests still require real calls.
- Added user-visible GPT-5.6 tool feedback before every call: a concise operational reason, tool name, and exact command/action. The Responses stream now removes premature result text and closes the feedback item before emitting the function call.
- Prevented GPT-5.6 from ending a multi-step tool task after an intermediate observation with only a progress update or future-tense promise; incomplete work must continue with the next tool call.
- Fixed Desktop-style long conversations losing GPT-5.6 tool enforcement after an earlier tool call. A new user action now requires a tool again; only the immediate tool-result follow-up is allowed to answer normally.
- Hardened GPT-5.6 tool enforcement: explicit terminal/GUI/process launches and file writes must call tools, narration or manual-work instructions cannot replace execution, and success/failure cannot be claimed before a real tool observation.
- Updated the existing CC Switch Codex `flowith` provider to load the GPT-5.6 model catalog, without changing its stored authentication or the current `botcf 1` selection.
- Fixed Codex 0.144.x tool and terminal support for the direct Flowith GPT-5.6 model names by adding a local model catalog and an independent 8788 profile.
- Preserved the existing botcf base configuration; `codex -p flowith-8788` explicitly selects the local proxy.
- Verified real `gpt-5.6-sol` terminal execution, function-tool execution, tool-result round trips, and final responses through port 8788.
