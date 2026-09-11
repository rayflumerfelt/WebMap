"""Exporting a dataset, end to end. `11-file-io.md` §7, `12-roadmap.md` Phase 6.

The criterion is *"shapefile export reports truncation and collision before
writing"*, and "before writing" is the part worth testing through the job: the
warnings have to reach the caller in the result, and a format that loses
something has to stop rather than produce a file nobody was warned about.

Needs Postgres and MinIO. Skips with instructions when either is absent.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import numpy as np
import pytest
import shapely
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import BUCKET, EXTENT, TEXAS_CENTRAL, worker_context
from webmap_core.db.session import principal_session
from webmap_core.permissions import Principal
from webmap_core.services import exports, jobs
from webmap_worker.tasks.export import export_task

pytestmark = pytest.mark.integration


LONG_COLUMNS = [
    {"name": "porosity_average", "type": "number"},
    {"name": "porosity_amplitude", "type": "number"},
    {"name": "operator", "type": "text"},
]


async def register_leases(
    engine: AsyncEngine, storage: Any, principal: Principal
) -> UUID:
    """Three lease polygons with two colliding long field names."""
    from webmap_io.parquet import write_features
    from webmap_io.storage import feature_key, put_bytes

    west, south, east, north = EXTENT
    geometry = np.array(
        [
            shapely.box(
                west + index * 1000.0,
                south,
                west + (index + 1) * 1000.0,
                south + 1000.0,
            )
            for index in range(3)
        ],
        dtype=object,
    )
    props: list[dict[str, Any]] = [
        {
            "porosity_average": 0.10 + index / 100,
            "porosity_amplitude": 9.0 - index,
            "operator": f"Operator {index}",
        }
        for index in range(3)
    ]

    dataset_id = uuid4()
    key = feature_key(str(dataset_id), 1)
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "leases.parquet"
        write_features(path, geometry=geometry, props=props, srid=TEXAS_CENTRAL)
        put_bytes(storage, BUCKET, key, path.read_bytes())

    async with principal_session(engine, principal) as conn:
        await conn.execute(
            text(
                """
                INSERT INTO dataset (
                    id, name, kind, geometry_kind, connector, storage_srid,
                    parquet_key, version, feature_count, attribute_schema,
                    owner_user_id, visibility)
                VALUES (
                    :id, 'Leases', 'vector', 'polygon', 'upload', :srid, :key, 1, 3,
                    CAST(:schema AS jsonb), :owner, 'private')
                """
            ),
            {
                "id": dataset_id,
                "srid": TEXAS_CENTRAL,
                "key": key,
                "schema": json.dumps(LONG_COLUMNS),
                "owner": principal.user_id,
            },
        )
    return dataset_id


async def run_export(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    principal: Principal,
    request: exports.ExportRequest,
) -> dict[str, Any]:
    from tests.test_jobs import FakeRedis

    redis = FakeRedis()
    async with principal_session(engine, principal) as conn:
        enqueued = await jobs.enqueue(
            conn, principal, kind="export", parameters=request.to_parameters(), redis=redis
        )
    context = jobs.context_for(principal, enqueued.job_id)
    return await export_task(
        worker_context(engine, storage, object_store, redis),
        {"context": context.to_payload(), "parameters": request.to_parameters()},
    )


def fetch(storage: Any, key: str, dest: Path) -> Path:
    from webmap_io.storage import get_file

    return get_file(storage, BUCKET, key, dest)


class TestGeoPackage:
    async def test_exports_and_reads_back(
        self,
        engine: AsyncEngine,
        storage: Any,
        object_store: Any,
        tmp_path: Path,
        principals: dict[str, object],
    ) -> None:
        owner = principals["owner"]
        assert isinstance(owner, Principal)
        dataset_id = await register_leases(engine, storage, owner)

        document = await run_export(
            engine,
            storage,
            object_store,
            owner,
            exports.ExportRequest(dataset_id=dataset_id, fmt="gpkg"),
        )

        from webmap_io.read import read_vector

        local = fetch(storage, document["object_key"], tmp_path / document["filename"])
        back = read_vector(local)
        assert back.feature_count == 3
        assert "porosity_average" in back.attributes
        assert document["warnings"] == []

    async def test_the_filename_carries_the_version(
        self,
        engine: AsyncEngine,
        storage: Any,
        object_store: Any,
        principals: dict[str, object],
    ) -> None:
        """Two exports of the same layer a week apart are otherwise the same
        filename in a downloads folder."""
        owner = principals["owner"]
        assert isinstance(owner, Principal)
        dataset_id = await register_leases(engine, storage, owner)

        document = await run_export(
            engine,
            storage,
            object_store,
            owner,
            exports.ExportRequest(dataset_id=dataset_id, fmt="gpkg"),
        )

        assert document["filename"] == "leases_v1.gpkg"


class TestShapefile:
    async def test_refuses_until_the_loss_is_accepted(
        self,
        engine: AsyncEngine,
        storage: Any,
        object_store: Any,
        principals: dict[str, object],
    ) -> None:
        """Truncation and collision reported *before* writing — the Phase 6
        criterion. A file produced without that warning is one the recipient
        discovers is wrong."""
        owner = principals["owner"]
        assert isinstance(owner, Principal)
        dataset_id = await register_leases(engine, storage, owner)

        with pytest.raises(exports.ExportError, match="porosity_average -> porosity_a"):
            await run_export(
                engine,
                storage,
                object_store,
                owner,
                exports.ExportRequest(dataset_id=dataset_id, fmt="shapefile"),
            )

    async def test_proceeds_when_accepted_and_still_reports(
        self,
        engine: AsyncEngine,
        storage: Any,
        object_store: Any,
        tmp_path: Path,
        principals: dict[str, object],
    ) -> None:
        """The warnings travel with the result either way: §4.2 wants them in
        the MCP response, not only in a dialog that was already dismissed."""
        owner = principals["owner"]
        assert isinstance(owner, Principal)
        dataset_id = await register_leases(engine, storage, owner)

        document = await run_export(
            engine,
            storage,
            object_store,
            owner,
            exports.ExportRequest(dataset_id=dataset_id, fmt="shapefile", accept_loss=True),
        )

        codes = {warning["code"] for warning in document["warnings"]}
        assert {"field_truncation", "field_collision", "prefer_gpkg"} <= codes

    async def test_the_zip_holds_a_complete_shapefile(
        self,
        engine: AsyncEngine,
        storage: Any,
        object_store: Any,
        tmp_path: Path,
        principals: dict[str, object],
    ) -> None:
        """Users forward exactly what they are given."""
        owner = principals["owner"]
        assert isinstance(owner, Principal)
        dataset_id = await register_leases(engine, storage, owner)

        document = await run_export(
            engine,
            storage,
            object_store,
            owner,
            exports.ExportRequest(dataset_id=dataset_id, fmt="shapefile", accept_loss=True),
        )

        local = fetch(storage, document["object_key"], tmp_path / document["filename"])
        with zipfile.ZipFile(local) as bundle:
            suffixes = {Path(name).suffix.lower() for name in bundle.namelist()}
        assert {".shp", ".shx", ".dbf", ".prj"} <= suffixes

    async def test_the_field_arrives_under_the_promised_name(
        self,
        engine: AsyncEngine,
        storage: Any,
        object_store: Any,
        tmp_path: Path,
        principals: dict[str, object],
    ) -> None:
        owner = principals["owner"]
        assert isinstance(owner, Principal)
        dataset_id = await register_leases(engine, storage, owner)

        document = await run_export(
            engine,
            storage,
            object_store,
            owner,
            exports.ExportRequest(dataset_id=dataset_id, fmt="shapefile", accept_loss=True),
        )

        from webmap_io.read import read_vector

        local = fetch(storage, document["object_key"], tmp_path / document["filename"])
        extracted = tmp_path / "out"
        with zipfile.ZipFile(local) as bundle:
            bundle.extractall(extracted)
        shp = next(extracted.glob("*.shp"))
        assert "porosity_a" in read_vector(shp).attributes


class TestSelection:
    async def test_exports_only_the_named_columns(
        self,
        engine: AsyncEngine,
        storage: Any,
        object_store: Any,
        tmp_path: Path,
        principals: dict[str, object],
    ) -> None:
        owner = principals["owner"]
        assert isinstance(owner, Principal)
        dataset_id = await register_leases(engine, storage, owner)

        document = await run_export(
            engine,
            storage,
            object_store,
            owner,
            exports.ExportRequest(dataset_id=dataset_id, fmt="gpkg", columns=["operator"]),
        )

        from webmap_io.read import read_vector

        local = fetch(storage, document["object_key"], tmp_path / document["filename"])
        attributes = read_vector(local).attributes
        assert "operator" in attributes
        assert "porosity_average" not in attributes

    async def test_reprojects_on_the_way_out(
        self,
        engine: AsyncEngine,
        storage: Any,
        object_store: Any,
        tmp_path: Path,
        principals: dict[str, object],
    ) -> None:
        """`CLAUDE.md` §3.1: reprojection at defined boundaries. An export is
        one — the file is leaving, and the recipient's CRS is theirs."""
        owner = principals["owner"]
        assert isinstance(owner, Principal)
        dataset_id = await register_leases(engine, storage, owner)

        document = await run_export(
            engine,
            storage,
            object_store,
            owner,
            exports.ExportRequest(dataset_id=dataset_id, fmt="gpkg", target_srid=4326),
        )

        from webmap_io.read import read_vector

        local = fetch(storage, document["object_key"], tmp_path / document["filename"])
        back = read_vector(local)
        assert back.srid == 4326
        # West Texas in degrees, not in feet.
        assert -104.0 < shapely.get_x(back.geometry[0].centroid) < -100.0


class TestAudit:
    async def test_an_export_is_recorded(
        self,
        engine: AsyncEngine,
        storage: Any,
        object_store: Any,
        principals: dict[str, object],
    ) -> None:
        """`03-auth-security.md` §9 lists export explicitly. Exporting is
        reading, and the control on reading is knowing it happened."""
        owner = principals["owner"]
        assert isinstance(owner, Principal)
        dataset_id = await register_leases(engine, storage, owner)

        await run_export(
            engine,
            storage,
            object_store,
            owner,
            exports.ExportRequest(dataset_id=dataset_id, fmt="gpkg"),
        )

        async with principal_session(engine, owner) as conn:
            count = (
                await conn.execute(
                    text(
                        """
                        SELECT count(*) FROM audit_event
                         WHERE action = 'export.created' AND object_id = :id
                        """
                    ),
                    {"id": dataset_id},
                )
            ).scalar_one()
        assert count == 1
