"""The database-backed grant store, and grant management.

`webmap_core.permissions` holds the *rule* as a pure function so it can be
tested exhaustively without a database (`CLAUDE.md` §6.1). This module is the
loader that feeds it, plus the owner-only operations from
`03-auth-security.md` §3.3.
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from webmap_core.exceptions import NotFound
from webmap_core.permissions import (
    Grant,
    GrantRole,
    Ownable,
    Principal,
    require_owner,
)
from webmap_core.services.directory import display_name_for

#: Object types that may carry grants. Checked rather than interpolated —
#: `access_grant.object_type` is TEXT, and a typo would create grants that
#: silently never match, which reads as "sharing does not work".
GRANTABLE = frozenset(
    {
        "project",
        "dataset",
        "style_template",
        "palette",
        "map_session",
        "render",
        # `adr/0010`. Their RLS policies (migration 0004) already consult
        # `access_grant` with these object types, so leaving them out here
        # would make the policy reachable and the grant unwritable — sharing a
        # layer would fail with "not a grantable object type" while the
        # database was ready for it.
        "layer",
        "basemap",
    }
)


def _check_object_type(object_type: str) -> str:
    if object_type not in GRANTABLE:
        raise ValueError(
            f"'{object_type}' is not a grantable object type. Grants apply to "
            f"{', '.join(sorted(GRANTABLE))}. A mismatch here creates grant "
            f"rows that never match a policy, which looks like sharing "
            f"silently failing."
        )
    return object_type


class DbGrantStore:
    """Loads grants and owner names for `permissions.require`.

    Constructed per request against the principal's own connection, so the
    grant lookup runs inside the same RLS context as everything else.
    """

    def __init__(self, conn: AsyncConnection, object_type: str) -> None:
        self._conn = conn
        self._object_type = _check_object_type(object_type)

    async def grants_for(self, obj: Ownable, principal: Principal) -> Sequence[Grant]:
        """Only the grants that could apply to this principal.

        Filtered in SQL rather than loading every grant on the object and
        filtering in Python: an object shared with a large team has many
        rows, and none of the others can change the answer.
        """
        result = await self._conn.execute(
            text(
                """
                SELECT role, grantee_user_id, grantee_team_id
                FROM access_grant
                WHERE object_type = :object_type
                  AND object_id = :object_id
                  AND (grantee_user_id = :user_id
                       OR grantee_team_id = ANY(CAST(:team_ids AS uuid[])))
                """
            ),
            {
                "object_type": self._object_type,
                "object_id": obj.id,
                "user_id": principal.user_id,
                "team_ids": list(principal.team_ids),
            },
        )
        return [
            Grant(
                role=GrantRole(row.role),
                grantee_user_id=row.grantee_user_id,
                grantee_team_id=row.grantee_team_id,
            )
            for row in result
        ]

    async def owner_display_name(self, obj: Ownable) -> str:
        return await display_name_for(self._conn, obj.owner_user_id)


async def create_grant(
    conn: AsyncConnection,
    principal: Principal,
    obj: Ownable,
    object_type: str,
    role: GrantRole,
    *,
    grantee_user_id: UUID | None = None,
    grantee_team_id: UUID | None = None,
) -> UUID:
    """Share an object. Owner-only (`03-auth-security.md` §3.3).

    Only the owner may grant, so a user cannot escalate their own access by
    granting themselves editor — the check is `require_owner`, not
    `require(EDITOR)`.
    """
    _check_object_type(object_type)
    # Validates the exactly-one-grantee rule before touching the database, so
    # the error is the domain's message rather than a CHECK violation.
    Grant(role=role, grantee_user_id=grantee_user_id, grantee_team_id=grantee_team_id)

    await require_owner(DbGrantStore(conn, object_type), principal, obj)

    result = await conn.execute(
        text(
            """
            INSERT INTO access_grant (
                object_type, object_id, grantee_user_id, grantee_team_id,
                role, granted_by)
            VALUES (:object_type, :object_id, :user, :team, :role, :by)
            ON CONFLICT (object_type, object_id, grantee_user_id)
                WHERE grantee_user_id IS NOT NULL
                DO UPDATE SET role = EXCLUDED.role, granted_by = EXCLUDED.granted_by
            RETURNING id
            """
            if grantee_user_id is not None
            else """
            INSERT INTO access_grant (
                object_type, object_id, grantee_user_id, grantee_team_id,
                role, granted_by)
            VALUES (:object_type, :object_id, :user, :team, :role, :by)
            ON CONFLICT (object_type, object_id, grantee_team_id)
                WHERE grantee_team_id IS NOT NULL
                DO UPDATE SET role = EXCLUDED.role, granted_by = EXCLUDED.granted_by
            RETURNING id
            """
        ),
        {
            "object_type": object_type,
            "object_id": obj.id,
            "user": grantee_user_id,
            "team": grantee_team_id,
            "role": role.value,
            "by": principal.user_id,
        },
    )
    return UUID(str(result.scalar_one()))


async def revoke_grant(
    conn: AsyncConnection,
    principal: Principal,
    obj: Ownable,
    object_type: str,
    grant_id: UUID,
) -> None:
    """Remove a grant. Owner-only, and scoped to this object.

    `object_id` is in the WHERE clause as well as `grant_id`: without it, an
    owner of one object could revoke a grant on another by id.
    """
    _check_object_type(object_type)
    await require_owner(DbGrantStore(conn, object_type), principal, obj)

    result = await conn.execute(
        text(
            "DELETE FROM access_grant WHERE id = :id "
            "AND object_type = :object_type AND object_id = :object_id"
        ),
        {"id": grant_id, "object_type": object_type, "object_id": obj.id},
    )
    if result.rowcount == 0:
        raise NotFound(
            f"No grant {grant_id} on this object. It may already have been "
            f"revoked, or it belongs to a different object."
        )


async def list_grants(
    conn: AsyncConnection, object_type: str, object_id: UUID
) -> list[dict[str, object]]:
    """Every grant on an object, for the sharing UI.

    The caller must have already established that the principal can see the
    object — this reads `access_grant`, which carries no RLS policy of its
    own because the read policies on ownable tables query it.
    """
    _check_object_type(object_type)
    result = await conn.execute(
        text(
            """
            SELECT g.id, g.role, g.created_at,
                   u.display_name AS user_name, t.slug AS team_slug
            FROM access_grant g
            LEFT JOIN app_user u ON u.id = g.grantee_user_id
            LEFT JOIN team t ON t.id = g.grantee_team_id
            WHERE g.object_type = :object_type AND g.object_id = :object_id
            ORDER BY g.created_at
            """
        ),
        {"object_type": object_type, "object_id": object_id},
    )
    return [
        {
            "id": row.id,
            "role": row.role,
            "grantee": row.user_name or f"team:{row.team_slug}",
            "created_at": row.created_at,
        }
        for row in result
    ]
