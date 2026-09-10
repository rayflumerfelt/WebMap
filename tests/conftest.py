"""Fixtures for tests that need a real database.

The permission model has two layers and `03-auth-security.md` §3.1 requires
both. The application layer is tested exhaustively without a database
(`python/webmap_core/tests/test_permissions.py`); this file exists for the
other half, which cannot be faked — RLS either filters a real query on a real
Postgres or it does not.

Each session builds a throwaway database, applies the real migration, and
drops it. Not a hand-written schema: the point is to test the policies the
migration actually creates, so a policy that is wrong in the migration is
wrong here too.
"""

import json
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import numpy as np
import pytest
import pytest_asyncio
import shapely
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from webmap_core.db.session import principal_session
from webmap_core.permissions import Principal
from webmap_core.services import gridding, jobs

#: Superuser connection, used only to create and drop the test database.
#: Everything inside it runs as the same two roles production uses.
ADMIN_URL = os.environ.get(
    "WEBMAP_TEST_ADMIN_URL", "postgresql://postgres:postgres@localhost:5433/postgres"
)
TEST_DB = "webmap_test"


def _admin_dsn(database: str) -> str:
    return ADMIN_URL.rsplit("/", 1)[0] + f"/{database}"


def _postgres_available() -> str | None:
    """Return None if reachable, else why not.

    Integration tests skip rather than fail when the stack is down: a
    developer running the unit suite should not have to start Docker, and the
    skip reason has to say how to fix it or it just looks broken.
    """
    try:
        import psycopg
    except ImportError:  # pragma: no cover
        return "psycopg is not installed"
    try:
        with psycopg.connect(ADMIN_URL, connect_timeout=3):
            return None
    except Exception as exc:
        return (
            f"No Postgres at {ADMIN_URL.rsplit('@', 1)[-1]} ({type(exc).__name__}). "
            f"Start it with: docker compose -f infra/compose.yaml up -d postgres"
        )


@pytest.fixture(scope="session")
def database_urls() -> Iterator[tuple[str, str]]:
    """Create a migrated test database; yield (app_url, migration_url)."""
    unavailable = _postgres_available()
    if unavailable:
        pytest.skip(unavailable)

    import psycopg
    from alembic import command
    from alembic.config import Config

    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        # The two roles infra/docker/postgres-init.sql creates. Created here
        # too, idempotently, because CI's Postgres service has no init script
        # — without this the integration suite would silently skip in CI,
        # which is the one place it most needs to run.
        for role, extra in (("webmap_migrator", "CREATEROLE"), ("webmap_app", "NOBYPASSRLS")):
            exists = conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
            ).fetchone()
            if not exists:
                conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'webmap' {extra}")
        conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {TEST_DB} OWNER webmap_migrator")

    # The same grants infra/docker/postgres-init.sql applies to the real
    # database. Repeated rather than shared because a test database that
    # diverges from production's privileges tests the wrong thing.
    with psycopg.connect(_admin_dsn(TEST_DB), autocommit=True) as conn:
        conn.execute("ALTER SCHEMA public OWNER TO webmap_migrator")
        conn.execute("GRANT USAGE ON SCHEMA public TO webmap_app")
        conn.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE webmap_migrator IN SCHEMA public "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO webmap_app"
        )
        conn.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE webmap_migrator IN SCHEMA public "
            "GRANT USAGE, SELECT ON SEQUENCES TO webmap_app"
        )

    migration_url = f"postgresql+psycopg://webmap_migrator:webmap@localhost:5433/{TEST_DB}"
    app_url = f"postgresql+asyncpg://webmap_app:webmap@localhost:5433/{TEST_DB}"

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", migration_url)
    command.upgrade(config, "head")

    yield app_url, migration_url

    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")


