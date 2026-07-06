---
name: llm-proxy-streaming
description: Debug and fix streaming behavior in LLM proxy adapters. Use when SSE chunks, OpenAI chat/completions streams, Responses API streams, Anthropic Messages streams, XML/ReAct tool bridges, or think-tag filtering lose text, duplicate text, leak raw tool XML, truncate final chunks, mishandle partial tags, or diverge between streamed deltas and final completed payloads.
---

# LLM Proxy Streaming

Use a red-green workflow. Streaming bugs are often boundary bugs, so prove the exact lost, duplicated, or leaked bytes before changing parser logic.

## Workflow

1. Reproduce with fake upstream chunks first.
   - Prefer unit tests that call the real streaming adapter/generator.
   - Split chunks at hostile boundaries: `"<th"`, `"</thin"`, `"<tool"`, `"</tool_call"`, JSON argument fragments, multibyte/CJK text, and empty final chunks.
   - Assert observable client output, not internal buffers.

2. Verify RED.
   - Confirm the test fails because visible text is missing, duplicated, or leaked.
   - Keep the failing assertion narrow: expected delta text, completed `output_text`, Anthropic `text_delta`, or tool call object.

3. Fix only the proven path.
   - Distinguish real protocol markers from unconfirmed partial prefixes.
   - At end of stream, flush pending text that is outside a confirmed hidden block.
   - Do not flush pending text that is inside a confirmed `<think>...</think>` reasoning block or a confirmed tool XML block.
   - If a partial marker was held only to wait for the next chunk and no next chunk arrives, treat it as visible text.

4. Verify GREEN and surrounding behavior.
   - Run the focused regression.
   - Run the surrounding streaming tests for chat, Responses, and Anthropic Messages.
   - Run the full test suite and compile check before claiming completion.

## Guardrails

- Do not require a repaired stream to preserve original upstream chunk boundaries. SSE may split `"answer <th"` into `"answer "` and `"<th"`; the requirement is lossless aggregate visible text.
- Keep `<think>` semantics intact: complete think blocks should not leak into visible answer text.
- Keep XML/ReAct semantics intact: complete tool calls should become tool objects and raw XML should not leak.
- Avoid broad parser rewrites unless three focused attempts fail. Most chunk truncation bugs are final-flush or pending-buffer state bugs.
