"""Refreshing a dataset from a share, end to end. `11-file-io.md` §2.4.

The criterion this serves is Phase 6's — *"share-sourced datasets sync on
schedule and show `synced_at` in the UI"* — but the properties worth asserting
are the ones §2.4 calls out about what a geologist sees while it happens: never
a half-loaded layer, and never a stale one they believe is current.

Needs Postgres and MinIO. Skips with instructions when either is absent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import EXTENT, TEXAS_CENTRAL, worker_context
from webmap_core.db.session import principal_session
from webmap_core.models import SyncState
from webmap_core.permissions import Principal
from webmap_core.services import jobs, sync
from webmap_worker.tasks.sync import connector_for, sync_task

pytestmark = pytest.mark.integration


HEADER = "x,y,z\n"


def write_picks(path: Path, rows: int, *, offset: float = 0.0) -> None:
    """A tiny XYZ file on a 'share'. Synthetic, per `CLAUDE.md` §7.5.

    Inside `conftest.EXTENT`, which is where every other fixture in this repo
    puts its data. The first version invented its own coordinates and landed at
    21 degrees north — in Mexico, four hundred miles south of the Permian —
    which the WGS84 assertion below caught and nothing else would have.
    """
    west, south, east, north = EXTENT
    lines = [HEADER]
    for index in range(rows):
        fraction = index / max(rows - 1, 1)
        x = west + (east - west) * fraction
        y = south + (north - south) * fraction
        lines.append(f"{x},{y},{-9000.0 - index * 10.0 + offset}\n")
    path.write_text("".join(lines), encoding="utf-8")


@pytest.fixture
def share_root(tmp_path: Path) -> Path:
    root = tmp_path / "share" / "picks"
    root.mkdir(parents=True)
    write_picks(root / "wolfcamp.csv", 40)
    return root


async def register_share_dataset(
    engine: AsyncEngine, principal: Principal, *, checksum: str | None
) -> UUID:
    """A dataset that came off a share, as the ingest path would leave it."""
    async with principal_session(engine, principal) as conn:
        result = await conn.execute(
            text(
                """
                INSERT INTO dataset (
                    name, kind, connector, storage_srid, parquet_key, version,
                    feature_count, owner_user_id, visibility, source_uri,
                    source_checksum, sync_state, source_options)
                VALUES (
                    'Shared Wolfcamp picks', 'pointset', 'fileshare', :srid,
                    :key, 1, 40, :owner, 'private',
                    'share://picks/wolfcamp.csv', :checksum, 'ready',
                    CAST(:options AS jsonb))
                RETURNING id
                """
            ),
            {
                "srid": TEXAS_CENTRAL,
                "key": f"features/{uuid4()}/v1.parquet",
                "owner": principal.user_id,
                "checksum": checksum,
                # The mapping a person chose at ingest, which is what makes a
                # tabular source re-readable at all (`11` §2.4).
                "options": '{"x_column": "x", "y_column": "y", "z_column": "z"}',
            },
        )
        return UUID(str(result.scalar_one()))


async def run_sync(
    engine: AsyncEngine,
    storage: Any,
    object_store: Any,
    principal: Principal,
    request: sync.SyncRequest,
    share_root: Path,
) -> dict[str, Any]:
    """Enqueue and execute, the way the API and the worker would."""
    from uuid import uuid4 as _uuid4

    from tests.test_jobs import FakeRedis
    from webmap_io.connectors.fileshare import FileShareConnector, ShareConfig

    redis = FakeRedis()
    async with principal_session(engine, principal) as conn:
        enqueued = await jobs.enqueue(
            conn, principal, kind="sync", parameters=request.to_parameters(), redis=redis
        )
    context = jobs.context_for(principal, enqueued.job_id)

    ctx = worker_context(engine, storage, object_store, redis)
    shares = {"picks": ShareConfig(root=share_root, team_id=_uuid4())}

    # The worker builds its connector from deployment configuration; the test
    # substitutes a share map pointing at tmp_path rather than a mounted CIFS
    # volume, which is the one thing about this path that cannot be exercised
    # in a unit test.
    import webmap_worker.tasks.sync as sync_task_module

    original = sync_task_module.connector_for
    sync_task_module.connector_for = lambda kind, _ctx: (  # type: ignore[assignment]
        FileShareConnector(shares) if kind == "fileshare" else original(kind, _ctx)
    )
    try:
        return await sync_task(
            ctx, {"context": context.to_payload(), "parameters": request.to_parameters()}
        )
    finally:
        sync_task_module.connector_for = original


async def dataset_row(engine: AsyncEngine, principal: Principal, dataset_id: UUID) -> Any:
    async with principal_session(engine, principal) as conn:
        return (
            await conn.execute(
                text(
                    """
                    SELECT version, parquet_key, feature_count, sync_state,
                           synced_at, source_checksum, bbox_4326
                      FROM dataset WHERE id = :id
                    """
                ),
                {"id": dataset_id},
            )
        ).one()


class TestChangedSource:
    async def test_writes_a_new_version_and_moves_the_pointer(
        self,
        engine: AsyncEngine,
        storage: Any,
        object_store: Any,
        share_root: Path,
        principals: dict[str, object],
    ) -> None:
        """The pointer move is the commit (`adr/0005`). A new object under a new
        version, then one guarded update — so a render that started before the
        swap finishes against the object it started with."""
        owner = principals["owner"]
        assert isinstance(owner, Principal)
        dataset_id = await register_share_dataset(engine, owner, checksum=None)
        before = await dataset_row(engine, owner, dataset_id)

        document = await run_sync(
            engine,
            storage,
            object_store,
            owner,
            sync.SyncRequest(dataset_id=dataset_id),
            share_root,
        )
        after = await dataset_row(engine, owner, dataset_id)

        assert document["changed"] is True
        assert after.version == before.version + 1
        assert after.parquet_key != before.parquet_key
        assert after.feature_count == 40

    async def test_records_the_checksum_it_read(
        self,
        engine: AsyncEngine,
        storage: Any,
        object_store: Any,
        share_root: Path,
        principals: dict[str, object],
    ) -> None:
        """What makes "which copy of the share file is this?" answerable a year
        later, and what the next sync compares against."""
        owner = principals["owner"]
        assert isinstance(owner, Principal)
        dataset_id = await register_share_dataset(engine, owner, checksum=None)

        await run_sync(
            engine,
            storage,
            object_store,
            owner,
            sync.SyncRequest(dataset_id=dataset_id),
            share_root,
        )
        row = await dataset_row(engine, owner, dataset_id)

        from webmap_io.connectors.fileshare import checksum_of

        assert row.source_checksum == checksum_of(share_root / "wolfcamp.csv")
        assert row.sync_state == SyncState.READY
        assert row.synced_at is not None

    async def test_the_extent_follows_the_new_data(
        self,
        engine: AsyncEngine,
        storage: Any,
        object_store: Any,
        share_root: Path,
        principals: dict[str, object],
    ) -> None:
        """An extent left at the old file's is what makes "zoom to layer" land
        somewhere the data no longer is."""
        owner = principals["owner"]
        assert isinstance(owner, Principal)
        dataset_id = await register_share_dataset(engine, owner, checksum=None)

        await run_sync(
            engine,
            storage,
            object_store,
            owner,
            sync.SyncRequest(dataset_id=dataset_id),
            share_root,
        )
        row = await dataset_row(engine, owner, dataset_id)

        assert row.bbox_4326 is not None
        west, south, east, north = row.bbox_4326
        # West Texas: a bbox that came out as zeros, or in feet, fails here.
        assert -104.0 < west < -100.0
        assert 30.0 < south < 34.0
        assert east > west and north > south


class TestUnchangedSource:
    async def test_skips_the_read_and_still_records_the_check(
        self,
        engine: AsyncEngine,
        storage: Any,
        object_store: Any,
        share_root: Path,
        principals: dict[str, object],
    ) -> None:
        """ "Checked, unchanged" is a successful sync. The layer's tooltip should
        say when it was last *confirmed*, not when it last changed."""
        from webmap_io.connectors.fileshare import checksum_of

        owner = principals["owner"]
        assert isinstance(owner, Principal)
        current = checksum_of(share_root / "wolfcamp.csv")
        dataset_id = await register_share_dataset(engine, owner, checksum=current)

        document = await run_sync(
            engine,
            storage,
            object_store,
            owner,
            sync.SyncRequest(dataset_id=dataset_id),
            share_root,
        )
        row = await dataset_row(engine, owner, dataset_id)

        assert document["changed"] is False
        assert "unchanged" in document["reason"]
        assert row.version == 1
        assert row.synced_at is not None

    async def test_force_re_reads_anyway(
        self,
        engine: AsyncEngine,
        storage: Any,
        object_store: Any,
        share_root: Path,
        principals: dict[str, object],
    ) -> None:
        from webmap_io.connectors.fileshare import checksum_of

        owner = principals["owner"]
        assert isinstance(owner, Principal)
        current = checksum_of(share_root / "wolfcamp.csv")
        dataset_id = await register_share_dataset(engine, owner, checksum=current)

        document = await run_sync(
            engine,
            storage,
            object_store,
            owner,
            sync.SyncRequest(dataset_id=dataset_id, force=True),
            share_root,
        )

        assert document["changed"] is True


class TestFailure:
    async def test_a_vanished_source_marks_the_layer_stale_not_failed(
        self,
        engine: AsyncEngine,
        storage: Any,
        object_store: Any,
        share_root: Path,
        principals: dict[str, object],
    ) -> None:
        """Different sentences in the UI. The data is still readable and is
        simply no longer being refreshed, which is not the same as "the last
        sync blew up"."""
        owner = principals["owner"]
        assert isinstance(owner, Principal)
        dataset_id = await register_share_dataset(engine, owner, checksum=None)
        (share_root / "wolfcamp.csv").unlink()

        with pytest.raises(Exception, match="moved or renamed"):
            await run_sync(
                engine,
                storage,
                object_store,
                owner,
                sync.SyncRequest(dataset_id=dataset_id),
                share_root,
            )

        row = await dataset_row(engine, owner, dataset_id)
        assert row.sync_state == SyncState.STALE
        assert row.version == 1, "a failed sync must not advance the pointer"

    async def test_a_derived_dataset_has_nothing_to_sync_from(
        self,
        engine: AsyncEngine,
        storage: Any,
        object_store: Any,
        share_root: Path,
        principals: dict[str, object],
    ) -> None:
        """Uploads and derived datasets are immutable by design, and the
        message says which of the two this is rather than just refusing."""
        owner = principals["owner"]
        assert isinstance(owner, Principal)
        async with principal_session(engine, owner) as conn:
            dataset_id = UUID(
                str(
                    (
                        await conn.execute(
                            text(
                                """
                                INSERT INTO dataset (
                                    name, kind, connector, storage_srid,
                                    parquet_key, version, owner_user_id, visibility)
                                VALUES ('Derived contours', 'vector', 'derived',
                                        :srid, :key, 1, :owner, 'private')
                                RETURNING id
                                """
                            ),
                            {
                                "srid": TEXAS_CENTRAL,
                                "key": f"features/{uuid4()}/v1.parquet",
                                "owner": owner.user_id,
                            },
                        )
                    ).scalar_one()
                )
            )

        with pytest.raises(sync.SyncError, match="no source to sync from"):
            async with principal_session(engine, owner) as conn:
                await sync.resolve_source(conn, owner, sync.SyncRequest(dataset_id=dataset_id))


class TestConnectorSelection:
    def test_an_unconfigured_kind_says_what_to_do_instead(self) -> None:
        from webmap_io.connectors.base import ConnectorError

        with pytest.raises(ConnectorError, match="re-import the file"):
            connector_for("postgis", {"store": None, "shares": {}})
