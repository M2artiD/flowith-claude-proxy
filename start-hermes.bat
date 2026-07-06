@echo off
setlocal
cd /d "%~dp0"
call claude-proxy\start-hermes.bat %*
exit /b %ERRORLEVEL%
