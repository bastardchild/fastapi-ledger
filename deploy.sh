#!/usr/bin/env bash
#
# Safe deployment runbook for the UNMER Digital Ledger stack on
# AlmaLinux / Rocky Linux / RHEL (no systemd).
#
# Safety principles — no drastic changes on the production server:
#   - ledger.db is NEVER overwritten or deleted: it is excluded from rsync
#     and backed up before anything else changes.
#   - Apache is only gracefully reloaded, and only when its config snippet
#     actually changed (no hard restarts, so live connections survive).
#   - Only missing OS packages are installed; a single unavailable package
#     cannot abort the whole run.
#   - The old service is stopped before a new one starts, so two processes
#     never fight over the port.
#   - The venv is reused when healthy instead of rebuilt every time.
#   - Crontab changes are opt-in (SETUP_CRON, default on).
#
# Usage (run as a user with sudo privileges):
#   sudo bash deploy.sh
#   sudo DRY_RUN=1 bash deploy.sh            # preview only, changes nothing
#   sudo SETUP_CRON=0 bash deploy.sh        # leave apache's crontab alone
#   sudo INSTALL_OS_PACKAGES=0 bash deploy.sh
#   sudo RELOAD_APACHE=0 bash deploy.sh     # skip even a graceful reload
#   sudo REPO_DIR=/path/to/checkout bash deploy.sh
#
# Review each step before production.

set -euo pipefail

APP_USER="apache"
APP_GROUP="apache"

# WordPress document root for unmer.ac.id:
WP_ROOT="/var/www/unmer.ac.id"

# The FastAPI service lives inside the WordPress web root:
APP_DIR="${WP_ROOT}/fastapi-ledger"

# Repository checkout root (script dir's parent by default):
REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# WordPress plugins source (in the repo) and destination (on the server):
PLUGIN_SRC="${REPO_DIR}/wp-content/plugins/unmer-ledger"
PLUGIN_DST="${WP_ROOT}/wp-content/plugins/unmer-ledger"

# Timestamped backups of irreplaceable data live here (outside the web root):
BACKUP_ROOT="/var/backups/unmer-ledger"

VENV_DIR="venv"
SESSION="fastapi-ledger"
PORT="8000"

DRY_RUN="${DRY_RUN:-0}"
SETUP_CRON="${SETUP_CRON:-1}"
INSTALL_OS_PACKAGES="${INSTALL_OS_PACKAGES:-1}"
RELOAD_APACHE="${RELOAD_APACHE:-1}"

# Run a command as the app user. The apache account uses /sbin/nologin on
# AlmaLinux/RHEL, so plain `sudo -u apache` fails — prefer runuser.
as_app() {
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "${APP_USER}" -- "$@"
  else
    su -s /bin/bash "${APP_USER}" -c "$*"
  fi
}

echo "== 0. Verify the repository checkout =="
if [ ! -d "${PLUGIN_SRC}" ]; then
  echo "ERROR: plugin source not found at ${PLUGIN_SRC}" >&2
  echo "Run this script from a repository checkout, or export REPO_DIR." >&2
  exit 1
fi
if [ ! -f "${REPO_DIR}/fastapi-ledger/main.py" ]; then
  echo "ERROR: FastAPI source not found at ${REPO_DIR}/fastapi-ledger/main.py" >&2
  exit 1
fi

if [ "${DRY_RUN}" = "1" ]; then
  echo "DRY RUN — no changes will be made. Planned actions:"
  echo "  - check (and only if missing, install) OS packages for: python3, screen, crontab, ss, rsync, curl, semanage, logrotate"
  echo "  - select the first Python >= 3.10 among python3.12, python3.11, python3"
  echo "  - back up ${APP_DIR}/ledger.db and ${PLUGIN_DST} (if they exist) under ${BACKUP_ROOT}/<timestamp>/"
  echo "  - rsync ${REPO_DIR}/fastapi-ledger/ -> ${APP_DIR}/ (extra server files are kept, ledger.db excluded)"
  echo "  - reuse the venv when healthy, else recreate it; install the app with its venv pip"
  echo "  - write .htaccess + httpd deny snippet + logrotate config only if their content changed"
  echo "  - graceful Apache reload only if the deny snippet changed"
  echo "  - stop screen session '${SESSION}', start a new single-worker uvicorn on 127.0.0.1:${PORT}"
  echo "  - refresh the @reboot cron entry for ${APP_USER} (only if SETUP_CRON=1)"
  echo "  - verify the listener and the API on 127.0.0.1:${PORT}"
  exit 0
fi

