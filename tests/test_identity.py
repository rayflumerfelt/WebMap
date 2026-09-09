"""Claims mapping and token verification. No database, no network.

The deployment-day unknowns `adr/0009-offline-identity-seam.md` names live
here: which claim carries the email, what the groups claim is called, and
whether a single group arrives as a string or a list. Those are the three
things most likely to be wrong when a real tenant is connected, so they are
the three things tested hardest.
"""

import time

import pytest

from webmap_core.identity import (
    AuthenticationFailed,
    claims_from_mapping,
    normalise_groups,
)
from webmap_core.settings import Settings

VALID = {
    "sub": "abc-123",
    "email": "ada.lovelace@example.test",
    "name": "Ada Lovelace",
    "groups": ["group-permian", "group-exploration"],
}


def _settings(**overrides: object) -> Settings:
    return Settings(environment="local", auth_mode="dev", **overrides)  # type: ignore[arg-type]


# --- Claims mapping ---------------------------------------------------------


def test_maps_a_standard_payload() -> None:
    claims = claims_from_mapping(VALID)

    assert claims.subject == "abc-123"
    assert claims.email == "ada.lovelace@example.test"
    assert claims.display_name == "Ada Lovelace"
    assert claims.groups == ("group-exploration", "group-permian")


def test_a_missing_sub_is_fatal_and_says_why() -> None:
    """`sub` is the only claim this system keys users on.

    Falling back to email would key on something that changes, and a renamed
    person becomes a second user owning none of their datasets.
    """
    with pytest.raises(AuthenticationFailed) as excinfo:
        claims_from_mapping({k: v for k, v in VALID.items() if k != "sub"})

    message = str(excinfo.value)
    assert "'sub'" in message
    assert "email changes" in message


@pytest.mark.parametrize("claim", ["email", "preferred_username", "upn"])
def test_email_is_taken_from_whichever_claim_carries_it(claim: str) -> None:
    """Entra emits any of these depending on tenant configuration.

    Getting it wrong on deployment day looks like every user failing to log
    in, so all three fallbacks are exercised rather than assumed.
    """
    payload = {"sub": "abc-123", "name": "Ada", claim: "ada@example.test"}

    assert claims_from_mapping(payload).email == "ada@example.test"


def test_no_email_claim_at_all_names_the_fix() -> None:
    with pytest.raises(AuthenticationFailed) as excinfo:
        claims_from_mapping({"sub": "abc-123", "name": "Ada"})

    assert "token configuration in the IdP" in str(excinfo.value)


def test_a_configured_groups_claim_is_honoured() -> None:
    """Some tenants emit `roles` rather than `groups`."""
    payload = {**VALID, "roles": ["group-a"]}
    del payload["groups"]

    claims = claims_from_mapping(payload, groups_claim="roles")

    assert claims.groups == ("group-a",)


def test_a_single_group_arriving_as_a_bare_string_is_accepted() -> None:
    """A real IdP quirk. Unhandled, it makes a one-team user a no-team user —
    the failure looks like a permission bug rather than a parsing one."""
    claims = claims_from_mapping({**VALID, "groups": "group-permian"})

    assert claims.groups == ("group-permian",)


def test_display_name_falls_back_to_email() -> None:
    payload = {k: v for k, v in VALID.items() if k != "name"}

    assert claims_from_mapping(payload).display_name == VALID["email"]


def test_missing_groups_means_no_teams_not_an_error() -> None:
    """A user in no mapped group is legitimate — they see only their own work."""
    payload = {k: v for k, v in VALID.items() if k != "groups"}

    assert claims_from_mapping(payload).groups == ()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ()),
        ([], ()),
        (["  b  ", "a"], ("a", "b")),
        (["a", "a", "a"], ("a",)),
        (["", "  ", "a"], ("a",)),
    ],
)
def test_group_normalisation(raw: list[str] | None, expected: tuple[str, ...]) -> None:
    assert normalise_groups(raw) == expected


def test_raw_claims_are_kept_but_not_compared() -> None:
    """`raw` is for audit and debugging, never for authorization.

    Excluded from equality so two Claims that agree on every field this system
    reads are equal even when the tokens differed elsewhere.
    """
    a = claims_from_mapping({**VALID, "extra": 1})
    b = claims_from_mapping({**VALID, "extra": 2})

    assert a == b
    assert a.raw["extra"] == 1


