"""Saving feature edits. `09-editing.md` §5.3, §13.

Thin by design: validate, delegate to `webmap_core.services.features`, shape
the response. The reads live in `tiles.py` alongside the tile endpoints they
share a query path with; this is the one write.

**One request per save, not per change.** §13 coalesces by feature id in the
browser — a vertex drag fires dozens of change events and one request each
would overwhelm the API and make undo incoherent. The batch limit here is the
backstop for a client that stopped coalescing, and its message says so.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body
from pydantic import BaseModel, ConfigDict, Field

from webmap_api.dependencies import AppSettings, CurrentPrincipal, ScopedConn
from webmap_core.services.features import FeatureEdit, save_feature_edits

router = APIRouter(prefix="/api/v1/features", tags=["features"])


class FeatureEditIn(BaseModel):
    """One feature's change.

    `extra="forbid"`, so a client sending `props` instead of `properties`
    finds out rather than saving a geometry edit that silently dropped the
    attributes.
    """

    model_config = ConfigDict(extra="forbid")

    feature_id: int
    #: GeoJSON geometry in WGS84. Absent keeps the current geometry.
    geometry: dict[str, Any] | None = None
    #: Replaces the feature's properties whole. Absent keeps them.
    properties: dict[str, Any] | None = None
    deleted: bool = False


class SaveEditsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The version these edits were made against. §5.3 puts optimistic
    #: concurrency on this pointer and nowhere else, so it is required rather
    #: than defaulted — a save that guessed would overwrite whoever saved first.
    base_version: int = Field(ge=1)
    edits: list[FeatureEditIn] = Field(min_length=1)


@router.post("/{dataset_id}/edits")
async def save_edits(
    dataset_id: UUID,
    principal: CurrentPrincipal,
    conn: ScopedConn,
    settings: AppSettings,
    body: Annotated[SaveEditsIn, Body()],
) -> dict[str, Any]:
    """Write a new feature version with these edits applied.

    Returns the new version so the client can keep editing without reloading —
    the next save is made against it. A 409 means somebody saved first; the
    client's edits are still in its buffer and §5.3's Refresh rebases them.
    """
    from webmap_io.storage import StorageConfig, client

    storage = client(
        StorageConfig(
            endpoint=settings.s3_endpoint,
            bucket=settings.s3_bucket,
            access_key=settings.s3_access_key.get_secret_value(),
            secret_key=settings.s3_secret_key.get_secret_value(),
            region=settings.s3_region,
        )
    )

    result = await save_feature_edits(
        conn,
        principal,
        dataset_id,
        base_version=body.base_version,
        edits=[
            FeatureEdit(
                feature_id=edit.feature_id,
                geometry=edit.geometry,
                properties=edit.properties,
                deleted=edit.deleted,
            )
            for edit in body.edits
        ],
        store=storage,
        bucket=settings.s3_bucket,
    )

    return {
        "version": result.version,
        "feature_count": result.feature_count,
        "bbox_4326": list(result.bbox_4326),
    }
