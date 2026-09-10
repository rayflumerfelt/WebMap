"""Role capabilities. `adr/0010` §2.

**Two axes, deliberately not merged.** Object access — who may read or edit
*this* layer — is `permissions.require` over owner, grant and visibility, and
nothing here touches it. Capabilities are what a role lets you *do* at all:
publish organisation-wide, manage a team's membership, set defaults, transfer a
departed colleague's work.

They are columns rather than rows in `access_grant` because they are not about
an object. `is_global_admin` on `app_user`, `role` on `team_member` — per team,
because "administrator of Permian, member of Delaware" is ordinary and one
four-value enum on the user cannot say it.

Read from the database on each check rather than carried on `Principal`. A
`Principal` is built at authentication and lives for the request; an
administrator demoted mid-session would keep their capability until the token
expired, which is exactly the window that matters. The cost is one indexed
lookup on the small number of operations that need one.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from webmap_core.exceptions import PermissionDenied
from webmap_core.permissions import Principal, Visibility

#: `team_member.role` — `team_role_t` from migration 0004. "Team Member" is
#: not a stored role: the requirement distinguishes it from "User" only by
#: belonging to a team, which `team_member` already records (`adr/0010` §2).
TEAM_ROLES = ("member", "admin")


async def is_global_admin(conn: AsyncConnection, principal: Principal) -> bool:
    row = await conn.execute(
        text("SELECT is_global_admin FROM app_user WHERE id = :id"),
        {"id": principal.user_id},
    )
    return bool(row.scalar_one_or_none())


async def require_global_admin(
    conn: AsyncConnection, principal: Principal, *, action: str
) -> None:
    """Assert the global-administrator capability, naming what it was for.

    `action` is a phrase that completes "requires a global administrator to
    …" — the message is the whole reason this is not a bare boolean, because
    the person reading it needs to know who to ask and what to ask for.
    """
    if not await is_global_admin(conn, principal):
        raise PermissionDenied(
            f"{action} requires a global administrator. Ask one to do it, or to "
            f"grant you the capability."
        )


async def team_role(conn: AsyncConnection, principal: Principal, team_id: UUID) -> str | None:
    """This principal's role in `team_id`, or None if not a member."""
    row = await conn.execute(
        text("SELECT role FROM team_member WHERE team_id = :team AND user_id = :user"),
        {"team": team_id, "user": principal.user_id},
    )
    value = row.scalar_one_or_none()
    return None if value is None else str(value)


async def require_team_admin(
    conn: AsyncConnection, principal: Principal, team_id: UUID, *, action: str
) -> None:
    """Team administrator, or global. `adr/0010` §2.

    Global administrators pass without being members, because the alternative
    is that nobody can fix a team whose only administrator has left.
    """
    if await team_role(conn, principal, team_id) == "admin":
        return
    if await is_global_admin(conn, principal):
        return

    name = (
        await conn.execute(text("SELECT name FROM team WHERE id = :id"), {"id": team_id})
    ).scalar_one_or_none()
    label = f"'{name}'" if name else str(team_id)
    raise PermissionDenied(
        f"{action} requires an administrator of team {label}. Ask one of its "
        f"administrators, or a global administrator."
    )


async def require_publish_scope(
    conn: AsyncConnection,
    principal: Principal,
    visibility: Visibility,
    owner_team_id: UUID | None,
) -> None:
    """May this principal publish at this visibility? `adr/0010` §2.

    - `private` — always.
    - `team` — a member of that team. Enforced here as well as by
      `resolve_owner_team`, which resolves the *which* and not the *may*.
    - `org` — **global administrator only.** This is the behaviour change
      `adr/0010` §2 flags: organisation-wide publication used to be open to
      anyone, and an org-visible object is readable by every account in the
      deployment with no further step.

    Called at creation and at any change of visibility, not at read. A row that
    is already `org` stays readable — retro-hiding published work on a
    permission change would break maps that reference it, and the ADR's remedy
    for a wrongly published object is to change its visibility deliberately.
    """
    if visibility is Visibility.PRIVATE:
        return

    if visibility is Visibility.TEAM:
        if owner_team_id is None or owner_team_id in principal.team_ids:
            return
        raise PermissionDenied(
            "You are not a member of that team, so publishing to it would share "
            "work with people you cannot see yourself. Ask a team administrator "
            "to add you, or publish privately and grant access directly."
        )

    if not await is_global_admin(conn, principal):
        raise PermissionDenied(
            "Publishing organisation-wide requires a global administrator — an "
            "org-visible object is readable by every account in the deployment. "
            "Publish to a team instead, or ask an administrator to publish it."
        )


__all__ = [
    "TEAM_ROLES",
    "is_global_admin",
    "require_global_admin",
    "require_publish_scope",
    "require_team_admin",
    "team_role",
]
