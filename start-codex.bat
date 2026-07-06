@echo off
setlocal
cd /d "%~dp0"
call claude-proxy\start-codex.bat %*
exit /b %ERRORLEVEL%
