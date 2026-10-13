import pytest

from conftest import sample_payload


@pytest.mark.parametrize(
    "bad_ref", ["SLASH/ID", "has space", "dot.id", "back\\slash", "semi;colon", ""]
)
def test_reference_id_charset_rejected(client, bad_ref):
    response = client.post("/api/dokumen", json=sample_payload(reference_id=bad_ref))
    assert response.status_code == 422


@pytest.mark.parametrize("good_ref", ["ABC-123_xyz", "202603131", "a", "R0"])
def test_reference_id_charset_accepted(client, good_ref):
    response = client.post("/api/dokumen", json=sample_payload(reference_id=good_ref))
    assert response.status_code == 200


def test_create_response_includes_reference_id(client):
    body = client.post("/api/dokumen", json=sample_payload()).json()
    assert body["reference_id"] == "202603131"


def test_genesis_block_uses_zero_previous_hash(client):
    response = client.post("/api/dokumen", json=sample_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["block_index"] == 1

    record = client.get("/api/dokumen/202603131").json()
    assert record["previous_hash"] == "0" * 64
    assert len(record["block_hash"]) == 64


def test_hash_matches_formula(client):
    client.post("/api/dokumen", json=sample_payload())
    record = client.get("/api/dokumen/202603131").json()

    import json

    import main

    payload = {field: record[field] for field in main.PAYLOAD_FIELDS}
    expected = main.compute_block_hash(
        record["block_index"],
        record["previous_hash"],
        main._payload_sorted_json(payload),
        record["created_at"],
    )
    assert record["block_hash"] == expected


def test_duplicate_reference_id_returns_400(client):
    assert client.post("/api/dokumen", json=sample_payload()).status_code == 200
    duplicate = client.post("/api/dokumen", json=sample_payload())
    assert duplicate.status_code == 400


def test_second_block_links_to_first(client):
    client.post("/api/dokumen", json=sample_payload())
    client.post(
        "/api/dokumen", json=sample_payload(reference_id="202603132")
    )
    first = client.get("/api/dokumen/202603131").json()
    second = client.get("/api/dokumen/202603132").json()
    assert second["block_index"] == 2
    assert second["previous_hash"] == first["block_hash"]


def test_get_missing_returns_404(client):
    assert client.get("/api/dokumen/does-not-exist").status_code == 404


def test_audit_clean_chain(client):
    client.post("/api/dokumen", json=sample_payload())
    client.post("/api/dokumen", json=sample_payload(reference_id="202603132"))
    audit = client.get("/api/ledger/audit").json()
    assert audit == {"integrity": True, "total_blocks": 2}


def test_audit_detects_tampering(client, fresh_db):
    import sqlite3

    client.post("/api/dokumen", json=sample_payload())
    conn = sqlite3.connect(fresh_db)
    conn.execute(
        "UPDATE ledger SET judul_dokumen = 'DIPALSUKAN' WHERE block_index = 1"
    )
    conn.commit()
    conn.close()

    audit = client.get("/api/ledger/audit").json()
    assert audit["integrity"] is False
    assert audit["total_blocks"] == 1
