"""Scoped tile tokens. `03-auth-security.md` §6.

Tile endpoints are hit thousands of times during a single pan. The temptation
to leave them open for performance is exactly how data leaks.

A token here is scoped to **one dataset and one user** for fifteen minutes, so
a leaked tile URL exposes exactly one already-authorized layer. This is not a
general-purpose credential and must never become one.

The multi-layer render path deliberately does *not* use these — see §6.1 and
`render_token` below.
"""

import base64
import binascii
import hashlib
import hmac
import time
from uuid import UUID

from webmap_core.exceptions import InvalidToken

DEFAULT_TTL_SECONDS = 900


def mint_tile_token(
    dataset_id: UUID,
    user_id: UUID,
    secret: bytes,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> str:
    """Scope: one dataset, one user, fifteen minutes.

    A token for dataset A cannot fetch dataset B.
    """
    if not secret:
        raise ValueError(
            "Refusing to mint a tile token with an empty signing secret. Set "
            "WEBMAP_TILE_TOKEN_SECRET — an unsigned token authorizes everyone."
        )
    expires = int((now if now is not None else time.time()) + ttl_seconds)
    payload = f"{dataset_id}:{user_id}:{expires}"
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode()


def verify_tile_token(
    token: str, dataset_id: UUID, secret: bytes, *, now: float | None = None
) -> UUID:
    """Return the user_id the token was minted for, or raise.

    Order matters. The signature is checked *before* the fields are trusted,
    because an unverified expiry or dataset id is attacker-controlled input.
    Comparison is constant-time so a forged signature cannot be recovered a
    byte at a time from response timings.
    """
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidToken("Tile token is not valid base64url.") from exc

    try:
        ds, uid, expires_raw, sig_hex = raw.rsplit(":", 3)
    except ValueError as exc:
        raise InvalidToken("Tile token is malformed.") from exc

    expected = hmac.new(secret, f"{ds}:{uid}:{expires_raw}".encode(), hashlib.sha256)
    try:
        presented = bytes.fromhex(sig_hex)
    except ValueError as exc:
        raise InvalidToken("Signature mismatch") from exc
    if not hmac.compare_digest(presented, expected.digest()):
        raise InvalidToken("Signature mismatch")

    # Only now are the fields trustworthy.
    try:
        if UUID(ds) != dataset_id:
            raise InvalidToken("Token is not valid for this dataset")
        if int(expires_raw) < (now if now is not None else time.time()):
            raise InvalidToken("Token expired")
        return UUID(uid)
    except ValueError as exc:
        raise InvalidToken("Tile token carries malformed identifiers.") from exc


def render_token_claims(
    user_id: UUID,
    team_ids: frozenset[UUID],
    render_id: UUID,
    *,
    ttl_seconds: int = 300,
    now: float | None = None,
) -> dict[str, object]:
    """Claims for a render-scoped token. `03-auth-security.md` §6.1.

    A render is multi-layer by definition, so a per-dataset token cannot serve
    it. Instead the render service is issued a short-lived token carrying the
    requesting user's identity, and the tile proxy builds a `Principal` from
    it and runs the ordinary `require(..., VIEWER)` check per dataset.

    **One authorization code path, not two.** A render can therefore never
    reach a layer its requester cannot, even if style assembly has a bug.

    The token is injected by the Playwright route handler (§7.2) and never
    enters the page's JavaScript context.
    """
    issued = int(now if now is not None else time.time())
    return {
        "sub": str(user_id),
        "teams": sorted(str(t) for t in team_ids),
        "render_id": str(render_id),
        "aud": "webmap-tiles",
        "iss": "webmap-api",
        "iat": issued,
        # Renders finish in under 5 s (06 §11). A five-minute window is
        # already generous; anything longer is a credential, not a scope.
        "exp": issued + ttl_seconds,
    }
