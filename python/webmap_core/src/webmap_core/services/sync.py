"""Refreshing a dataset from its source. `11-file-io.md` §2.4.

**Every external source is an upstream, never a live source.** Reading
shapefiles off a share for each map render is miserably slow, so a share- or
database-sourced dataset is materialised into GeoParquet and re-materialised
when the upstream changes. This module is that re-materialisation.

Two properties do the work, and both are about what a geologist sees:

**The pointer move is the commit** (`adr/0005`, `09` §13). A new object is
written first, under a new version, and only then does `dataset.parquet_key`
advance — in one statement, guarded on the version it read. Nobody sees a
half-loaded layer, a render that started before the swap finishes against the
object it started with, and a failure half way through leaves an orphaned
object that a sweep collects rather than a dataset pointing at nothing.

**An unchanged source is not re-ingested.** Checked by content, so a share
restored from backup does not produce a version for every dataset on it. The
inverse matters more: a source that cannot be checksummed is treated as changed,
because a sync that wrongly skips leaves someone reading last month's picks
believing they are current.

`sync_state` is the story the UI tells while this runs: `SYNCING` from the
moment the fetch starts, `READY` with a fresh `synced_at` on success, `FAILED`
with the reason on failure — and `STALE` when the source has gone, which is a
different thing from failing and needs a different sentence in the UI.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from webmap_core.jobs import JobContext
from webmap_core.logging import get_logger
from webmap_core.models import SyncState
from webmap_core.permissions import Permission, Principal
from webmap_core.services.gridding import record_lineage
from webmap_core.services.ownable import load_and_require

log = get_logger(__name__)


class SyncError(Exception):
    """A sync that could not complete. Carries what to do next."""


@dataclass(frozen=True)
class SyncRequest:
    dataset_id: UUID
    #: Re-read even when the checksum matches. For the case where the upstream
    #: was edited in place by something that preserved its bytes' length and
    #: somebody has reason to doubt the checksum — rare, and worth having.
    force: bool = False

    @classmethod
    def from_parameters(cls, parameters: dict[str, Any]) -> SyncRequest:
        return cls(
            dataset_id=UUID(str(parameters["dataset_id"])),
            force=bool(parameters.get("force", False)),
        )

    def to_parameters(self) -> dict[str, Any]:
        return {"dataset_id": str(self.dataset_id), "force": self.force}


@dataclass(frozen=True)
class SyncSource:
    """The dataset to refresh, after its permission check."""

    dataset_id: UUID
    name: str
    connector: str
    source_uri: str
    source_checksum: str | None
    storage_srid: int
    version: int
    owner_team_id: UUID | None
    project_id: UUID | None
    #: What it takes to read the source again — the column mapping for a CSV,
    #: the layer name for a multi-layer format, the encoding. Chosen once by a
    #: person at ingest and kept, because `11` §3.1 forbids sniffing it and a
    #: mapping nobody recorded is a dataset that can never be refreshed.
    source_options: dict[str, Any]


@dataclass(frozen=True)
class SyncOutcome:
    dataset_id: UUID
    changed: bool
    version: int
    feature_count: int | None
    #: A sentence for the job document and the layer's tooltip. Says *why* when
    #: nothing changed, because "sync succeeded, nothing happened" is the
    #: result most likely to be read as a failure.
    reason: str


async def resolve_source(
    conn: AsyncConnection, principal: Principal, request: SyncRequest
) -> SyncSource:
    """Permission-checked lookup of the dataset to refresh.

    **Editor, not viewer.** A sync advances the dataset's version and replaces
    what every reader sees; that it reads from upstream rather than from a user's
    edits does not make it less of a write.
    """
    row = (
        await conn.execute(
            text(
                """
                SELECT id, name, connector, source_uri, source_checksum,
                       storage_srid, version, owner_team_id, project_id,
                       source_options
                  FROM dataset
                 WHERE id = :id AND deleted_at IS NULL
                """
            ),
            {"id": request.dataset_id},
        )
    ).one_or_none()

    if row is None:
        raise SyncError(
            f"No dataset {request.dataset_id} is visible to you, so there is "
            f"nothing to sync. It may have been deleted, or you may not have "
            f"access to it."
        )

    await load_and_require(conn, "dataset", request.dataset_id, principal, Permission.EDITOR)

    if not row.source_uri:
        raise SyncError(
            f"'{row.name}' has no source to sync from — it was uploaded or "
            f"derived, and both are immutable by design (`CLAUDE.md` §3.4). "
            f"Only share- and database-sourced datasets refresh."
        )

    return SyncSource(
        dataset_id=request.dataset_id,
        name=str(row.name),
        connector=str(row.connector),
        source_uri=str(row.source_uri),
        source_checksum=row.source_checksum,
        storage_srid=int(row.storage_srid),
        version=int(row.version),
        owner_team_id=row.owner_team_id,
        project_id=row.project_id,
        source_options=dict(row.source_options or {}),
    )


async def mark_state(
    conn: AsyncConnection, dataset_id: UUID, state: SyncState, *, synced: bool = False
) -> None:
    """Move the dataset's sync state, and the timestamp with it on success."""
    await conn.execute(
        text(
            """
            UPDATE dataset
               SET sync_state = :state,
                   synced_at = CASE WHEN :synced THEN now() ELSE synced_at END,
                   updated_at = now()
             WHERE id = :id
            """
        ),
        {"id": dataset_id, "state": state.value, "synced": synced},
    )


async def is_current(connector: Any, source: SyncSource, *, force: bool) -> bool:
    """Whether the stored copy already matches upstream."""
    if force:
        return False
    return not await connector.has_changed(source.source_uri, source.source_checksum)


