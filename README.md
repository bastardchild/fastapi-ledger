# FastAPI Ledger Microservice

Microservice **Python FastAPI** untuk sistem **UNMER Digital Ledger Dokumen**. Menyimpan
metadata dokumen resmi ke dalam **append-only hash-chain** (blockchain-style) di SQLite,
menghitung hash SHA-256 antar-block, dan menghasilkan QR code untuk verifikasi dokumen fisik.

Service ini berjalan **lokal saja** di `http://127.0.0.1:8000` dan hanya diakses oleh
plugin WordPress melalui HTTP localhost. Database ledger (`ledger.db`) **terpisah total**
dari database MySQL WordPress.

---

## Arsitektur

```
Pemindai QR ──HTTPS──▶ WordPress ──HTTP 127.0.0.1:8000──▶ FastAPI ──▶ ledger.db (SQLite)
                      /dokumen/tampilkan/{id}/            (uvicorn, screen)
```

- **WordPress plugin** — form admin, tampilan QR, halaman verifikasi publik.
- **FastAPI microservice** (folder ini) — hash-chain, hashing SHA-256, generate QR, audit.

---

## Struktur Folder

```
fastapi-ledger/
├── venv/                     # Python Virtual Environment (tidak di-commit)
├── main.py                   # Aplikasi FastAPI: schema, hashing, endpoints
├── pyproject.toml            # Dependensi Python (PEP 621, pinned)
├── ledger.db                 # SQLite (auto-created, tidak di-commit)
├── deploy.sh                 # Runbook deployment (AlmaLinux/RHEL, screen)
├── restart.sh                # Restart service
├── stop.sh                   # Stop service
├── tests/                    # Test suite pytest
├── .gitignore
└── README.md
```

---

## Persyaratan

- Python **3.10+**
- `python3` + `python3-pip` (AlmaLinux/RHEL: `sudo dnf install python3 python3-pip`)
- `screen` untuk menjalankan service tanpa systemd (`sudo dnf install screen`)

> **Aturan penting:** seluruh instalasi paket **harus** di dalam virtual environment.
> Instalasi global (`sudo pip`) **dilarang**. `main.py` menolak berjalan di interpreter
> sistem (cek `sys.prefix` vs `sys.base_prefix` saat startup) — hanya override darurat
> dengan `UNMER_ALLOW_SYSTEM_PYTHON=1`.

---

## Setup & Menjalankan (Development)

```bash
cd fastapi-ledger
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install .
```

Dependensi dikelola di `pyproject.toml` (PEP 621, fully pinned).
Untuk tool test: `pip install ".[test]"`.

Jalankan service:

```bash
uvicorn main:app --host 127.0.0.1 --port 8000
```

Verifikasi cepat:

```bash
curl -s http://127.0.0.1:8000/api/ledger/audit
# {"integrity":true,"total_blocks":0}
```

> Windows (PowerShell): aktivasi venv dengan `.\.venv\Scripts\activate` dan jalankan
> `python -m uvicorn main:app --host 127.0.0.1 --port 8000`.

---

## Konfigurasi (Environment Variables)

| Variable | Default | Keterangan |
|----------|---------|-----------|
| `VERIFY_BASE_URL` | `https://unmer.ac.id` | Host publik yang dipakai saat membangun URL QR. **Ubah bila domain berbeda** — URL ter-*bake* ke QR saat `POST`. |
| `LEDGER_DB_PATH` | `<folder-service>/ledger.db` | Lokasi file SQLite ledger. |

---

## Skema Database (SQLite `ledger`)

| Kolom | Tipe | Constraint | Catatan |
|-------|------|-----------|---------|
| `block_index` | INTEGER | PK AUTOINCREMENT | Posisi block, mulai 1 |
| `reference_id` | TEXT | UNIQUE NOT NULL, indexed | contoh `202603131`; hanya `[A-Za-z0-9_-]`, maks 64 char |
| `jenis_dokumen` | TEXT | NOT NULL | Surat Keterangan / SK / Sertifikat / Surat Tugas |
| `nomor_dokumen` | TEXT | NOT NULL | contoh `042/BAA/UNMER/III/2026` |
| `judul_dokumen` | TEXT | NOT NULL | contoh `Keterangan Aktif Kuliah` |
| `tanggal_surat` | TEXT | NOT NULL | tanggal dokumen, ISO `YYYY-MM-DD` |
| `penandatangan` | TEXT | NOT NULL | pejabat penandatangan |
| `created_at` | TEXT | NOT NULL | ISO 8601 UTC (dibuat server) |
| `previous_hash` | TEXT | NOT NULL | hash block sebelumnya; `"0"*64` untuk genesis |
| `block_hash` | TEXT | NOT NULL | SHA-256 (lihat rumus) |

