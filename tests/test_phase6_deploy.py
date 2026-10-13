"""Phase 6 tests: validate the AlmaLinux/RHEL (screen-based) deployment assets."""

import os
import re

SERVICE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY = os.path.join(SERVICE_DIR, "deploy.sh")
STOP = os.path.join(SERVICE_DIR, "stop.sh")
RESTART = os.path.join(SERVICE_DIR, "restart.sh")
SERVICE_FILE = os.path.join(SERVICE_DIR, "fastapi-ledger.service")

WP_ROOT = "/var/www/unmer.ac.id"
APP_DIR = f"{WP_ROOT}/fastapi-ledger"


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def test_no_systemd_unit_remains():
    assert not os.path.isfile(SERVICE_FILE), "systemd unit should not exist (no systemd)"


def test_pyproject_exists_and_replaces_requirements():
    pyproject = os.path.join(SERVICE_DIR, "pyproject.toml")
    assert os.path.isfile(pyproject), "dependencies live in pyproject.toml"
    assert not os.path.isfile(
        os.path.join(SERVICE_DIR, "requirements.txt")
    ), "requirements.txt must be gone (single source of truth)"


def test_dependencies_fully_pinned():
    try:
        import tomllib
    except ImportError:  # Python < 3.11 fallback
        import tomli as tomllib
    pyproject = os.path.join(SERVICE_DIR, "pyproject.toml")
    with open(pyproject, "rb") as handle:
        data = tomllib.load(handle)
    project = data.get("project", {})
    deps = project.get("dependencies", [])
    assert deps, "pyproject.toml must declare dependencies"
    for dep in deps:
        assert "==" in dep, f"unpinned dependency: {dep}"
    names = [re.split(r"[<>=!~\s\[]", dep, maxsplit=1)[0].lower() for dep in deps]
    for required in ("fastapi", "pydantic", "qrcode", "uvicorn"):
        assert required in names, f"missing dependency: {required}"
    assert project.get("requires-python"), "requires-python must be declared"


def test_deploy_script_exists():
    assert os.path.isfile(DEPLOY)


def test_stop_and_restart_scripts_exist():
    assert os.path.isfile(STOP)
    assert os.path.isfile(RESTART)


def test_deploy_uses_dnf():
    assert "dnf install" in read(DEPLOY), "AlmaLinux/RHEL uses dnf"


def test_deploy_defines_app_dir_under_unmer_ac_id():
    content = read(DEPLOY)
    assert WP_ROOT in content
    assert 'APP_DIR="${WP_ROOT}/fastapi-ledger"' in content


def test_deploy_uses_apache_user():
    content = read(DEPLOY)
    assert 'APP_USER="apache"' in content
    assert 'APP_GROUP="apache"' in content


def test_deploy_creates_venv_without_system_pip():
    content = read(DEPLOY)
    assert "-m venv" in content, "deploy must create the venv itself"
    assert "pip\" install ." in content, "deploy must install from pyproject.toml"
    assert "requirements.txt" not in content
    assert not re.search(r"sudo\s+pip", content), "sudo pip is prohibited"
    assert "${VENV_DIR}/bin/pip" in content


def test_scripts_never_run_bare_interpreter():
    """Everything executed must be a venv binary; the only bare python
    allowed is `python3 -m venv` (which creates the venv itself)."""
    for path in (DEPLOY, RESTART, STOP):
        content = read(path)
        for lineno, line in enumerate(content.splitlines(), start=1):
            stripped = line.split("#", 1)[0]
            # Drop venv binary invocations ("${DIR}/venv/bin/pip") first so
            # only genuinely bare interpreters remain.
            stripped = re.sub(r'"[^"]*/bin/[^"]*"', "", stripped)
            stripped = re.sub(r'"[^"]*"', "", stripped)
            # `install --upgrade pip` upgrades pip *inside* the venv via the
            # venv pip binary — the trailing `pip` is a package name, not a
            # executed binary.
            stripped = stripped.replace("install --upgrade pip", "install")
            if "python3 -m venv" in stripped or "python3-pip" in stripped:
                continue
            # Version-candidate enumeration (`for candidate in python3.12 ...`)
            # and dnf package names are not executions.
            if "candidate in" in stripped or "dnf install" in stripped:
                continue
            assert not re.search(
                r"(?<![\w/])(python3?|uvicorn|pip3?)(?![\w/])", stripped
            ), f"{path}:{lineno} runs a bare interpreter: {stripped.strip()}"


