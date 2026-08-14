"""Unit tests for the durable Valkey-backed credential store.

These tests inject an in-memory fake client, so they need no live Valkey and
never touch the network. They cover the store round-trip, encryption at rest,
the redeploy-survival self-test, and the backend auto-selection that keeps
per-account credentials off the ephemeral container filesystem.
"""

import json
from datetime import datetime

import pytest
from google.oauth2.credentials import Credentials

from auth.credential_store import (
    ValkeyCredentialStore,
    get_selected_backend,
)

SIGNING_KEY = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


class FakeRedis:
    """Minimal in-memory stand-in mimicking redis-py bytes semantics."""

    def __init__(self):
        self.kv = {}
        self.sets = {}

    @staticmethod
    def _b(value):
        return value.encode("utf-8") if isinstance(value, str) else value

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value):
        self.kv[key] = self._b(value)
        return True

    def delete(self, *keys):
        removed = 0
        for key in keys:
            if key in self.kv:
                del self.kv[key]
                removed += 1
        return removed

    def sadd(self, key, *values):
        bucket = self.sets.setdefault(key, set())
        added = 0
        for value in values:
            member = self._b(value)
            if member not in bucket:
                bucket.add(member)
                added += 1
        return added

    def srem(self, key, *values):
        bucket = self.sets.get(key, set())
        removed = 0
        for value in values:
            member = self._b(value)
            if member in bucket:
                bucket.discard(member)
                removed += 1
        return removed

    def smembers(self, key):
        return set(self.sets.get(key, set()))


def _sample_credentials():
    return Credentials(
        token="access-token-abc",
        refresh_token="refresh-token-xyz",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="client-id-123",
        client_secret="client-secret-456",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        expiry=datetime(2030, 1, 2, 3, 4, 5),
    )


@pytest.fixture
def encrypted_env(monkeypatch):
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY", SIGNING_KEY)


def test_store_and_get_round_trip(encrypted_env):
    store = ValkeyCredentialStore(client=FakeRedis())
    assert store.store_credential("admin@deflorance.com", _sample_credentials()) is True

    loaded = store.get_credential("admin@deflorance.com")
    assert loaded is not None
    assert loaded.token == "access-token-abc"
    assert loaded.refresh_token == "refresh-token-xyz"
    assert loaded.client_id == "client-id-123"
    assert loaded.scopes == ["https://www.googleapis.com/auth/gmail.readonly"]
    assert loaded.expiry == datetime(2030, 1, 2, 3, 4, 5)


def test_values_are_encrypted_at_rest(encrypted_env):
    fake = FakeRedis()
    store = ValkeyCredentialStore(client=fake)
    store.store_credential("user@example.com", _sample_credentials())

    raw = fake.get(store._key("user@example.com"))
    assert raw is not None
    # The refresh token must not be recoverable from the raw stored blob.
    assert b"refresh-token-xyz" not in raw
    # And it must be a Fernet token, not plaintext JSON.
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw.decode("utf-8"))


def test_unencrypted_fallback_when_no_key(monkeypatch):
    monkeypatch.delenv("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    fake = FakeRedis()
    store = ValkeyCredentialStore(client=fake)
    store.store_credential("user@example.com", _sample_credentials())

    raw = fake.get(store._key("user@example.com"))
    # Without key material the value is stored as plaintext JSON (dev only).
    assert json.loads(raw.decode("utf-8"))["refresh_token"] == "refresh-token-xyz"


def test_get_unknown_user_returns_none(encrypted_env):
    store = ValkeyCredentialStore(client=FakeRedis())
    assert store.get_credential("nobody@example.com") is None


def test_corrupt_blob_returns_none(encrypted_env):
    fake = FakeRedis()
    store = ValkeyCredentialStore(client=fake)
    fake.set(store._key("user@example.com"), b"not-a-valid-fernet-token")
    assert store.get_credential("user@example.com") is None


def test_list_users_and_delete(encrypted_env):
    store = ValkeyCredentialStore(client=FakeRedis())
    store.store_credential("a@example.com", _sample_credentials())
    store.store_credential("b@example.com", _sample_credentials())
    assert store.list_users() == ["a@example.com", "b@example.com"]

    assert store.delete_credential("a@example.com") is True
    assert store.list_users() == ["b@example.com"]
    assert store.get_credential("a@example.com") is None


def test_self_test_reports_previous_sentinel(encrypted_env):
    # A shared client simulates the SAME Valkey surviving across store instances
    # (i.e. across an app redeploy). The second store instance must see the
    # sentinel written by the first.
    shared = FakeRedis()

    first = ValkeyCredentialStore(client=shared).self_test()
    assert first["ok"] is True
    assert first["previous_sentinel"] is None
    written = first["current_sentinel"]

    second = ValkeyCredentialStore(client=shared).self_test()
    assert second["ok"] is True
    assert second["previous_sentinel"] == written


def test_backend_autoselects_valkey_with_proxy_valkey(monkeypatch):
    monkeypatch.delenv("WORKSPACE_MCP_CREDENTIAL_STORE_BACKEND", raising=False)
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "valkey")
    monkeypatch.setenv(
        "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_HOST", "redis.railway.internal"
    )
    assert get_selected_backend() == "valkey"


def test_explicit_backend_overrides_autoselect(monkeypatch):
    monkeypatch.setenv("WORKSPACE_MCP_CREDENTIAL_STORE_BACKEND", "local_directory")
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "valkey")
    monkeypatch.setenv(
        "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_HOST", "redis.railway.internal"
    )
    assert get_selected_backend() == "local_directory"


def test_backend_defaults_to_local_directory(monkeypatch):
    monkeypatch.delenv("WORKSPACE_MCP_CREDENTIAL_STORE_BACKEND", raising=False)
    monkeypatch.delenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("WORKSPACE_MCP_OAUTH_PROXY_VALKEY_HOST", raising=False)
    assert get_selected_backend() == "local_directory"
