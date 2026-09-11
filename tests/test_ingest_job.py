"""Ingesting a large upload through the queue. `10-jobs-async.md` §6.

The inline path is `tests/test_ingest.py`. This is the other half of §6's
conditional async: the same read, run on the worker because doing it in the
request would block an API process for longer than §6 permits.

The property that matters is that the two paths produce the *same dataset*. A
second ingest implementation that drifted from the first would mean a layer
imported at 30 MB and the same layer at 40 MB differing in ways nobody could
explain.

Needs Postgres and MinIO. Skips with instructions when either is absent.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import BUCKET, EXTENT, TEXAS_CENTRAL, worker_context
from webmap_core.db.session import principal_session
from webmap_core.models import Visibility
from webmap_core.permissions import Principal
from webmap_core.services import jobs
from webmap_worker.tasks.ingest import ingest_task, options_from

pytestmark = pytest.mark.integration


def picks_csv(path: Path, rows: int = 50) -> Path:
    """Synthetic picks inside the repo's own extent (`CLAUDE.md` §7.5)."""
    west, south, east, north = EXTENT
    lines = ["x,y,z\n"]
    for index in range(rows):
        fraction = index / max(rows - 1, 1)
        lines.append(
            f"{west + (east - west) * fraction},"
            f"{south + (north - south) * fraction},"
            f"{-9000.0 - index * 5.0}\n"
        )
    path.write_text("".join(lines), encoding="utf-8")
    return path


def spool(storage: Any, path: Path) -> str:
    """Put the upload where the worker can reach it, as the route does."""
    from webmap_io.storage import put_file

    key = f"uploads/{uuid4()}/{path.name}"
    put_file(storage, BUCKET, key, path)
    return key


def parameters_for(key: str, name: str = "Queued picks") -> dict[str, Any]:
    return {
        "object_key": key,
        "name": name,
        "project_id": None,
        "visibility": Visibility.PRIVATE.value,
        "owner_team_id": None,
        "kind": None,
        "description": None,
        "srid_override": TEXAS_CENTRAL,
        "encoding": None,
        "x_column": "x",
        "y_column": "y",
        "z_column": "z",
    }


async def run_ingest(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    principal: Principal,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    from tests.test_jobs import FakeRedis

    redis = FakeRedis()
    async with principal_session(engine, principal) as conn:
        enqueued = await jobs.enqueue(
            conn, principal, kind="ingest", parameters=parameters, redis=redis
        )
    context = jobs.context_for(principal, enqueued.job_id)
    return await ingest_task(
        worker_context(engine, storage, object_store, redis),
        {"context": context.to_payload(), "parameters": parameters},
    )


class TestQueuedIngest:
    async def test_registers_the_dataset(
        self,
        engine: AsyncEngine,
        storage: Any,
        object_store: Any,
        tmp_path: Path,
        principals: dict[str, object],
    ) -> None:
        owner = principals["owner"]
        assert isinstance(owner, Principal)
        key = spool(storage, picks_csv(tmp_path / "picks.csv"))

        document = await run_ingest(engine, storage, object_store, owner, parameters_for(key))

        assert document["feature_count"] == 50
        assert document["srid"] == TEXAS_CENTRAL
        async with principal_session(engine, owner) as conn:
            name = (
                await conn.execute(
                    text("SELECT name FROM dataset WHERE id = :id"),
                    {"id": document["dataset_id"]},
                )
            ).scalar_one()
        assert name == "Queued picks"

    async def test_keeps_the_column_mapping_for_a_later_sync(
        self,
        engine: AsyncEngine,
        storage: Any,
        object_store: Any,
        tmp_path: Path,
        principals: dict[str, object],
    ) -> None:
        """`11` §2.4: a tabular dataset with no recorded mapping can never be
        refreshed, because §3.1 forbids sniffing which column is X."""
        owner = principals["owner"]
        assert isinstance(owner, Principal)
        key = spool(storage, picks_csv(tmp_path / "picks.csv"))

        document = await run_ingest(engine, storage, object_store, owner, parameters_for(key))

        async with principal_session(engine, owner) as conn:
            options = (
                await conn.execute(
                    text("SELECT source_options FROM dataset WHERE id = :id"),
                    {"id": document["dataset_id"]},
                )
            ).scalar_one()
        assert options["x_column"] == "x"
        assert options["z_column"] == "z"

    async def test_matches_what_the_inline_path_produces(
        self,
        engine: AsyncEngine,
        storage: Any,
        object_store: Any,
        tmp_path: Path,
        principals: dict[str, object],
    ) -> None:
        """The property that makes two code paths acceptable: a layer imported
        at 30 MB and the same layer at 40 MB must not differ in ways nobody can
        explain."""
        from webmap_core.services.ingest import ingest_upload

        owner = principals["owner"]
        assert isinstance(owner, Principal)
        source = picks_csv(tmp_path / "picks.csv")
        key = spool(storage, source)

        queued = await run_ingest(engine, storage, object_store, owner, parameters_for(key))

        with tempfile.TemporaryDirectory() as workdir:
            local = picks_csv(Path(workdir) / "picks.csv")
            async with principal_session(engine, owner) as conn:
                inline = await ingest_upload(
                    conn,
                    owner,
                    local,
                    options_from(parameters_for(key, name="Inline picks")),
                    storage=storage,
                    bucket=BUCKET,
                )

        assert queued["feature_count"] == inline.feature_count
        assert queued["srid"] == inline.srid
        assert queued["geometry_kind"] == inline.geometry_kind.value
        assert queued["bbox_4326"] == pytest.approx(inline.bbox_4326)

    async def test_a_failed_read_registers_nothing(
        self,
        engine: AsyncEngine,
        storage: Any,
        object_store: Any,
        tmp_path: Path,
        principals: dict[str, object],
    ) -> None:
        """Phase 4's criterion from the other side: registration is the last
        thing that happens, so a failure leaves an orphaned object rather than a
        half-registered layer."""
        owner = principals["owner"]
        assert isinstance(owner, Principal)
        broken = tmp_path / "broken.csv"
        broken.write_text("not,a,pick\n", encoding="utf-8")
        key = spool(storage, broken)

        async with principal_session(engine, owner) as conn:
            before = (await conn.execute(text("SELECT count(*) FROM dataset"))).scalar_one()

        with pytest.raises(Exception):
            await run_ingest(
                engine,
                storage,
                object_store,
                owner,
                parameters_for(key, name="Broken picks"),
            )

        async with principal_session(engine, owner) as conn:
            after = (await conn.execute(text("SELECT count(*) FROM dataset"))).scalar_one()
        assert after == before


class TestOptions:
    def test_rebuilds_what_the_caller_stated(self) -> None:
        """Stated, never inferred. A payload that lost the CRS or the column
        mapping would put the pipeline back to guessing."""
        options = options_from(parameters_for("uploads/x/picks.csv"))

        assert options.srid_override == TEXAS_CENTRAL
        assert (options.x_column, options.y_column, options.z_column) == ("x", "y", "z")
        assert options.visibility == Visibility.PRIVATE
