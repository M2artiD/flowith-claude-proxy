#!/usr/bin/env bash
# Flowith Claude Proxy — Linux/macOS one-click launcher
# Usage: ./start.sh [--port 8787] [--host 127.0.0.1]

set -e

HOST="127.0.0.1"
PORT="8787"

# Allow --host / --port overrides
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    *) shift ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 1. Check .env
if [ ! -f ".env" ]; then
  if [ -f ".env.example" ]; then
    cp .env.example .env
    echo "[!] .env created from .env.example — please edit it and set FLOWITH_API_KEY"
  else
    echo "[!] Please create .env with FLOWITH_API_KEY=your_key"
  fi
  exit 1
fi

# 2. Activate venv if present
if [ -f ".venv/bin/activate" ]; then
  echo "[*] Activating virtualenv ..."
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# 3. Ensure the package is installed
if ! python -c "import flowith_claude_proxy" 2>/dev/null; then
  echo "[*] Installing package (first run) ..."
  pip install -q -e .
fi

# 4. Launch
echo ""
python -m flowith_claude_proxy --host "$HOST" --port "$PORT"
