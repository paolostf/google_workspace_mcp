"""Unit tests for the durable Valkey-backed credential store.

These tests inject an in-memory fake client, so they need no live Valkey and
never touch the network. They cover the store round-trip, encryption at rest,
the redeploy-survival self-test, and the backend auto-selection that keeps
per-account credentials off the ephemeral container filesystem.
"""

import asyncio
import json
import types
from datetime import datetime

import pytest
from google.oauth2.credentials import Credentials

from auth import rotation_grace_provider as rgp
from auth.credential_store import (
    ValkeyCredentialStore,
    credentials_from_google_token_response,
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


# --- OAuth-consent -> per-account mirror (the bug this fixes) ---------------


def test_credentials_from_token_response_builds_creds(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid-123")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret-456")
    idp = {
        "access_token": "at",
        "refresh_token": "rt",
        "scope": "https://www.googleapis.com/auth/gmail.readonly openid",
        "expires_in": 3599,
        "token_type": "Bearer",
    }
    creds = credentials_from_google_token_response(idp)
    assert creds is not None
    assert creds.token == "at"
    assert creds.refresh_token == "rt"
    assert creds.client_id == "cid-123"
    assert creds.token_uri == "https://oauth2.googleapis.com/token"
    assert "https://www.googleapis.com/auth/gmail.readonly" in creds.scopes
    assert creds.expiry is not None


def test_credentials_from_token_response_requires_refresh_token():
    assert credentials_from_google_token_response({"access_token": "at"}) is None
    # An explicit override (e.g. preserved from a prior consent) is honored.
    creds = credentials_from_google_token_response(
        {"access_token": "at"}, refresh_token="rt-preserved"
    )
    assert creds is not None and creds.refresh_token == "rt-preserved"


def test_oauth_consent_mirrors_into_per_account_store(monkeypatch):
    """Regression for the wrong-store bug: an OAuth 2.1 proxy consent (idp token
    response) must land in the per-account ValkeyCredentialStore keyed by email,
    which is what /health and the static-bearer path read.
    """
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY", SIGNING_KEY)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid-123")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret-456")

    store = ValkeyCredentialStore(client=FakeRedis())
    monkeypatch.setattr("auth.credential_store.get_credential_store", lambda: store)

    async def fake_resolve_email(access_token):
        assert access_token == "at-deflorance"
        return "admin@deflorance.com"

    fake_self = types.SimpleNamespace(_resolve_google_email=fake_resolve_email)

    idp = {
        "access_token": "at-deflorance",
        "refresh_token": "rt-deflorance",
        "scope": "https://www.googleapis.com/auth/gmail.readonly",
        "expires_in": 3599,
    }
    asyncio.run(
        rgp.RotationGraceGoogleProvider._mirror_credential_to_per_account_store(
            fake_self, idp
        )
    )

    stored = store.get_credential("admin@deflorance.com")
    assert stored is not None
    assert stored.refresh_token == "rt-deflorance"
    assert stored.token == "at-deflorance"
    # And it now shows up for the readiness/static-bearer read path.
    assert "admin@deflorance.com" in store.list_users()


def test_mirror_preserves_existing_refresh_token_on_refresh(monkeypatch):
    """A refresh-grant response omits the refresh token; the mirror must keep the
    one already stored so the account stays usable.
    """
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY", SIGNING_KEY)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid-123")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret-456")

    store = ValkeyCredentialStore(client=FakeRedis())
    store.store_credential("admin@deflorance.com", _sample_credentials())
    monkeypatch.setattr("auth.credential_store.get_credential_store", lambda: store)

    async def fake_resolve_email(access_token):
        return "admin@deflorance.com"

    fake_self = types.SimpleNamespace(_resolve_google_email=fake_resolve_email)

    # Refresh response: new access token, NO refresh token.
    idp = {"access_token": "at-refreshed", "expires_in": 3599}
    asyncio.run(
        rgp.RotationGraceGoogleProvider._mirror_credential_to_per_account_store(
            fake_self, idp
        )
    )

    stored = store.get_credential("admin@deflorance.com")
    assert stored is not None
    assert stored.token == "at-refreshed"  # access token refreshed
    assert stored.refresh_token == "refresh-token-xyz"  # preserved from before
