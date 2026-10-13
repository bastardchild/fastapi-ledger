#!/usr/bin/env bash
#
# Stop the UNMER FastAPI ledger service (tmux-based, no systemd).

set -euo pipefail

APP_USER="apache"
SESSION="fastapi-ledger"

# The apache account uses /sbin/nologin — prefer runuser over sudo -u.
as_app() {
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "${APP_USER}" -- "$@"
  else
    su -s /bin/bash "${APP_USER}" -c "$*"
  fi
}

echo "Stopping tmux session '${SESSION}'..."
as_app tmux kill-session -t "${SESSION}" 2>/dev/null || echo "Session not running."
pkill -u "${APP_USER}" -f "uvicorn main:app" 2>/dev/null || true
echo "Stopped."
