"""OIDC token verification against the corporate IdP. `03-auth-security.md` §2, §4.

The production path. Fetches the discovery document and JWKS lazily, caches
the signing keys, and verifies signature, issuer, expiry, and **audience**.

Audience is not optional. Without it, a token the IdP minted for any other
application in the same tenant is accepted here — the user is real, the
signature is real, and the token was never meant for us (`03` §4.4).
"""

import time
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient

from webmap_core.identity import AuthenticationFailed, Claims, claims_from_mapping
from webmap_core.logging import get_logger
from webmap_core.settings import Settings

log = get_logger(__name__)

#: Discovery documents change rarely; re-reading one per request would add a
#: network round trip to every call. An hour is short enough that a key
#: rollover recovers on its own.
_DISCOVERY_TTL_SECONDS = 3600


class OidcTokenVerifier:
    """Verifies bearer tokens issued by the corporate directory."""

    mode = "oidc"

    def __init__(self, settings: Settings) -> None:
        if not settings.oidc_discovery_url:
            raise ValueError(
                "auth_mode=oidc requires WEBMAP_OIDC_DISCOVERY_URL — the IdP's "
                "OpenID configuration endpoint, usually "
                "https://login.microsoftonline.com/<tenant>/v2.0/"
                ".well-known/openid-configuration. Refusing to start with an "
                "auth mode that cannot verify anything."
            )
        self._settings = settings
        self._metadata: dict[str, Any] | None = None
        self._metadata_fetched_at = 0.0
        self._jwks: PyJWKClient | None = None

    async def _discovery(self) -> dict[str, Any]:
        now = time.monotonic()
        if (
            self._metadata is not None
            and now - self._metadata_fetched_at < _DISCOVERY_TTL_SECONDS
        ):
            return self._metadata

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.get(self._settings.oidc_discovery_url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                # Distinguish "the IdP is unreachable" from "your token is
                # bad" — they need entirely different responses from an
                # operator, and conflating them sends people to the wrong one.
                raise AuthenticationFailed(
                    f"Could not reach the identity provider at "
                    f"{self._settings.oidc_discovery_url}. This is an outage "
                    f"or a network policy problem, not a problem with your "
                    f"credentials."
                ) from exc

        self._metadata = dict(response.json())
        self._metadata_fetched_at = now
        self._jwks = PyJWKClient(str(self._metadata["jwks_uri"]), cache_keys=True)
        return self._metadata

    async def verify(self, token: str) -> Claims:
        metadata = await self._discovery()
        assert self._jwks is not None  # set alongside metadata

        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "RS384", "RS512", "ES256"],
                audience=self._settings.oidc_audience,
                issuer=str(metadata["issuer"]),
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except jwt.PyJWTError as exc:
            # One message for every cause. Telling a caller whether the
            # signature or the expiry failed tells an attacker which half of a
            # forged token to fix; the detail goes to the log instead.
            log.warning("token_rejected", reason=type(exc).__name__, detail=str(exc))
            raise AuthenticationFailed(
                "The access token was rejected. It may have expired, or it may "
                "have been issued for a different application. Sign in again."
            ) from exc

        return claims_from_mapping(
            payload,
            groups_claim=self._settings.oidc_groups_claim,
            name_claim=self._settings.oidc_name_claim,
            email_claim=self._settings.oidc_email_claim,
        )