# --- The dev verifier -------------------------------------------------------


async def test_dev_token_round_trip() -> None:
    from webmap_api.auth.dev import DEV_USERS, DevTokenVerifier

    verifier = DevTokenVerifier(_settings())
    claims = await verifier.verify(verifier.issue(DEV_USERS["ada"]))

    assert claims.subject == "dev|ada"
    assert claims.email == "ada.lovelace@webmap.local"
    assert claims.groups == ("seed-group-permian",)


async def test_a_token_from_another_process_is_rejected() -> None:
    """The whole security model of the dev verifier, asserted.

    Each instance generates its own key, so a token minted anywhere else —
    including a previous run of this process — verifies nowhere.
    """
    from webmap_api.auth.dev import DEV_USERS, DevTokenVerifier

    issuer = DevTokenVerifier(_settings())
    other = DevTokenVerifier(_settings())

    with pytest.raises(AuthenticationFailed):
        await other.verify(issuer.issue(DEV_USERS["ada"]))


async def test_an_expired_token_is_rejected() -> None:
    from webmap_api.auth.dev import DEV_USERS, DevTokenVerifier

    verifier = DevTokenVerifier(_settings())
    token = verifier.issue(DEV_USERS["ada"], ttl_seconds=-1)

    with pytest.raises(AuthenticationFailed):
        await verifier.verify(token)


async def test_a_token_for_a_different_audience_is_rejected() -> None:
    """Audience binding, which `03-auth-security.md` §4.4 requires.

    Tested on the dev verifier because it is the one that can mint a token for
    an arbitrary audience offline. The production verifier passes the same
    `audience` argument to the same library call, so this covers the decision
    even though it cannot cover that code path.
    """
    from webmap_api.auth.dev import DEV_USERS, DevTokenVerifier

    issuer = DevTokenVerifier(_settings(oidc_audience="api://something-else"))
    receiver = DevTokenVerifier(_settings(oidc_audience="api://webmap"))
    # Same key, so only the audience differs.
    receiver._key = issuer._key

    with pytest.raises(AuthenticationFailed):
        await receiver.verify(issuer.issue(DEV_USERS["ada"]))


async def test_a_dev_token_carries_identity_and_no_privilege() -> None:
    """`03` §4.4: the token says who you are, never what you may do.

    If a role or scope claim ever appears here, revoking someone's access
    stops taking effect until their token expires.
    """
    import jwt

    from webmap_api.auth.dev import DEV_USERS, DevTokenVerifier

    verifier = DevTokenVerifier(_settings())
    payload = jwt.decode(
        verifier.issue(DEV_USERS["ada"]),
        verifier._key,
        algorithms=["HS256"],
        audience="api://webmap",
    )

    forbidden = {"role", "roles", "scope", "scp", "permissions", "visibility"}
    assert not (forbidden & set(payload))


async def test_the_dev_roster_has_the_shape_the_criteria_need() -> None:
    """Two users on one team and one on another.

    Without that, "User A cannot read User B's private dataset" cannot be
    demonstrated by hand against the local stack.
    """
    from webmap_api.auth.dev import DEV_USERS

    by_group: dict[tuple[str, ...], list[str]] = {}
    for key, user in DEV_USERS.items():
        by_group.setdefault(user.groups, []).append(key)

    assert len(by_group) >= 2, "the roster must span at least two teams"
    assert any(len(users) >= 2 for users in by_group.values()), (
        "at least two users must share a team, or team visibility cannot be shown"
    )


async def test_dev_tokens_expire_within_the_hour() -> None:
    """Bounded by `access_token_ttl_seconds`, which is capped at 3600.

    A long-lived development token is the thing most likely to be pasted
    somewhere it outlives its usefulness.
    """
    import jwt

    from webmap_api.auth.dev import DEV_USERS, DevTokenVerifier

    verifier = DevTokenVerifier(_settings())
    payload = jwt.decode(
        verifier.issue(DEV_USERS["ada"]),
        verifier._key,
        algorithms=["HS256"],
        audience="api://webmap",
    )

    assert payload["exp"] - time.time() <= 3600
