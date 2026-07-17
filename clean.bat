@echo off
setlocal
cd /d "%~dp0"

set "REMOVE_VENV=0"
set "NO_PAUSE=0"
rem Double-click / default: clean files AND stop local proxy processes.
set "STOP_PROXY=1"

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--venv" (
    set "REMOVE_VENV=1"
    shift
    goto parse_args
)
if /i "%~1"=="--no-pause" (
    set "NO_PAUSE=1"
    shift
    goto parse_args
)
if /i "%~1"=="--stop-proxy" (
    set "STOP_PROXY=1"
    shift
    goto parse_args
)
if /i "%~1"=="--keep-proxy" (
    set "STOP_PROXY=0"
    shift
    goto parse_args
)
if /i "%~1"=="--no-stop-proxy" (
    set "STOP_PROXY=0"
    shift
    goto parse_args
)
if /i "%~1"=="--help" (
    shift
    goto parse_args_for_help
)
if /i "%~1"=="/?" (
    shift
    goto parse_args_for_help
)
echo [ERROR] Unknown option: %~1
goto show_usage

:parse_args_for_help
if "%~1"=="" goto show_usage
if /i "%~1"=="--no-pause" (
    set "NO_PAUSE=1"
    shift
    goto parse_args_for_help
)
shift
goto parse_args_for_help

:args_done
set "REMOVE_VENV_ARG="
set "STOP_PROXY_ARG="
if "%REMOVE_VENV%"=="1" set "REMOVE_VENV_ARG=-RemoveVenv"
if "%STOP_PROXY%"=="1" set "STOP_PROXY_ARG=-StopProxy"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\clean.ps1" -Root "%~dp0." %REMOVE_VENV_ARG% %STOP_PROXY_ARG%
set "EXIT_CODE=%ERRORLEVEL%"
goto finish

:show_usage
echo Usage: clean.bat [--venv] [--keep-proxy] [--stop-proxy] [--no-pause]
echo.
echo Default (double-click):
echo   - Stop proxy listeners on 8787/8788/8789 and orphan python -m proxy processes
echo   - Remove install lock (if empty), pytest/pycache, debug dumps, logs
echo   - Remove _*.py / _*.json / _*.txt / _*.out / _*.bat / _*.ps1 probe helpers
echo   - Remove *_dump.txt and claude-proxy\proxy\*.bak
echo.
echo Options:
echo   --venv          Also delete claude-proxy\venv so dependencies reinstall next launch.
echo   --keep-proxy    Do NOT stop proxy processes; only clean files.
echo   --no-stop-proxy Same as --keep-proxy.
echo   --stop-proxy    Explicitly stop proxies (default; kept for compatibility).
echo   --no-pause      Do not wait for a keypress at the end.
set "EXIT_CODE=0"
goto finish

:finish
if "%NO_PAUSE%"=="0" pause
exit /b %EXIT_CODE%
