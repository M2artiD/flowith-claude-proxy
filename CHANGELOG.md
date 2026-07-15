# Changelog

## 2026-07-15

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
