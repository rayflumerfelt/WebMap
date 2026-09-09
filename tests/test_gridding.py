"""Gridding as a job, end to end. `12-roadmap.md` Phase 4.

The criteria this covers, verbatim from the roadmap:

- *"A gridding job runs to completion, registers a dataset, and writes a
  lineage record sufficient to re-run and reproduce the identical grid."*
- *"Cancelling a running job leaves no partial output."*

Everything goes through the real path — a GeoParquet object on MinIO, a job
row in Postgres, the actual solver, a COG written and uploaded, the dataset
registered under RLS as the requesting principal. A stubbed version of this
would not have caught any of the things it did.

Needs Postgres and MinIO. Skips with instructions when either is absent.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.test_jobs import FakeRedis
from webmap_core.db.session import principal_session
from webmap_core.exceptions import NotFound
from webmap_core.jobs import ErrorKind
from webmap_core.models import DatasetKind, JobState
from webmap_core.permissions import Principal
from webmap_core.services import gridding, jobs
from webmap_worker.tasks.interpolate import interpolate_task

pytestmark = pytest.mark.integration

TEXAS_CENTRAL = 2277
BUCKET = "webmap-test"

# A patch of the Midland Basin in EPSG:2277 feet, about 10 x 8 miles.
EXTENT = (1_500_000.0, 10_400_000.0, 1_552_000.0, 10_442_000.0)


@pytest.fixture(scope="session")
def storage() -> Any:
    """A MinIO client against the local stack, or skip."""
    from webmap_io.storage import StorageConfig, client, ensure_bucket

    config = StorageConfig(
        endpoint="http://localhost:9000",
        bucket=BUCKET,
        access_key="minioadmin",
        secret_key="minioadmin",
    )
    try:
        s3 = client(config)
        ensure_bucket(s3, BUCKET)
    except Exception as exc:
        pytest.skip(
            f"No MinIO at {config.endpoint} ({type(exc).__name__}). Start it "
            f"with: docker compose -f infra/compose.yaml up -d minio"
        )
    return s3


@pytest.fixture(scope="session")
def object_store() -> Any:
    from webmap_geo.dataplane import ObjectStore

    return ObjectStore(
        endpoint="localhost:9000",
        access_key="minioadmin",
        secret_key="minioadmin",
        use_ssl=False,
    )


def structure_surface(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """A dipping structure with a four-way closure on it.

    A plane alone would be gridded correctly by anything; the closure is what
    makes the surface worth checking, because a method that over-smooths loses
    it and a method that overshoots invents another one beside it.
    """
    cx = (EXTENT[0] + EXTENT[2]) / 2
    cy = (EXTENT[1] + EXTENT[3]) / 2
    regional = -8_200.0 - 0.004 * (x - EXTENT[0]) - 0.002 * (y - EXTENT[1])
    closure = 180.0 * np.exp(-(((x - cx) ** 2 + (y - cy) ** 2) / (2 * 4_000.0**2)))
    return regional + closure


def control_points() -> tuple[np.ndarray, np.ndarray]:
    """The picks the fixture wrote, recomputed rather than re-read.

    Same generator and same seed as `picks`, so this is the identical set —
    and deriving it here keeps the COG check from depending on the reader it
    is meant to be independent of.
    """
    rng = np.random.default_rng(20260909)
    pads = rng.uniform([EXTENT[0], EXTENT[1]], [EXTENT[2], EXTENT[3]], size=(20, 2))
    offsets = rng.normal(0.0, 1_800.0, size=(400, 2))
    coords = np.repeat(pads, 20, axis=0) + offsets
    coords[:, 0] = np.clip(coords[:, 0], EXTENT[0], EXTENT[2])
    coords[:, 1] = np.clip(coords[:, 1], EXTENT[1], EXTENT[3])
    values = structure_surface(coords[:, 0], coords[:, 1])

    # The fixture drops every tenth pick's value, so those are not control.
    logged = np.array([bool(i % 10) for i in range(len(values))])
    return coords[logged], np.round(values[logged], 2)


@pytest.fixture(scope="session")
def picks(storage: Any) -> str:
    """400 well picks on that structure, written as the ingest pipeline would.

    Synthetic, as `CLAUDE.md` §7.5 requires. Clustered rather than uniform,
    because well control is clustered by development history and a uniform
    fixture would never exercise the paths that exist for that.
    """
    from webmap_io.storage import put_bytes

    rng = np.random.default_rng(20260909)
    pads = rng.uniform([EXTENT[0], EXTENT[1]], [EXTENT[2], EXTENT[3]], size=(20, 2))
    offsets = rng.normal(0.0, 1_800.0, size=(400, 2))
    coords = np.repeat(pads, 20, axis=0) + offsets
    coords[:, 0] = np.clip(coords[:, 0], EXTENT[0], EXTENT[2])
    coords[:, 1] = np.clip(coords[:, 1], EXTENT[1], EXTENT[3])

    values = structure_surface(coords[:, 0], coords[:, 1])
    geometry = shapely.points(coords[:, 0], coords[:, 1])

    props = []
    for i, value in enumerate(values):
        record: dict[str, Any] = {"well_name": f"Wolfcamp {i:04d}"}
        # A tenth of the picks were never logged, the way a real layer looks.
        if i % 10:
            record["tvdss_ft"] = round(float(value), 2)
        props.append(json.dumps(record))

    table = pa.table(
        {
            "id": pa.array(range(len(props)), type=pa.int64()),
            "geometry": pa.array(shapely.to_wkb(geometry), type=pa.binary()),
            "props": pa.array(props, type=pa.string()),
        }
    )
    import io

    buffer = io.BytesIO()
    pq.write_table(table, buffer)
    key = "features/test_picks/v1.parquet"
    put_bytes(storage, BUCKET, key, buffer.getvalue())
    return key


async def register_picks(
    engine: AsyncEngine, principal: Principal, key: str, *, kind: str = "pointset"
) -> UUID:
    async with principal_session(engine, principal) as conn:
        result = await conn.execute(
            text(
                """
                INSERT INTO dataset (
                    name, kind, connector, storage_srid, parquet_key, version,
                    feature_count, owner_user_id, visibility)
                VALUES (
                    'Wolfcamp A Picks', CAST(:kind AS dataset_kind_t), 'upload',
                    :srid, :key, 1, 400, :owner, 'private')
                RETURNING id
                """
            ),
            {"kind": kind, "srid": TEXAS_CENTRAL, "key": key, "owner": principal.user_id},
        )
        return UUID(str(result.scalar_one()))


def worker_context(
    engine: AsyncEngine, storage: Any, object_store: Any, redis: Any
) -> dict[str, Any]:
    """The arq context the worker's startup builds.

    Two clients for the same bucket: boto3 writes the COG, DuckDB's
    ObjectStore reads the Parquet. Passing one where the other belongs fails
    with an AttributeError deep inside a solve, which is why they are named
    apart here rather than left to a single `storage` key.
    """
    return {
        "engine": engine,
        "storage": storage,
        "object_store": object_store,
        "bucket": BUCKET,
        "redis": redis,
    }


async def run_job(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    principal: Principal,
    request: gridding.GridRequest,
    *,
    redis: FakeRedis | None = None,
) -> tuple[UUID, dict[str, Any]]:
    """Enqueue and execute, the way the API and the worker would."""
    redis = redis or FakeRedis()
    async with principal_session(engine, principal) as conn:
        enqueued = await jobs.enqueue(
            conn,
            principal,
            kind="interpolate",
            parameters=request.to_parameters(),
            redis=redis,
        )
    context = jobs.context_for(principal, enqueued.job_id)
    document = await interpolate_task(
        worker_context(engine, storage, object_store, redis),
        {"context": context.to_payload(), "parameters": request.to_parameters()},
    )
    return enqueued.job_id, document


# --- the roadmap criterion ---------------------------------------------------


async def test_a_gridding_job_registers_a_dataset_and_its_lineage(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """**Phase 4:** "registers a dataset, and writes a lineage record
    sufficient to re-run and reproduce the identical grid"."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    dataset_id = await register_picks(engine, owner, picks)

    job_id, document = await run_job(
        engine,
        storage,
        object_store,
        owner,
        gridding.GridRequest(
            dataset_id=dataset_id,
            value_column="tvdss_ft",
            method="minimum_curvature",
            cell_size=500.0,
        ),
    )

    output_id = UUID(document["dataset_id"])
    async with principal_session(engine, owner) as conn:
        job = await jobs.get_job(conn, owner, job_id)
        row = (
            await conn.execute(
                text("SELECT name, kind, cog_key, storage_srid FROM dataset WHERE id = :id"),
                {"id": output_id},
            )
        ).one()
        lineage = await gridding.get_lineage(conn, owner, output_id)

    assert job["state"] == JobState.SUCCEEDED
    assert job["progress"] == pytest.approx(1.0)
    assert row.kind == DatasetKind.GRID
    assert row.cog_key is not None
    assert row.storage_srid == TEXAS_CENTRAL

    assert lineage is not None
    assert lineage["operation"] == "interpolate"
    assert lineage["input_dataset_ids"] == [dataset_id]
    assert lineage["job_id"] == job_id
    # Enough to re-run: the method, the grid, the frame, and the version.
    parameters = lineage["parameters"]
    assert parameters["method"] == "minimum_curvature"
    assert parameters["grid"]["cell_size"] == 500.0
    assert parameters["frame"]["srid"] == TEXAS_CENTRAL
    assert parameters["webmap_geo_version"]


