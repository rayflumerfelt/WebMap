"""User and team reconciliation from directory claims. `03-auth-security.md` §2.

The directory is the source of truth; `app_user` and `team_member` are a
cache. A user removed from a group upstream loses that team's access on their
next login, with no manual cleanup — which is the point, and also why the
change is audited: it silently alters what they can see.

This is the one service that runs *before* a `Principal` exists — the user may
not have a row yet — so it takes an unscoped connection. That is safe because
`app_user`, `team`, and `team_member` carry no RLS policies: they are not
ownable objects (`02-data-model.md` §3.3 lists what is). Anything reaching an
ownable table belongs behind `principal_session` instead.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from webmap_core.identity import AuthenticationFailed, Claims, SyncedIdentity
from webmap_core.logging import get_logger
from webmap_core.permissions import Channel, Principal

log = get_logger(__name__)


async def sync_user_from_claims(conn: AsyncConnection, claims: Claims) -> SyncedIdentity:
    """Upsert the user and reconcile team membership from directory groups.

    Returns what changed, so the caller can audit a first login and any
    membership drift rather than discovering both from a diff of behaviour.
    """
    user_id, created = await _upsert_user(conn, claims)
    added, removed = await _reconcile_teams(conn, user_id, claims.groups)
    team_ids = await team_ids_for(conn, user_id)

    if created or added or removed:
        log.info(
            "directory_synced",
            user_id=str(user_id),
            created=created,
            teams_added=list(added),
            teams_removed=list(removed),
        )

    return SyncedIdentity(
        user_id=user_id,
        team_ids=team_ids,
        display_name=claims.display_name,
        created=created,
        teams_added=added,
        teams_removed=removed,
    )


async def _upsert_user(conn: AsyncConnection, claims: Claims) -> tuple[UUID, bool]:
    """Insert or refresh the user row, keyed on `subject`.

    Keyed on subject, not email: people are renamed and remarried, and an
    email-keyed user becomes a *second* user owning none of their datasets.
    Email and display name are refreshed on every login so the UI and
    permission messages stay current when they do change.

    `xmax = 0` is the standard Postgres trick for telling an INSERT from an
    UPDATE in a single upsert — it is zero only for a freshly inserted row.
    """
    result = await conn.execute(
        text(
            """
            INSERT INTO app_user (subject, email, display_name, last_seen_at)
            VALUES (:subject, :email, :display_name, now())
            ON CONFLICT (subject) DO UPDATE SET
                email = EXCLUDED.email,
                display_name = EXCLUDED.display_name,
                last_seen_at = now()
            RETURNING id, (xmax = 0) AS inserted
            """
        ),
        {
            "subject": claims.subject,
            "email": claims.email,
            "display_name": claims.display_name,
        },
    )
    row = result.one()
    return row.id, bool(row.inserted)


async def _reconcile_teams(
    conn: AsyncConnection, user_id: UUID, groups: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Make `team_member` match the directory groups, and report the delta.

    Only groups mapped to a team by `team.idp_group_id` participate. A user is
    typically in dozens of directory groups that mean nothing here, so an
    unmapped group is ignored rather than auto-creating a team — otherwise the
    team list becomes a mirror of the whole directory and `visibility='team'`
    stops meaning anything.

    Membership is replaced, not merged: removal upstream must take effect.
    """
    desired = await conn.execute(
        text("SELECT id, slug FROM team WHERE idp_group_id = ANY(:groups)"),
        {"groups": list(groups)},
    )
    desired_rows = {row.id: row.slug for row in desired}

    current = await conn.execute(
        text(
            """
            SELECT t.id, t.slug FROM team_member m
            JOIN team t ON t.id = m.team_id
            WHERE m.user_id = :user_id
            """
        ),
        {"user_id": user_id},
    )
    current_rows = {row.id: row.slug for row in current}

    to_add = sorted(desired_rows.keys() - current_rows.keys(), key=lambda k: desired_rows[k])
    to_remove = sorted(current_rows.keys() - desired_rows.keys(), key=lambda k: current_rows[k])

    if to_add:
        await conn.execute(
            text(
                "INSERT INTO team_member (team_id, user_id) "
                "SELECT unnest(CAST(:team_ids AS uuid[])), :user_id "
                "ON CONFLICT DO NOTHING"
            ),
            {"team_ids": to_add, "user_id": user_id},
        )
    if to_remove:
        await conn.execute(
            text(
                "DELETE FROM team_member WHERE user_id = :user_id "
                "AND team_id = ANY(CAST(:team_ids AS uuid[]))"
            ),
            {"team_ids": to_remove, "user_id": user_id},
        )

    return (
        tuple(desired_rows[t] for t in to_add),
        tuple(current_rows[t] for t in to_remove),
    )


