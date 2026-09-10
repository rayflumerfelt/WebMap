"""Preference endpoints and default-basemap resolution. `adr/0010` §4."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Query, status

from webmap_api.dependencies import CurrentPrincipal, ScopedConn
from webmap_core.services import preferences as service

router = APIRouter(prefix="/api/v1/preferences", tags=["preferences"])


@router.get("")
async def get_preferences(principal: CurrentPrincipal, conn: ScopedConn) -> dict[str, Any]:
    """Every tier that applies to the caller, **unresolved**.

    The preferences screen needs the tiers separately: a user should be able to
    see that their team sets a contour default even while their own general
    default overrides it, or the override looks like the setting not working.
    """
    return await service.get_preferences(conn, principal)


@router.get("/default-basemap")
async def resolve_default_basemap(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    presentation: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """The basemap a new map should open with, and where it came from.

    `basemap_id: null` is a legitimate answer — a deployment with no global
    default opens maps on a blank background, which is not an error.

    `tier` and `team_slug` are returned because `adr/0010` §4 requires the UI to
    name the team a default came from. "Why is my map opening with someone
    else's backdrop" is otherwise unanswerable from the screen.
    """
    resolved = await service.resolve_default_basemap(conn, principal, presentation=presentation)
    if resolved is None:
        return {"basemap_id": None, "tier": None}
    return {
        "basemap_id": str(resolved.basemap_id),
        "tier": resolved.tier,
        "presentation_key": resolved.presentation_key,
        "team_id": None if resolved.team_id is None else str(resolved.team_id),
        "team_slug": resolved.team_slug,
    }


@router.put("/default-basemap", status_code=status.HTTP_204_NO_CONTENT)
async def set_user_default_basemap(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    basemap_id: Annotated[UUID | None, Body()] = None,
    presentation: Annotated[str | None, Body()] = None,
) -> None:
    """Set the caller's own default; `basemap_id: null` clears it."""
    await service.set_user_default_basemap(
        conn, principal, basemap_id=basemap_id, presentation=presentation
    )


@router.put("/teams/{team_id}/default-basemap", status_code=status.HTTP_204_NO_CONTENT)
async def set_team_default_basemap(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    team_id: UUID,
    basemap_id: Annotated[UUID | None, Body()] = None,
    presentation: Annotated[str | None, Body()] = None,
) -> None:
    """Team administrator, or global (`adr/0010` §2)."""
    await service.set_team_default_basemap(
        conn, principal, team_id, basemap_id=basemap_id, presentation=presentation
    )


@router.put("/global/default-basemap", status_code=status.HTTP_204_NO_CONTENT)
async def set_global_default_basemap(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    basemap_id: Annotated[UUID | None, Body()] = None,
    presentation: Annotated[str | None, Body()] = None,
) -> None:
    """Global administrator only — this is what every account falls back to."""
    await service.set_global_default_basemap(
        conn, principal, basemap_id=basemap_id, presentation=presentation
    )
