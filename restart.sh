#!/usr/bin/env bash
#
# Restart the UNMER FastAPI ledger service (screen-based, no systemd).

set -euo pipefail

APP_USER="apache"
APP_DIR="/var/www/unmer.ac.id/fastapi-ledger"
SESSION="fastapi-ledger"
PORT="8000"

# The apache account uses /sbin/nologin — prefer runuser over sudo -u.
as_app() {
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "${APP_USER}" -- "$@"
  else
    su -s /bin/bash "${APP_USER}" -c "$*"
  fi
}

echo "Stopping screen session '${SESSION}'..."
as_app screen -S "${SESSION}" -X quit 2>/dev/null || echo "Session not running."
sleep 1

echo "Starting service (single worker)..."
cd "${APP_DIR}"
# NOTE: exactly one uvicorn worker (hash-chain write lock is per-process).
as_app screen -dmS "${SESSION}" \
  "${APP_DIR}/venv/bin/uvicorn" main:app --host 127.0.0.1 --port "${PORT}" --workers 1
sleep 2

echo "Verifying..."
( ss -ltnp 2>/dev/null || netstat -ltnp 2>/dev/null ) | grep ":${PORT}" || true
if curl -sf --max-time 10 "http://127.0.0.1:${PORT}/api/ledger/audit"; then
  echo
else
  echo "ERROR: service did not respond on 127.0.0.1:${PORT}" >&2
  exit 1
fi
echo "Restarted."
