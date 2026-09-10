"""The three preference tiers, and default-basemap resolution. `adr/0010` §4.

```
user.defaults[presentation] → user.defaults[*]
  → team.defaults[presentation] → team.defaults[*]
    → global.defaults[presentation] → global.defaults[*]
      → no basemap
```

**A tier is exhausted before resolution descends**, which is the part that is
easy to get backwards. Matching the presentation across all three tiers first
would let a team's contour default beat a user's own general default — and a
user who set a general default meant it to beat their team's.

Ties between a user's teams resolve **alphabetically by `team.slug`**, and the
caller is told which team a default came from. Most-recently-updated was the
alternative and is worse: a colleague editing their team's preferences would
silently change what opens on someone else's screen, with no visible cause.

One more rule that is not in the ADR and follows from it: a default naming a
basemap the caller **cannot currently resolve** — deleted, or never shared with
them — falls through to the next candidate rather than resolving to nothing.
The tiers exist so that a default always lands somewhere; stopping at a dangling
one would defeat that, and the user whose default broke is rarely the one who
broke it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from webmap_core.exceptions import NotFound
from webmap_core.logging import get_logger
from webmap_core.permissions import Principal
from webmap_core.services.audit import AuditAction, record
from webmap_core.services.capabilities import require_global_admin, require_team_admin
from webmap_core.services.layers import PRESENTATIONS

log = get_logger(__name__)

#: The key in `default_basemaps` meaning "whatever the presentation".
ANY_PRESENTATION = "*"

#: Which tier a resolved default came from, for the UI to name.
TIERS = ("user", "team", "global")


@dataclass(frozen=True)
class ResolvedDefault:
    """A default basemap and where it came from.

    The provenance is not decoration: `adr/0010` §4 requires the UI to name the
    team a default came from, because "why is this map opening with someone
    else's backdrop" is otherwise unanswerable from the screen.
    """

    basemap_id: UUID
    tier: str
    presentation_key: str
    team_id: UUID | None = None
    team_slug: str | None = None


def _as_mapping(value: Any) -> dict[str, str]:
    """`default_basemaps` as a plain dict of str → str.

    asyncpg hands JSONB back already decoded; other drivers hand back text.
    Both happen in this codebase depending on how a connection was made, and a
    silent `str` here would make every lookup miss rather than fail.
    """
    if value is None:
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError(
            f"default_basemaps must be a JSON object keyed on presentation; "
            f"found {type(value).__name__}."
        )
    return {str(k): str(v) for k, v in value.items() if v is not None}


def _candidates(defaults: dict[str, str], presentation: str | None) -> list[str]:
    """The keys to try in one tier, most specific first."""
    keys = [] if presentation is None else [presentation]
    keys.append(ANY_PRESENTATION)
    return [key for key in keys if key in defaults]


async def _visible(conn: AsyncConnection, basemap_id: UUID) -> bool:
    """Can the current principal resolve this basemap at all?

    RLS answers it: the connection is already principal-scoped, so a basemap
    invisible to them simply is not there. No application check needed, and
    none wanted — a second one here could disagree with the policy.
    """
    row = await conn.execute(
        text("SELECT 1 FROM basemap WHERE id = :id AND deleted_at IS NULL"),
        {"id": basemap_id},
    )
    return row.scalar_one_or_none() is not None


async def resolve_default_basemap(
    conn: AsyncConnection, principal: Principal, *, presentation: str | None = None
) -> ResolvedDefault | None:
    """The basemap a new map of this presentation should open with.

    Returns `None` when no tier has one, which is a legitimate state — a fresh
    deployment has no global default, and a map with no basemap is a map on a
    blank background, not an error.
    """
    if presentation is not None and presentation not in PRESENTATIONS:
        raise ValueError(
            f"'{presentation}' is not a presentation. Defaults are keyed on how "
            f"a layer is drawn: {', '.join(PRESENTATIONS)}, or omit it for the "
            f"general default."
        )

    user_defaults = _as_mapping(
        (
            await conn.execute(
                text("SELECT default_basemaps FROM user_preferences WHERE user_id = :id"),
                {"id": principal.user_id},
            )
        ).scalar_one_or_none()
    )
    for key in _candidates(user_defaults, presentation):
        basemap_id = UUID(user_defaults[key])
        if await _visible(conn, basemap_id):
            return ResolvedDefault(basemap_id=basemap_id, tier="user", presentation_key=key)

    # Alphabetically by slug, so the answer is the same for everyone on the
    # same teams and does not move when a colleague edits a preference.
    teams = (
        await conn.execute(
            text(
                """
                SELECT t.id, t.slug, p.default_basemaps
                FROM team t
                JOIN team_member m ON m.team_id = t.id AND m.user_id = :user
                LEFT JOIN team_preferences p ON p.team_id = t.id
                ORDER BY t.slug
                """
            ),
            {"user": principal.user_id},
        )
    ).all()
    for team in teams:
        team_defaults = _as_mapping(team.default_basemaps)
        for key in _candidates(team_defaults, presentation):
            basemap_id = UUID(team_defaults[key])
            if await _visible(conn, basemap_id):
                return ResolvedDefault(
                    basemap_id=basemap_id,
                    tier="team",
                    presentation_key=key,
                    team_id=team.id,
                    team_slug=team.slug,
                )

    global_defaults = _as_mapping(
        (
            await conn.execute(text("SELECT default_basemaps FROM global_preferences"))
        ).scalar_one_or_none()
    )
    for key in _candidates(global_defaults, presentation):
        basemap_id = UUID(global_defaults[key])
        if await _visible(conn, basemap_id):
            return ResolvedDefault(basemap_id=basemap_id, tier="global", presentation_key=key)

    return None


# --- writing the tiers ----------------------------------------------------------


def _validate_key(presentation: str | None) -> str:
    if presentation is None:
        return ANY_PRESENTATION
    if presentation not in PRESENTATIONS and presentation != ANY_PRESENTATION:
        raise ValueError(
            f"'{presentation}' is not a presentation. Defaults are keyed on "
            f"{', '.join(PRESENTATIONS)}, or '{ANY_PRESENTATION}' for the "
            f"general default."
        )
    return presentation


async def _require_settable(conn: AsyncConnection, basemap_id: UUID | None) -> None:
    """A default must name a basemap the setter can see.

    Otherwise an administrator could set a global default nobody but them can
    open, and every user would fall through to no basemap while the preference
    screen showed one set. RLS does the seeing; this turns the absence into a
    message.
    """
    if basemap_id is None:
        return
    if not await _visible(conn, basemap_id):
        raise NotFound(
            f"No basemap {basemap_id} that you can access, so it cannot be made "
            f"a default. Check it has not been deleted, and that it is shared "
            f"with the people the default is for."
        )


async def set_user_default_basemap(
    conn: AsyncConnection,
    principal: Principal,
    *,
    basemap_id: UUID | None,
    presentation: str | None = None,
) -> None:
    """Set — or clear, with `basemap_id=None` — one of the caller's defaults."""
    key = _validate_key(presentation)
    await _require_settable(conn, basemap_id)

    await conn.execute(
        text(
            """
            INSERT INTO user_preferences (user_id, default_basemaps)
            VALUES (:user, CAST(:patch AS JSONB))
            ON CONFLICT (user_id) DO UPDATE SET
                default_basemaps = CASE
                    WHEN :clear THEN user_preferences.default_basemaps - :key
                    ELSE user_preferences.default_basemaps || CAST(:patch AS JSONB)
                END,
                updated_at = now()
            """
        ),
        {
            "user": principal.user_id,
            "key": key,
            "clear": basemap_id is None,
            "patch": json.dumps({} if basemap_id is None else {key: str(basemap_id)}),
        },
    )
    await record(
        conn,
        action=AuditAction.PREFERENCES_UPDATED,
        principal=principal,
        object_type="user_preferences",
        object_id=principal.user_id,
        detail={"tier": "user", "key": key, "basemap_id": str(basemap_id or "")},
    )


