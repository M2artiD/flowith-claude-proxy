@echo off
setlocal
cd /d "%~dp0claude-proxy"

where python >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python not found. Install Python 3.8+ first.
    pause
    exit /b 1
)

set "INSTALL_LOCK=%CD%\.install.lock"
set "INSTALL_LOCK_STALE_SECONDS=300"

:wait_install_lock
mkdir "%INSTALL_LOCK%" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$lock=$env:INSTALL_LOCK; $item=Get-Item -LiteralPath $lock -ErrorAction SilentlyContinue; if (-not $item) { exit 0 }; $age=((Get-Date)-$item.LastWriteTime).TotalSeconds; $active=Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and ($_.CommandLine -match 'requirements\.txt' -or $_.CommandLine -match 'pip install' -or $_.CommandLine -match 'ensurepip' -or $_.CommandLine -match 'python.* -m venv') } | Select-Object -First 1; if (($age -gt [int]$env:INSTALL_LOCK_STALE_SECONDS) -and -not $active) { exit 42 }; exit 0" >nul 2>&1
    if ERRORLEVEL 42 (
        if not ERRORLEVEL 43 (
            echo [WARN] Stale dependency install lock found. Clearing it...
            rmdir "%INSTALL_LOCK%" >nul 2>&1
            goto wait_install_lock
        )
    )
    echo [INFO] Another launcher is installing dependencies. Waiting...
    timeout /t 2 /nobreak >nul
    goto wait_install_lock
)

if exist venv\Scripts\python.exe (
    venv\Scripts\python.exe -c "import pip" >nul 2>&1
    if %ERRORLEVEL% NEQ 0 (
        echo [WARN] Existing venv has broken pip. Recreating venv...
        rd /s /q venv
    )
)

if not exist venv\Scripts\python.exe (
    echo [INFO] Creating venv...
    python -m venv venv
)

echo [INFO] Installing dependencies...
venv\Scripts\python.exe -m ensurepip --upgrade >nul 2>&1
venv\Scripts\python.exe -m pip install -q -r requirements.txt
set "PIP_EXIT=%ERRORLEVEL%"
rmdir "%INSTALL_LOCK%" >nul 2>&1
if %PIP_EXIT% NEQ 0 (
    echo [ERROR] Dependency install failed.
    pause
    exit /b %PIP_EXIT%
)

call venv\Scripts\activate.bat

chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if not exist .env (
    if exist .env.example (
        echo [INFO] .env not found. Creating from .env.example...
        copy .env.example .env >nul
        echo [WARN] Please edit claude-proxy\.env and set FLOWITH_API_KEY.
    ) else (
        echo [ERROR] .env not found and .env.example is missing.
    )
    pause
    exit /b 1
)

rd /s /q proxy\__pycache__ 2>nul

set FLOWITH_API_PROFILE=codex
set FLOWITH_API_PORT=8788

echo.
echo =====================================
echo   Flowith Codex/OpenAI Proxy
echo   http://127.0.0.1:8788/v1
echo   Endpoints: /v1/responses, /v1/chat/completions
echo   Streaming: text/tool stream guard enabled
echo =====================================
echo.

python -m proxy
pause
