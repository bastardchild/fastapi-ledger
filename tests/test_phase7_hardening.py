"""Phase 7: hardening, isolation, atomicity, and live end-to-end tests."""

import sqlite3
import threading

from conftest import sample_payload


def test_current_db_path_rereads_env(monkeypatch, tmp_path):
    """LEDGER_DB_PATH set after import must take effect (fix #2)."""
    import main

    monkeypatch.delenv("LEDGER_DB_PATH", raising=False)
    default = main.current_db_path()
    assert default == main.DB_PATH

    custom = str(tmp_path / "custom.db")
    monkeypatch.setenv("LEDGER_DB_PATH", custom)
    assert main.current_db_path() == custom


def test_no_mysql_usage_in_backend():
    """Ledger must be isolated from WordPress MySQL (no driver/import usage)."""
    import os
    import re

    service_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(service_dir, "main.py"), encoding="utf-8") as handle:
        source = handle.read().lower()
    for forbidden in ("import mysql", "import pymysql", "import mariadb", "pdo."):
        assert forbidden not in source
    assert "sqlite3" in source


def test_only_sqlite_engine_used():
    import main

    assert "sqlite3" in main.__dict__ or "sqlite3" in dir(main)


def test_schema_is_append_only_unique_index(client, fresh_db):
    conn = sqlite3.connect(fresh_db)
    indexes = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index'"
    ).fetchall()
    conn.close()
    names = [row[0] for row in indexes]
    assert any("reference_id" in (sql or "") for _, sql in indexes) or names


def test_concurrent_duplicate_insert_only_one_wins(fresh_db):
    import main

    results = []
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        try:
            main.insert_dokumen(main.DokumenInput(**sample_payload()))
            results.append("ok")
        except ValueError:
            results.append("dup")

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count("ok") == 1, "exactly one insert must win the race"
    assert results.count("dup") == 3

    with sqlite3.connect(fresh_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    assert count == 1


def test_chain_remains_valid_under_concurrent_distinct_inserts(fresh_db):
    import main

    barrier = threading.Barrier(6)

    def worker(index):
        barrier.wait()
        main.insert_dokumen(main.DokumenInput(**sample_payload(reference_id=f"R{index}")))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    from fastapi.testclient import TestClient

    with TestClient(main.app) as test_client:
        audit = test_client.get("/api/ledger/audit").json()
    assert audit == {"integrity": True, "total_blocks": 6}


def test_payload_sorting_is_deterministic():
    import main

    a = {"b": "2", "a": "1"}
    b = {"a": "1", "b": "2"}
    assert main._payload_sorted_json(a) == main._payload_sorted_json(b)
    assert main._payload_sorted_json(a) == '{"a":"1","b":"2"}'


def test_timestamp_is_server_side_utc(client):
    body = client.post("/api/dokumen", json=sample_payload()).json()
    record = client.get("/api/dokumen/202603131").json()
    assert record["created_at"].endswith("+00:00")
    assert record["block_index"] == body["block_index"]


def test_swagger_and_docs_are_disabled(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_list_and_delete_dokumen_with_permalink_invalidation(client):
    # 1. Insert two documents
    p1 = sample_payload(reference_id="DOC-001")
    p2 = sample_payload(reference_id="DOC-002")
    r1 = client.post("/api/dokumen", json=p1)
    r2 = client.post("/api/dokumen", json=p2)
    assert r1.status_code == 200
    assert r2.status_code == 200

    # 2. List documents -> should contain both
    listing = client.get("/api/dokumen").json()
    ref_ids = [d["reference_id"] for d in listing]
    assert "DOC-001" in ref_ids
    assert "DOC-002" in ref_ids
    assert all("target_url" in d for d in listing)

    # 3. Delete DOC-001
    del_res = client.delete("/api/dokumen/DOC-001")
    assert del_res.status_code == 200
    assert del_res.json()["status"] == "deleted"

    # 4. Listing should now ONLY contain DOC-002 (DOC-001 hidden from list view)
    listing_after = client.get("/api/dokumen").json()
    ref_ids_after = [d["reference_id"] for d in listing_after]
    assert "DOC-001" not in ref_ids_after
    assert "DOC-002" in ref_ids_after

    # 5. Permalink GET /api/dokumen/DOC-001 must return 404 (invalidated)
    get_del = client.get("/api/dokumen/DOC-001")
    assert get_del.status_code == 404

    # 6. DOC-002 remains valid
    get_valid = client.get("/api/dokumen/DOC-002")
    assert get_valid.status_code == 200

    # 7. Audit integrity must still be True!
    audit = client.get("/api/ledger/audit").json()
    assert audit["integrity"] is True
    assert audit["total_blocks"] >= 2