def read_source(path: Path, source: SyncSource) -> Any:
    """Read the fetched file, the way this dataset was read the first time.

    **The SRID is not re-derived from the file.** A dataset's `storage_srid` is
    what every tile, every grid and every export of it has used; letting an
    upstream file change it mid-life would silently reproject a layer under the
    maps built on it. A file that now declares a different CRS is a new dataset,
    not a new version of this one.

    **Nor is the column mapping re-guessed.** A CSV of picks has no intrinsic
    geometry, and `11` §3.1 forbids sniffing which columns hold X and Y —
    sniffing swaps latitude and longitude often enough to put a mirrored map in
    a deck. The mapping a person chose at ingest is in `source_options`, and a
    CSV that arrived without one is refused rather than guessed at.
    """
    from webmap_io.read import read_vector, read_xyz

    options = source.source_options
    x, y = options.get("x_column"), options.get("y_column")

    if x and y:
        result = read_xyz(
            path,
            x_column=str(x),
            y_column=str(y),
            z_column=str(options.get("z_column") or ""),
            srid=source.storage_srid,
            delimiter=options.get("delimiter"),
        )
    elif path.suffix.lower() in {".csv", ".txt", ".xyz"}:
        raise SyncError(
            f"'{source.name}' is tabular and no column mapping was recorded for "
            f"it, so there is no safe way to re-read it — guessing which column "
            f"holds X is how a map ends up mirrored. Re-import the file and name "
            f"the X, Y and Z columns; the mapping is kept from then on."
        )
    else:
        result = read_vector(
            path,
            layer=options.get("layer"),
            srid_override=source.storage_srid,
            encoding=options.get("encoding"),
        )

    if result.feature_count == 0:
        raise SyncError(
            f"'{source.name}' read zero features from {source.source_uri}. The "
            f"upstream file may have been emptied or replaced with a different "
            f"format; the stored copy has been left alone."
        )
    return result


async def write_version(
    conn: AsyncConnection,
    source: SyncSource,
    result: Any,
    checksum: str | None,
    *,
    store: Any,
    bucket: str,
) -> tuple[int, int]:
    """Write the next version's object and advance the pointer.

    Returns `(version, feature_count)`. The guarded update is what makes this
    safe to run beside an edit: if the version moved while the file was being
    read, no rows are updated and the sync fails rather than overwriting
    somebody's save with an upstream copy.
    """
    import json

    from webmap_io.parquet import write_features
    from webmap_io.read import attribute_schema
    from webmap_io.storage import feature_key, put_bytes

    next_version = source.version + 1
    key = feature_key(str(source.dataset_id), next_version)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "features.parquet"
        write_features(
            path,
            geometry=result.geometry,
            props=result.props(),
            srid=source.storage_srid,
        )
        put_bytes(store, bucket, key, path.read_bytes())

    updated = await conn.execute(
        text(
            """
            UPDATE dataset
               SET parquet_key = :key,
                   version = :next,
                   feature_count = :count,
                   bbox_4326 = :bbox,
                   attribute_schema = CAST(:schema AS jsonb),
                   source_checksum = :checksum,
                   sync_state = :state,
                   synced_at = now(),
                   updated_at = now()
             WHERE id = :id AND version = :base
            """
        ),
        {
            "key": key,
            "next": next_version,
            "count": result.feature_count,
            "bbox": _bbox_of(result, source.storage_srid),
            "schema": json.dumps(attribute_schema(result)),
            "checksum": checksum,
            "state": SyncState.READY.value,
            "id": source.dataset_id,
            "base": source.version,
        },
    )

    if updated.rowcount == 0:
        raise SyncError(
            f"'{source.name}' moved from version {source.version} while it was "
            f"being read from upstream — somebody saved an edit, or another "
            f"sync finished first. Nothing was overwritten. Run the sync again."
        )

    await conn.execute(
        text(
            """
            INSERT INTO dataset_version (dataset_id, version, parquet_key, created_at)
            VALUES (:id, :version, :key, now())
            ON CONFLICT DO NOTHING
            """
        ),
        {"id": source.dataset_id, "version": next_version, "key": key},
    )
    return next_version, int(result.feature_count)


def _bbox_of(result: Any, srid: int) -> list[float] | None:
    """The WGS84 bbox the registry stores, from the geometry just read.

    Recomputed rather than carried over: the whole point of a sync is that the
    upstream changed, and an extent left at the old file's is what makes a
    "zoom to layer" land somewhere the data no longer is.
    """
    import shapely

    from webmap_geo.crs import WGS84, transform_bbox

    if len(result.geometry) == 0:
        return None
    bounds = shapely.total_bounds(result.geometry)
    return list(transform_bbox((bounds[0], bounds[1], bounds[2], bounds[3]), srid, WGS84))


async def record_sync_lineage(
    conn: AsyncConnection,
    principal: Principal,
    context: JobContext,
    source: SyncSource,
    version: int,
    checksum: str | None,
) -> None:
    """A lineage record for the refresh.

    `CLAUDE.md` §3.3 asks for one on every derived dataset, and a synced version
    is derived from an upstream file at a point in time. The checksum is the
    part worth keeping: it is what makes "which copy of the share file is this?"
    answerable a year later.
    """
    await record_lineage(
        conn,
        principal,
        output_dataset_id=source.dataset_id,
        operation="sync",
        parameters={
            "source_uri": source.source_uri,
            "connector": source.connector,
            "version": version,
            "source_checksum": checksum,
            "synced_at": datetime.now(UTC).isoformat(),
        },
        input_dataset_ids=[],
        job_id=context.job_id,
    )


__all__ = [
    "SyncError",
    "SyncOutcome",
    "SyncRequest",
    "SyncSource",
    "is_current",
    "mark_state",
    "read_source",
    "record_sync_lineage",
    "resolve_source",
    "write_version",
]
