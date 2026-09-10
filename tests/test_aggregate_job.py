"""Spatial aggregation end to end. `05-geoprocessing.md` §8, `12-roadmap.md` Phase 4.

The catalog itself is unit-tested against closed forms in
`python/webmap_geo/tests/test_aggregate.py`. What this file covers is
everything around it that unit tests cannot reach: that both operands are
permission-checked, that lineage names every parent, that the registered
geometry kind matches what the operation actually produced, and that the two
storage clients are not confused for one another.

That last one is not hypothetical. The first run of this job passed the boto3
client where DuckDB's httpfs configuration belongs and failed with
`AttributeError: 'S3' object has no attribute 'endpoint'` — the exact symptom
`gridding.load_control`'s docstring warns about, from inside the read.

Needs Postgres and MinIO. Skips with instructions when either is absent.
"""

from __future__ import annotations

import contextlib
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import register_picks, worker_context
from webmap_core.db.session import principal_session
from webmap_core.jobs import ErrorKind
from webmap_core.models import DatasetKind, GeometryKind, JobState
from webmap_core.permissions import Principal
from webmap_core.services import aggregation, jobs
from webmap_core.services.gridding import get_lineage
from webmap_worker.tasks.aggregate import aggregate_task

pytestmark = pytest.mark.integration


async def run_aggregate(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    owner: Principal,
    request: aggregation.AggregateRequest,
) -> tuple[UUID, dict[str, Any]]:
    from tests.test_jobs import FakeRedis

    redis = FakeRedis()
    async with principal_session(engine, owner) as conn:
        enqueued = await jobs.enqueue(
            conn,
            owner,
            kind="aggregate",
            parameters=request.to_parameters(),
            redis=redis,
        )
    context = jobs.context_for(owner, enqueued.job_id)
    document = await aggregate_task(
        worker_context(engine, storage, object_store, redis),
        {"context": context.to_payload(), "parameters": request.to_parameters()},
    )
    return enqueued.job_id, document


# --- the operation runs and registers a layer ---------------------------------


async def test_a_buffer_registers_a_polygon_layer_with_lineage(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    source = await register_picks(engine, owner, picks)

    job_id, document = await run_aggregate(
        engine,
        storage,
        object_store,
        owner,
        aggregation.AggregateRequest(
            op="buffer", dataset_ids=[source], params={"distance": 500.0}
        ),
    )
    output = UUID(document["dataset_id"])

    async with principal_session(engine, owner) as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT kind, geometry_kind, parquet_key, feature_count, caption "
                    "FROM dataset WHERE id = :id"
                ),
                {"id": output},
            )
        ).one()
        lineage = await get_lineage(conn, owner, output)
        job = await jobs.get_job(conn, owner, job_id)

    assert job["state"] == JobState.SUCCEEDED
    assert row.kind == DatasetKind.VECTOR
    assert row.parquet_key is not None
    assert row.feature_count == document["feature_count"] > 0
    assert "buffer" in row.caption

    assert lineage is not None
    assert lineage["operation"] == "aggregate.buffer"
    assert lineage["input_dataset_ids"] == [source]
    assert lineage["parameters"]["params"]["distance"] == pytest.approx(500.0)


