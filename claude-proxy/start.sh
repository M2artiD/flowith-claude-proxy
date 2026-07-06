#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[ERROR] Python 3.8+ not found."
  exit 1
fi

export FLOWITH_API_PROFILE="${FLOWITH_API_PROFILE:-claude}"
export FLOWITH_API_PORT="${FLOWITH_API_PORT:-8787}"
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

check_port() {
  local port="$1"
  local listening=1
  if command -v ss >/dev/null 2>&1; then
    ss -ltn "( sport = :${port} )" | grep -q ":${port}" && listening=0 || listening=1
  elif command -v netstat >/dev/null 2>&1; then
    netstat -an | grep -E "[.:]${port}[[:space:]].*LISTEN" >/dev/null && listening=0 || listening=1
  else
    return 0
  fi
  [[ "$listening" -ne 0 ]] && return 0

  local health_status=20
  python3 - "$port" <<'PY' >/dev/null 2>&1 || health_status=$?
import json
import sys
import urllib.request

port = int(sys.argv[1])
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
        data = json.loads(r.read().decode("utf-8"))
        if r.status == 200 and data.get("ok") is True:
            sys.exit(10)
except Exception:
    pass
sys.exit(20)
PY
  if [[ "$health_status" == "10" ]]; then
    echo "[OK] Proxy already running on http://127.0.0.1:${port}"
    echo "     Reuse this instance, or stop it before launching a fresh one."
    exit 0
  fi

  echo "[ERROR] Port ${port} is already in use by a non-proxy or unhealthy process."
  echo "        Kill the previous instance, or change FLOWITH_API_PORT, then retry."
  echo "        On Windows, clean.bat --stop-proxy can stop known proxy listeners."
  exit 1
}

check_port "${FLOWITH_API_PORT}"

INSTALL_LOCK=".install.lock"
INSTALL_LOCK_HELD=0
cleanup_lock() {
  if [[ "$INSTALL_LOCK_HELD" == "1" ]]; then
    rmdir "$INSTALL_LOCK" >/dev/null 2>&1 || true
  fi
}
trap cleanup_lock EXIT

until mkdir "$INSTALL_LOCK" >/dev/null 2>&1; do
  echo "[INFO] Another launcher is installing dependencies. Waiting..."
  sleep 2
done
INSTALL_LOCK_HELD=1

if [[ -x venv/bin/python ]] && ! venv/bin/python -c "import pip" >/dev/null 2>&1; then
  echo "[WARN] Existing venv has broken pip. Recreating venv..."
  rm -rf venv
fi


if [[ ! -d venv ]]; then
  echo "[INFO] Creating venv..."
  python3 -m venv venv
fi

source venv/bin/activate

python -m ensurepip --upgrade >/dev/null 2>&1 || true
python -m pip install -q -r requirements.txt
cleanup_lock
INSTALL_LOCK_HELD=0

rm -rf proxy/__pycache__

if [[ ! -f .env ]]; then
  if [[ -f .env.example ]]; then
    echo "[INFO] .env not found. Creating from .env.example..."
    cp .env.example .env
    echo "[WARN] Edit claude-proxy/.env and set FLOWITH_API_KEY."
  else
    echo "[ERROR] .env not found and .env.example is missing."
  fi
  exit 1
fi

echo
echo "====================================="
echo "  Flowith Proxy"
echo "  http://127.0.0.1:${FLOWITH_API_PORT}"
echo "  Profile: ${FLOWITH_API_PROFILE}"
echo "  Streaming: text/tool stream guard enabled"
echo "====================================="
echo

python -m proxy