### Rumus Hash

```
payload_sorted_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
block_hash = sha256(f"{block_index}{previous_hash}{payload_sorted_json}{created_at}")
```

- `payload` = field metadata: `reference_id` … `penandatangan`.
- Genesis (`block_index == 1`): `previous_hash = "0" * 64`.
- `created_at` dan `block_index` **selalu** dibuat di server, tidak pernah dari client.

---

## API Contract

### `POST /api/dokumen`

Mencatat dokumen baru ke ledger.

**Body:**
```json
{
  "reference_id": "202603131",
  "jenis_dokumen": "Surat Keterangan",
  "nomor_dokumen": "042/BAA/UNMER/III/2026",
  "judul_dokumen": "Keterangan Aktif Kuliah",
  "tanggal_surat": "2026-03-13",
  "penandatangan": "Prof. Dr. Ir. Budi Utomo, M.T. (Rektor)"
}
```

**Validasi:** `reference_id` harus unik → bila duplikat mengembalikan **HTTP 400**.

**Response 200:**
```json
{
  "status": "success",
  "block_index": 1,
  "block_hash": "821aac93...987fed",
  "target_url": "https://unmer.ac.id/dokumen/tampilkan/202603131/",
  "qr_base64": "data:image/png;base64,..."
}
```

### `GET /api/dokumen/{ref_id}`

Ambil satu record berdasarkan `reference_id`.

- **200** — JSON lengkap record.
- **404** — bila tidak ditemukan.

### `GET /api/ledger/audit`

Memvalidasi integritas seluruh rantai dari block 1: mencocokkan `previous_hash` dengan
`block_hash` block sebelumnya dan menghitung ulang setiap `block_hash`.

**Response:**
```json
{ "integrity": true, "total_blocks": 42 }
```

---

## Deployment (AlmaLinux / Rocky / RHEL — tanpa systemd)

Service dijalankan menggunakan **GNU screen** (detached) sebagai user `apache`.
Tidak ada unit systemd; auto-start saat boot memakai entri `@reboot` pada crontab user.

**Lokasi di server:**

| Item | Path |
|------|------|
| WordPress document root | `/var/www/unmer.ac.id` |
| FastAPI service | `/var/www/unmer.ac.id/fastapi-ledger` |
| Plugin WordPress | `/var/www/unmer.ac.id/wp-content/plugins/unmer-ledger` |
| User/group service | `apache` |

Deploy cukup lewat satu skrip (lihat `deploy.sh` untuk runbook lengkap):

```bash
# Preview dulu — tidak mengubah apa pun di server:
sudo DRY_RUN=1 bash deploy.sh

# Deploy betulan (dari root checkout repo):
sudo bash deploy.sh
```

Jaminan keamanan skrip deploy:
- `ledger.db` tidak pernah ditimpa/dihapus (di-exclude dari rsync, tanpa `--delete`)
  dan selalu di-backup ke `/var/backups/unmer-ledger/<timestamp>/` sebelum ada perubahan.
- Plugin yang ada di-backup ke lokasi yang sama sebelum diganti.
- Apache tidak pernah di-restart paksa: hanya graceful reload, dan hanya bila
  config snippet berubah.
- Hanya paket OS yang belum ada yang di-install; venv dipakai ulang bila sehat.
- Service lama di-stop dulu sebelum yang baru start (tidak ada rebutan port).

Variabel opsional:

| Variable | Default | Efek |
|----------|---------|------|
| `DRY_RUN=1` | `0` | Tampilkan rencana saja, tanpa perubahan |
| `SETUP_CRON=0` | `1` | Jangan ubah crontab milik `apache` |
| `INSTALL_OS_PACKAGES=0` | `1` | Lewati install paket OS |
| `RELOAD_APACHE=0` | `1` | Lewati graceful reload Apache |
| `REPO_DIR=...` | induk `deploy.sh` | Root checkout repo bila skrip dijalankan dari luar repo |

