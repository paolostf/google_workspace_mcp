"""Tests for the authenticated POST /attachments upload route."""

import json

import pytest
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse

import core.attachment_storage as storage_mod
import core.server as server_mod
from core.server import serve_attachment, upload_attachment


class DummyAuthProvider:
    """Verifies exactly one bearer token, like the MCP endpoint's provider."""

    def __init__(self, valid_token: str = "good-token"):
        self.valid_token = valid_token

    async def verify_token(self, token: str):
        if token == self.valid_token:
            return object()  # any non-None verified token
        return None


def _build_request(
    body: bytes = b"",
    headers: dict | None = None,
    query_string: bytes = b"",
    chunk_size: int | None = None,
) -> Request:
    raw_headers = [
        (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/attachments",
        "raw_path": b"/attachments",
        "query_string": query_string,
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("localhost", 8000),
    }

    if chunk_size:
        chunks = [body[i : i + chunk_size] for i in range(0, len(body), chunk_size)]
    else:
        chunks = [body]
    messages = [
        {"type": "http.request", "body": chunk, "more_body": True} for chunk in chunks
    ]
    messages.append({"type": "http.request", "body": b"", "more_body": False})
    message_iter = iter(messages)

    async def receive():
        return next(message_iter)

    return Request(scope, receive)


def _build_get_request(file_id: str) -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/attachments/{file_id}",
        "raw_path": f"/attachments/{file_id}".encode(),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("localhost", 8000),
        "path_params": {"file_id": file_id},
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


@pytest.fixture
def real_storage(tmp_path, monkeypatch):
    """Fresh AttachmentStorage backed by tmp_path, with a static URL builder."""
    monkeypatch.setattr(storage_mod, "STORAGE_DIR", tmp_path)
    storage = storage_mod.AttachmentStorage()
    monkeypatch.setattr(storage_mod, "_attachment_storage", storage)
    monkeypatch.setattr(
        storage_mod,
        "get_attachment_url",
        lambda file_id: f"http://localhost:8000/attachments/{file_id}",
    )
    return storage


@pytest.fixture
def auth_provider(monkeypatch):
    provider = DummyAuthProvider()
    monkeypatch.setattr(server_mod, "get_auth_provider", lambda: provider)
    return provider


def _auth_headers(extra: dict | None = None) -> dict:
    headers = {"authorization": "Bearer good-token"}
    headers.update(extra or {})
    return headers


# ---------------------------------------------------------------------------
# Auth enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_401_without_bearer_token(auth_provider):
    response = await upload_attachment(_build_request(body=b"data"))

    assert isinstance(response, JSONResponse)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_upload_401_with_invalid_token(auth_provider):
    response = await upload_attachment(
        _build_request(body=b"data", headers={"authorization": "Bearer wrong-token"})
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_upload_401_when_no_auth_provider_configured(monkeypatch):
    """Fail-closed: without an OAuth provider the route rejects, never opens."""
    monkeypatch.setattr(server_mod, "get_auth_provider", lambda: None)

    response = await upload_attachment(
        _build_request(body=b"data", headers={"authorization": "Bearer good-token"})
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_upload_401_when_verify_token_raises(monkeypatch):
    class ExplodingProvider:
        async def verify_token(self, token):
            raise RuntimeError("verification backend down")

    monkeypatch.setattr(server_mod, "get_auth_provider", lambda: ExplodingProvider())

    response = await upload_attachment(
        _build_request(body=b"data", headers=_auth_headers())
    )

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Upload -> serve roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_roundtrip_returns_identical_bytes(auth_provider, real_storage):
    payload = b"%PDF-1.3\x00\x01\xff binary payload \x00" * 100

    response = await upload_attachment(
        _build_request(
            body=payload,
            headers=_auth_headers(
                {"content-type": "application/pdf", "x-filename": "report.pdf"}
            ),
            chunk_size=64,
        )
    )

    assert response.status_code == 201
    result = json.loads(response.body)
    assert result["size"] == len(payload)
    assert result["expires_in"] == real_storage.expiration_seconds
    assert result["filename"].startswith("report_")
    assert result["filename"].endswith(".pdf")
    assert "/attachments/" in result["url"]

    file_id = result["url"].rsplit("/", 1)[-1]
    metadata = real_storage.get_attachment_metadata(file_id)
    assert metadata["mime_type"] == "application/pdf"
    assert metadata["original_filename"] == "report.pdf"

    get_response = await serve_attachment(_build_get_request(file_id))
    assert isinstance(get_response, FileResponse)
    assert get_response.status_code == 200
    with open(get_response.path, "rb") as f:
        assert f.read() == payload


@pytest.mark.asyncio
async def test_upload_filename_from_query_param(auth_provider, real_storage):
    response = await upload_attachment(
        _build_request(
            body=b"col1,col2\n1,2\n",
            headers=_auth_headers({"content-type": "text/csv"}),
            query_string=b"filename=data.csv",
        )
    )

    assert response.status_code == 201
    result = json.loads(response.body)
    assert result["filename"].startswith("data_")
    assert result["filename"].endswith(".csv")


@pytest.mark.asyncio
async def test_upload_consumable_by_gmail_local_attachment_path(
    auth_provider, real_storage
):
    """send_gmail_message resolves uploaded /attachments URLs from local disk."""
    from gmail.gmail_tools import _try_read_local_attachment

    payload = b"attach me \x00\xff"
    response = await upload_attachment(
        _build_request(
            body=payload,
            headers=_auth_headers(
                {"content-type": "text/plain", "x-filename": "note.txt"}
            ),
        )
    )
    assert response.status_code == 201
    file_id = json.loads(response.body)["url"].rsplit("/", 1)[-1]

    resolved = _try_read_local_attachment(f"/attachments/{file_id}")
    assert resolved is not None
    data, filename, mime_type = resolved
    assert data == payload
    assert filename.startswith("note_")
    assert mime_type == "text/plain"


# ---------------------------------------------------------------------------
# Multipart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_multipart_file_field(auth_provider, real_storage):
    boundary = "testboundary123"
    payload = b"multipart file bytes \x00\x01"
    body = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="hello.txt"\r\n'
            "Content-Type: text/plain\r\n\r\n"
        ).encode()
        + payload
        + f"\r\n--{boundary}--\r\n".encode()
    )

    response = await upload_attachment(
        _build_request(
            body=body,
            headers=_auth_headers(
                {"content-type": f"multipart/form-data; boundary={boundary}"}
            ),
        )
    )

    assert response.status_code == 201
    result = json.loads(response.body)
    assert result["size"] == len(payload)
    assert result["filename"].startswith("hello_")

    file_id = result["url"].rsplit("/", 1)[-1]
    metadata = real_storage.get_attachment_metadata(file_id)
    assert metadata["mime_type"] == "text/plain"
    file_path = real_storage.get_attachment_path(file_id)
    assert file_path.read_bytes() == payload


@pytest.mark.asyncio
async def test_upload_multipart_without_file_field_400(auth_provider, real_storage):
    boundary = "testboundary123"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="note"\r\n\r\n'
        "just text\r\n"
        f"--{boundary}--\r\n"
    ).encode()

    response = await upload_attachment(
        _build_request(
            body=body,
            headers=_auth_headers(
                {"content-type": f"multipart/form-data; boundary={boundary}"}
            ),
        )
    )

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Size limit and validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_413_when_body_exceeds_limit(
    auth_provider, real_storage, monkeypatch
):
    monkeypatch.setattr(server_mod, "MAX_UPLOAD_ATTACHMENT_BYTES", 10)

    response = await upload_attachment(
        _build_request(body=b"x" * 11, headers=_auth_headers(), chunk_size=4)
    )

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_upload_413_from_content_length_header(auth_provider, monkeypatch):
    monkeypatch.setattr(server_mod, "MAX_UPLOAD_ATTACHMENT_BYTES", 10)

    response = await upload_attachment(
        _build_request(
            body=b"", headers=_auth_headers({"content-length": "999999999"})
        )
    )

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_upload_empty_body_400(auth_provider, real_storage):
    response = await upload_attachment(_build_request(body=b"", headers=_auth_headers()))

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# TTL / cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uploaded_attachment_expires_after_ttl(auth_provider, real_storage):
    from datetime import datetime, timedelta

    response = await upload_attachment(
        _build_request(body=b"ephemeral", headers=_auth_headers())
    )
    assert response.status_code == 201
    file_id = json.loads(response.body)["url"].rsplit("/", 1)[-1]

    file_path = real_storage.get_attachment_path(file_id)
    assert file_path is not None and file_path.exists()

    # Force expiry, as the TTL clock would after expiration_seconds.
    real_storage._metadata[file_id]["expires_at"] = datetime.now() - timedelta(
        seconds=1
    )

    get_response = await serve_attachment(_build_get_request(file_id))
    assert isinstance(get_response, JSONResponse)
    assert get_response.status_code == 404
    assert not file_path.exists()


@pytest.mark.asyncio
async def test_upload_sweeps_expired_entries(auth_provider, real_storage):
    from datetime import datetime, timedelta

    first = await upload_attachment(
        _build_request(body=b"old upload", headers=_auth_headers())
    )
    old_id = json.loads(first.body)["url"].rsplit("/", 1)[-1]
    old_path = real_storage.get_attachment_path(old_id)
    real_storage._metadata[old_id]["expires_at"] = datetime.now() - timedelta(seconds=1)

    second = await upload_attachment(
        _build_request(body=b"new upload", headers=_auth_headers())
    )

    assert second.status_code == 201
    assert old_id not in real_storage._metadata
    assert not old_path.exists()
