"""Loading ownable rows for permission checks.

Two layers, both required (`03-auth-security.md` §3.1), and they divide the
error space between them:

- **RLS hides a row entirely** when the principal has no access at all. The
  service cannot then say who owns it, and should not: telling someone that
  an object exists and naming its owner is itself a disclosure. That case is
  `NotFound`.
- **RLS shows a row** the principal can view but not edit. Here the
  application check produces the message that names the owner and the missing
  level, so the conversation can continue (`03` §3.2).

So a 404 means "invisible to you", and a 403 always carries a useful next
step. That asymmetry is deliberate rather than an accident of ordering.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from webmap_core.exceptions import NotFound, PermissionDenied
from webmap_core.permissions import Permission, Principal, Visibility, require
from webmap_core.services.grants import GRANTABLE, DbGrantStore


@dataclass(frozen=True)
class OwnableRow:
    """The ownership block every ownable table repeats (`02` §3.3).

    Enough to resolve a permission and write a good message; deliberately not
    the whole row, so a permission check cannot come to depend on a column
    that only one table has.
    """

    id: UUID
    owner_user_id: UUID
    owner_team_id: UUID | None
    visibility: str
    name: str
    deleted_at: object | None = None


async def load_ownable(
    conn: AsyncConnection, table: str, object_id: UUID, *, include_deleted: bool = False
) -> OwnableRow:
    """Load an ownable row through RLS, or raise NotFound.

    `table` is interpolated, so it is checked against the known set first —
    every caller passes a literal today, and this is what keeps that true.
    """
    if table not in GRANTABLE:
        raise ValueError(
            f"'{table}' is not an ownable table. Ownable tables are "
            f"{', '.join(sorted(GRANTABLE))} (02-data-model.md §3.3)."
        )

    name_column = "name" if table != "render" else "coalesce(caption, id::text) AS name"
    deleted_clause = "" if include_deleted else " AND deleted_at IS NULL"
    result = await conn.execute(
        text(
            f"SELECT id, owner_user_id, owner_team_id, visibility, {name_column}, "
            f"deleted_at FROM {table} WHERE id = :id{deleted_clause}"
        ),
        {"id": object_id},
    )
    row = result.one_or_none()
    if row is None:
        raise NotFound(
            f"No {table} with id {object_id} that you can access. It may not "
            f"exist, it may have been deleted, or it may belong to someone "
            f"who has not shared it with you. Search by name to find what you "
            f"do have access to."
        )
    return OwnableRow(
        id=row.id,
        owner_user_id=row.owner_user_id,
        owner_team_id=row.owner_team_id,
        visibility=row.visibility,
        name=row.name,
        deleted_at=row.deleted_at,
    )


async def load_and_require(
    conn: AsyncConnection,
    table: str,
    object_id: UUID,
    principal: Principal,
    level: Permission,
    *,
    include_deleted: bool = False,
) -> OwnableRow:
    """Load an ownable row and assert a permission level on it.

    The pairing is the point: every service function that touches an object
    goes through here, so "did anyone forget the check" is answerable by
    grepping for direct `load_ownable` calls.
    """
    obj = await load_ownable(conn, table, object_id, include_deleted=include_deleted)
    await require(DbGrantStore(conn, table), principal, obj, level)
    return obj


def resolve_owner_team(
    principal: Principal, visibility: Visibility, owner_team_id: UUID | None
) -> UUID | None:
    """The team an object is owned by, given what the caller asked for.

    **`visibility='team'` with no `owner_team_id` is a contradiction**, and one
    that fails silently rather than loudly: the RLS clause is
    `visibility = 'team' AND owner_team_id = ANY(...)`, so a NULL team matches
    nobody and the object is private in all but name. Someone sharing work with
    their team would see it succeed and their team would see nothing.

    So the combination is resolved here rather than stored. A caller on exactly
    one team meant that team — which is the case for every Claude-created
    object, since the MCP tools have no concept of teams to pass one from. A
    caller on several is asked which, because guessing would widen access
    beyond what they can see themselves.

    Enforced underneath by a CHECK constraint (migration 0003), so a path that
    forgets to call this fails at the write instead of quietly hiding data.
    """
    if visibility is not Visibility.TEAM or owner_team_id is not None:
        return owner_team_id

    teams = sorted(principal.team_ids)
    if len(teams) == 1:
        return teams[0]
    if not teams:
        raise PermissionDenied(
            "You are not a member of any team, so 'team' visibility would share "
            "this with nobody. Use 'private' to keep it to yourself or 'org' to "
            "share it organisation-wide, or ask an administrator to add you to a "
            "team."
        )
    raise PermissionDenied(
        f"You are on {len(teams)} teams, so 'team' visibility is ambiguous — say "
        f"which one owns this by passing owner_team_id. Your teams: "
        f"{', '.join(str(team) for team in teams)}."
    )
