import importlib
import sys

import pytest


def test_python_version():
    assert sys.version_info >= (3, 10), "Python 3.10+ required"


@pytest.mark.parametrize("module", ["fastapi", "uvicorn", "pydantic", "qrcode"])
def test_required_modules_importable(module):
    importlib.import_module(module)


def test_running_inside_virtualenv():
    assert sys.prefix != sys.base_prefix, "Tests must run inside the project venv"


def test_venv_guard_passes_inside_venv(monkeypatch):
    import main

    monkeypatch.delenv("UNMER_ALLOW_SYSTEM_PYTHON", raising=False)
    main._require_virtualenv()  # must not raise


def test_venv_guard_refuses_system_interpreter(monkeypatch):
    import main

    monkeypatch.delenv("UNMER_ALLOW_SYSTEM_PYTHON", raising=False)
    monkeypatch.setattr(sys, "prefix", "/usr")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    with pytest.raises(RuntimeError, match="virtual environment"):
        main._require_virtualenv()


def test_venv_guard_override_env(monkeypatch):
    import main

    monkeypatch.setenv("UNMER_ALLOW_SYSTEM_PYTHON", "1")
    monkeypatch.setattr(sys, "prefix", "/usr")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    main._require_virtualenv()  # must not raise with explicit override
