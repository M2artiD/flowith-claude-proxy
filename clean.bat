@echo off
setlocal

cd /d "%~dp0"
set "ROOT_DIR=%CD%"
set "PROXY_DIR=%ROOT_DIR%\claude-proxy"
set "REMOVE_VENV=0"
set "NO_PAUSE=0"

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
if /i "%~1"=="--help" (
    call :usage
    exit /b 0
)
if /i "%~1"=="/?" (
    call :usage
    exit /b 0
)

echo [ERROR] Unknown option: %~1
call :usage
exit /b 1

:args_done
if not exist "%PROXY_DIR%\" (
    echo [ERROR] Expected folder not found: "%PROXY_DIR%"
    set "EXIT_CODE=1"
    goto finish
)

echo [INFO] Cleaning Flowith Claude/Codex Proxy local artifacts...
echo [INFO] Project: "%ROOT_DIR%"
echo.

call :remove_empty_lock "%PROXY_DIR%\.install.lock"
call :remove_tree "%ROOT_DIR%\.pytest_cache" "root pytest cache"
call :remove_tree "%PROXY_DIR%\.pytest_cache" "proxy pytest cache"
call :remove_tree "%PROXY_DIR%\proxy\__pycache__" "proxy bytecode cache"
call :remove_tree "%PROXY_DIR%\tests\__pycache__" "test bytecode cache"

if "%REMOVE_VENV%"=="1" (
    call :remove_tree "%PROXY_DIR%\venv" "virtual environment"
) else (
    echo [INFO] Keeping venv. Run clean.bat --venv to force dependency reinstall on next launch.
)

echo.
echo [DONE] Clean complete.
set "EXIT_CODE=0"
goto finish

:remove_empty_lock
set "TARGET=%~1"
if not exist "%TARGET%\" (
    echo [OK] Dependency install lock not found.
    exit /b 0
)

set "LOCK_HAS_CONTENT="
for /f "delims=" %%A in ('dir /a /b "%TARGET%" 2^>nul') do set "LOCK_HAS_CONTENT=1"
if defined LOCK_HAS_CONTENT (
    echo [WARN] Dependency install lock is not empty; leaving it in place: "%TARGET%"
    exit /b 0
)

rmdir "%TARGET%" 2>nul
if exist "%TARGET%\" (
    echo [WARN] Could not remove dependency install lock: "%TARGET%"
) else (
    echo [OK] Removed dependency install lock.
)
exit /b 0

:remove_tree
set "TARGET=%~1"
set "LABEL=%~2"
if exist "%TARGET%\" (
    echo [INFO] Removing %LABEL%...
    rd /s /q "%TARGET%" 2>nul
    if exist "%TARGET%\" (
        echo [WARN] Could not remove %LABEL%: "%TARGET%"
    ) else (
        echo [OK] Removed %LABEL%.
    )
) else (
    echo [OK] %LABEL% not found.
)
exit /b 0

:usage
echo Usage: clean.bat [--venv] [--no-pause]
echo.
echo Default cleanup:
echo   - claude-proxy\.install.lock if it is empty
echo   - .pytest_cache folders
echo   - project __pycache__ folders
echo.
echo Options:
echo   --venv      Also delete claude-proxy\venv so dependencies reinstall next launch.
echo   --no-pause  Do not wait for a keypress at the end.
exit /b 0

:finish
if "%NO_PAUSE%"=="0" pause
exit /b %EXIT_CODE%
