import base64
import io

from conftest import sample_payload
from PIL import Image


def test_response_includes_qr_data_uri(client):
    body = client.post("/api/dokumen", json=sample_payload()).json()
    assert body["qr_base64"].startswith("data:image/png;base64,")
    assert body["target_url"] == (
        "https://unmer.ac.id/dokumen/tampilkan/202603131/"
    )


def test_qr_base64_decodes_to_valid_png(client):
    body = client.post("/api/dokumen", json=sample_payload()).json()
    encoded = body["qr_base64"].split(",", 1)[1]
    raw = base64.b64decode(encoded)
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"

    image = Image.open(io.BytesIO(raw))
    image.verify()
    assert image.format == "PNG"


def test_qr_encodes_target_url(client):
    body = client.post("/api/dokumen", json=sample_payload()).json()
    encoded = body["qr_base64"].split(",", 1)[1]
    raw = base64.b64decode(encoded)

    from pyzbar.pyzbar import decode  # optional dependency

    results = decode(Image.open(io.BytesIO(raw)))
    assert results, "QR code could not be decoded"
    assert results[0].data.decode("utf-8") == body["target_url"]