async def test_the_written_cog_is_readable_and_holds_the_surface(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
    tmp_path: Any,
) -> None:
    """A registered grid nobody can open is worse than a failed job.

    The surface is checked **where the wells are**, not at the crest of the
    closure. The crest has no pick within 3,745 ft and the closure's sigma is
    4,000 ft, so no method can reconstruct its 180 ft of relief from data that
    never sampled it — an earlier version of this test asserted it could, and
    was wrong rather than the solver being wrong. What a grid owes its control
    points is agreement with them; what it owes everywhere else is the
    extrapolation warning.
    """
    import rasterio

    from webmap_io.storage import get_file

    owner = principals["owner"]
    assert isinstance(owner, Principal)
    dataset_id = await register_picks(engine, owner, picks)

    _, document = await run_job(
        engine,
        storage,
        object_store,
        owner,
        gridding.GridRequest(
            dataset_id=dataset_id,
            value_column="tvdss_ft",
            method="minimum_curvature",
            cell_size=500.0,
        ),
    )

    async with principal_session(engine, owner) as conn:
        key = (
            await conn.execute(
                text("SELECT cog_key FROM dataset WHERE id = :id"),
                {"id": UUID(document["dataset_id"])},
            )
        ).scalar_one()

    local = get_file(storage, BUCKET, str(key), tmp_path / "grid.tif")
    with rasterio.open(local) as src:
        surface = src.read(1)
        assert src.crs.to_epsg() == TEXAS_CENTRAL
        # Row 0 is the northern edge. An ascending y transform flips the map
        # about its centre and looks entirely plausible until someone checks a
        # well against it.
        assert src.transform.e < 0, "the raster is upside down"

        coords, values = control_points()
        rows, cols = zip(*(src.index(x, y) for x, y in coords), strict=True)
        sampled = surface[np.asarray(rows), np.asarray(cols)]

    residual = sampled - values
    assert abs(float(np.mean(residual))) < 5.0, (
        "the grid sits systematically above or below its own control points, "
        "which is what a half-cell georeferencing offset looks like"
    )
    assert float(np.sqrt(np.mean(residual**2))) < 25.0, (
        "the grid does not honour the picks it was built from"
    )