async def set_team_default_basemap(
    conn: AsyncConnection,
    principal: Principal,
    team_id: UUID,
    *,
    basemap_id: UUID | None,
    presentation: str | None = None,
) -> None:
    """Team administrator, or global. `adr/0010` §2."""
    key = _validate_key(presentation)
    await require_team_admin(conn, principal, team_id, action="Setting a team default")
    await _require_settable(conn, basemap_id)

    await conn.execute(
        text(
            """
            INSERT INTO team_preferences (team_id, default_basemaps)
            VALUES (:team, CAST(:patch AS JSONB))
            ON CONFLICT (team_id) DO UPDATE SET
                default_basemaps = CASE
                    WHEN :clear THEN team_preferences.default_basemaps - :key
                    ELSE team_preferences.default_basemaps || CAST(:patch AS JSONB)
                END,
                updated_at = now()
            """
        ),
        {
            "team": team_id,
            "key": key,
            "clear": basemap_id is None,
            "patch": json.dumps({} if basemap_id is None else {key: str(basemap_id)}),
        },
    )
    await record(
        conn,
        action=AuditAction.PREFERENCES_UPDATED,
        principal=principal,
        object_type="team_preferences",
        object_id=team_id,
        detail={"tier": "team", "key": key, "basemap_id": str(basemap_id or "")},
    )


