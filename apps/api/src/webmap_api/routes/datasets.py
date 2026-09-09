"""Dataset registry endpoints. `02-data-model.md` §3.5.

Thin by design: validate, delegate to `webmap_core.services.datasets`, shape
the response. Every handler takes `ScopedConn`, which cannot be obtained
without a verified principal, so there is no route here that can reach an
ownable table without an RLS context.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Query, Request, status

from webmap_api.dependencies import CurrentPrincipal, ScopedConn
from webmap_core.models import DatasetKind
from webmap_core.permissions import GrantRole, Visibility
from webmap_core.services import datasets as service
from webmap_core.services.audit import AuditAction, record
from webmap_core.services.grants import create_grant, list_grants, revoke_grant
from webmap_core.services.ownable import load_ownable

router = APIRouter(prefix="/api/v1/datasets", tags=["datasets"])


@router.get("")
async def list_datasets(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    project_id: Annotated[UUID | None, Query()] = None,
    kind: Annotated[DatasetKind | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """List datasets. The pagination contract from `04-mcp-server.md` §4.1."""
    page = await service.list_datasets(
        conn, principal, project_id=project_id, kind=kind, limit=limit, offset=offset
    )
    return {
        "total": page.total,
        "count": len(page.items),
        "offset": page.offset,
        "has_more": page.has_more,
        "next_offset": page.next_offset,
        "items": page.items,
    }


@router.get("/search")
async def search_datasets(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    q: Annotated[str, Query(min_length=1, description="Free text over name and description")],
    kind: Annotated[DatasetKind | None, Query()] = None,
    bbox: Annotated[
        list[float] | None,
        Query(description="[west, south, east, north] in EPSG:4326"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """Resolve an informal name to datasets.

    Declared before `/{dataset_id}` so the literal path wins — otherwise
    "search" is parsed as a UUID and every search is a 422.
    """
    if bbox is not None and len(bbox) != 4:
        raise ValueError(
            f"bbox needs exactly four numbers [west, south, east, north] in "
            f"EPSG:4326; got {len(bbox)}."
        )
    items = await service.search_datasets(conn, principal, q, bbox=bbox, kind=kind, limit=limit)
    return {"count": len(items), "items": items}


@router.get("/{dataset_id}")
async def get_dataset(
    request: Request, principal: CurrentPrincipal, conn: ScopedConn, dataset_id: UUID
) -> dict[str, Any]:
    """Full detail for one dataset.

    Reads through MCP are audited (`03-auth-security.md` §10); reads from the
    browser are not, because the SPA polls this and the volume would bury
    everything else. The channel is what distinguishes them.
    """
    detail = await service.get_dataset(conn, principal, dataset_id)
    if principal.channel.value == "claude":
        await record(
            conn,
            action=AuditAction.DATASET_READ_MCP,
            principal=principal,
            object_type="dataset",
            object_id=dataset_id,
            detail={"name": detail.get("name")},
            ip_address=request.client.host if request.client else None,
        )
    return detail


@router.patch("/{dataset_id}")
async def update_dataset(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    dataset_id: UUID,
    name: Annotated[str | None, Body()] = None,
    description: Annotated[str | None, Body()] = None,
    caption: Annotated[str | None, Body()] = None,
    visibility: Annotated[Visibility | None, Body()] = None,
) -> dict[str, str]:
    await service.update_dataset(
        conn,
        principal,
        dataset_id,
        name=name,
        description=description,
        caption=caption,
        visibility=visibility,
    )
    return {"status": "updated"}


@router.delete("/{dataset_id}", status_code=status.HTTP_200_OK)
async def delete_dataset(
    principal: CurrentPrincipal, conn: ScopedConn, dataset_id: UUID
) -> dict[str, Any]:
    """Soft delete, reversible for 30 days (`03-auth-security.md` §8)."""
    await service.soft_delete_dataset(conn, principal, dataset_id)
    return {
        "status": "deleted",
        "recoverable": True,
        "detail": (
            "Soft-deleted. It is recoverable for 30 days and stays resolvable "
            "by lineage records so provenance chains do not break."
        ),
    }


# --- Sharing ----------------------------------------------------------------


@router.get("/{dataset_id}/grants")
async def get_grants(
    principal: CurrentPrincipal, conn: ScopedConn, dataset_id: UUID
) -> dict[str, Any]:
    """Who this dataset is shared with.

    Loading the row first is the permission check: RLS hides it from anyone
    who cannot see the dataset, so the grant list cannot leak from an object
    the caller has no access to.
    """
    await load_ownable(conn, "dataset", dataset_id)
    return {"items": await list_grants(conn, "dataset", dataset_id)}


@router.post("/{dataset_id}/grants", status_code=status.HTTP_201_CREATED)
async def add_grant(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    dataset_id: UUID,
    role: Annotated[GrantRole, Body()],
    user_id: Annotated[UUID | None, Body()] = None,
    team_id: Annotated[UUID | None, Body()] = None,
) -> dict[str, str]:
    """Share a dataset. Owner-only (`03-auth-security.md` §3.3)."""
    obj = await load_ownable(conn, "dataset", dataset_id)
    grant_id = await create_grant(
        conn,
        principal,
        obj,
        "dataset",
        role,
        grantee_user_id=user_id,
        grantee_team_id=team_id,
    )
    await record(
        conn,
        action=AuditAction.GRANT_CREATED,
        principal=principal,
        object_type="dataset",
        object_id=dataset_id,
        detail={
            "role": role.value,
            "grantee_user_id": str(user_id) if user_id else None,
            "grantee_team_id": str(team_id) if team_id else None,
        },
    )
    return {"grant_id": str(grant_id)}


@router.delete("/{dataset_id}/grants/{grant_id}")
async def remove_grant(
    principal: CurrentPrincipal, conn: ScopedConn, dataset_id: UUID, grant_id: UUID
) -> dict[str, str]:
    obj = await load_ownable(conn, "dataset", dataset_id)
    await revoke_grant(conn, principal, obj, "dataset", grant_id)
    await record(
        conn,
        action=AuditAction.GRANT_REVOKED,
        principal=principal,
        object_type="dataset",
        object_id=dataset_id,
        detail={"grant_id": str(grant_id)},
    )
    return {"status": "revoked"}


__all__ = ["router"]