### Manajemen service

```bash
# Lihat daftar screen (pakai runuser: user apache memakai shell nologin)
sudo runuser -u apache -- screen -ls

# Masuk ke session (lihat log; keluar dengan Ctrl+A lalu D)
sudo runuser -u apache -- screen -r fastapi-ledger

# Restart
sudo bash /var/www/unmer.ac.id/fastapi-ledger/restart.sh

# Stop
sudo bash /var/www/unmer.ac.id/fastapi-ledger/stop.sh
```

Script bantu: `deploy.sh` (install/update), `restart.sh`, `stop.sh`.

> Jangan pernah menyalin folder `.venv` dari mesin pengembangan — selalu buat ulang di server.
> Service **hanya** listen di `127.0.0.1:8000` dan tidak terekspos ke jaringan publik.
> Tidak perlu membuka port di firewall — binding loopback berarti port tidak terlihat
> dari jaringan luar sama sekali. Verifikasi dengan:
> `ss -ltnp | grep ":8000"` (harus `127.0.0.1:8000`, bukan `0.0.0.0:8000`).
> Folder service dilindungi `.htaccess` (`Require all denied`) agar `ledger.db` tidak dapat diakses via web.


---

## Testing

Seluruh test dijalankan di dalam virtual environment.

```bash
# Linux
source venv/bin/activate
pip install ".[test]"
pytest tests -v

# Windows (PowerShell)
.\.venv\Scripts\python.exe -m pip install ".[test]"
.\.venv\Scripts\python.exe -m pytest tests -v
```

Cakupan test:

| File | Fokus |
|------|-------|
| `test_phase0_environment.py` | Python 3.10+, venv, dependensi |
| `test_phase1_ledger_core.py` | Genesis, rumus hash, duplikat 400, 404, audit, deteksi tampering |
| `test_phase2_qr.py` | QR data URI, decode ke PNG, isi URL benar |
| `test_phase6_deploy.py` | Validasi aset deploy (script screen, user `apache`, path `/var/www/unmer.ac.id`) |
| `test_phase7_hardening.py` | Isolasi DB, insert konkuren, atomicity, UTC server-side |
| `test_phase7_live_e2e.py` | End-to-end melawan server uvicorn sungguhan |

> `pyzbar` (untuk verifikasi QR) memerlukan pustaka `libzbar` di Linux:
> `sudo dnf install zbar`.

---

## Backup & Restore

```bash
# Backup
sudo bash /var/www/unmer.ac.id/fastapi-ledger/stop.sh
sudo cp /var/www/unmer.ac.id/fastapi-ledger/ledger.db /backup/ledger-$(date +%F).db
sudo bash /var/www/unmer.ac.id/fastapi-ledger/restart.sh

# Restore
sudo bash /var/www/unmer.ac.id/fastapi-ledger/stop.sh
sudo cp /backup/ledger-YYYY-MM-DD.db /var/www/unmer.ac.id/fastapi-ledger/ledger.db
sudo chown apache:apache /var/www/unmer.ac.id/fastapi-ledger/ledger.db
sudo bash /var/www/unmer.ac.id/fastapi-ledger/restart.sh
```

> `ledger.db` **tidak** boleh di-commit ke Git dan tidak pernah disimpan di MySQL WordPress.

---

## Troubleshooting

| Gejala | Kemungkinan Penyebab | Solusi |
|--------|----------------------|--------|
| `ledger.db` tidak dibuat | Permission folder | Pastikan `apache` punya akses tulis ke `/var/www/unmer.ac.id/fastapi-ledger` |
| 400 saat catat dokumen | `reference_id` duplikat | Gunakan ID lain |
| QR menunjuk domain salah | `VERIFY_BASE_URL` beda | Set env sebelum `POST` (URL ter-bake saat generate) |
| Service tidak mau start | venv/path salah | Cek `sudo -u apache screen -r fastapi-ledger`; pastikan venv dibuat di server |
| Service mati setelah logout | screen tidak jalan | Pastikan pakai `screen -dmS`; tambahkan entri `@reboot` crontab |
| audit `integrity:false` | Data dimodifikasi manual | Chain rusak terdeteksi — restore dari backup |

---

## Lisensi

Bagian dari sistem UNMER Digital Ledger Dokumen. Lisensi mengikuti proyek induk (GPL-2.0-or-later).
