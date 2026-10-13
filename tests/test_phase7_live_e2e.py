"""Phase 7: live end-to-end test against a real uvicorn server (no mocks)."""

import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

SERVICE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("live") / "ledger.db"
    port = _free_port()
    env = os.environ.copy()
    env["LEDGER_DB_PATH"] = str(db_path)
    env["PYTHONUNBUFFERED"] = "1"

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=SERVICE_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            httpx.get(f"{base_url}/api/ledger/audit", timeout=1.0)
            break
        except httpx.HTTPError:
            if process.poll() is not None:
                output = process.stdout.read().decode("utf-8", "replace")
                raise RuntimeError(f"server exited early:\n{output}")
            time.sleep(0.3)
    else:
        process.terminate()
        raise RuntimeError("server did not start in time")

    yield base_url
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def _payload(reference_id="LIVE001"):
    return {
        "reference_id": reference_id,
        "jenis_dokumen": "Sertifikat / Piagam",
        "nomor_dokumen": "105/SK-REK/2026",
        "judul_dokumen": "Pelatihan Cyber Security",
        "tanggal_surat": "2026-03-13",
        "penandatangan": "Dekan Fakultas Teknik",
    }


def test_live_create_and_verify(live_server):
    with httpx.Client(base_url=live_server, timeout=10.0) as client:
        created = client.post("/api/dokumen", json=_payload())
        assert created.status_code == 200
        body = created.json()
        assert body["status"] == "success"
        assert body["block_index"] == 1
        assert body["qr_base64"].startswith("data:image/png;base64,")
        assert body["target_url"] == (
            "https://unmer.ac.id/dokumen/tampilkan/LIVE001/"
        )

        fetched = client.get("/api/dokumen/LIVE001")
        assert fetched.status_code == 200
        assert fetched.json()["nomor_dokumen"] == "105/SK-REK/2026"

        audit = client.get("/api/ledger/audit").json()
        assert audit == {"integrity": True, "total_blocks": 1}

        dup = client.post("/api/dokumen", json=_payload())
        assert dup.status_code == 400

        missing = client.get("/api/dokumen/GHOST")
        assert missing.status_code == 404


def test_live_service_bound_to_localhost(live_server):
    assert live_server.startswith("http://127.0.0.1:")
