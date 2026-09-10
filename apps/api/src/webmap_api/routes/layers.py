"""Layer and basemap endpoints. `adr/0010` §3, `07` §6.1.

Two routers in one module because the objects are one model: a basemap is
meaningless without layers, and the delete refusal on `/layers/{id}` is phrased
in terms of basemaps. Splitting them would put the two halves of one rule in
two files.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Query, status

from webmap_api.dependencies import CurrentPrincipal, ScopedConn
from webmap_core.permissions import Visibility
from webmap_core.services import layers as service

router = APIRouter(prefix="/api/v1/layers", tags=["layers"])
basemaps_router = APIRouter(prefix="/api/v1/basemaps", tags=["basemaps"])


# --- layers -------------------------------------------------------------------


@router.get("")
async def list_layers(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    dataset_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    items = await service.list_layers(conn, principal, dataset_id=dataset_id, limit=limit)
    return {"count": len(items), "items": items}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_layer(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    name: Annotated[str, Body(min_length=1)],
    dataset_id: Annotated[UUID, Body()],
    presentation: Annotated[
        str,
        Body(
            description=(
                "How the layer is drawn — vector, filled_grid, contour, "
                "filled_contour, hillshade, points. Not the dataset kind: a grid "
                "can be a filled surface or a set of contours."
            )
        ),
    ],
    description: Annotated[str | None, Body()] = None,
    style_template_id: Annotated[UUID | None, Body()] = None,
    symbology: Annotated[dict[str, Any] | None, Body()] = None,
    default_opacity: Annotated[float, Body(ge=0.0, le=1.0)] = 1.0,
    visibility: Annotated[Visibility, Body()] = Visibility.PRIVATE,
    owner_team_id: Annotated[UUID | None, Body()] = None,
) -> dict[str, str]:
    layer_id = await service.create_layer(
        conn,
        principal,
        name=name,
        dataset_id=dataset_id,
        presentation=presentation,
        description=description,
        style_template_id=style_template_id,
        symbology=symbology,
        default_opacity=default_opacity,
        visibility=visibility,
        owner_team_id=owner_team_id,
    )
    return {"layer_id": str(layer_id)}


@router.get("/{layer_id}")
async def get_layer(
    principal: CurrentPrincipal, conn: ScopedConn, layer_id: UUID
) -> dict[str, Any]:
    layer = await service.get_layer(conn, principal, layer_id)
    return {
        "id": str(layer.id),
        "name": layer.name,
        "description": layer.description,
        "dataset_id": str(layer.dataset_id),
        "presentation": layer.presentation,
        "style_template_id": (
            None if layer.style_template_id is None else str(layer.style_template_id)
        ),
        "symbology": layer.symbology,
        "default_opacity": layer.default_opacity,
        "visibility": layer.visibility,
    }


@router.patch("/{layer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def update_layer(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    layer_id: UUID,
    name: Annotated[str | None, Body()] = None,
    description: Annotated[str | None, Body()] = None,
    presentation: Annotated[str | None, Body()] = None,
    style_template_id: Annotated[UUID | None, Body()] = None,
    symbology: Annotated[dict[str, Any] | None, Body()] = None,
    default_opacity: Annotated[float | None, Body(ge=0.0, le=1.0)] = None,
) -> None:
    await service.update_layer(
        conn,
        principal,
        layer_id,
        name=name,
        description=description,
        presentation=presentation,
        style_template_id=style_template_id,
        symbology=symbology,
        default_opacity=default_opacity,
    )


@router.post("/{layer_id}/duplicate", status_code=status.HTTP_201_CREATED)
async def duplicate_layer(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    layer_id: UUID,
    name: Annotated[str | None, Body(embed=True)] = None,
) -> dict[str, str]:
    """Copy a visible layer into the caller's own, by reference.

    The dataset is shared, not copied — the same shapefile styled two ways is
    two layers over one dataset (`07` §6.1).
    """
    new_id = await service.duplicate_layer(conn, principal, layer_id, name=name)
    return {"layer_id": str(new_id)}


@router.get("/{layer_id}/basemaps")
async def layer_basemaps(
    principal: CurrentPrincipal, conn: ScopedConn, layer_id: UUID
) -> dict[str, Any]:
    """Which basemaps use this layer — the delete-refusal answer, in advance.

    The UI asks this before offering to delete, so the warning arrives while it
    can still change what the user does rather than as a 403 afterwards.
    """
    await service.get_layer(conn, principal, layer_id)
    names = await service.basemaps_using(conn, layer_id)
    return {"count": len(names), "names": names}


@router.delete("/{layer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_layer(principal: CurrentPrincipal, conn: ScopedConn, layer_id: UUID) -> None:
    """Soft-delete. Refused with the basemaps named if any still use it."""
    await service.delete_layer(conn, principal, layer_id)


# --- basemaps ------------------------------------------------------------------


@basemaps_router.get("")
async def list_basemaps(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    items = await service.list_basemaps(conn, principal, limit=limit)
    return {"count": len(items), "items": items}


@basemaps_router.post("", status_code=status.HTTP_201_CREATED)
async def create_basemap(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    name: Annotated[str, Body(min_length=1)],
    layer_ids: Annotated[list[UUID], Body(description="Bottom first — index is draw order.")],
    description: Annotated[str | None, Body()] = None,
    visibility: Annotated[Visibility, Body()] = Visibility.PRIVATE,
    owner_team_id: Annotated[UUID | None, Body()] = None,
) -> dict[str, str]:
    basemap_id = await service.create_basemap(
        conn,
        principal,
        name=name,
        layer_ids=layer_ids,
        description=description,
        visibility=visibility,
        owner_team_id=owner_team_id,
    )
    return {"basemap_id": str(basemap_id)}


@basemaps_router.post("/from-map", status_code=status.HTTP_201_CREATED)
async def save_map_as_basemap(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    name: Annotated[str, Body(min_length=1)],
    layer_ids: Annotated[list[UUID], Body()],
    active_layer_id: Annotated[UUID | None, Body()] = None,
    include_active: Annotated[bool, Body()] = False,
    visibility: Annotated[Visibility, Body()] = Visibility.PRIVATE,
) -> dict[str, str]:
    """`07` §6.1 — save the current map as a reusable basemap.

    The active layer is left out unless `include_active`, so the basemap is a
    backdrop rather than a snapshot of somebody's working grid.
    """
    basemap_id = await service.save_map_as_basemap(
        conn,
        principal,
        name=name,
        layer_ids=layer_ids,
        active_layer_id=active_layer_id,
        include_active=include_active,
        visibility=visibility,
    )
    return {"basemap_id": str(basemap_id)}


@basemaps_router.get("/{basemap_id}/layers")
async def basemap_layers(
    principal: CurrentPrincipal, conn: ScopedConn, basemap_id: UUID
) -> dict[str, Any]:
    items = await service.basemap_layers(conn, principal, basemap_id)
    return {"count": len(items), "items": items}


@basemaps_router.put("/{basemap_id}/layers", status_code=status.HTTP_204_NO_CONTENT)
async def set_basemap_layers(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    basemap_id: UUID,
    layer_ids: Annotated[list[UUID], Body(embed=True)],
) -> None:
    """Replace the membership, bottom first. PUT because it is a whole list."""
    await service.set_basemap_layers(conn, principal, basemap_id, layer_ids)


@basemaps_router.delete("/{basemap_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_basemap(
    principal: CurrentPrincipal, conn: ScopedConn, basemap_id: UUID
) -> None:
    """Soft-delete the basemap. Its layers survive — that is the model."""
    await service.delete_basemap(conn, principal, basemap_id)
