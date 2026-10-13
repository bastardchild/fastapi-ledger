import os
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_ledger.db"
    monkeypatch.setattr(main, "DB_PATH", str(db_file))
    main.init_db(str(db_file))
    return str(db_file)


@pytest.fixture()
def client(fresh_db):
    with TestClient(main.app) as test_client:
        yield test_client


def sample_payload(**overrides):
    payload = {
        "reference_id": "202603131",
        "jenis_dokumen": "Surat Keterangan",
        "nomor_dokumen": "042/BAA/UNMER/III/2026",
        "judul_dokumen": "Keterangan Aktif Kuliah",
        "tanggal_surat": "2026-03-13",
        "penandatangan": "Prof. Dr. Ir. Budi Utomo, M.T. (Rektor)",
    }
    payload.update(overrides)
    return payload
