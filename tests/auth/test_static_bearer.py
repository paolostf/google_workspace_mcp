"""Tests for the additive static bearer authentication mode."""

import hmac as hmac_module
import json
from types import SimpleNamespace

import pytest

from auth.google_auth import GoogleAuthenticationError
from auth.service_decorator import get_authenticated_google_service_oauth21
from auth.static_bearer import (
    STATIC_BEARERS_ENV_VAR,
    StaticBearerAccessToken,
    build_static_bearer_access_token,
    get_static_bearer_map,
    install_static_bearer_verification,
    is_static_bearer_configured,
    load_static_account_credentials,
    resolve_static_bearer_email,
)

KEY_A = "a" * 32
KEY_B = "b" * 32
KEY_C = "c" * 32
EMAIL_A = "user-a@example.com"
EMAIL_B = "user-b@example.com"
EMAIL_C = "user-c@example.com"

VALID_CONFIG = json.dumps({KEY_A: EMAIL_A, KEY_B: EMAIL_B, KEY_C: EMAIL_C})


@pytest.fixture
def configured_env(monkeypatch):
    monkeypatch.setenv(STATIC_BEARERS_ENV_VAR, VALID_CONFIG)


# ---------------------------------------------------------------------------
# Key resolution
# ---------------------------------------------------------------------------


def test_resolve_returns_mapped_email_on_match(configured_env):
    assert resolve_static_bearer_email(KEY_A) == EMAIL_A
    assert resolve_static_bearer_email(KEY_B) == EMAIL_B
    assert resolve_static_bearer_email(KEY_C) == EMAIL_C


def test_resolve_returns_none_when_no_match(configured_env):
    assert resolve_static_bearer_email("x" * 32) is None
    assert resolve_static_bearer_email(KEY_A[:-1]) is None
    assert resolve_static_bearer_email(KEY_A + "a") is None


def test_resolve_returns_none_for_empty_token(configured_env):
    assert resolve_static_bearer_email("") is None
    assert resolve_static_bearer_email(None) is None


def test_disabled_when_env_absent(monkeypatch):
    monkeypatch.delenv(STATIC_BEARERS_ENV_VAR, raising=False)
    assert resolve_static_bearer_email(KEY_A) is None
    assert is_static_bearer_configured() is False
    assert get_static_bearer_map() == {}


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        "{broken json",
        '["a", "b"]',
        '"just a string"',
        "42",
        "   ",
    ],
)
def test_disabled_on_malformed_env(monkeypatch, raw):
    monkeypatch.setenv(STATIC_BEARERS_ENV_VAR, raw)
    assert resolve_static_bearer_email(KEY_A) is None
    assert is_static_bearer_configured() is False


def test_invalid_entries_are_skipped_fail_closed(monkeypatch):
    config = json.dumps(
        {
            "short-key": EMAIL_A,  # shorter than MIN_STATIC_KEY_LENGTH
            "k" * 32: "not-an-email",  # email without @
            KEY_B: EMAIL_B,  # the only valid entry
        }
    )
    monkeypatch.setenv(STATIC_BEARERS_ENV_VAR, config)
    assert resolve_static_bearer_email("short-key") is None
    assert resolve_static_bearer_email("k" * 32) is None
    assert resolve_static_bearer_email(KEY_B) == EMAIL_B
    assert get_static_bearer_map() == {KEY_B: EMAIL_B}


def test_non_string_values_are_skipped(monkeypatch):
    monkeypatch.setenv(STATIC_BEARERS_ENV_VAR, json.dumps({"n" * 32: 12345}))
    assert is_static_bearer_configured() is False
    assert resolve_static_bearer_email("n" * 32) is None


def test_resolution_is_timing_safe_and_scans_all_keys(configured_env, monkeypatch):
    calls = []
    real_compare_digest = hmac_module.compare_digest

    def counting_compare_digest(a, b):
        calls.append((a, b))
        return real_compare_digest(a, b)

    monkeypatch.setattr(
        "auth.static_bearer.hmac.compare_digest", counting_compare_digest
    )

    # Token matches the FIRST configured key: all 3 keys must still be
    # compared (no early exit) and comparison must go through compare_digest.
    assert resolve_static_bearer_email(KEY_A) == EMAIL_A
    assert len(calls) == 3

    calls.clear()
    assert resolve_static_bearer_email("z" * 32) is None
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# Provider verify_token wrapper
# ---------------------------------------------------------------------------


def _make_fake_provider(sentinel):
    async def original_verify_token(token):
        return sentinel

    return SimpleNamespace(
        required_scopes=["scope-1", "scope-2"],
        verify_token=original_verify_token,
    )