echo "== 1. Install missing OS packages only =="
if [ "${INSTALL_OS_PACKAGES}" = "1" ]; then
  # binary -> dnf package. Each missing binary is installed on its own so a
  # single unavailable package cannot abort the whole transaction.
  for spec in "python3:python3" "screen:screen" "crontab:cronie" "ss:iproute" "rsync:rsync" "curl:curl" "semanage:policycoreutils-python-utils" "logrotate:logrotate"; do
    bin="${spec%%:*}"
    pkg="${spec##*:}"
    if command -v "$bin" >/dev/null 2>&1; then
      echo "  have $bin ($pkg), skipping"
    else
      echo "  installing $pkg ..."
      sudo dnf install -y "$pkg" || echo "  WARNING: could not install $pkg, continuing"
    fi
  done
else
  echo "  (skipped: INSTALL_OS_PACKAGES=0)"
fi

echo "== 1b. Select Python >= 3.10 =="
PYBIN=""
for candidate in python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 \
    && "$candidate" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
    PYBIN="$candidate"
    break
  fi
done
if [ -z "$PYBIN" ]; then
  echo "  No Python >= 3.10 found; attempting to install python3.12 ..."
  sudo dnf install -y python3.12 2>/dev/null || true
  FALLBACK_PY="python3.12"
  if command -v "$FALLBACK_PY" >/dev/null 2>&1 \
    && "$FALLBACK_PY" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
    PYBIN="$FALLBACK_PY"
  else
    echo "ERROR: need Python >= 3.10 (tried python3.12/3.11/3 and dnf install python3.12)" >&2
    exit 1
  fi
fi
echo "  using $PYBIN"

# screen supervises the service; fail fast with a clear hint instead of an
# obscure runuser error later.
if ! command -v screen >/dev/null 2>&1; then
  echo "ERROR: 'screen' is not installed and could not be installed." >&2
  echo "On Rocky/AlmaLinux enable EPEL (dnf install epel-release) or ask to switch supervision to tmux." >&2
  exit 1
fi

echo "== 2. Create directories =="
sudo mkdir -p "${APP_DIR}"

echo "== 3. Back up irreplaceable data first =="
TS="$(date +%Y%m%d-%H%M%S)"
if [ -f "${APP_DIR}/ledger.db" ] || [ -d "${PLUGIN_DST}" ]; then
  sudo mkdir -p "${BACKUP_ROOT}/${TS}"
  if [ -f "${APP_DIR}/ledger.db" ]; then
    sudo cp -p "${APP_DIR}/ledger.db" "${BACKUP_ROOT}/${TS}/ledger.db"
    echo "  backed up ledger.db -> ${BACKUP_ROOT}/${TS}/ledger.db"
  fi
  if [ -d "${PLUGIN_DST}" ]; then
    sudo cp -rp "${PLUGIN_DST}" "${BACKUP_ROOT}/${TS}/plugin-unmer-ledger"
    echo "  backed up plugin -> ${BACKUP_ROOT}/${TS}/plugin-unmer-ledger"
  fi
else
  echo "  (nothing to back up: fresh install)"
fi

echo "== 4. Copy the FastAPI source into place =="
# From the repository checkout. Files are only added/updated, never deleted,
# so extra files on the server survive. ledger.db is excluded regardless.
sudo rsync -a --exclude '.venv' --exclude 'venv' --exclude '__pycache__' \
  --exclude '.pytest_cache' --exclude '.htaccess' \
  --exclude 'ledger.db' --exclude 'ledger.log' \
  "${REPO_DIR}/fastapi-ledger/" "${APP_DIR}/"

echo "== 5. Reuse or create the isolated Python virtual environment =="
VENV_PY="${APP_DIR}/${VENV_DIR}/bin/python"
if [ -x "${VENV_PY}" ] \
  && "${VENV_PY}" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
  echo "  reusing healthy venv"
else
  echo "  creating fresh venv with ${PYBIN} ..."
  sudo rm -rf "${APP_DIR}/${VENV_DIR}"
  sudo "$PYBIN" -m venv "${APP_DIR}/${VENV_DIR}"
fi
cd "${APP_DIR}"
sudo "${VENV_DIR}/bin/pip" install --upgrade pip
sudo "${VENV_DIR}/bin/pip" install .

echo "== 6. Protect the service directory from web access =="
# ledger.db must NEVER be downloadable over HTTP. Belt and suspenders:
# .htaccess (needs AllowOverride) plus a server-level conf snippet.
# Each file is written only when its content actually changed.
HTACCESS_WANT="Require all denied"
if [ -f "${APP_DIR}/.htaccess" ] && [ "$(cat "${APP_DIR}/.htaccess")" = "${HTACCESS_WANT}" ]; then
  echo "  .htaccess unchanged, skipping"
