"""Saving a batch of feature edits. `09-editing.md` §5.3, §13, `adr/0005`.

Orchestration only. The geometry work is `webmap_io.edits.apply_edits`, the
reprojection is `webmap_geo.crs`, and what is left here is the part that has to
be got right about *ordering and identity*:

**The pointer is the commit.** The new object is written first, under a name
nothing refers to yet, and only then does `dataset.version` advance. Until it
does, the object is invisible and a crash leaves an orphan the retention job
collects — readers never see a half-written layer, and that falls out of the
storage model rather than being a property anyone has to preserve.

**The version is checked twice, and the second one is the real one.** The first
check answers the client early and with a useful message. The second is the
`WHERE version = :base` on the pointer update, which is what actually closes
the race: two editors who both read version 7 both write their object, and
exactly one of them advances the pointer. The loser gets a 409 and §5.3's
Refresh, not a silent overwrite of work they never saw.

**Editor permission, checked against the registry row.** RLS protects the row;
it has no reach into object storage (`02` §4.1), so this function is the only
thing standing between a principal and a write to somebody else's layer.

The browser sends WGS84 because that is what GeoJSON and MapLibre are; the
feature object holds the dataset's own CRS because that is what every
measurement is done in. The conversion happens here, once, at the boundary
(`adr/0003`).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from webmap_core.exceptions import NotFound, VersionConflict
from webmap_core.logging import get_logger
from webmap_core.permissions import Permission, Principal
from webmap_core.services.audit import AuditAction, record
from webmap_core.services.ownable import load_and_require

log = get_logger(__name__)

#: More changes than this in one request and something is wrong with the
#: client's coalescing: §13 coalesces by feature id, and §17 caps the editable
#: working set at 5,000 features, so a batch larger than the cap cannot be a
#: batch of distinct features.
MAX_CHANGES = 5_000


@dataclass(frozen=True)
class FeatureEdit:
    """One feature's change, as the browser sends it.

    `geometry` is a GeoJSON geometry in **WGS84**. `properties` replaces the
    feature's properties whole; `None` keeps them.
    """

    feature_id: int
    geometry: Mapping[str, Any] | None = None
    properties: Mapping[str, Any] | None = None
    deleted: bool = False


@dataclass(frozen=True)
class SaveResult:
    """What the client needs to keep editing without reloading."""

    version: int
    feature_count: int
    bbox_4326: tuple[float, float, float, float]


async def save_feature_edits(
    conn: AsyncConnection,
    principal: Principal,
    dataset_id: UUID,
    *,
    base_version: int,
    edits: Sequence[FeatureEdit],
    store: Any,
    bucket: str,
) -> SaveResult:
    """Write the next version of a dataset's features and advance the pointer."""
    if not edits:
        raise ValueError(
            "No edits to save. Saving nothing would still advance the dataset "
            "version and invalidate every cached tile for this layer."
        )
    if len(edits) > MAX_CHANGES:
        raise ValueError(
            f"{len(edits):,} changes in one save, over the {MAX_CHANGES:,} limit. "
            f"Edits coalesce by feature id before they are sent (`09` §13), and "
            f"the editable working set is capped at {MAX_CHANGES:,} features — a "
            f"batch this large means the coalescing did not run."
        )

    await load_and_require(conn, "dataset", dataset_id, principal, Permission.EDITOR)

    row = (
        await conn.execute(
            text(
                """
                SELECT parquet_key, version, storage_srid, kind, name
                FROM dataset WHERE id = :id
                """
            ),
            {"id": dataset_id},
        )
    ).one()

    if row.parquet_key is None:
        raise NotFound(
            f"'{row.name}' is a {row.kind} and has no feature object to edit. "
            f"Grids are edited by re-gridding their control points, not by "
            f"moving cells."
        )
    if int(row.version) != base_version:
        raise VersionConflict(
            f"'{row.name}' is at version {row.version} and these edits were made "
            f"against version {base_version}. Someone saved while you were "
            f"editing. Refresh to rebase your changes onto theirs — your edits "
            f"are kept."
        )

    storage_srid = int(row.storage_srid)
    changes = _to_changes(edits, storage_srid)
    next_version = int(row.version) + 1

    from webmap_io.edits import apply_edits
    from webmap_io.storage import feature_key, get_file, put_file

    key = feature_key(str(dataset_id), next_version)

    with TemporaryDirectory() as tmp:
        source = get_file(store, bucket, str(row.parquet_key), Path(tmp) / "current.parquet")
        written = apply_edits(source, Path(tmp) / "next.parquet", changes, srid=storage_srid)
        # Object first, under a name nothing refers to yet. §13: advancing the
        # pointer below is the commit, and an object written for a save that
        # then loses the race is an orphan, which is recoverable.
        put_file(store, bucket, key, written.path)

    from webmap_geo.crs import WGS84, transform_bbox

    bbox_4326 = transform_bbox(written.bbox, storage_srid, WGS84)

    updated = await conn.execute(
        text(
            """
            UPDATE dataset
               SET version = :next,
                   parquet_key = :key,
                   feature_count = :count,
                   bbox_4326 = :bbox,
                   updated_at = now()
             WHERE id = :id AND version = :base
            """
        ),
        {
            "next": next_version,
            "key": key,
            "count": written.feature_count,
            "bbox": list(bbox_4326),
            "id": dataset_id,
            "base": base_version,
        },
    )
    if updated.rowcount != 1:
        # The check above answered early and politely; this one is the one that
        # actually closes the race. Two editors who both read version 7 both
        # reach here, and exactly one advances the pointer.
        raise VersionConflict(
            f"'{row.name}' was saved by someone else while this save was being "
            f"written. Refresh to rebase your changes onto theirs — your edits "
            f"are kept."
        )

    await conn.execute(
        text(
            """
            INSERT INTO dataset_version (
                dataset_id, version, parquet_key, feature_count, created_by)
            VALUES (:id, :version, :key, :count, :by)
            """
        ),
        {
            "id": dataset_id,
            "version": next_version,
            "key": key,
            "count": written.feature_count,
            "by": principal.user_id,
        },
    )

    # `DATASET_UPDATED` rather than an event of its own: `03` §10 asks for
    # dataset create, update and delete, and an edit is an update whose detail
    # says what kind. The counts are what makes the record answer "what did
    # they change" without opening two Parquet objects.
    await record(
        conn,
        action=AuditAction.DATASET_UPDATED,
        principal=principal,
        object_type="dataset",
        object_id=dataset_id,
        detail={
            "change": "features",
            "version": next_version,
            "base_version": base_version,
            "edited": sum(1 for edit in edits if not edit.deleted),
            "deleted": sum(1 for edit in edits if edit.deleted),
            "feature_count": written.feature_count,
        },
    )

    log.info(
        "features.saved",
        dataset_id=str(dataset_id),
        version=next_version,
        changes=len(edits),
        feature_count=written.feature_count,
    )
    return SaveResult(
        version=next_version,
        feature_count=written.feature_count,
        bbox_4326=bbox_4326,
    )


def _to_changes(edits: Sequence[FeatureEdit], storage_srid: int) -> list[Any]:
    """Client edits as storage-CRS changes.

    Geometries are transformed in one batch rather than one at a time: a
    `Transformer` is cached but each call still crosses into PROJ, and a save
    of a few hundred edited features is a few hundred crossings for no reason.
    """
    import shapely

    from webmap_geo.crs import WGS84, transform_geometries
    from webmap_io.edits import FeatureChange

    with_geometry = [index for index, edit in enumerate(edits) if edit.geometry is not None]
    geometries = np.asarray(
        [shapely.from_geojson(json.dumps(edits[index].geometry)) for index in with_geometry],
        dtype=object,
    )
    if len(geometries):
        geometries = transform_geometries(geometries, WGS84, storage_srid)

    projected = dict(zip(with_geometry, geometries, strict=True))
    return [
        FeatureChange(
            feature_id=edit.feature_id,
            geometry=projected.get(index),
            props=None if edit.properties is None else dict(edit.properties),
            deleted=edit.deleted,
        )
        for index, edit in enumerate(edits)
    ]