@pytest.mark.asyncio
async def test_verify_token_wrapper_returns_static_token_on_match(configured_env):
    sentinel = object()
    provider = _make_fake_provider(sentinel)
    assert install_static_bearer_verification(provider) is True

    result = await provider.verify_token(KEY_A)
    assert isinstance(result, StaticBearerAccessToken)
    assert result.email == EMAIL_A
    assert result.claims["email"] == EMAIL_A
    assert result.token == KEY_A
    assert result.scopes == ["scope-1", "scope-2"]
    assert result.client_id == "gws-static-bearer"


@pytest.mark.asyncio
async def test_verify_token_wrapper_delegates_on_no_match(configured_env):
    sentinel = object()
    provider = _make_fake_provider(sentinel)
    install_static_bearer_verification(provider)

    assert await provider.verify_token("y" * 32) is sentinel


@pytest.mark.asyncio
async def test_verify_token_wrapper_is_passthrough_when_disabled(monkeypatch):
    monkeypatch.delenv(STATIC_BEARERS_ENV_VAR, raising=False)
    sentinel = object()
    provider = _make_fake_provider(sentinel)
    install_static_bearer_verification(provider)

    # Even a would-be key delegates to the original OAuth verification.
    assert await provider.verify_token(KEY_A) is sentinel


def test_install_returns_false_for_none_provider():
    assert install_static_bearer_verification(None) is False


def test_install_skips_provider_without_verify_token(configured_env):
    provider = SimpleNamespace(required_scopes=[])
    assert install_static_bearer_verification(provider) is False


def test_build_static_bearer_access_token_fields():
    token = build_static_bearer_access_token(KEY_A, EMAIL_A, ["s"])
    assert isinstance(token, StaticBearerAccessToken)
    assert token.email == EMAIL_A
    assert token.sub == EMAIL_A
    assert token.claims == {"email": EMAIL_A, "sub": EMAIL_A}
    assert token.session_id.startswith("static_bearer_")
    assert token.expires_at is not None


# ---------------------------------------------------------------------------
# Stored-credential loading for statically authenticated accounts
# ---------------------------------------------------------------------------


class _FakeSessionStore:
    def __init__(self, credentials=None):
        self._credentials = credentials
        self.stored_sessions = []

    def get_credentials(self, user_email):
        return self._credentials

    def store_session(self, **kwargs):
        self.stored_sessions.append(kwargs)


def test_load_credentials_prefers_credential_store(monkeypatch):
    stored = SimpleNamespace(valid=True, scopes=["s1"])
    monkeypatch.setattr(
        "auth.google_auth.get_credentials",
        lambda user_google_email, required_scopes: stored,
    )
    assert load_static_account_credentials(EMAIL_A, ["s1"]) is stored


def test_load_credentials_unknown_email_returns_none(monkeypatch):
    monkeypatch.setattr(
        "auth.google_auth.get_credentials",
        lambda user_google_email, required_scopes: None,
    )
    monkeypatch.setattr(
        "auth.oauth21_session_store.get_oauth21_session_store",
        lambda: _FakeSessionStore(credentials=None),
    )
    assert load_static_account_credentials("nobody@example.com", ["s1"]) is None


def test_load_credentials_falls_back_to_session_store(monkeypatch):
    session_creds = SimpleNamespace(valid=True, scopes=["s1"], refresh_token=None)
    monkeypatch.setattr(
        "auth.google_auth.get_credentials",
        lambda user_google_email, required_scopes: None,
    )
    monkeypatch.setattr(
        "auth.oauth21_session_store.get_oauth21_session_store",
        lambda: _FakeSessionStore(credentials=session_creds),
    )
    assert load_static_account_credentials(EMAIL_A, ["s1"]) is session_creds


def test_load_credentials_refreshes_expired_session_creds(monkeypatch):
    class _RefreshableCreds:
        def __init__(self):
            self.valid = False
            self.refresh_token = "refresh-token"
            self.token = "old-access-token"
            self.token_uri = "https://oauth2.googleapis.com/token"
            self.client_id = "cid"
            self.client_secret = "secret"
            self.scopes = ["s1"]
            self.expiry = None
            self.refresh_calls = 0

        def refresh(self, request):
            self.refresh_calls += 1
            self.valid = True
            self.token = "new-access-token"

    creds = _RefreshableCreds()
    store = _FakeSessionStore(credentials=creds)
    monkeypatch.setattr(
        "auth.google_auth.get_credentials",
        lambda user_google_email, required_scopes: None,
    )
    monkeypatch.setattr(
        "auth.oauth21_session_store.get_oauth21_session_store",
        lambda: store,
    )
    monkeypatch.setattr("auth.oauth_config.is_stateless_mode", lambda: True)

    result = load_static_account_credentials(EMAIL_A, ["s1"])
    assert result is creds
    assert creds.refresh_calls == 1
    assert len(store.stored_sessions) == 1
    assert store.stored_sessions[0]["user_email"] == EMAIL_A
    assert store.stored_sessions[0]["access_token"] == "new-access-token"


