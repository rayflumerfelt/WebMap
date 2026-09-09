"""Contouring a stored grid, end to end. `12-roadmap.md` Phase 4.

The criterion: *"contour intervals are round numbers a geologist would
choose."* That is a statement about reading rather than about arithmetic,
which is why most of these tests are about what ends up on the map — the
interval, the index contours, and whether the lines sit on the grid they came
from.

The grid under test is produced by a real gridding job, so this also proves
the two jobs compose: the COG one writes is one the other can read back.

Needs Postgres and MinIO. Skips with instructions when either is absent.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import numpy as np
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import (
    BUCKET,
    TEXAS_CENTRAL,
    register_picks,
    run_job,
    worker_context,
)
from webmap_core.db.session import principal_session
from webmap_core.exceptions import NotFound
from webmap_core.jobs import ErrorKind
from webmap_core.models import DatasetKind, GeometryKind, JobState
from webmap_core.permissions import Principal
from webmap_core.services import contours, gridding, jobs
from webmap_core.services.gridding import get_lineage
from webmap_geo.exceptions import DegenerateInput
from webmap_worker.tasks.contour import contour_task

pytestmark = pytest.mark.integration


async def make_grid(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    owner: Principal,
    picks_key: str,
) -> UUID:
    """A real gridded surface, produced by the gridding job."""
    dataset_id = await register_picks(engine, owner, picks_key)
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
    return UUID(document["dataset_id"])


async def run_contour(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    owner: Principal,
    request: contours.ContourRequest,
    *,
    redis: Any = None,
) -> tuple[UUID, dict[str, Any]]:
    from tests.test_jobs import FakeRedis

    redis = redis or FakeRedis()
    async with principal_session(engine, owner) as conn:
        enqueued = await jobs.enqueue(
            conn,
            owner,
            kind="contour",
            parameters=request.to_parameters(),
            redis=redis,
        )
    context = jobs.context_for(owner, enqueued.job_id)
    document = await contour_task(
        worker_context(engine, storage, object_store, redis),
        {"context": context.to_payload(), "parameters": request.to_parameters()},
    )
    return enqueued.job_id, document


# --- the roadmap criterion ---------------------------------------------------


async def test_the_interval_is_a_round_number(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """**Phase 4:** "contour intervals are round numbers a geologist would
    choose." A geologist reads the interval off the legend and holds it in
    their head; 137.4 ft is not something anyone holds."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    grid_id = await make_grid(engine, storage, object_store, owner, picks)

    job_id, document = await run_contour(
        engine, storage, object_store, owner, contours.ContourRequest(dataset_id=grid_id)
    )

    interval = document["interval"]
    mantissa = interval / 10 ** np.floor(np.log10(interval))
    assert round(float(mantissa), 6) in (1.0, 2.0, 2.5, 5.0), (
        f"interval {interval:g} is not a round number"
    )

    async with principal_session(engine, owner) as conn:
        job = await jobs.get_job(conn, owner, job_id)
    assert job["state"] == JobState.SUCCEEDED


async def test_the_lines_are_registered_as_a_line_layer_with_lineage(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    grid_id = await make_grid(engine, storage, object_store, owner, picks)

    _, document = await run_contour(
        engine, storage, object_store, owner, contours.ContourRequest(dataset_id=grid_id)
    )
    output_id = UUID(document["dataset_id"])

    async with principal_session(engine, owner) as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT kind, geometry_kind, parquet_key, feature_count, "
                    "storage_srid, caption FROM dataset WHERE id = :id"
                ),
                {"id": output_id},
            )
        ).one()
        lineage = await get_lineage(conn, owner, output_id)

    assert row.kind == DatasetKind.VECTOR
    assert row.geometry_kind == GeometryKind.LINESTRING
    assert row.parquet_key is not None
    assert row.feature_count == document["feature_count"]
    assert row.storage_srid == TEXAS_CENTRAL
    # The caption names the interval, which is the first thing anyone asks
    # about a contour map and the last thing a feature count tells them.
    assert f"{document['interval']:g}" in row.caption

    assert lineage is not None
    assert lineage["operation"] == "contour"
    assert lineage["input_dataset_ids"] == [grid_id]