async def test_the_registered_geometry_kind_is_what_the_operation_produced(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """**Read off the output, never assumed from the input.** `centroid` turns
    polygons into points and `buffer` turns points into polygons; registering
    the input's kind mislabels the layer and breaks the symbology the frontend
    picks for it — silently, because the geometry itself is fine."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    source = await register_picks(engine, owner, picks)

    _, buffered = await run_aggregate(
        engine,
        storage,
        object_store,
        owner,
        aggregation.AggregateRequest(
            op="buffer", dataset_ids=[source], params={"distance": 500.0}
        ),
    )

    assert buffered["geometry_kind"] == GeometryKind.POLYGON.value, (
        "a buffer of points is polygons"
    )

    async with principal_session(engine, owner) as conn:
        row = (
            await conn.execute(
                text("SELECT geometry_kind FROM dataset WHERE id = :id"),
                {"id": UUID(buffered["dataset_id"])},
            )
        ).one()
    assert row.geometry_kind == GeometryKind.POLYGON


async def test_a_two_layer_overlay_records_both_parents(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """A derived layer that names only the first parent cannot be reproduced,
    which is the whole point of the record (`CLAUDE.md` §3.3)."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    points = await register_picks(engine, owner, picks)

    # A hull of the same points makes a mask guaranteed to overlap them.
    _, hull = await run_aggregate(
        engine,
        storage,
        object_store,
        owner,
        aggregation.AggregateRequest(op="convex_hull", dataset_ids=[points]),
    )
    mask = UUID(hull["dataset_id"])

    _, clipped = await run_aggregate(
        engine,
        storage,
        object_store,
        owner,
        aggregation.AggregateRequest(op="clip", dataset_ids=[points, mask]),
    )

    async with principal_session(engine, owner) as conn:
        lineage = await get_lineage(conn, owner, UUID(clipped["dataset_id"]))

    assert lineage is not None
    assert lineage["input_dataset_ids"] == [points, mask]


# --- permissions ----------------------------------------------------------------


async def test_every_operand_is_permission_checked_not_just_the_first(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """**The check this job exists to get right.** An overlay that validated
    only the left operand would let a caller read a layer they cannot see
    through the geometry of one they can — the output would carry the shape of
    the private layer even though its attributes never appeared."""
    owner = principals["owner"]
    outsider = principals["outsider"]
    assert isinstance(owner, Principal) and isinstance(outsider, Principal)

    mine = await register_picks(engine, outsider, picks)
    theirs = await register_picks(engine, owner, picks)

    from webmap_core.exceptions import NotFound

    # The outsider owns the *left* operand, so a job that checked only the
    # first input would sail past this and read the owner's layer.
    with pytest.raises(NotFound) as excinfo:
        await run_aggregate(
            engine,
            storage,
            object_store,
            outsider,
            aggregation.AggregateRequest(op="clip", dataset_ids=[mine, theirs]),
        )

    # `NotFound`, not `PermissionDenied`, and deliberately so: saying "you lack
    # permission" would confirm the layer exists. The message offers the next
    # move instead of the fact.
    assert "that you can access" in str(excinfo.value)
    assert str(theirs) in str(excinfo.value)

    # **The error kind must not leak what the message refuses to.** `classify`
    # maps NotFound to INPUT rather than PERMISSION; a machine-readable
    # PERMISSION here would confirm the layer exists and belongs to someone
    # else. Both kinds are non-retryable, so nothing behavioural is lost.
    async with principal_session(engine, outsider) as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT error_kind FROM job WHERE kind = 'aggregate' "
                    "ORDER BY queued_at DESC LIMIT 1"
                )
            )
        ).one()
    assert row.error_kind == ErrorKind.INPUT, "the kind would otherwise leak existence"


# --- refusals --------------------------------------------------------------------


async def test_an_unknown_operation_lists_the_ones_that_exist(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """`CLAUDE.md` §8. A conversation that guessed a name needs the list, not
    "invalid operation"."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    source = await register_picks(engine, owner, picks)

    with pytest.raises(Exception) as excinfo:
        await run_aggregate(
            engine,
            storage,
            object_store,
            owner,
            aggregation.AggregateRequest(op="smoosh", dataset_ids=[source]),
        )

    assert "dissolve" in str(excinfo.value), "the refusal should name the catalog"


async def test_a_failed_aggregation_is_classified_as_bad_input(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """The classification decides whether a caller should retry or change
    something. A degenerate parameter is theirs to fix, not a transient."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    source = await register_picks(engine, owner, picks)

    # The task re-raises after recording the failure, which is how arq learns
    # not to retry it. The row is what this test is about.
    with contextlib.suppress(Exception):
        await run_aggregate(
            engine,
            storage,
            object_store,
            owner,
            aggregation.AggregateRequest(
                op="buffer", dataset_ids=[source], params={"distance": 0}
            ),
        )

    async with principal_session(engine, owner) as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT state, error_kind, error FROM job "
                    "WHERE kind = 'aggregate' ORDER BY queued_at DESC LIMIT 1"
                )
            )
        ).one()

    assert rows.state == JobState.FAILED
    assert rows.error_kind == ErrorKind.INPUT
    assert "distance of 0" in rows.error


async def test_layers_in_different_coordinate_systems_are_refused(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    picks: str,
    principals: dict[str, object],
) -> None:
    """**Not an empty result.** Coordinates in different frames do not
    overlap, so an unguarded overlay returns nothing and reads as "these
    layers do not touch"."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    here = await register_picks(engine, owner, picks)

    async with principal_session(engine, owner) as conn:
        elsewhere = UUID(
            str(
                (
                    await conn.execute(
                        text(
                            """
                            INSERT INTO dataset (
                                name, kind, connector, storage_srid, parquet_key,
                                version, feature_count, owner_user_id, visibility)
                            VALUES ('Elsewhere', 'pointset', 'upload', 32613, :key,
                                    1, 10, :owner, 'private')
                            RETURNING id
                            """
                        ),
                        {"key": picks, "owner": owner.user_id},
                    )
                ).scalar_one()
            )
        )

    with pytest.raises(Exception) as excinfo:
        await run_aggregate(
            engine,
            storage,
            object_store,
            owner,
            aggregation.AggregateRequest(op="clip", dataset_ids=[here, elsewhere]),
        )

    assert "different coordinate systems" in str(excinfo.value)


# --- progress -------------------------------------------------------------------


def test_the_aggregate_phase_weights_sum_to_one() -> None:
    """A phase set that does not sum to 1.0 leaves the bar short of the end,
    which reads as a job that stalled."""
    from webmap_worker.progress import PHASES

    assert sum(weight for _, weight in PHASES["aggregate"]) == pytest.approx(1.0)