#: How long a user row is trusted before the directory is reconciled again.
#: Reconciling on every request would write `last_seen_at` on every call for
#: no benefit; never reconciling would let `team_member` drift indefinitely.
#: Authorization does not wait for this — `team_ids` come from the token's own
#: group claim, so they are always as fresh as the token itself.
RECONCILE_AFTER = timedelta(minutes=5)


async def resolve_principal(
    conn: AsyncConnection, claims: Claims, channel: Channel
) -> Principal:
    """Turn verified claims into the `Principal` every query runs as.

    Team membership is derived from the **token's** group claim rather than
    from the `team_member` table. That is deliberate: `team_member` is a cache
    of the directory (`03-auth-security.md` §2), and reading the claim makes
    authorization exactly as fresh as the token — 30 minutes at most (`03`
    §4.4) — instead of as fresh as the last reconcile. The table is still
    reconciled, so anything that needs to ask "who is on this team" offline
    has an answer.

    A group with no matching `team.idp_group_id` contributes nothing. Users
    belong to dozens of directory groups that mean nothing here.
    """
    result = await conn.execute(
        text("SELECT id, is_active, last_seen_at FROM app_user WHERE subject = :subject"),
        {"subject": claims.subject},
    )
    row = result.one_or_none()

    if row is None or row.last_seen_at is None or _is_stale(row.last_seen_at):
        user_id = (await sync_user_from_claims(conn, claims)).user_id
        is_active = True if row is None else bool(row.is_active)
    else:
        user_id = row.id
        is_active = bool(row.is_active)

    if not is_active:
        # Deactivation is the local kill switch, independent of the directory:
        # it takes effect on the next request rather than at token expiry.
        raise AuthenticationFailed(
            f"The account for {claims.email} is deactivated in WebMap. The "
            f"directory still authenticates it, so this is a WebMap-side "
            f"block — ask an administrator to reactivate it."
        )

    return Principal(
        user_id=user_id,
        team_ids=await teams_for_groups(conn, claims.groups),
        channel=channel,
    )


def _is_stale(last_seen_at: datetime) -> bool:
    reference = last_seen_at
    if reference.tzinfo is None:  # pragma: no cover - column is TIMESTAMPTZ
        reference = reference.replace(tzinfo=UTC)
    return datetime.now(UTC) - reference > RECONCILE_AFTER


async def teams_for_groups(conn: AsyncConnection, groups: tuple[str, ...]) -> frozenset[UUID]:
    """Map directory group identifiers to WebMap team ids."""
    if not groups:
        return frozenset()
    result = await conn.execute(
        text("SELECT id FROM team WHERE idp_group_id = ANY(:groups)"),
        {"groups": list(groups)},
    )
    return frozenset(row.id for row in result)


async def team_ids_for(conn: AsyncConnection, user_id: UUID) -> frozenset[UUID]:
    result = await conn.execute(
        text("SELECT team_id FROM team_member WHERE user_id = :user_id"),
        {"user_id": user_id},
    )
    return frozenset(row.team_id for row in result)


async def display_name_for(conn: AsyncConnection, user_id: UUID) -> str:
    """Used in permission messages, which name the owner so the conversation
    can continue (`03-auth-security.md` §3.2)."""
    result = await conn.execute(
        text("SELECT display_name FROM app_user WHERE id = :id"), {"id": user_id}
    )
    row = result.one_or_none()
    return str(row.display_name) if row else "the owner"