async def test_every_fifth_contour_is_marked_as_an_index_contour(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """**The convention on every published structure map**, and the reason a
    dense one is readable at all. A layer that loses the flag is a field of
    identical hairlines."""
    from webmap_geo.attributes import feature_attributes

    owner = principals["owner"]
    assert isinstance(owner, Principal)
    grid_id = await make_grid(engine, storage, object_store, owner, picks)

    _, document = await run_contour(
        engine, storage, object_store, owner, contours.ContourRequest(dataset_id=grid_id)
    )

    async with principal_session(engine, owner) as conn:
        key = (
            await conn.execute(
                text("SELECT parquet_key FROM dataset WHERE id = :id"),
                {"id": UUID(document["dataset_id"])},
            )
        ).scalar_one()

    page = feature_attributes(f"s3://{BUCKET}/{key}", object_store, limit=5_000)
    indexed = {item["value"] for item in page.items if item["is_index"]}
    plain = {item["value"] for item in page.items if not item["is_index"]}

    assert indexed, "no contour was marked as an index contour"
    assert plain, "every contour was marked as an index contour"
    assert indexed == set(document["index_levels"])
    # Every fifth *level*, so the gap between index values is five intervals.
    if len(indexed) > 1:
        gaps = np.diff(sorted(indexed))
        assert np.allclose(gaps, 5 * document["interval"])


async def test_the_contours_sit_on_the_grid_they_came_from(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
    tmp_path: Any,
) -> None:
    """**The half-cell trap.** A GeoTIFF's origin is the outer edge of its
    first pixel and a GridDefinition's origin is a cell centre. Getting that
    wrong puts the contour set half a cell from the surface it describes,
    which is invisible on a smooth grid and wrong on every map made from it.
    """
    import rasterio
    import shapely

    from webmap_geo.dataplane import connect
    from webmap_io.storage import get_file

    owner = principals["owner"]
    assert isinstance(owner, Principal)
    grid_id = await make_grid(engine, storage, object_store, owner, picks)

    _, document = await run_contour(
        engine,
        storage,
        object_store,
        owner,
        contours.ContourRequest(dataset_id=grid_id, interval=25.0),
    )

    async with principal_session(engine, owner) as conn:
        cog_key = (
            await conn.execute(
                text("SELECT cog_key FROM dataset WHERE id = :id"), {"id": grid_id}
            )
        ).scalar_one()
        parquet_key = (
            await conn.execute(
                text("SELECT parquet_key FROM dataset WHERE id = :id"),
                {"id": UUID(document["dataset_id"])},
            )
        ).scalar_one()

    with connect(object_store) as duck:
        rows = duck.execute(
            "SELECT geometry, props FROM read_parquet($key) LIMIT 40",
            {"key": f"s3://{BUCKET}/{parquet_key}"},
        ).fetchall()

    local = get_file(storage, BUCKET, str(cog_key), tmp_path / "grid.tif")
    with rasterio.open(local) as src:
        surface = src.read(1)
        residuals = []
        for wkb, props in rows:
            import json

            value = json.loads(props)["value"]
            line = shapely.from_wkb(bytes(wkb))
            # Sample the surface along the contour. Where the line runs, the
            # grid must read its own label back.
            for x, y in list(line.coords)[::5]:
                row, col = src.index(x, y)
                if 0 <= row < surface.shape[0] and 0 <= col < surface.shape[1]:
                    residuals.append(float(surface[row, col]) - value)

    assert residuals
    # A quarter of the 25 ft interval. Larger than nearest-cell sampling
    # error, far smaller than the half-cell offset this test exists to catch.
    assert float(np.mean(np.abs(residuals))) < 6.25, (
        "the contours do not lie on the values they are labelled with"
    )


# --- level choice ------------------------------------------------------------


async def test_an_explicit_interval_is_honoured(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """A house interval beats an automatic one — matching the rest of a
    prospect's maps is the whole reason someone states it."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    grid_id = await make_grid(engine, storage, object_store, owner, picks)

    _, document = await run_contour(
        engine,
        storage,
        object_store,
        owner,
        contours.ContourRequest(dataset_id=grid_id, interval=20.0),
    )

    assert document["interval"] == pytest.approx(20.0)
    assert all(abs(level % 20.0) < 1e-6 for level in document["levels"])


async def test_explicit_levels_beat_everything(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """For matching a partner's map exactly, where the levels are whatever
    that map used and roundness is not the point."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    grid_id = await make_grid(engine, storage, object_store, owner, picks)

    wanted = [-8_400.0, -8_350.0, -8_300.0, -8_250.0]
    _, document = await run_contour(
        engine,
        storage,
        object_store,
        owner,
        contours.ContourRequest(dataset_id=grid_id, levels=wanted, interval=999.0),
    )

    assert document["levels"] == wanted


async def test_levels_that_all_miss_the_surface_say_so(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """A silently empty contour layer looks like a broken grid.

    Reached with explicit levels rather than a coarse interval: a coarse
    interval cannot miss a range that straddles zero, because zero itself is
    always one of its multiples. That is not a hypothetical here — minimum
    curvature's overshoot puts this TVDSS grid's maximum at about +2,700 ft
    against data that never leaves -8,452 to -8,224, which is exactly what
    the overshoot warning fires about.
    """
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    grid_id = await make_grid(engine, storage, object_store, owner, picks)

    with pytest.raises(DegenerateInput, match="No contours were produced"):
        await run_contour(
            engine,
            storage,
            object_store,
            owner,
            contours.ContourRequest(dataset_id=grid_id, levels=[50_000.0, 60_000.0]),
        )


def test_an_interval_coarser_than_the_surface_names_one_that_fits() -> None:
    """The `choose_levels` branch directly, on a depth surface that stays
    below sea level — which is what a TVDSS grid normally looks like, and
    where a too-coarse interval genuinely produces nothing."""
    surface = np.linspace(-8_450.0, -8_220.0, 400).reshape(20, 20)

    with pytest.raises(DegenerateInput, match="produces no contours"):
        contours.choose_levels(
            surface,
            contours.ContourRequest(dataset_id=uuid4(), interval=100_000.0),
        )


async def test_an_interval_far_too_fine_names_one_that_would_work(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """`CLAUDE.md` §8: name the limit, the offending value, and what to change
    it to. Twenty thousand hairlines is not a map anyone can read."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    grid_id = await make_grid(engine, storage, object_store, owner, picks)

    with pytest.raises(Exception, match="not a map anyone can read"):
        await run_contour(
            engine,
            storage,
            object_store,
            owner,
            contours.ContourRequest(dataset_id=grid_id, interval=0.02),
        )


# --- failures and identity ---------------------------------------------------


async def test_contouring_a_point_layer_says_to_grid_it_first(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    pointset_id = await register_picks(engine, owner, picks)

    with pytest.raises(Exception, match="resolve_feature_object"):
        await run_contour(
            engine,
            storage,
            object_store,
            owner,
            contours.ContourRequest(dataset_id=pointset_id),
        )


async def test_a_contour_job_cannot_read_a_grid_its_requester_cannot_see(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """The same rule as gridding (`03` §5.1): the only identity in scope is
    the one in the payload."""
    owner, outsider = principals["owner"], principals["outsider"]
    assert isinstance(owner, Principal) and isinstance(outsider, Principal)
    grid_id = await make_grid(engine, storage, object_store, owner, picks)

    async with principal_session(engine, owner) as conn:
        await conn.execute(
            text("UPDATE dataset SET visibility = 'private' WHERE id = :id"),
            {"id": grid_id},
        )

    # NotFound, not PermissionDenied: a private grid is invisible to the
    # outsider, and naming a permission on it would confirm it exists.
    with pytest.raises(NotFound):
        await run_contour(
            engine,
            storage,
            object_store,
            outsider,
            contours.ContourRequest(dataset_id=grid_id),
        )

    async with principal_session(engine, outsider) as conn:
        listed = await jobs.list_jobs(conn, outsider)
        job = await jobs.get_job(conn, outsider, listed[0]["id"])

    assert job["state"] == JobState.FAILED
    assert job["error_kind"] in (ErrorKind.PERMISSION, ErrorKind.INPUT)


async def test_cancelling_a_contour_job_registers_nothing(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    from tests.test_jobs import FakeRedis

    owner = principals["owner"]
    assert isinstance(owner, Principal)
    redis = FakeRedis()
    grid_id = await make_grid(engine, storage, object_store, owner, picks)
    request = contours.ContourRequest(dataset_id=grid_id)

    async with principal_session(engine, owner) as conn:
        enqueued = await jobs.enqueue(
            conn, owner, kind="contour", parameters=request.to_parameters(), redis=redis
        )
        await jobs.mark_running(conn, enqueued.job_id)
        await jobs.request_cancel(conn, owner, enqueued.job_id, redis)

    context = jobs.context_for(owner, enqueued.job_id)
    document = await contour_task(
        worker_context(engine, storage, object_store, redis),
        {"context": context.to_payload(), "parameters": request.to_parameters()},
    )

    async with principal_session(engine, owner) as conn:
        job = await jobs.get_job(conn, owner, enqueued.job_id)
        lines = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM dataset "
                    "WHERE geometry_kind = 'linestring' AND connector = 'derived'"
                )
            )
        ).scalar_one()

    assert document == {"cancelled": True}
    assert job["state"] == JobState.CANCELLED
    assert int(lines) == 0


# --- not contouring the same thing twice -------------------------------------


async def test_existing_contours_of_a_grid_are_findable(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """So a conversation can say "you already contoured this at 50 ft" rather
    than producing a second identical layer beside the first."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    grid_id = await make_grid(engine, storage, object_store, owner, picks)

    _, document = await run_contour(
        engine,
        storage,
        object_store,
        owner,
        contours.ContourRequest(dataset_id=grid_id, interval=50.0),
    )

    async with principal_session(engine, owner) as conn:
        existing = await contours.contours_of(conn, owner, grid_id)

    assert [record["id"] for record in existing] == [UUID(document["dataset_id"])]
    assert existing[0]["parameters"]["interval"] == pytest.approx(50.0)


def test_the_quoted_interval_is_the_median_gap() -> None:
    """An explicit level list need not be evenly spaced. Quoting its first gap
    as "the interval" would put a number on the legend that most of the map
    does not follow."""
    assert contours.interval_of(np.array([0.0, 10.0, 20.0, 30.0])) == pytest.approx(10.0)
    assert contours.interval_of(np.array([0.0, 1.0, 11.0, 21.0])) == pytest.approx(10.0)
    assert contours.interval_of(np.array([5.0])) == 0.0


# --- filled bands ------------------------------------------------------------


async def test_filling_produces_a_polygon_layer_beside_the_lines(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """`08` §5.2. A colour-filled grid renders these bands; only this produces
    them, and the difference is whether anything can be measured or exported."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    grid_id = await make_grid(engine, storage, object_store, owner, picks)

    _, document = await run_contour(
        engine,
        storage,
        object_store,
        owner,
        contours.ContourRequest(dataset_id=grid_id, fill=True),
    )

    assert "band_dataset_id" in document, "fill=True produced no bands"
    band_id = UUID(document["band_dataset_id"])
    assert band_id != UUID(document["dataset_id"]), "bands and lines are one layer"

    async with principal_session(engine, owner) as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT kind, geometry_kind, feature_count, attribute_schema, caption "
                    "FROM dataset WHERE id = :id"
                ),
                {"id": band_id},
            )
        ).one()

    assert row.kind == DatasetKind.VECTOR
    assert row.geometry_kind == GeometryKind.POLYGON
    assert row.feature_count == document["band_count"] > 0
    assert {field["name"] for field in row.attribute_schema} == {
        "lower",
        "upper",
        "midpoint",
        "is_open_ended",
        "area",
    }
    assert "filled bands" in row.caption


async def test_bands_are_not_produced_unless_asked_for(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """A second layer appearing in the tree unbidden is worse than no feature:
    nobody knows where it came from and everybody deletes it."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    grid_id = await make_grid(engine, storage, object_store, owner, picks)

    _, document = await run_contour(
        engine, storage, object_store, owner, contours.ContourRequest(dataset_id=grid_id)
    )

    assert "band_dataset_id" not in document


async def test_the_bands_record_lineage_to_the_grid_they_filled(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """A derived dataset that cannot say what made it cannot be reproduced,
    and `CLAUDE.md` §3.3 makes that non-negotiable."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    grid_id = await make_grid(engine, storage, object_store, owner, picks)

    _, document = await run_contour(
        engine,
        storage,
        object_store,
        owner,
        contours.ContourRequest(dataset_id=grid_id, fill=True, interval=50.0),
    )

    async with principal_session(engine, owner) as conn:
        lineage = await get_lineage(conn, owner, UUID(document["band_dataset_id"]))

    assert lineage is not None, "the band layer has no lineage record"
    assert lineage["operation"] == "contour_bands"
    assert lineage["input_dataset_ids"] == [grid_id]
    assert lineage["parameters"]["interval"] == pytest.approx(50.0)


async def test_the_bands_and_the_lines_share_one_level_list(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """The whole reason they are produced together. Two calls could be given
    different levels, and then the fill edges wander across the contours."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    grid_id = await make_grid(engine, storage, object_store, owner, picks)

    _, document = await run_contour(
        engine,
        storage,
        object_store,
        owner,
        contours.ContourRequest(dataset_id=grid_id, fill=True),
    )

    async with principal_session(engine, owner) as conn:
        lines = await get_lineage(conn, owner, UUID(document["dataset_id"]))
        bands = await get_lineage(conn, owner, UUID(document["band_dataset_id"]))

    assert lines is not None and bands is not None
    assert bands["parameters"]["levels"] == lines["parameters"]["levels"]


async def test_a_filled_job_reports_progress_through_its_own_phases(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """Filling is a separate job kind because the phase weights must sum to
    1.0 either way. A skipped phase would leave the bar short of the end,
    which reads as a job that stalled."""
    from webmap_worker.progress import PHASES

    assert sum(weight for _, weight in PHASES["contour_filled"]) == pytest.approx(1.0)
    assert "Filling bands" in [name for name, _ in PHASES["contour_filled"]]
    assert "Filling bands" not in [name for name, _ in PHASES["contour"]]