def test_main_refuses_system_interpreter():
    service_dir = SERVICE_DIR
    with open(os.path.join(service_dir, "main.py"), encoding="utf-8") as handle:
        source = handle.read()
    assert "_require_virtualenv()" in source
    assert "UNMER_ALLOW_SYSTEM_PYTHON" in source


def test_deploy_uses_screen_not_systemd():
    content = read(DEPLOY)
    assert "screen -dmS" in content
    assert "systemctl" not in content, "no systemd on this server"


def test_deploy_runs_service_as_apache_user():
    content = read(DEPLOY)
    assert "runuser" in content, "apache uses /sbin/nologin; must use runuser"
    assert 'sudo -u "${APP_USER}" screen' not in content


def test_deploy_checks_plugin_source_exists():
    content = read(DEPLOY)
    assert "PLUGIN_SRC" in content
    assert "REPO_DIR" in content
    assert "not found" in content


def test_deploy_uses_single_worker():
    for path in (DEPLOY, RESTART):
        content = read(path)
        assert "--workers 1" in content, f"{path} must pin a single uvicorn worker"


def test_deploy_adds_server_level_deny():
    content = read(DEPLOY)
    assert "conf.d/fastapi-ledger-deny" in content
    assert "<Directory" in content


def test_deploy_adds_logrotate():
    content = read(DEPLOY)
    assert "logrotate.d/fastapi-ledger" in content


def test_deploy_binds_localhost_only():
    for path in (DEPLOY, RESTART):
        content = read(path)
        assert "--host 127.0.0.1" in content
        assert "0.0.0.0" not in content, f"{path} must never bind all interfaces"


def test_deploy_denies_web_access_to_service_dir():
    content = read(DEPLOY)
    assert "Require all denied" in content, "ledger.db must not be web-accessible"


def test_deploy_installs_plugin_into_wp_root():
    content = read(DEPLOY)
    assert "PLUGIN_DST" in content
    assert "wp-content/plugins/unmer-ledger" in content
    assert 'rm -rf "${PLUGIN_DST}"' in content, "must clear dest or cp nests on re-deploy"


def test_reboot_entry_uses_screen_with_absolute_paths():
    content = read(DEPLOY)
    assert "command -v screen" in content
    assert "screen -dmS ${SESSION}" in content or "screen -dmS" in content
    assert "REBOOT_CMD" in content


def test_verify_step_fails_loudly_and_shows_listener():
    for path in (DEPLOY, RESTART):
        content = read(path)
        assert "curl -sf" in content, f"{path} must fail loudly when service is down"
        assert "ss -ltnp" in content, f"{path} must show the bound listener"


def test_rsync_never_deletes():
    content = read(DEPLOY)
    assert "rsync -a" in content
    assert "--delete" not in content, "deploy must never delete server-side files"


def test_irreplaceable_data_backed_up_first():
    content = read(DEPLOY)
    assert "BACKUP_ROOT" in content
    assert re.search(r'cp -p "\$\{APP_DIR\}/ledger\.db"', content), "ledger.db must be backed up"
    assert re.search(r'cp -rp "\$\{PLUGIN_DST\}"', content), "plugin must be backed up"
    assert content.index("BACKUP_ROOT") < content.index("rsync -a"), "backup must precede changes"


def test_old_session_stopped_before_start():
    content = read(DEPLOY)
    assert 'screen -S "${SESSION}" -X quit' in content
    assert content.index('-X quit') < content.index('screen -dmS'), "stop must precede start"


def test_apache_never_hard_restarted():
    content = read(DEPLOY)
    assert "apachectl graceful" in content
    assert "apachectl restart" not in content, "never hard-restart the production web server"


def test_config_writes_are_conditional():
    content = read(DEPLOY)
    assert "cmp -s" in content, "config files must be written only when changed"
    assert "RELOAD_APACHE" in content


def test_cron_is_opt_in_and_dry_run_exists():
    content = read(DEPLOY)
    assert "SETUP_CRON" in content
    assert "DRY_RUN" in content


def test_python_version_selected():
    content = read(DEPLOY)
    assert "python3.12" in content
    assert "(3, 10)" in content


def test_venv_reused_when_healthy():
    content = read(DEPLOY)
    assert "reusing healthy venv" in content


def test_stop_quits_the_screen_session():
    content = read(STOP)
    assert "screen -S" in content
    assert "-X quit" in content
    assert "runuser" in content


def test_restart_starts_venv_uvicorn():
    content = read(RESTART)
    assert "screen -dmS" in content
    assert "/venv/bin/uvicorn" in content
    assert "--host 127.0.0.1" in content
    assert "runuser" in content