async def set_global_default_basemap(
    conn: AsyncConnection,
    principal: Principal,
    *,
    basemap_id: UUID | None,
    presentation: str | None = None,
) -> None:
    """Global administrator only.

    The `id` column is `BOOLEAN PRIMARY KEY CHECK (id)`, so `VALUES (TRUE, …)`
    with `ON CONFLICT (id)` is the whole upsert — there is exactly one row and
    the database is what guarantees it.
    """
    key = _validate_key(presentation)
    await require_global_admin(conn, principal, action="Setting a deployment-wide default")
    await _require_settable(conn, basemap_id)

    await conn.execute(
        text(
            """
            INSERT INTO global_preferences (id, default_basemaps)
            VALUES (TRUE, CAST(:patch AS JSONB))
            ON CONFLICT (id) DO UPDATE SET
                default_basemaps = CASE
                    WHEN :clear THEN global_preferences.default_basemaps - :key
                    ELSE global_preferences.default_basemaps || CAST(:patch AS JSONB)
                END,
                updated_at = now()
            """
        ),
        {
            "key": key,
            "clear": basemap_id is None,
            "patch": json.dumps({} if basemap_id is None else {key: str(basemap_id)}),
        },
    )
    await record(
        conn,
        action=AuditAction.PREFERENCES_UPDATED,
        principal=principal,
        object_type="global_preferences",
        detail={"tier": "global", "key": key, "basemap_id": str(basemap_id or "")},
    )


async def get_preferences(conn: AsyncConnection, principal: Principal) -> dict[str, Any]:
    """Every tier that applies to this caller, unresolved.

    The preferences screen needs the tiers *separately* — a user should see
    that their team sets a contour default even while their own general default
    overrides it, because otherwise the override looks like the setting not
    working.
    """
    user = _as_mapping(
        (
            await conn.execute(
                text("SELECT default_basemaps FROM user_preferences WHERE user_id = :id"),
                {"id": principal.user_id},
            )
        ).scalar_one_or_none()
    )
    teams = [
        {
            "team_id": row.id,
            "slug": row.slug,
            "default_basemaps": _as_mapping(row.default_basemaps),
        }
        for row in (
            await conn.execute(
                text(
                    """
                    SELECT t.id, t.slug, p.default_basemaps
                    FROM team t
                    JOIN team_member m ON m.team_id = t.id AND m.user_id = :user
                    LEFT JOIN team_preferences p ON p.team_id = t.id
                    ORDER BY t.slug
                    """
                ),
                {"user": principal.user_id},
            )
        ).all()
    ]
    global_defaults = _as_mapping(
        (
            await conn.execute(text("SELECT default_basemaps FROM global_preferences"))
        ).scalar_one_or_none()
    )
    return {"user": user, "teams": teams, "global": global_defaults}


__all__ = [
    "ANY_PRESENTATION",
    "TIERS",
    "ResolvedDefault",
    "get_preferences",
    "resolve_default_basemap",
    "set_global_default_basemap",
    "set_team_default_basemap",
    "set_user_default_basemap",
]
