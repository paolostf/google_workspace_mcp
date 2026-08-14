"""
Refresh-token rotation grace window for the Google Workspace MCP.

WHY THIS EXISTS
---------------
FastMCP's OAuthProxy rotates the refresh token on every use and immediately
invalidates the previous one (`oauth_proxy.py`: delete of the old JTI mapping
plus the old refresh-token hash, "one-time use enforced"). That is correct
OAuth 2.1 practice for a single sequential client, but it breaks any client
that opens several concurrent connections with the same stored credentials.

Observed failure (2026-07-27, this deployment): two `POST /token` refreshes
landed in the same second, both returned 200 with "rotated refresh"
(refresh_jti=X3fvdRUy and refresh_jti=flrswyYS). The client can only persist
one of them, so the next request presented an already-rotated token; its JTI
mapping was gone and the server answered `invalid_grant` / 401
`invalid_token`. All three gws-* connections died at once and every scheduled
run failed until a human re-authenticated. Root cause of the concurrency is
legitimate and permanent on our side: the Inbox Operator runs many parallel
headless workers, each with its own MCP client connection.

THE FIX
-------
Keep rotation, but honour the OAuth 2.1 security BCP allowance for a short
grace period on the superseded refresh token: after a successful rotation the
old token is restored for GRACE_SECONDS so that refreshes already in flight
still succeed, then it expires on its own. This removes the race without
giving up one-time-use semantics beyond that window.

Configure with REFRESH_ROTATION_GRACE_SECONDS (default 120, 0 disables).
"""

import asyncio
import logging
import os
import time

from fastmcp.server.auth.providers.google import GoogleProvider

try:  # fastmcp >= 3.2 keeps these in the oauth_proxy package
    from fastmcp.server.auth.oauth_proxy.models import (
        JTIMapping,
        RefreshTokenMetadata,
        _hash_token,
    )
except ImportError:  # older single-module layout
    from fastmcp.server.auth.oauth_proxy import (  # type: ignore[no-redef]
        JTIMapping,
        RefreshTokenMetadata,
        _hash_token,
    )

logger = logging.getLogger(__name__)

_DEFAULT_GRACE_SECONDS = 120
_MAX_GRACE_SECONDS = 900


def get_rotation_grace_seconds() -> int:
    """Grace window for a superseded refresh token, in seconds."""
    raw = os.getenv("REFRESH_ROTATION_GRACE_SECONDS", "")
    if not raw:
        return _DEFAULT_GRACE_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid REFRESH_ROTATION_GRACE_SECONDS=%r, using %d",
            raw,
            _DEFAULT_GRACE_SECONDS,
        )
        return _DEFAULT_GRACE_SECONDS
    return max(0, min(value, _MAX_GRACE_SECONDS))


class RotationGraceGoogleProvider(GoogleProvider):
    """GoogleProvider that keeps a just-rotated refresh token usable briefly.

    Concurrent refreshes from parallel workers are a normal operating mode
    here, so a superseded token must not immediately become an authentication
    failure for the whole grant.
    """

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        grace = get_rotation_grace_seconds()
        old_jti = None
        if grace:
            try:
                old_jti = self.jwt_issuer.verify_token(refresh_token.token)["jti"]
            except Exception:
                # Unverifiable token: let the parent raise the proper error.
                old_jti = None
        old_mapping = None
        if old_jti:
            old_mapping = await self._jti_mapping_store.get(key=old_jti)

        token = await super().exchange_refresh_token(client, refresh_token, scopes)

        if grace and old_jti and old_mapping is not None:
            # Parent deleted both entries; restore them for the grace window so
            # an in-flight concurrent refresh still resolves.
            await self._jti_mapping_store.put(
                key=old_jti,
                value=JTIMapping(
                    jti=old_jti,
                    upstream_token_id=old_mapping.upstream_token_id,
                    created_at=old_mapping.created_at,
                ),
                ttl=grace,
            )
            await self._refresh_token_store.put(
                key=_hash_token(refresh_token.token),
                value=RefreshTokenMetadata(
                    client_id=client.client_id,
                    scopes=scopes,
                    expires_at=int(time.time()) + grace,
                    created_at=time.time(),
                ),
                ttl=grace,
            )
            logger.info(
                "Refresh rotated with %ds grace on superseded token (old_jti=%s)",
                grace,
                old_jti[:8],
            )
        return token

    async def _extract_upstream_claims(self, idp_tokens):
        """Mirror the freshly-obtained Google credential into the per-account
        credential store keyed by email.

        FastMCP's OAuth 2.1 proxy stores the upstream Google token only under the
        proxy SESSION/client (by upstream_token_id). The static-bearer path has a
        synthetic session and reads the per-account store by email, so without
        this mirror an OAuth consent never reaches that store and the account
        stays "no stored Google credentials". This hook runs on BOTH the
        authorization-code exchange (consent) and refresh, so it seeds on consent
        and keeps the access token fresh on refresh. Best-effort: a mirror
        failure never breaks the OAuth flow.
        """
        claims = await super()._extract_upstream_claims(idp_tokens)
        try:
            await self._mirror_credential_to_per_account_store(idp_tokens)
        except Exception as exc:  # never break auth on a mirror failure
            logger.warning("Per-account credential mirror failed: %s", exc)
        return claims

    async def _mirror_credential_to_per_account_store(self, idp_tokens) -> None:
        if not isinstance(idp_tokens, dict):
            return
        access_token = idp_tokens.get("access_token")
        if not access_token:
            return

        email = await self._resolve_google_email(access_token)
        if not email:
            logger.warning(
                "Per-account credential mirror: could not resolve account email; skipping."
            )
            return

        from auth.credential_store import (
            get_credential_store,
            credentials_from_google_token_response,
        )

        store = get_credential_store()

        refresh_token = idp_tokens.get("refresh_token")
        if not refresh_token:
            # Refresh-grant responses usually omit the refresh token; preserve the
            # one already stored for this account so a refresh keeps it usable.
            try:
                existing = await asyncio.to_thread(store.get_credential, email)
            except Exception:
                existing = None
            if existing is not None and existing.refresh_token:
                refresh_token = existing.refresh_token

        credentials = credentials_from_google_token_response(
            idp_tokens, refresh_token=refresh_token
        )
        if credentials is None:
            logger.warning(
                "Per-account credential mirror: no refresh token available for %s; skipping.",
                email,
            )
            return

        stored = await asyncio.to_thread(store.store_credential, email, credentials)
        logger.info(
            "Per-account credential mirror: %s for %s.",
            "stored" if stored else "store REJECTED",
            email,
        )

    async def _resolve_google_email(self, access_token):
        """Resolve the Google account email for an access token via tokeninfo
        (falling back to the v2 userinfo endpoint), the same endpoints the
        FastMCP GoogleTokenVerifier uses. Returns None on failure.
        """
        import httpx

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://oauth2.googleapis.com/tokeninfo",
                    params={"access_token": access_token},
                    headers={"User-Agent": "gws-mcp-credential-mirror"},
                )
                if resp.status_code == 200:
                    email = resp.json().get("email")
                    if email:
                        return email
                resp2 = await client.get(
                    "https://www.googleapis.com/oauth2/v2/userinfo",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "User-Agent": "gws-mcp-credential-mirror",
                    },
                )
                if resp2.status_code == 200:
                    return resp2.json().get("email")
        except Exception as exc:
            logger.debug(
                "Per-account credential mirror: email resolution failed: %s", exc
            )
        return None