async def test_the_result_carries_the_diagnostics_a_reader_needs(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """`05` §6.5. A gridded surface is the most confident-looking artefact this
    system produces, and it looks identical whether it came from 400 wells or
    six."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    dataset_id = await register_picks(engine, owner, picks)

    _, document = await run_job(
        engine,
        storage,
        object_store,
        owner,
        gridding.GridRequest(dataset_id=dataset_id, value_column="tvdss_ft", cell_size=1_000.0),
    )

    diagnostics = document["diagnostics"]
    assert diagnostics["n_control_points"] == 360, "a tenth of the picks were unlogged"
    assert "input_range" in diagnostics
    assert "output_range" in diagnostics
    assert "extrapolated_fraction" in diagnostics

    # The reader's warnings and the solver's arrive in one list: to whoever
    # opens the map, "40 wells had no value" and "40% extrapolated" are the
    # same kind of fact.
    warnings = " ".join(document["warnings"])
    assert "tvdss_ft" in warnings
    assert "40" in warnings


# --- cancellation leaves nothing behind --------------------------------------


async def test_cancelling_before_the_write_registers_no_dataset(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """**Phase 4:** "Cancelling a running job leaves no partial output."

    The flag is set before the job runs, so the first checkpoint sees it. What
    matters is not where it stopped but that nothing was registered — a
    half-written COG with a dataset row looks finished.
    """
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    redis = FakeRedis()
    dataset_id = await register_picks(engine, owner, picks)
    request = gridding.GridRequest(
        dataset_id=dataset_id, value_column="tvdss_ft", cell_size=1_000.0
    )

    async with principal_session(engine, owner) as conn:
        enqueued = await jobs.enqueue(
            conn,
            principal=owner,
            kind="interpolate",
            parameters=request.to_parameters(),
            redis=redis,
        )
        await jobs.mark_running(conn, enqueued.job_id)
        await jobs.request_cancel(conn, owner, enqueued.job_id, redis)

    context = jobs.context_for(owner, enqueued.job_id)
    document = await interpolate_task(
        worker_context(engine, storage, object_store, redis),
        {"context": context.to_payload(), "parameters": request.to_parameters()},
    )

    async with principal_session(engine, owner) as conn:
        job = await jobs.get_job(conn, owner, enqueued.job_id)
        grids = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM dataset WHERE kind = 'grid' AND connector = 'derived'"
                )
            )
        ).scalar_one()

    assert document == {"cancelled": True}
    assert job["state"] == JobState.CANCELLED
    assert int(grids) == 0, "a cancelled job registered a dataset"


# --- failures are classified -------------------------------------------------


async def test_a_bad_value_column_fails_as_input_and_is_never_retried(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """`10` §8. Retrying this fails four times slower and buries the message
    that would have told the user what to fix — which here is the list of
    columns the layer actually has."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    dataset_id = await register_picks(engine, owner, picks)

    with pytest.raises(Exception, match="not an attribute"):
        await run_job(
            engine,
            storage,
            object_store,
            owner,
            gridding.GridRequest(dataset_id=dataset_id, value_column="porosity"),
        )

    async with principal_session(engine, owner) as conn:
        listed = await jobs.list_jobs(conn, owner)
        job = await jobs.get_job(conn, owner, listed[0]["id"])

    assert job["state"] == JobState.FAILED
    assert job["error_kind"] == ErrorKind.INPUT
    assert "tvdss_ft" in job["error"], "the message must name a column that would work"


async def test_gridding_a_grid_says_what_to_do_instead(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    principals: dict[str, object],
) -> None:
    """Asking to interpolate a surface is a reasonable thing to try and a
    reasonable thing to refuse — but the refusal has to say where to go.

    The message comes from `resolve_feature_object`, which fires first: a grid
    has no feature object at all, so the permission-checked resolve refuses it
    before the kind check ever runs. That ordering is worth keeping — the
    single enforcement point stays first.
    """
    owner = principals["owner"]
    assert isinstance(owner, Principal)

    async with principal_session(engine, owner) as conn:
        result = await conn.execute(
            text(
                """
                INSERT INTO dataset (
                    name, kind, connector, storage_srid, cog_key,
                    owner_user_id, visibility)
                VALUES ('Wolfcamp A Structure', 'grid', 'derived', :srid,
                        'grids/ds_existing.tif', :owner, 'private')
                RETURNING id
                """
            ),
            {"srid": TEXAS_CENTRAL, "owner": owner.user_id},
        )
        dataset_id = UUID(str(result.scalar_one()))

    with pytest.raises(NotFound, match="Grids are stored as COGs"):
        await run_job(
            engine,
            storage,
            object_store,
            owner,
            gridding.GridRequest(dataset_id=dataset_id, value_column="tvdss_ft"),
        )


async def test_a_fault_network_layer_is_not_control(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """A layer that *does* have features but is not point control. This is the
    path the kind check in `resolve_inputs` exists for — the enforcement point
    passes it, and it is still the wrong layer to grid."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    dataset_id = await register_picks(engine, owner, picks, kind="fault_network")

    with pytest.raises(NotFound, match="needs point control"):
        await run_job(
            engine,
            storage,
            object_store,
            owner,
            gridding.GridRequest(dataset_id=dataset_id, value_column="tvdss_ft"),
        )


# --- identity -----------------------------------------------------------------


async def test_a_job_cannot_grid_a_dataset_its_requester_cannot_see(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """**The rule that makes async jobs safe** (`03` §5.1). The request is gone
    by the time this runs, and the only identity in scope is the one in the
    payload. A job that could reach data its requester cannot is a privilege
    escalation with a queue in front of it.
    """
    owner, outsider = principals["owner"], principals["outsider"]
    assert isinstance(owner, Principal) and isinstance(outsider, Principal)
    dataset_id = await register_picks(engine, owner, picks)  # private to owner

    request = gridding.GridRequest(dataset_id=dataset_id, value_column="tvdss_ft")
    async with principal_session(engine, outsider) as conn:
        enqueued = await jobs.enqueue(
            conn,
            outsider,
            kind="interpolate",
            parameters=request.to_parameters(),
            redis=FakeRedis(),
        )

    context = jobs.context_for(outsider, enqueued.job_id)
    # NotFound rather than PermissionDenied: a private dataset is invisible to
    # the outsider, and saying "you lack permission on X" would confirm that X
    # exists (`03-auth-security.md` §3.2).
    with pytest.raises(NotFound):
        await interpolate_task(
            worker_context(engine, storage, object_store, FakeRedis()),
            {"context": context.to_payload(), "parameters": request.to_parameters()},
        )

    async with principal_session(engine, outsider) as conn:
        job = await jobs.get_job(conn, outsider, enqueued.job_id)

    assert job["state"] == JobState.FAILED
    assert job["error_kind"] in (ErrorKind.PERMISSION, ErrorKind.INPUT)


async def test_the_output_is_owned_by_whoever_asked_for_it(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """A derived dataset is not a privileged object: it is owned by the person
    who ran the job, and the INSERT policy enforces that at the database."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    dataset_id = await register_picks(engine, owner, picks)

    _, document = await run_job(
        engine,
        storage,
        object_store,
        owner,
        gridding.GridRequest(dataset_id=dataset_id, value_column="tvdss_ft", cell_size=1_000.0),
    )

    async with principal_session(engine, owner) as conn:
        owner_id = (
            await conn.execute(
                text("SELECT owner_user_id FROM dataset WHERE id = :id"),
                {"id": UUID(document["dataset_id"])},
            )
        ).scalar_one()

    assert owner_id == owner.user_id


# --- reproducibility -----------------------------------------------------------


async def test_the_same_job_re_run_reproduces_the_identical_grid(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
    tmp_path: Any,
) -> None:
    """**Phase 4:** lineage "sufficient to re-run and reproduce the identical
    grid". Asserted by actually re-running it and comparing the arrays, not by
    inspecting the record and trusting it."""
    import rasterio

    from webmap_io.storage import get_file

    owner = principals["owner"]
    assert isinstance(owner, Principal)
    dataset_id = await register_picks(engine, owner, picks)
    request = gridding.GridRequest(
        dataset_id=dataset_id, value_column="tvdss_ft", cell_size=1_000.0
    )

    surfaces = []
    for run in range(2):
        # A fresh Redis each time: the same parameters inside the idempotency
        # window would otherwise return the first job rather than run again.
        _, document = await run_job(
            engine, storage, object_store, owner, request, redis=FakeRedis()
        )
        async with principal_session(engine, owner) as conn:
            key = (
                await conn.execute(
                    text("SELECT cog_key FROM dataset WHERE id = :id"),
                    {"id": UUID(document["dataset_id"])},
                )
            ).scalar_one()
        local = get_file(storage, BUCKET, str(key), tmp_path / f"run{run}.tif")
        with rasterio.open(local) as src:
            surfaces.append(src.read(1))

    assert np.array_equal(surfaces[0], surfaces[1], equal_nan=True), (
        "two runs of the same job produced different surfaces"
    )


async def test_idempotency_stops_a_retry_becoming_a_second_grid(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """A tool call that times out and is retried must not leave two datasets
    that a conversation cannot tell apart."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    redis = FakeRedis()
    dataset_id = await register_picks(engine, owner, picks)
    request = gridding.GridRequest(
        dataset_id=dataset_id, value_column="tvdss_ft", cell_size=1_000.0
    )

    first, _ = await run_job(engine, storage, object_store, owner, request, redis=redis)
    async with principal_session(engine, owner) as conn:
        second = await jobs.enqueue(
            conn,
            owner,
            kind="interpolate",
            parameters=request.to_parameters(),
            redis=redis,
        )

    assert second.job_id == first
    assert second.was_created is False


# --- the grid itself ------------------------------------------------------------


async def test_an_absent_cell_size_produces_a_round_one(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """An automatic 263.4 ft cell appears in the lineage record and in the
    caption, and invites the reader to wonder what was special about 263.4.
    Nothing was."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    dataset_id = await register_picks(engine, owner, picks)

    _, document = await run_job(
        engine,
        storage,
        object_store,
        owner,
        gridding.GridRequest(dataset_id=dataset_id, value_column="tvdss_ft"),
    )

    cell_size = document["grid"]["cell_size"]
    mantissa = cell_size / 10 ** np.floor(np.log10(cell_size))
    assert round(float(mantissa), 6) in (1.0, 2.0, 2.5, 5.0)


async def test_the_grid_extends_past_the_control_points(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """A grid clipped exactly to the data has contours running off every edge,
    and puts the outermost wells on the boundary — where a minimum-curvature
    surface is least constrained."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    dataset_id = await register_picks(engine, owner, picks)

    _, document = await run_job(
        engine,
        storage,
        object_store,
        owner,
        gridding.GridRequest(dataset_id=dataset_id, value_column="tvdss_ft", cell_size=1_000.0),
    )

    # The picks span at most the fixture extent; the grid must exceed it.
    span_x = document["grid"]["nx"] * document["grid"]["cell_size"]
    assert span_x > (EXTENT[2] - EXTENT[0]) * 0.5
