@echo off
setlocal
cd /d "%~dp0"
rem Root wrapper for the Codex/OpenAI proxy on port 8788.
rem Prefer this over ad-hoc `python -m proxy` so profile/port stay correct.
call claude-proxy\start-codex.bat %*
exit /b %ERRORLEVEL%