def test_load_credentials_returns_none_when_refresh_fails(monkeypatch):
    class _FailingCreds:
        valid = False
        refresh_token = "refresh-token"

        def refresh(self, request):
            raise RuntimeError("revoked")

    monkeypatch.setattr(
        "auth.google_auth.get_credentials",
        lambda user_google_email, required_scopes: None,
    )
    monkeypatch.setattr(
        "auth.oauth21_session_store.get_oauth21_session_store",
        lambda: _FakeSessionStore(credentials=_FailingCreds()),
    )
    assert load_static_account_credentials(EMAIL_A, ["s1"]) is None


# ---------------------------------------------------------------------------
# Service-layer integration (get_authenticated_google_service_oauth21)
# ---------------------------------------------------------------------------


def _patch_static_auth_context(monkeypatch, access_token):
    monkeypatch.setattr(
        "auth.service_decorator.get_auth_provider", lambda: object()
    )
    monkeypatch.setattr(
        "auth.service_decorator.get_access_token", lambda: access_token
    )


@pytest.mark.asyncio
async def test_static_token_with_no_stored_credentials_raises_clear_error(
    monkeypatch,
):
    access_token = build_static_bearer_access_token(KEY_A, EMAIL_A, ["s1"])
    _patch_static_auth_context(monkeypatch, access_token)
    monkeypatch.setattr(
        "auth.service_decorator.load_static_account_credentials",
        lambda email, scopes: None,
    )

    with pytest.raises(GoogleAuthenticationError, match="no stored Google credentials"):
        await get_authenticated_google_service_oauth21(
            service_name="gmail",
            version="v1",
            tool_name="test_tool",
            user_google_email=EMAIL_A,
            required_scopes=["s1"],
        )


@pytest.mark.asyncio
async def test_static_token_uses_stored_credentials_for_mapped_account(monkeypatch):
    access_token = build_static_bearer_access_token(KEY_A, EMAIL_A, ["s1"])
    _patch_static_auth_context(monkeypatch, access_token)

    stored_creds = SimpleNamespace(valid=True, scopes=["s1"])
    observed = {}
    monkeypatch.setattr(
        "auth.service_decorator.load_static_account_credentials",
        lambda email, scopes: stored_creds if email == EMAIL_A else None,
    )

    def fake_build(service_name, version, credentials):
        observed["build"] = (service_name, version, credentials)
        return "fake-service"

    monkeypatch.setattr("auth.service_decorator.build", fake_build)

    service, email = await get_authenticated_google_service_oauth21(
        service_name="gmail",
        version="v1",
        tool_name="test_tool",
        user_google_email=EMAIL_A,
        required_scopes=["s1"],
    )
    assert service == "fake-service"
    assert email == EMAIL_A
    assert observed["build"] == ("gmail", "v1", stored_creds)


@pytest.mark.asyncio
async def test_static_token_scope_mismatch_raises(monkeypatch):
    access_token = build_static_bearer_access_token(KEY_A, EMAIL_A, ["s1"])
    _patch_static_auth_context(monkeypatch, access_token)
    monkeypatch.setattr(
        "auth.service_decorator.load_static_account_credentials",
        lambda email, scopes: SimpleNamespace(valid=True, scopes=[]),
    )

    with pytest.raises(GoogleAuthenticationError, match="lack required scopes"):
        await get_authenticated_google_service_oauth21(
            service_name="gmail",
            version="v1",
            tool_name="test_tool",
            user_google_email=EMAIL_A,
            required_scopes=["s1"],
        )


@pytest.mark.asyncio
async def test_static_token_cannot_impersonate_another_account(monkeypatch):
    """A static key stays pinned to its mapped account."""
    access_token = build_static_bearer_access_token(KEY_A, EMAIL_A, ["s1"])
    _patch_static_auth_context(monkeypatch, access_token)

    with pytest.raises(GoogleAuthenticationError, match="does not match requested user"):
        await get_authenticated_google_service_oauth21(
            service_name="gmail",
            version="v1",
            tool_name="test_tool",
            user_google_email=EMAIL_B,
            required_scopes=["s1"],
        )
