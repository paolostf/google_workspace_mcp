"""
Static bearer authentication (additive, opt-in).

Maps pre-shared static bearer keys to Google account emails via the
``GWS_STATIC_BEARERS`` environment variable:

    GWS_STATIC_BEARERS='{"<key1>": "user1@example.com", "<key2>": "user2@example.com"}'

When an incoming request's ``Authorization: Bearer <token>`` matches a
configured key (timing-safe comparison), the request is authenticated as the
mapped account and Google credentials are loaded through the same stored
credential paths the OAuth flows use (persistent credential store first, then
the in-memory OAuth 2.1 session store).

Fail-closed behavior:
- Env var absent, empty, or malformed JSON  -> feature disabled entirely.
- Entry with a non-string key/value, an email without "@", or a key shorter
  than ``MIN_STATIC_KEY_LENGTH`` characters -> that entry is skipped.
- A matched key whose account has no stored Google credentials -> a clear
  ``GoogleAuthenticationError`` is raised by the service layer (no crash).

The OAuth paths are untouched: when no static key matches, token verification
delegates to the original provider implementation unchanged.
"""

import hashlib
import hmac
import json
import logging
import os
import time
from functools import lru_cache
from typing import Dict, List, Optional

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials

from auth.oauth_types import WorkspaceAccessToken

logger = logging.getLogger(__name__)

# Environment variable holding the JSON mapping of static keys to emails.
STATIC_BEARERS_ENV_VAR = "GWS_STATIC_BEARERS"

# Reject configured keys shorter than this (guessable keys are worse than none).
MIN_STATIC_KEY_LENGTH = 16


class StaticBearerAccessToken(WorkspaceAccessToken):
    """Marker access-token type for statically authenticated sessions.

    The service layer uses an ``isinstance`` check on this type to route
    credential loading through the stored-credential paths instead of
    treating the bearer value itself as a Google access token.
    """


@lru_cache(maxsize=8)
def _parse_static_bearer_config(raw: str) -> tuple:
    """Parse and validate the raw env value into ((key, email), ...).

    Returns an empty tuple (feature disabled) when the value is missing or
    malformed. Individual invalid entries are skipped. Cached per raw string
    so repeated per-request calls are cheap while env changes (tests,
    restarts) are picked up.
    """
    if not raw or not raw.strip():
        return ()

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error(
            "%s is not valid JSON; static bearer authentication DISABLED: %s",
            STATIC_BEARERS_ENV_VAR,
            exc,
        )
        return ()

    if not isinstance(parsed, dict):
        logger.error(
            "%s must be a JSON object mapping keys to emails; "
            "static bearer authentication DISABLED (got %s).",
            STATIC_BEARERS_ENV_VAR,
            type(parsed).__name__,
        )
        return ()

    entries = []
    for key, email in parsed.items():
        if not isinstance(key, str) or not isinstance(email, str):
            logger.warning(
                "%s: skipping entry with non-string key or value.",
                STATIC_BEARERS_ENV_VAR,
            )
            continue
        key = key.strip()
        email = email.strip()
        if not key or not email or "@" not in email:
            logger.warning(
                "%s: skipping entry with empty key or invalid email.",
                STATIC_BEARERS_ENV_VAR,
            )
            continue
        if len(key) < MIN_STATIC_KEY_LENGTH:
            logger.warning(
                "%s: skipping key shorter than %d characters (email: %s).",
                STATIC_BEARERS_ENV_VAR,
                MIN_STATIC_KEY_LENGTH,
                email,
            )
            continue
        entries.append((key, email))

    if entries:
        logger.info(
            "Static bearer authentication configured for %d account(s).",
            len(entries),
        )
    return tuple(entries)


def get_static_bearer_map() -> Dict[str, str]:
    """Return the configured static key -> email mapping ({} when disabled)."""
    return dict(_parse_static_bearer_config(os.getenv(STATIC_BEARERS_ENV_VAR, "")))


def is_static_bearer_configured() -> bool:
    """True when at least one valid static bearer entry is configured."""
    return bool(_parse_static_bearer_config(os.getenv(STATIC_BEARERS_ENV_VAR, "")))


def resolve_static_bearer_email(token: Optional[str]) -> Optional[str]:
    """Resolve a bearer token to its mapped account email, or None.

    Every configured key is compared with ``hmac.compare_digest`` and the
    loop never exits early, so comparison time does not reveal whether or
    where a match occurred.
    """
    if not token:
        return None

    entries = _parse_static_bearer_config(os.getenv(STATIC_BEARERS_ENV_VAR, ""))
    if not entries:
        return None

    token_bytes = token.encode("utf-8")
    matched_email: Optional[str] = None
    for key, email in entries:
        if hmac.compare_digest(token_bytes, key.encode("utf-8")):
            matched_email = email
    return matched_email