else
  printf '%s\n' "${HTACCESS_WANT}" | sudo tee "${APP_DIR}/.htaccess" >/dev/null
fi
CONF_DST="/etc/httpd/conf.d/fastapi-ledger-deny.conf"
CONF_TMP="$(mktemp)"
cat >"${CONF_TMP}" <<EOF
<Directory "${APP_DIR}">
    Require all denied
</Directory>
EOF
CONF_CHANGED=0
if [ -f "${CONF_DST}" ] && cmp -s "${CONF_TMP}" "${CONF_DST}"; then
  echo "  httpd deny snippet unchanged, skipping"
else
  sudo cp "${CONF_TMP}" "${CONF_DST}"
  CONF_CHANGED=1
fi
rm -f "${CONF_TMP}"
if [ "${CONF_CHANGED}" = "1" ]; then
  if [ "${RELOAD_APACHE}" = "1" ]; then
    echo "  config changed: gracefully reloading Apache (live connections finish first)"
    sudo apachectl graceful || echo "  WARNING: graceful reload failed; run 'apachectl configtest'"
  else
    echo "  config changed but reload skipped (RELOAD_APACHE=0); reload Apache manually later"
  fi
else
  echo "  Apache reload not needed"
fi

echo "== 7. Rotate the service log =="
LOGROTATE_DST="/etc/logrotate.d/fastapi-ledger"
LOGROTATE_TMP="$(mktemp)"
cat >"${LOGROTATE_TMP}" <<EOF
${APP_DIR}/ledger.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
    copytruncate
    su ${APP_USER} ${APP_GROUP}
}
EOF
if [ -f "${LOGROTATE_DST}" ] && cmp -s "${LOGROTATE_TMP}" "${LOGROTATE_DST}"; then
  echo "  logrotate config unchanged, skipping"
else
  sudo cp "${LOGROTATE_TMP}" "${LOGROTATE_DST}"
fi
rm -f "${LOGROTATE_TMP}"

echo "== 8. Set ownership =="
sudo chown -R "${APP_USER}:${APP_GROUP}" "${APP_DIR}"

echo "== 9. Install the WordPress plugin =="
# A backup was taken in step 3. Remove first: cp -r over an existing dir
# would nest a copy inside it.
sudo rm -rf "${PLUGIN_DST}"
sudo cp -r "${PLUGIN_SRC}" "${PLUGIN_DST}"
sudo chown -R "${APP_USER}:${APP_GROUP}" "${PLUGIN_DST}"

echo "== 10. Restart the service inside screen (single worker) =="
cd "${APP_DIR}"
# NOTE: exactly one uvicorn worker. The hash-chain write lock is per-process;
# more workers would race (UNIQUE constraint becomes the only backstop).
# Stop the old session first so the new one can bind the port.
as_app screen -S "${SESSION}" -X quit 2>/dev/null || echo "  (no previous session running)"
sleep 1
as_app screen -dmS "${SESSION}" \
  "${APP_DIR}/${VENV_DIR}/bin/uvicorn" main:app --host 127.0.0.1 --port "${PORT}" --workers 1

echo "== 11. Auto-start on boot via cron =="
if [ "${SETUP_CRON}" = "1" ]; then
  # Adds an @reboot entry for the apache user (idempotent):
  REBOOT_CMD="@reboot cd ${APP_DIR} && ${APP_DIR}/${VENV_DIR}/bin/uvicorn main:app --host 127.0.0.1 --port ${PORT} --workers 1 >> ${APP_DIR}/ledger.log 2>&1"
  ( as_app crontab -l 2>/dev/null | grep -vF "${APP_DIR}/${VENV_DIR}/bin/uvicorn"; echo "${REBOOT_CMD}" ) | as_app crontab -
else
  echo "  (skipped: SETUP_CRON=0)"
fi

echo "== 12. Verify =="
sleep 2
as_app screen -ls || true
echo "--- listener must be loopback-only below (127.0.0.1, never all interfaces) ---"
( ss -ltnp 2>/dev/null || netstat -ltnp 2>/dev/null ) | grep ":${PORT}" || true
if curl -sf --max-time 10 "http://127.0.0.1:${PORT}/api/ledger/audit"; then
  echo
else
  echo "ERROR: service did not respond on 127.0.0.1:${PORT}" >&2
  exit 1
fi

echo
echo "Deployment complete."
echo "The service listens ONLY on 127.0.0.1:${PORT} (not exposed to the network)."
echo "Manage it with: runuser -u ${APP_USER} -- screen -r ${SESSION}"
echo "Activate the plugin in wp-admin, then set Settings > Permalinks to 'Post name'."
