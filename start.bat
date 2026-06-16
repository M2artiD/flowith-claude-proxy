@echo off
setlocal
cd /d "%~dp0claude-proxy"

where python >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python not found. Install Python 3.8+ first.
    pause
    exit /b 1
)

if not exist venv\Scripts\activate.bat (
    echo [INFO] Creating venv...
    python -m venv venv
)

call venv\Scripts\activate.bat

echo [INFO] Installing dependencies...
pip install -q -r requirements.txt >nul 2>&1

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