@pytest_asyncio.fixture
async def engine(database_urls: tuple[str, str]) -> AsyncIterator[AsyncEngine]:
    """An engine connecting as the **application** role.

    Not the migrator. The migrator owns the tables and would be the wrong
    subject for an RLS test even with FORCE enabled, because it is also the
    role that could drop the policies.
    """
    from webmap_core.db.session import create_engine

    app_url, _ = database_urls
    eng = create_engine(app_url)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def migrator_engine(database_urls: tuple[str, str]) -> AsyncIterator[AsyncEngine]:
    """An engine as the privileged role, for arranging fixtures.

    Only for inserting `app_user` and `team` rows, which carry no policies.
    A test that reaches an *ownable* table through this engine is testing
    nothing — use `engine` and a principal.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    _, migration_url = database_urls
    eng = create_async_engine(migration_url.replace("+psycopg", "+asyncpg"))
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def clean_database(migrator_engine: AsyncEngine) -> AsyncIterator[None]:
    """Truncate between tests so each starts from a known state."""
    from sqlalchemy import text

    async with migrator_engine.begin() as conn:
        await conn.execute(
            text(
                # `adr/0010`'s tables belong here too. A basemap surviving into
                # the next test would make a delete refusal fire in a test that
                # never created a basemap, and the failure would point at the
                # wrong file.
                "TRUNCATE audit_event, lineage, dataset_version, fault_network, "
                "basemap_layer, basemap, layer, user_preferences, "
                "team_preferences, global_preferences, "
                "dataset, map_session, render, style_template, palette, project, "
                "access_grant, team_member, team, app_user RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest_asyncio.fixture
async def people(migrator_engine: AsyncEngine, clean_database: None) -> dict[str, UUID]:
    """Two users on one team, one on another, and an unaffiliated user.

    The shape the acceptance criteria need: `owner` and `teammate` share a
    team, `outsider` is on a different one, and `stranger` is on none — which
    separates "cannot see it because the team is wrong" from "cannot see it
    because there is no team at all".
    """
    from sqlalchemy import text

    ids: dict[str, UUID] = {
        "owner": uuid4(),
        "teammate": uuid4(),
        "outsider": uuid4(),
        "stranger": uuid4(),
        "owner_team": uuid4(),
        "other_team": uuid4(),
    }
    async with migrator_engine.begin() as conn:
        for key, email, name in [
            ("owner", "owner@example.test", "Olive Owner"),
            ("teammate", "teammate@example.test", "Tomas Teammate"),
            ("outsider", "outsider@example.test", "Otto Outsider"),
            ("stranger", "stranger@example.test", "Sam Stranger"),
        ]:
            await conn.execute(
                text(
                    "INSERT INTO app_user (id, subject, email, display_name) "
                    "VALUES (:id, :subject, :email, :name)"
                ),
                {"id": ids[key], "subject": f"test|{key}", "email": email, "name": name},
            )
        for key, slug, group in [
            ("owner_team", "owner-team", "group-owner"),
            ("other_team", "other-team", "group-other"),
        ]:
            await conn.execute(
                text(
                    "INSERT INTO team (id, slug, display_name, idp_group_id) "
                    "VALUES (:id, :slug, :slug, :group)"
                ),
                {"id": ids[key], "slug": slug, "group": group},
            )
        for user, team in [
            ("owner", "owner_team"),
            ("teammate", "owner_team"),
            ("outsider", "other_team"),
        ]:
            await conn.execute(
                text("INSERT INTO team_member (team_id, user_id) VALUES (:team, :user)"),
                {"team": ids[team], "user": ids[user]},
            )
    return ids


@pytest.fixture
def principals(people: dict[str, UUID]) -> dict[str, object]:
    from webmap_core.permissions import Channel, Principal

    return {
        "owner": Principal(people["owner"], frozenset({people["owner_team"]}), Channel.WEB),
        "teammate": Principal(
            people["teammate"], frozenset({people["owner_team"]}), Channel.WEB
        ),
        "outsider": Principal(
            people["outsider"], frozenset({people["other_team"]}), Channel.WEB
        ),
        "stranger": Principal(people["stranger"], frozenset(), Channel.WEB),
    }


async def raw_visible_dataset_ids(
    engine: AsyncEngine, principal: object, dataset_id: UUID
) -> list[UUID]:
    """Query `dataset` directly under a principal's RLS context.

    Deliberately bypasses every service function. This is how the *database*
    layer gets tested on its own: if the application check were the only thing
    working, this would still return the row.
    """
    from sqlalchemy import text

    from webmap_core.db.session import principal_session
    from webmap_core.permissions import Principal

    assert isinstance(principal, Principal)
    async with principal_session(engine, principal) as conn:
        result = await conn.execute(
            text("SELECT id FROM dataset WHERE id = :id"), {"id": dataset_id}
        )
        return [row.id for row in result]


async def insert_dataset(
    conn: AsyncConnection,
    *,
    owner_id: UUID,
    team_id: UUID | None,
    visibility: str,
    name: str = "Test Dataset",
) -> UUID:
    """Insert a dataset through the application role, under RLS."""
    from sqlalchemy import text

    result = await conn.execute(
        text(
            """
            INSERT INTO dataset (
                name, kind, connector, storage_srid, parquet_key,
                owner_user_id, owner_team_id, visibility)
            VALUES (:name, 'pointset', 'upload', 2277, 'features/test/v1.parquet',
                    :owner, :team, CAST(:visibility AS visibility_t))
            RETURNING id
            """
        ),
        {
            "name": name,
            "owner": owner_id,
            "team": team_id,
            "visibility": visibility,
        },
    )
    return UUID(str(result.scalar_one()))


TEXAS_CENTRAL = 2277
BUCKET = "webmap-test"

#: A patch of the Midland Basin in EPSG:2277 feet, about 10 x 8 miles.
EXTENT = (1_500_000.0, 10_400_000.0, 1_552_000.0, 10_442_000.0)


# --- the data plane ---------------------------------------------------------
#
# Shared by every test that goes through object storage. They live here rather
# than in one test module and being imported by the others: a fixture imported
# by name shadows the parameter of the same name in a helper's signature, and
# ruff is right to call that a redefinition.


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

    # **Written by the real writer, not by hand.** A hand-built Parquet has no
    # GeoParquet `geo` metadata, so DuckDB hands its geometry column back as a
    # BLOB rather than as GEOMETRY — and a reader that only ever saw the BLOB
    # form passes every unit test and fails on every ingested layer. That
    # happened; this fixture is why it will not happen twice.
    import tempfile

    from webmap_io.parquet import write_features

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "picks.parquet"
        write_features(
            path,
            geometry=np.asarray(geometry, dtype=object),
            props=[json.loads(record) for record in props],
            srid=TEXAS_CENTRAL,
        )
        payload = path.read_bytes()

    key = "features/test_picks/v1.parquet"
    put_bytes(storage, BUCKET, key, payload)
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
    redis: Any = None,
) -> tuple[UUID, dict[str, Any]]:
    """Enqueue and execute, the way the API and the worker would."""
    from tests.test_jobs import FakeRedis
    from webmap_worker.tasks.interpolate import interpolate_task

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
