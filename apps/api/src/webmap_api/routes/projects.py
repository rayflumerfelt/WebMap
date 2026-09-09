"""Project endpoints. `02-data-model.md` §3.4."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Query, status

from webmap_api.dependencies import CurrentPrincipal, ScopedConn
from webmap_core.models import Bbox, LengthUnit
from webmap_core.permissions import Visibility
from webmap_core.services import projects as service

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


@router.get("")
async def list_projects(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    items = await service.list_projects(conn, principal, limit=limit)
    return {"count": len(items), "items": items}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_project(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    slug: Annotated[str, Body(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")],
    name: Annotated[str, Body(min_length=1)],
    analysis_srid: Annotated[
        int,
        Body(
            description="EPSG code. Must be projected — never geographic, never 3857 by default."
        ),
    ],
    description: Annotated[str | None, Body()] = None,
    vertical_unit: Annotated[LengthUnit | None, Body()] = None,
    depth_positive_down: Annotated[bool, Body()] = True,
    default_extent: Annotated[Bbox | None, Body()] = None,
    owner_team_id: Annotated[UUID | None, Body()] = None,
    visibility: Annotated[Visibility, Body()] = Visibility.TEAM,
) -> dict[str, str]:
    """Create a project.

    `analysis_srid` is required and validated. A geographic CRS is refused
    with the message from `webmap_geo.crs`, which names the fix — a UTM or
    State Plane zone appropriate to the data extent.
    """
    project_id = await service.create_project(
        conn,
        principal,
        slug=slug,
        name=name,
        analysis_srid=analysis_srid,
        vertical_unit=vertical_unit,
        depth_positive_down=depth_positive_down,
        description=description,
        default_extent=default_extent,
        owner_team_id=owner_team_id,
        visibility=visibility,
    )
    return {"project_id": str(project_id)}


@router.get("/{project_id}")
async def get_project(
    principal: CurrentPrincipal, conn: ScopedConn, project_id: UUID
) -> dict[str, Any]:
    return await service.get_project(conn, principal, project_id)


@router.patch("/{project_id}")
async def update_project(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    project_id: UUID,
    name: Annotated[str | None, Body()] = None,
    description: Annotated[str | None, Body()] = None,
    default_extent: Annotated[Bbox | None, Body()] = None,
    visibility: Annotated[Visibility | None, Body()] = None,
) -> dict[str, str]:
    """Update a project.

    `analysis_srid` is absent by design: changing it would reinterpret every
    grid and lineage record already produced under the project.
    """
    await service.update_project(
        conn,
        principal,
        project_id,
        name=name,
        description=description,
        default_extent=default_extent,
        visibility=visibility,
    )
    return {"status": "updated"}


@router.delete("/{project_id}")
async def delete_project(
    principal: CurrentPrincipal, conn: ScopedConn, project_id: UUID
) -> dict[str, Any]:
    retained = await service.soft_delete_project(conn, principal, project_id)
    return {
        "status": "deleted",
        "datasets_retained": retained,
        "detail": (
            f"Soft-deleted, recoverable for 30 days. {retained} dataset(s) kept "
            f"and no longer belong to a project — deleting a container does not "
            f"delete the work inside it."
        ),
    }


__all__ = ["router"]
