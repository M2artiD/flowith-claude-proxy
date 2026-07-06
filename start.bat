@echo off
setlocal
cd /d "%~dp0"
call claude-proxy\start.bat %*
exit /b %ERRORLEVEL%
