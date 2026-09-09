"""Tests for scoped tile tokens.

These guard the control that stands between a tile URL and someone else's
data (`03-auth-security.md` §6). Every negative case here is a way the token
could authorize something it should not.
"""

from uuid import UUID

import pytest

from webmap_core.exceptions import InvalidToken
from webmap_core.signing import (
    mint_tile_token,
    render_token_claims,
    verify_tile_token,
)

SECRET = b"a-test-signing-key-of-reasonable-length"
OTHER_SECRET = b"a-different-key-entirely-of-the-same-size"
DATASET_A = UUID("11111111-1111-1111-1111-111111111111")
DATASET_B = UUID("22222222-2222-2222-2222-222222222222")
USER = UUID("33333333-3333-3333-3333-333333333333")
NOW = 1_757_000_000.0


def test_round_trip_returns_the_user() -> None:
    token = mint_tile_token(DATASET_A, USER, SECRET, now=NOW)

    assert verify_tile_token(token, DATASET_A, SECRET, now=NOW) == USER


def test_token_for_one_dataset_cannot_fetch_another() -> None:
    """The scoping property. A leaked URL exposes exactly one layer."""
    token = mint_tile_token(DATASET_A, USER, SECRET, now=NOW)

    with pytest.raises(InvalidToken, match="not valid for this dataset"):
        verify_tile_token(token, DATASET_B, SECRET, now=NOW)


def test_expired_token_is_refused() -> None:
    token = mint_tile_token(DATASET_A, USER, SECRET, ttl_seconds=900, now=NOW)

    with pytest.raises(InvalidToken, match="expired"):
        verify_tile_token(token, DATASET_A, SECRET, now=NOW + 901)


def test_token_is_still_valid_one_second_before_expiry() -> None:
    token = mint_tile_token(DATASET_A, USER, SECRET, ttl_seconds=900, now=NOW)

    assert verify_tile_token(token, DATASET_A, SECRET, now=NOW + 899) == USER


def test_a_token_signed_with_another_key_is_refused() -> None:
    token = mint_tile_token(DATASET_A, USER, OTHER_SECRET, now=NOW)

    with pytest.raises(InvalidToken, match="Signature mismatch"):
        verify_tile_token(token, DATASET_A, SECRET, now=NOW)


def test_tampering_with_the_expiry_is_refused() -> None:
    """The forgery this scheme exists to stop.

    Without the HMAC the payload is plainly readable and trivially editable —
    push the expiry out a year and the URL never dies.
    """
    import base64

    raw = base64.urlsafe_b64decode(
        mint_tile_token(DATASET_A, USER, SECRET, now=NOW).encode()
    ).decode()
    ds, uid, _expires, sig = raw.rsplit(":", 3)
    forged = base64.urlsafe_b64encode(
        f"{ds}:{uid}:{int(NOW) + 31_536_000}:{sig}".encode()
    ).decode()

    with pytest.raises(InvalidToken, match="Signature mismatch"):
        verify_tile_token(forged, DATASET_A, SECRET, now=NOW)


def test_tampering_with_the_dataset_is_refused_as_a_signature_failure() -> None:
    """Signature is checked before the fields are trusted.

    The distinction matters: a "not valid for this dataset" response to a
    forged token would confirm the signature verified, which it did not.
    """
    import base64

    raw = base64.urlsafe_b64decode(
        mint_tile_token(DATASET_A, USER, SECRET, now=NOW).encode()
    ).decode()
    _ds, uid, expires, sig = raw.rsplit(":", 3)
    forged = base64.urlsafe_b64encode(f"{DATASET_B}:{uid}:{expires}:{sig}".encode()).decode()

    with pytest.raises(InvalidToken, match="Signature mismatch"):
        verify_tile_token(forged, DATASET_B, SECRET, now=NOW)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-base64!!",
        "YWJj",  # valid base64, no separators
        "OjE6Mg==",  # too few fields
    ],
)
def test_malformed_tokens_raise_invalid_token_not_something_else(token: str) -> None:
    """Never let a ValueError or a UnicodeDecodeError escape as a 500.

    A malformed token is a 403, not a server error — otherwise a scanner can
    tell forged tokens from malformed ones by the status code.
    """
    with pytest.raises(InvalidToken):
        verify_tile_token(token, DATASET_A, SECRET, now=NOW)


def test_minting_without_a_secret_is_refused() -> None:
    """An unsigned token authorizes everyone. Fail at mint, not at verify."""
    with pytest.raises(ValueError, match="empty signing secret"):
        mint_tile_token(DATASET_A, USER, b"", now=NOW)


def test_render_claims_carry_identity_and_a_short_life() -> None:
    """`03` §6.1: the render token carries who, not what they may do."""
    render_id = UUID("44444444-4444-4444-4444-444444444444")
    teams = frozenset({DATASET_A, DATASET_B})  # arbitrary uuids as team ids

    claims = render_token_claims(USER, teams, render_id, now=NOW)

    assert claims["sub"] == str(USER)
    assert claims["aud"] == "webmap-tiles"
    assert claims["iss"] == "webmap-api"
    assert claims["render_id"] == str(render_id)
    assert claims["exp"] == int(NOW) + 300
    assert sorted(str(t) for t in teams) == claims["teams"]


def test_render_claims_grant_no_privilege() -> None:
    """Identity, not privilege (`03` §4.4).

    If a role, scope, or permission ever appears in these claims, revoking
    someone's access stops taking effect until their token expires.
    """
    claims = render_token_claims(
        USER, frozenset(), UUID("44444444-4444-4444-4444-444444444444"), now=NOW
    )

    forbidden = {"role", "roles", "scope", "scopes", "permissions", "visibility"}
    assert not (forbidden & set(claims))
