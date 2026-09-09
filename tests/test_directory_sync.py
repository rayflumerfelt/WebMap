"""Directory reconciliation. `03-auth-security.md` §2.

`12-roadmap.md` Phase 1: *"A geologist logs in via SSO and lands with the
correct team memberships."* The half of that worth testing is not the happy
path — it is what happens when membership *changes*, because the directory is
authoritative and a user removed from a group upstream must lose access here
without anyone doing anything.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from webmap_core.identity import AuthenticationFailed, Claims
from webmap_core.permissions import Channel
from webmap_core.services.directory import (
    resolve_principal,
    sync_user_from_claims,
    team_ids_for,
)

pytestmark = pytest.mark.integration


def claims_for(*groups: str, subject: str = "test|newcomer") -> Claims:
    return Claims(
        subject=subject,
        email="newcomer@example.test",
        display_name="Nadia Newcomer",
        groups=groups,
    )


async def _slugs(engine: AsyncEngine, user_id: UUID) -> set[str]:
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT t.slug FROM team_member m JOIN team t ON t.id = m.team_id "
                "WHERE m.user_id = :id"
            ),
            {"id": user_id},
        )
        return {row.slug for row in result}


async def test_first_login_creates_the_user_and_joins_mapped_teams(
    migrator_engine: AsyncEngine, people: dict[str, UUID]
) -> None:
    async with migrator_engine.begin() as conn:
        synced = await sync_user_from_claims(conn, claims_for("group-owner"))

    assert synced.created is True
    assert synced.teams_added == ("owner-team",)
    assert synced.team_ids == {people["owner_team"]}
    assert await _slugs(migrator_engine, synced.user_id) == {"owner-team"}


async def test_a_returning_user_is_not_reported_as_created(
    migrator_engine: AsyncEngine, people: dict[str, UUID]
) -> None:
    """`created` drives the first-login audit event, so a false positive would
    make every login look like a new account."""
    async with migrator_engine.begin() as conn:
        first = await sync_user_from_claims(conn, claims_for("group-owner"))
        second = await sync_user_from_claims(conn, claims_for("group-owner"))

    assert first.created is True
    assert second.created is False
    assert second.user_id == first.user_id
    assert second.teams_added == ()


async def test_removal_upstream_removes_membership_here(
    migrator_engine: AsyncEngine, people: dict[str, UUID]
) -> None:
    """The property the whole design rests on.

    Membership is *replaced*, not merged. If it merged, revoking a group in
    the directory would never take effect and access would only ever grow.
    """
    async with migrator_engine.begin() as conn:
        await sync_user_from_claims(conn, claims_for("group-owner", "group-other"))
    async with migrator_engine.begin() as conn:
        after = await sync_user_from_claims(conn, claims_for("group-owner"))

    assert after.teams_removed == ("other-team",)
    assert await _slugs(migrator_engine, after.user_id) == {"owner-team"}


async def test_losing_every_group_leaves_no_memberships(
    migrator_engine: AsyncEngine, people: dict[str, UUID]
) -> None:
    async with migrator_engine.begin() as conn:
        await sync_user_from_claims(conn, claims_for("group-owner"))
    async with migrator_engine.begin() as conn:
        after = await sync_user_from_claims(conn, claims_for())

    assert after.teams_removed == ("owner-team",)
    assert await _slugs(migrator_engine, after.user_id) == set()


async def test_unmapped_groups_are_ignored(
    migrator_engine: AsyncEngine, people: dict[str, UUID]
) -> None:
    """Users are in dozens of directory groups that mean nothing here.

    Auto-creating a team per group would make the team list a mirror of the
    whole directory, and `visibility='team'` would stop meaning anything.
    """
    async with migrator_engine.begin() as conn:
        synced = await sync_user_from_claims(
            conn, claims_for("group-owner", "some-unrelated-ad-group", "another-one")
        )

    assert synced.teams_added == ("owner-team",)
    async with migrator_engine.begin() as conn:
        count = await conn.execute(text("SELECT count(*) FROM team"))
    assert count.scalar_one() == 2, "no team should have been created from a claim"


async def test_email_and_name_refresh_on_login_but_the_user_is_the_same(
    migrator_engine: AsyncEngine, people: dict[str, UUID]
) -> None:
    """Keyed on `sub`, so a rename updates the row rather than forking it.

    An email-keyed user who marries becomes a second account owning none of
    their datasets — the failure this keying exists to prevent.
    """
    async with migrator_engine.begin() as conn:
        first = await sync_user_from_claims(conn, claims_for("group-owner"))
        renamed = Claims(
            subject="test|newcomer",
            email="nadia.married@example.test",
            display_name="Nadia Married",
            groups=("group-owner",),
        )
        second = await sync_user_from_claims(conn, renamed)

    assert second.user_id == first.user_id
    async with migrator_engine.begin() as conn:
        row = await conn.execute(
            text("SELECT email, display_name FROM app_user WHERE id = :id"),
            {"id": first.user_id},
        )
    updated = row.one()
    assert updated.email == "nadia.married@example.test"
    assert updated.display_name == "Nadia Married"


# --- resolve_principal ------------------------------------------------------


async def test_resolve_principal_creates_an_unknown_user(
    migrator_engine: AsyncEngine, people: dict[str, UUID]
) -> None:
    """The MCP path: a valid token for someone who has never used the browser.

    They must not be refused — their first contact with WebMap can legitimately
    be a tool call.
    """
    async with migrator_engine.begin() as conn:
        principal = await resolve_principal(conn, claims_for("group-owner"), Channel.CLAUDE)

    assert principal.channel is Channel.CLAUDE
    assert principal.team_ids == {people["owner_team"]}
    # The cache was reconciled too, not only the token-derived set.
    async with migrator_engine.begin() as conn:
        assert await team_ids_for(conn, principal.user_id) == {people["owner_team"]}


async def test_team_ids_come_from_the_token_not_the_cache(
    migrator_engine: AsyncEngine, people: dict[str, UUID]
) -> None:
    """Authorization is as fresh as the token, not as fresh as the reconcile.

    Arranged so the two disagree: the row is synced with one group, then a
    principal is resolved from a token carrying another. The token wins, which
    is what keeps a five-minute reconcile window from delaying a permission
    change by five minutes.
    """
    async with migrator_engine.begin() as conn:
        await sync_user_from_claims(conn, claims_for("group-owner"))

    async with migrator_engine.begin() as conn:
        principal = await resolve_principal(conn, claims_for("group-other"), Channel.WEB)

    assert principal.team_ids == {people["other_team"]}


async def test_a_deactivated_user_is_refused(
    migrator_engine: AsyncEngine, people: dict[str, UUID]
) -> None:
    """The local kill switch, independent of the directory.

    Takes effect on the next request rather than at token expiry, so a
    departing employee loses access immediately even while the IdP still
    authenticates them.
    """
    async with migrator_engine.begin() as conn:
        synced = await sync_user_from_claims(conn, claims_for("group-owner"))
        # Backdate so the staleness check does not re-sync and reset the flag.
        await conn.execute(
            text(
                "UPDATE app_user SET is_active = FALSE, "
                "last_seen_at = now() - interval '1 minute' WHERE id = :id"
            ),
            {"id": synced.user_id},
        )

    async with migrator_engine.begin() as conn:
        with pytest.raises(AuthenticationFailed) as excinfo:
            await resolve_principal(conn, claims_for("group-owner"), Channel.WEB)

    message = str(excinfo.value)
    assert "deactivated in WebMap" in message
    assert "administrator" in message


async def test_unknown_groups_resolve_to_no_teams(
    migrator_engine: AsyncEngine, people: dict[str, UUID]
) -> None:
    async with migrator_engine.begin() as conn:
        principal = await resolve_principal(
            conn, claims_for("not-a-mapped-group", subject=f"test|{uuid4()}"), Channel.WEB
        )

    assert principal.team_ids == frozenset()