def build_static_bearer_access_token(
    token: str, email: str, scopes: Optional[List[str]] = None
) -> StaticBearerAccessToken:
    """Build the access token object for a matched static bearer key."""
    from auth.external_oauth_provider import get_session_time

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
    return StaticBearerAccessToken(
        token=token,
        client_id="gws-static-bearer",
        scopes=list(scopes or []),
        session_id=f"static_bearer_{token_hash}",
        expires_at=int(time.time()) + get_session_time(),
        claims={"email": email, "sub": email},
        email=email,
        sub=email,
    )


def install_static_bearer_verification(provider) -> bool:
    """Wrap ``provider.verify_token`` with static bearer resolution.

    Additive: when the presented token matches a configured static key the
    wrapper returns a ``StaticBearerAccessToken`` for the mapped account;
    otherwise it delegates to the original ``verify_token`` unchanged. With
    no (valid) configuration the wrapper is a passthrough.
    """
    if provider is None:
        return False

    original_verify_token = getattr(provider, "verify_token", None)
    if original_verify_token is None:
        if is_static_bearer_configured():
            logger.warning(
                "Static bearer authentication NOT installed: provider %s has no "
                "verify_token method.",
                type(provider).__name__,
            )
        return False

    async def verify_token_with_static_bearers(token: str):
        try:
            email = resolve_static_bearer_email(token)
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Static bearer resolution failed: %s", exc)
            email = None
        if email is not None:
            logger.info("Static bearer key matched for account: %s", email)
            scopes = list(getattr(provider, "required_scopes", []) or [])
            return build_static_bearer_access_token(token, email, scopes)
        return await original_verify_token(token)

    provider.verify_token = verify_token_with_static_bearers
    if is_static_bearer_configured():
        logger.info(
            "Static bearer authentication ENABLED (%d account(s) configured).",
            len(get_static_bearer_map()),
        )
    return True


def _refresh_and_store_session_credentials(
    user_email: str, credentials: Credentials
) -> Optional[Credentials]:
    """Refresh session-store credentials and write them back where OAuth does."""
    from auth.oauth21_session_store import get_oauth21_session_store

    try:
        credentials.refresh(GoogleAuthRequest())
    except Exception as exc:
        logger.warning(
            "Static bearer: failed to refresh session credentials for %s: %s",
            user_email,
            exc,
        )
        return None

    # Write the refreshed token back to the same stores the OAuth paths use.
    try:
        get_oauth21_session_store().store_session(
            user_email=user_email,
            access_token=credentials.token,
            refresh_token=credentials.refresh_token,
            token_uri=credentials.token_uri,
            client_id=credentials.client_id,
            client_secret=credentials.client_secret,
            scopes=credentials.scopes,
            expiry=credentials.expiry,
            session_id=f"google_{user_email}",
            issuer="https://accounts.google.com",
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "Static bearer: could not update session store for %s: %s",
            user_email,
            exc,
        )

    try:
        from auth.oauth_config import is_stateless_mode

        if not is_stateless_mode():
            from auth.credential_store import get_credential_store

            get_credential_store().store_credential(user_email, credentials)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "Static bearer: could not persist refreshed credentials for %s: %s",
            user_email,
            exc,
        )

    return credentials


def load_static_account_credentials(
    user_email: str, required_scopes: List[str]
) -> Optional[Credentials]:
    """Load stored Google credentials for a statically authenticated account.

    Reuses the same downstream loaders the OAuth flows use, in order:
    1. ``auth.google_auth.get_credentials`` - persistent credential store,
       including refresh + persistence of rotated tokens.
    2. The in-memory OAuth 2.1 session store (populated by OAuth-authenticated
       activity for the same account), refreshing when possible.

    Returns None when the account has no usable stored credentials.
    """
    from auth.google_auth import get_credentials
    from auth.oauth21_session_store import get_oauth21_session_store

    credentials: Optional[Credentials] = None
    try:
        credentials = get_credentials(
            user_google_email=user_email,
            required_scopes=required_scopes,
        )
    except Exception as exc:
        logger.warning(
            "Static bearer: credential store lookup failed for %s: %s",
            user_email,
            exc,
        )

    if credentials and credentials.valid:
        return credentials

    try:
        session_credentials = get_oauth21_session_store().get_credentials(user_email)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "Static bearer: session store lookup failed for %s: %s", user_email, exc
        )
        session_credentials = None

    if not session_credentials:
        return None
    if session_credentials.valid:
        return session_credentials
    if session_credentials.refresh_token:
        return _refresh_and_store_session_credentials(user_email, session_credentials)
    return None
