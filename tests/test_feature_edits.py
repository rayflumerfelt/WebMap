"""Saving feature edits. `09-editing.md` §5.3, §13, `adr/0005`.

`python/webmap_io/tests/test_edits.py` covers what happens to the Parquet.
This covers what only the service can: the permission check that stands
between a principal and somebody else's layer, the version pointer that is the
commit, and the 409 that a second editor gets instead of silently overwriting
the first.

Needs Postgres and MinIO. Skips with instructions when either is absent.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID

import pyarrow.parquet as pq
import pytest
import shapely
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import BUCKET, TEXAS_CENTRAL, register_picks
from webmap_core.db.session import principal_session
from webmap_core.exceptions import PermissionDenied, VersionConflict
from webmap_core.permissions import GrantRole, Principal
from webmap_core.services.features import FeatureEdit, save_feature_edits
from webmap_core.services.grants import create_grant
from webmap_core.services.ownable import load_ownable

pytestmark = pytest.mark.integration

# Inside the picks extent, in WGS84 — the browser's units, which is what an
# edit arrives in.
SOMEWHERE_IN_MIDLAND = {"type": "Point", "coordinates": [-102.08, 31.99]}


async def dataset_row(engine: AsyncEngine, principal: Principal, dataset_id: UUID) -> Any:
    async with principal_session(engine, principal) as conn:
        return (
            await conn.execute(
                text(
                    "SELECT version, parquet_key, feature_count, bbox_4326 "
                    "FROM dataset WHERE id = :id"
                ),
                {"id": dataset_id},
            )
        ).one()


async def save(
    engine: AsyncEngine,
    principal: Principal,
    dataset_id: UUID,
    storage: Any,
    *,
    base_version: int,
    edits: list[FeatureEdit],
) -> Any:
    async with principal_session(engine, principal) as conn:
        return await save_feature_edits(
            conn,
            principal,
            dataset_id,
            base_version=base_version,
            edits=edits,
            store=storage,
            bucket=BUCKET,
        )


def read_geometry(storage: Any, key: str, feature_id: int) -> Any:
    from webmap_io.storage import get_file

    with TemporaryDirectory() as tmp:
        path = get_file(storage, BUCKET, key, Path(tmp) / "features.parquet")
        table = pq.read_table(path)
        ids = table.column("id").to_pylist()
        index = ids.index(feature_id)
        return shapely.from_wkb(table.column("geometry")[index].as_py())


def read_props(storage: Any, key: str, feature_id: int) -> dict[str, Any]:
    from webmap_io.storage import get_file

    with TemporaryDirectory() as tmp:
        path = get_file(storage, BUCKET, key, Path(tmp) / "features.parquet")
        table = pq.read_table(path)
        index = table.column("id").to_pylist().index(feature_id)
        value = table.column("props")[index].as_py()
        return dict(json.loads(value)) if value else {}


async def test_advances_the_version_and_writes_a_new_object(
    engine: AsyncEngine, principals: dict[str, Any], picks: str, storage: Any
) -> None:
    """§13: the object is written first and the pointer advance is the commit."""
    owner = principals["owner"]
    dataset_id = await register_picks(engine, owner, picks)

    result = await save(
        engine,
        owner,
        dataset_id,
        storage,
        base_version=1,
        edits=[FeatureEdit(feature_id=1, geometry=SOMEWHERE_IN_MIDLAND)],
    )

    row = await dataset_row(engine, owner, dataset_id)
    assert result.version == 2
    assert row.version == 2
    assert row.parquet_key.endswith("/v2.parquet")
    assert row.parquet_key != picks


async def test_the_edit_lands_in_the_storage_crs(
    engine: AsyncEngine, principals: dict[str, Any], picks: str, storage: Any
) -> None:
    """The browser sends WGS84 because that is what GeoJSON is; the object
    holds the dataset's own CRS because that is what every measurement is done
    in. A save that skipped the conversion would put the feature in the Gulf of
    Guinea, which is what unconverted degrees look like in State Plane feet."""
    import numpy as np

    from webmap_geo.crs import WGS84, transform_points

    owner = principals["owner"]
    dataset_id = await register_picks(engine, owner, picks)

    await save(
        engine,
        owner,
        dataset_id,
        storage,
        base_version=1,
        edits=[FeatureEdit(feature_id=1, geometry=SOMEWHERE_IN_MIDLAND)],
    )

    row = await dataset_row(engine, owner, dataset_id)
    moved = read_geometry(storage, row.parquet_key, 1)
    x, y = transform_points(
        np.array([SOMEWHERE_IN_MIDLAND["coordinates"][0]]),
        np.array([SOMEWHERE_IN_MIDLAND["coordinates"][1]]),
        WGS84,
        TEXAS_CENTRAL,
    )
    # A foot: the round trip through PROJ is exact well past that, and a
    # tolerance looser than a foot would not notice a wrong datum.
    assert moved.distance(shapely.Point(x[0], y[0])) < 1.0


async def test_a_geometry_edit_keeps_the_attributes(
    engine: AsyncEngine, principals: dict[str, Any], picks: str, storage: Any
) -> None:
    """A drag is not a rename."""
    owner = principals["owner"]
    dataset_id = await register_picks(engine, owner, picks)

    await save(
        engine,
        owner,
        dataset_id,
        storage,
        base_version=1,
        edits=[FeatureEdit(feature_id=1, geometry=SOMEWHERE_IN_MIDLAND)],
    )

    row = await dataset_row(engine, owner, dataset_id)
    assert read_props(storage, row.parquet_key, 1)["well_name"] == "Wolfcamp 0000"


async def test_a_delete_drops_the_feature_count(
    engine: AsyncEngine, principals: dict[str, Any], picks: str, storage: Any
) -> None:
    owner = principals["owner"]
    dataset_id = await register_picks(engine, owner, picks)

    result = await save(
        engine,
        owner,
        dataset_id,
        storage,
        base_version=1,
        edits=[FeatureEdit(feature_id=7, deleted=True)],
    )

    row = await dataset_row(engine, owner, dataset_id)
    assert result.feature_count == 399
    assert row.feature_count == 399


async def test_records_the_version_row(
    engine: AsyncEngine, principals: dict[str, Any], picks: str, storage: Any
) -> None:
    """Every version for 30 days (§13). The row is what the retention job
    thins and what a rollback would read."""
    owner = principals["owner"]
    dataset_id = await register_picks(engine, owner, picks)

    await save(
        engine,
        owner,
        dataset_id,
        storage,
        base_version=1,
        edits=[FeatureEdit(feature_id=2, geometry=SOMEWHERE_IN_MIDLAND)],
    )

    async with principal_session(engine, owner) as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT version, parquet_key, feature_count, created_by "
                    "FROM dataset_version WHERE dataset_id = :id AND version = 2"
                ),
                {"id": dataset_id},
            )
        ).one()
    assert row.feature_count == 400
    assert UUID(str(row.created_by)) == owner.user_id


async def test_a_stale_base_version_is_refused(
    engine: AsyncEngine, principals: dict[str, Any], picks: str, storage: Any
) -> None:
    """§5.3: the pointer is where optimistic concurrency lives. The second
    editor is told, and their edits stay in their buffer for the rebase."""
    owner = principals["owner"]
    dataset_id = await register_picks(engine, owner, picks)

    await save(
        engine,
        owner,
        dataset_id,
        storage,
        base_version=1,
        edits=[FeatureEdit(feature_id=1, geometry=SOMEWHERE_IN_MIDLAND)],
    )

    with pytest.raises(VersionConflict, match="Refresh"):
        await save(
            engine,
            owner,
            dataset_id,
            storage,
            base_version=1,
            edits=[FeatureEdit(feature_id=2, geometry=SOMEWHERE_IN_MIDLAND)],
        )


async def test_a_stranger_cannot_edit(
    engine: AsyncEngine, principals: dict[str, Any], picks: str, storage: Any
) -> None:
    """RLS protects the registry row and has no reach into object storage
    (`02` §4.1), so the service is the only thing standing between a principal
    and a write to somebody else's layer."""
    owner = principals["owner"]
    stranger = principals["stranger"]
    dataset_id = await register_picks(engine, owner, picks)

    with pytest.raises((PermissionDenied, Exception)) as raised:
        await save(
            engine,
            stranger,
            dataset_id,
            storage,
            base_version=1,
            edits=[FeatureEdit(feature_id=1, geometry=SOMEWHERE_IN_MIDLAND)],
        )
    # A private dataset is invisible to a stranger under RLS, so the refusal
    # arrives as NotFound rather than PermissionDenied — the distinction the
    # permission tests already pin down. What matters here is that it refused.
    assert type(raised.value).__name__ in {"PermissionDenied", "NotFound"}

    row = await dataset_row(engine, owner, dataset_id)
    assert row.version == 1


async def test_a_viewer_cannot_edit(
    engine: AsyncEngine, principals: dict[str, Any], picks: str, storage: Any
) -> None:
    """A viewer opens the editor and finds the tools greyed; the API refusing
    is what makes that a real restriction rather than a UI convention."""
    owner = principals["owner"]
    colleague = principals["outsider"]
    dataset_id = await register_picks(engine, owner, picks)

    # Through the service rather than by INSERT: the grant rules are its
    # business, and a test that wrote the row itself would keep passing if
    # they changed.
    async with principal_session(engine, owner) as conn:
        await create_grant(
            conn,
            owner,
            await load_ownable(conn, "dataset", dataset_id),
            "dataset",
            GrantRole.VIEWER,
            grantee_user_id=colleague.user_id,
        )

    with pytest.raises(PermissionDenied):
        await save(
            engine,
            colleague,
            dataset_id,
            storage,
            base_version=1,
            edits=[FeatureEdit(feature_id=1, geometry=SOMEWHERE_IN_MIDLAND)],
        )
