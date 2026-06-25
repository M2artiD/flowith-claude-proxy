# Working Notes

- Fixed streaming duplication / raw XML leakage across `chat.completions`, `responses`, and Claude Code `/v1/messages`.
- Added regression tests for:
  - OpenAI chat streaming with tools
  - OpenAI responses streaming with tools
  - Claude Code streaming text around XML tool calls
- Updated launcher `.bat` files at repo root and under `claude-proxy/` to reflect the new text/tool stream guard and UTF-8 console setup.
- Verified with `python -m pytest -q` from `claude-proxy/`: `38 passed, 5 subtests passed`.
- Verified `python -m compileall C:\Users\qiyan\Desktop\flowith-claude-proxy\claude-proxy\proxy -q`.
- Follow-up fix: `responses` streaming with tools no longer repeats completed text in final SSE payloads.
  - Root cause: some clients render `response.output_text.delta`, `response.output_text.done`, `response.output_item.done`, and `response.completed` text fields together.
  - Fix: in tools streaming mode, visible text is emitted through deltas only; final structural events keep empty text fields.
  - Manual verification showed `delta` chunks once each and `full_text_field=0`, `full_output_text=0`.
- Existing dirty files left untouched for now:
  - `claude-proxy/proxy/__main__.py`
  - `claude-proxy/proxy/config.py`
  - `claude-proxy/proxy/upstream.py`
- Those three appear to be prior tracing / UTF-8 / debug-instrumentation changes, not part of the streaming fix.
