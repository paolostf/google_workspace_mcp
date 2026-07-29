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
