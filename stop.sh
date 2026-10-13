#!/usr/bin/env bash
#
# Stop the UNMER FastAPI ledger service (screen-based, no systemd).

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

echo "Stopping screen session '${SESSION}'..."
as_app screen -S "${SESSION}" -X quit 2>/dev/null || echo "Session not running."
echo "Stopped."
