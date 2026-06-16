@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python not found. Install Python 3.8+ first.
    pause
    exit /b 1
)

set "INSTALL_LOCK=%CD%\.install.lock"

:wait_install_lock
mkdir "%INSTALL_LOCK%" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
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

if not exist .env (
    if exist .env.example (
        echo [INFO] .env not found. Creating from .env.example...
        copy .env.example .env >nul
        echo [WARN] Please edit .env and set FLOWITH_API_KEY.
    ) else (
        echo [ERROR] .env not found and .env.example is missing.
    )
    pause
    exit /b 1
)

rd /s /q proxy\__pycache__ 2>nul

set FLOWITH_API_PROFILE=claude

echo.
echo =====================================
echo   Flowith Claude Code Proxy
echo   http://127.0.0.1:8787
echo   Endpoint: /v1/messages
echo   Tool bridge: XML/ReAct
echo =====================================
echo.

python -m proxy
pause
