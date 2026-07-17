@echo off
setlocal EnableExtensions
cd /d "%~dp0"

where python >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python not found. Install Python 3.8+ first.
    pause
    exit /b 1
)

if not defined FLOWITH_API_PROFILE set "FLOWITH_API_PROFILE=codex"
if not defined FLOWITH_API_PORT set "FLOWITH_API_PORT=8788"
if not defined FLOWITH_OPEN_DASHBOARD if exist .env (
    for /f "usebackq tokens=1,* delims==" %%A in (`findstr /b /i "FLOWITH_OPEN_DASHBOARD=" .env 2^>nul`) do set "FLOWITH_OPEN_DASHBOARD=%%B"
)

rem Prefer a process-level key from .env when the shell has none.
if not defined FLOWITH_API_KEY if exist .env (
    for /f "usebackq tokens=1,* delims==" %%A in (`findstr /b /i "FLOWITH_API_KEY=" .env 2^>nul`) do set "FLOWITH_API_KEY=%%B"
)

set "PORT_ALREADY_RUNNING=0"
call :check_port %FLOWITH_API_PORT%
if "%PORT_ALREADY_RUNNING%"=="1" (
    call :open_dashboard %FLOWITH_API_PORT%
    exit /b 0
)
if ERRORLEVEL 1 exit /b 1

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

if not defined FLOWITH_API_KEY (
    for /f "usebackq tokens=1,* delims==" %%A in (`findstr /b /i "FLOWITH_API_KEY=" .env 2^>nul`) do set "FLOWITH_API_KEY=%%B"
)
if not defined FLOWITH_API_KEY (
    echo [WARN] FLOWITH_API_KEY is empty. Upstream calls will return 401/502.
)

rd /s /q proxy\__pycache__ 2>nul
rd /s /q proxy\codex\__pycache__ 2>nul

call :open_dashboard %FLOWITH_API_PORT%

echo.
echo =====================================
echo   Flowith Codex/OpenAI Proxy
echo   http://127.0.0.1:%FLOWITH_API_PORT%/v1
echo   Endpoints: /v1/responses, /v1/chat/completions
echo   Dashboard: http://127.0.0.1:%FLOWITH_API_PORT%/dashboard
echo   Profile:   %FLOWITH_API_PROFILE%
echo   Streaming: progressive tool flush + update_plan multi-tool
echo.
echo   Keep ONE 8788 instance. Codex 502 usually means Flowith upstream
echo   failed, not that this port is down. Health: /health
echo   Stop: clean.bat   (or clean.bat --keep-proxy to clean files only)
echo =====================================
echo.

rem Always launch with the venv interpreter so a polluted PATH cannot steal the process.
venv\Scripts\python.exe -m proxy
set "RUN_EXIT=%ERRORLEVEL%"
echo.
echo [INFO] Proxy exited with code %RUN_EXIT%.
pause
exit /b %RUN_EXIT%

:open_dashboard
set "_DASHBOARD_PORT=%~1"
if /i "%FLOWITH_OPEN_DASHBOARD%"=="false" exit /b 0
if /i "%FLOWITH_OPEN_DASHBOARD%"=="0" exit /b 0
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "$inner='$port=[int]$env:_DASHBOARD_PORT; for($i=0; $i -lt 40; $i++) { try { $r=Invoke-WebRequest -Uri (''http://127.0.0.1:{0}/dashboard'' -f $port) -UseBasicParsing -TimeoutSec 1; if ($r.StatusCode -eq 200) { Start-Process (''http://127.0.0.1:{0}/dashboard'' -f $port); break } } catch { Start-Sleep -Milliseconds 500 } }'; Start-Process powershell -WindowStyle Hidden -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-Command',$inner)" >nul 2>&1
exit /b 0

:check_port
set "_PORT=%~1"
netstat -ano | findstr /r /c:":%_PORT% .*LISTENING" >nul 2>&1
if %ERRORLEVEL% NEQ 0 exit /b 0
powershell -NoProfile -ExecutionPolicy Bypass -Command "$port=[int]$env:_PORT; try { $h=Invoke-WebRequest -Uri ('http://127.0.0.1:{0}/health' -f $port) -UseBasicParsing -TimeoutSec 2; if ($h.StatusCode -eq 200 -and $h.Content -match '\"ok\"\s*:\s*true') { exit 10 } } catch {}; exit 20" >nul 2>&1
if %ERRORLEVEL% EQU 10 (
    echo [OK] Proxy already running and healthy on http://127.0.0.1:%_PORT%
    echo      Reusing this instance. Do not start a second 8788 window.
    echo      Tip: Codex "502 Bad Gateway" usually means Flowith upstream failed;
    echo           local /health can still be ok. Retry, or clean.bat then start-codex.bat.
    set "PORT_ALREADY_RUNNING=1"
    exit /b 0
)
echo [ERROR] Port %_PORT% is already in use by a non-dashboard or unhealthy process.
echo         Kill the previous instance, or change the port, then retry.
echo         Tip: from project root, run clean.bat (stops proxies; use --keep-proxy for files only)
echo         Owning PIDs:
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /r /c:":%_PORT% .*LISTENING"') do echo           PID %%P
pause
exit /b 1


