"""UNMER Digital Ledger — FastAPI microservice.

Append-only, tamper-evident hash chain for academic document metadata.
Managed exclusively through this service; the SQLite database is never
touched by WordPress/MySQL.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sqlite3
import sys
import threading
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timezone
from typing import Dict, Optional

import qrcode
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


def _require_virtualenv() -> None:
    """Refuse to run on the system interpreter.

    The service must always execute inside its isolated virtual environment
    (system-wide installs via ``sudo pip`` are prohibited). Override only for
    emergencies with ``UNMER_ALLOW_SYSTEM_PYTHON=1``.
    """
    if os.environ.get("UNMER_ALLOW_SYSTEM_PYTHON") == "1":
        return
    base_prefix = getattr(sys, "base_prefix", sys.prefix)
    if sys.prefix == base_prefix:
        raise RuntimeError(
            "UNMER ledger must run inside its virtual environment "
            "(venv/bin/python, venv/bin/uvicorn). Refusing system interpreter. "
            "Set UNMER_ALLOW_SYSTEM_PYTHON=1 to override."
        )


_require_virtualenv()

SERVICE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("LEDGER_DB_PATH", os.path.join(SERVICE_DIR, "ledger.db"))
VERIFY_BASE_URL = os.environ.get("VERIFY_BASE_URL", "https://unmer.ac.id").rstrip("/")
GENESIS_HASH = "0" * 64

# Single-writer guard: all chain mutations are serialized so that
# "read previous hash -> compute -> insert" is atomic.
# NOTE: this lock is per-process. The service MUST run as a single uvicorn
# worker (--workers 1). With multiple workers the lock cannot serialize
# across processes; the UNIQUE constraint on reference_id then acts as the
# backstop (a lost race surfaces as HTTP 400, never a forked chain).
_write_lock = threading.Lock()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="UNMER Digital Ledger",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


class DokumenInput(BaseModel):
    reference_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    jenis_dokumen: str = Field(..., min_length=1, max_length=128)
    nomor_dokumen: str = Field(..., min_length=1, max_length=255)
    judul_dokumen: str = Field(..., min_length=1, max_length=255)
    tanggal_surat: date
    penandatangan: str = Field(..., min_length=1, max_length=255)


PAYLOAD_FIELDS = (
    "reference_id",
    "jenis_dokumen",
    "nomor_dokumen",
    "judul_dokumen",
    "tanggal_surat",
    "penandatangan",
)


def current_db_path() -> str:
    """Resolved at call time so env changes and tests take effect.

    Priority: LEDGER_DB_PATH env var first (live re-read on every call),
    then the module-level DB_PATH (which tests monkeypatch).
    """
    env_path = os.environ.get("LEDGER_DB_PATH")
    if env_path:
        return env_path
    return DB_PATH


@contextmanager
def connect(db_path: Optional[str] = None):
    path = db_path or current_db_path()
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: Optional[str] = None) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ledger (
                block_index     INTEGER PRIMARY KEY AUTOINCREMENT,
                reference_id    TEXT NOT NULL UNIQUE,
                jenis_dokumen   TEXT NOT NULL,
                nomor_dokumen   TEXT NOT NULL,
                judul_dokumen   TEXT NOT NULL,
                tanggal_surat   TEXT NOT NULL,
                penandatangan   TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                previous_hash   TEXT NOT NULL,
                block_hash      TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_reference_id "
            "ON ledger (reference_id)"
        )
        conn.commit()


def _payload_sorted_json(payload: Dict[str, str]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def compute_block_hash(
    block_index: int, previous_hash: str, payload_json: str, created_at: str
) -> str:
    material = f"{block_index}{previous_hash}{payload_json}{created_at}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _row_to_record(row: sqlite3.Row) -> Dict:
    return {key: row[key] for key in row.keys()}


def _previous_hash(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT block_hash FROM ledger ORDER BY block_index DESC LIMIT 1"
    ).fetchone()
    return row["block_hash"] if row else GENESIS_HASH


def generate_qr_data_uri(target_url: str) -> str:
    """Encode a URL into a PNG QR code returned as a base64 data URI."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(target_url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def insert_dokumen(data: DokumenInput) -> Dict:
    """Insert a new block. Raises ValueError on duplicate reference_id."""
    payload = {}
    for field in PAYLOAD_FIELDS:
        value = getattr(data, field)
        payload[field] = value.isoformat() if isinstance(value, date) else value
    payload_json = _payload_sorted_json(payload)

    with _write_lock, connect() as conn:
        existing = conn.execute(
            "SELECT 1 FROM ledger WHERE reference_id = ?", (data.reference_id,)
        ).fetchone()
        if existing:
            raise ValueError("reference_id already exists")

        next_index = (
            conn.execute(
                "SELECT COALESCE(MAX(block_index), 0) + 1 FROM ledger"
            ).fetchone()[0]
        )
        previous_hash = _previous_hash(conn)
        created_at = datetime.now(timezone.utc).isoformat()
        block_hash = compute_block_hash(
            next_index, previous_hash, payload_json, created_at
        )

        conn.execute(
            """
            INSERT INTO ledger (
                block_index, reference_id, jenis_dokumen, nomor_dokumen,
                judul_dokumen, tanggal_surat, penandatangan,
                created_at, previous_hash, block_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                next_index,
                data.reference_id,
                data.jenis_dokumen,
                data.nomor_dokumen,
                data.judul_dokumen,
                data.tanggal_surat.isoformat(),
                data.penandatangan,
                created_at,
                previous_hash,
                block_hash,
            ),
        )
        conn.commit()

    return {
        "block_index": next_index,
        "block_hash": block_hash,
        "previous_hash": previous_hash,
        "created_at": created_at,
        "target_url": f"{VERIFY_BASE_URL}/dokumen/tampilkan/{data.reference_id}/",
    }


@app.post("/api/dokumen")
def create_dokumen(data: DokumenInput) -> JSONResponse:
    try:
        result = insert_dokumen(data)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="reference_id sudah terdaftar pada ledger",
        )
    return JSONResponse(
        content={
            "status": "success",
            "block_index": result["block_index"],
            "block_hash": result["block_hash"],
            "reference_id": data.reference_id,
            "target_url": result["target_url"],
            "qr_base64": generate_qr_data_uri(result["target_url"]),
        }
    )


@app.get("/api/dokumen/{ref_id}")
def get_dokumen(ref_id: str) -> JSONResponse:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM ledger WHERE reference_id = ?", (ref_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Dokumen tidak ditemukan")
    return JSONResponse(content=_row_to_record(row))


@app.get("/api/ledger/audit")
def audit_ledger() -> JSONResponse:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM ledger ORDER BY block_index ASC"
        ).fetchall()

    integrity = True
    expected_prev = GENESIS_HASH
    expected_index = 1

    for row in rows:
        record = _row_to_record(row)
        payload = {field: record[field] for field in PAYLOAD_FIELDS}
        payload_json = _payload_sorted_json(payload)
        recomputed = compute_block_hash(
            record["block_index"],
            record["previous_hash"],
            payload_json,
            record["created_at"],
        )
        if (
            record["block_index"] != expected_index
            or record["previous_hash"] != expected_prev
            or record["block_hash"] != recomputed
        ):
            integrity = False
            break
        expected_prev = record["block_hash"]
        expected_index += 1

    return JSONResponse(
        content={"integrity": integrity, "total_blocks": len(rows)}
    )
