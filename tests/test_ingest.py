"""Ingest, end to end. `11-file-io.md` §6.

Two Phase 1 acceptance criteria live here:

- *"Uploading a shapefile with a .prj registers a dataset with correct CRS and
  bbox"*
- *"Uploading a shapefile without a .prj fails with the message from
  11-file-io.md §3"*

Both go through the real pipeline — read, write GeoParquet, upload the object,
insert the row under RLS as the uploading principal — so what is verified is
the path a geologist takes, not a stubbed version of it.

Needs Postgres and MinIO. Skips with instructions when either is absent.
"""

import zipfile
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.fixtures.build import build_all
from webmap_api.db.session import principal_session
from webmap_core.models import DatasetKind, GeometryKind
from webmap_core.permissions import Principal
from webmap_core.services import datasets as service
from webmap_core.services.ingest import IngestOptions, ingest_upload, preview
from webmap_io.exceptions import MissingCRS, UnsupportedFormat

pytestmark = pytest.mark.integration

TEXAS_CENTRAL = 2277
BUCKET = "webmap-test"


@pytest.fixture(scope="session")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return build_all(tmp_path_factory.mktemp("ingest-fixtures"))


@pytest.fixture(scope="session")
def storage() -> object:
    """A MinIO client against the local stack, or skip.

    A dedicated bucket rather than the application one, so a test run never
    leaves objects among real data.
    """
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


def zip_shapefile(shp: Path, destination: Path, *, include_prj: bool = True) -> Path:
    """Zip a shapefile and its sidecars, the way an upload actually arrives."""
    archive = destination / f"{shp.stem}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for suffix in (".shp", ".shx", ".dbf", ".cpg", ".prj"):
            if suffix == ".prj" and not include_prj:
                continue
            part = shp.with_suffix(suffix)
            if part.exists():
                zf.write(part, part.name)
    return archive


# --- the criteria -----------------------------------------------------------


async def test_a_shapefile_with_a_prj_registers_with_correct_crs_and_bbox(
    engine: AsyncEngine,
    people: dict[str, UUID],
    principals: dict[str, Principal],
    fixtures: dict[str, Path],
    storage: object,
    tmp_path: Path,
) -> None:
    """The Phase 1 acceptance criterion, end to end."""
    archive = zip_shapefile(fixtures["valid_points"], tmp_path)

    async with principal_session(engine, principals["owner"]) as conn:
        result = await ingest_upload(
            conn,
            principals["owner"],
            archive,
            IngestOptions(name="Wolfcamp A Picks", owner_team_id=people["owner_team"]),
            storage=storage,
            bucket=BUCKET,
        )

    assert result.srid == TEXAS_CENTRAL
    assert result.feature_count == 50
    assert result.kind is DatasetKind.POINTSET
    assert result.geometry_kind is GeometryKind.POINT

    # The bbox is in EPSG:4326 and must land in West Texas. This is the
    # assertion that would have caught the seed being 1,200 km south.
    west, south, east, north = result.bbox_4326
    assert -104 < west < -100 and -104 < east < -100
    assert 30.5 < south < 33.5 and 30.5 < north < 33.5

    # And it is readable back through the ordinary service path, as the owner.
    async with principal_session(engine, principals["owner"]) as conn:
        detail = await service.get_dataset(conn, principals["owner"], result.dataset_id)
    assert detail["storage_srid"] == TEXAS_CENTRAL
    assert detail["feature_count"] == 50
    assert detail["connector"] == "upload"


async def test_a_shapefile_without_a_prj_fails_with_the_documented_message(
    engine: AsyncEngine,
    people: dict[str, UUID],
    principals: dict[str, Principal],
    fixtures: dict[str, Path],
    storage: object,
    tmp_path: Path,
) -> None:
    """The other criterion, and the one that matters more.

    A guessed CRS produces a layer that renders, projects, and interpolates
    without complaint, in the wrong place.
    """
    archive = zip_shapefile(fixtures["valid_points"], tmp_path, include_prj=False)

    async with principal_session(engine, principals["owner"]) as conn:
        with pytest.raises(MissingCRS) as excinfo:
            await ingest_upload(
                conn,
                principals["owner"],
                archive,
                IngestOptions(name="No CRS"),
                storage=storage,
                bucket=BUCKET,
            )

    message = str(excinfo.value)
    assert "no coordinate reference system" in message
    assert ".prj" in message
    assert "specify the CRS explicitly" in message


async def test_nothing_is_registered_when_ingest_fails(
    engine: AsyncEngine,
    people: dict[str, UUID],
    principals: dict[str, Principal],
    fixtures: dict[str, Path],
    storage: object,
    tmp_path: Path,
) -> None:
    """A failed import leaves no half-registered dataset.

    The registry row is written last, after the object is in storage, so a
    read failure cannot produce a row pointing at nothing. An orphaned object
    is recoverable; a dangling pointer is not.
    """
    archive = zip_shapefile(fixtures["valid_points"], tmp_path, include_prj=False)

    async with principal_session(engine, principals["owner"]) as conn:
        with pytest.raises(MissingCRS):
            await ingest_upload(
                conn,
                principals["owner"],
                archive,
                IngestOptions(name="Should Not Exist"),
                storage=storage,
                bucket=BUCKET,
            )

    async with principal_session(engine, principals["owner"]) as conn:
        rows = await conn.execute(
            text("SELECT count(*) FROM dataset WHERE name = 'Should Not Exist'")
        )
    assert rows.scalar_one() == 0


# --- ownership and isolation ------------------------------------------------


async def test_an_uploaded_dataset_is_owned_by_the_uploader(
    engine: AsyncEngine,
    people: dict[str, UUID],
    principals: dict[str, Principal],
    fixtures: dict[str, Path],
    storage: object,
    tmp_path: Path,
) -> None:
    """Ingest is not a privileged path.

    The INSERT policy enforces owner_user_id = the acting principal, so an
    upload cannot plant a dataset owned by someone else even if the service
    tried to.
    """
    archive = zip_shapefile(fixtures["valid_points"], tmp_path)

    async with principal_session(engine, principals["teammate"]) as conn:
        result = await ingest_upload(
            conn,
            principals["teammate"],
            archive,
            IngestOptions(name="Teammate Upload", owner_team_id=people["owner_team"]),
            storage=storage,
            bucket=BUCKET,
        )

    async with principal_session(engine, principals["teammate"]) as conn:
        row = await conn.execute(
            text("SELECT owner_user_id FROM dataset WHERE id = :id"),
            {"id": result.dataset_id},
        )
    assert row.scalar_one() == people["teammate"]


async def test_an_upload_is_invisible_to_another_team(
    engine: AsyncEngine,
    people: dict[str, UUID],
    principals: dict[str, Principal],
    fixtures: dict[str, Path],
    storage: object,
    tmp_path: Path,
) -> None:
    """Ingest produces an ordinary owned object, with ordinary visibility."""
    from tests.conftest import raw_visible_dataset_ids

    archive = zip_shapefile(fixtures["valid_points"], tmp_path)
    async with principal_session(engine, principals["owner"]) as conn:
        result = await ingest_upload(
            conn,
            principals["owner"],
            archive,
            IngestOptions(name="Team Only", owner_team_id=people["owner_team"]),
            storage=storage,
            bucket=BUCKET,
        )

    assert await raw_visible_dataset_ids(engine, principals["teammate"], result.dataset_id) == [
        result.dataset_id
    ]
    assert (
        await raw_visible_dataset_ids(engine, principals["outsider"], result.dataset_id) == []
    )


# --- warnings and metadata --------------------------------------------------


async def test_dropped_geometries_are_reported_to_the_uploader(
    engine: AsyncEngine,
    people: dict[str, UUID],
    principals: dict[str, Principal],
    fixtures: dict[str, Path],
    storage: object,
) -> None:
    """`11` §6: a layer short of its source must say so, where it is seen."""
    async with principal_session(engine, principals["owner"]) as conn:
        result = await ingest_upload(
            conn,
            principals["owner"],
            fixtures["empty_geometries"],
            IngestOptions(name="Mostly Empty", owner_team_id=people["owner_team"]),
            storage=storage,
            bucket=BUCKET,
        )

    assert result.feature_count == 1
    assert any("Dropped 3 of 4" in w for w in result.warnings)


async def test_the_caption_states_only_checkable_facts(
    engine: AsyncEngine,
    people: dict[str, UUID],
    principals: dict[str, Principal],
    fixtures: dict[str, Path],
    storage: object,
    tmp_path: Path,
) -> None:
    """The caption is what Claude reads to describe a layer (`02` §3.5).

    Every element must be verifiable against the dataset row, because its
    purpose is to let Claude write a caption without inventing anything.
    """
    archive = zip_shapefile(fixtures["valid_points"], tmp_path)
    async with principal_session(engine, principals["owner"]) as conn:
        result = await ingest_upload(
            conn,
            principals["owner"],
            archive,
            IngestOptions(name="Wolfcamp A Picks", owner_team_id=people["owner_team"]),
            storage=storage,
            bucket=BUCKET,
        )

    assert "Wolfcamp A Picks" in result.caption
    assert "50" in result.caption
    assert "EPSG:2277" in result.caption
    assert "°N" in result.caption and "°W" in result.caption


async def test_the_attribute_schema_is_recorded(
    engine: AsyncEngine,
    people: dict[str, UUID],
    principals: dict[str, Principal],
    fixtures: dict[str, Path],
    storage: object,
    tmp_path: Path,
) -> None:
    """Claude needs the field names before it can name a value field."""
    archive = zip_shapefile(fixtures["valid_points"], tmp_path)
    async with principal_session(engine, principals["owner"]) as conn:
        result = await ingest_upload(
            conn,
            principals["owner"],
            archive,
            IngestOptions(name="With Schema", owner_team_id=people["owner_team"]),
            storage=storage,
            bucket=BUCKET,
        )
        detail = await service.get_dataset(conn, principals["owner"], result.dataset_id)

    names = {field["name"] for field in detail["attribute_schema"]}
    assert names == {"well_name", "porosity"}


async def test_the_feature_object_is_readable_back_through_duckdb(
    engine: AsyncEngine,
    people: dict[str, UUID],
    principals: dict[str, Principal],
    fixtures: dict[str, Path],
    storage: object,
    tmp_path: Path,
) -> None:
    """The registry row and the object have to agree.

    Resolving the key goes through `resolve_feature_object`, which is the
    single enforcement point for the data plane (`02` §4.1) — so this also
    exercises that path rather than reading `parquet_key` off the row.
    """
    from webmap_geo.dataplane import ObjectStore, connect

    archive = zip_shapefile(fixtures["valid_points"], tmp_path)
    async with principal_session(engine, principals["owner"]) as conn:
        result = await ingest_upload(
            conn,
            principals["owner"],
            archive,
            IngestOptions(name="Round Trip", owner_team_id=people["owner_team"]),
            storage=storage,
            bucket=BUCKET,
        )
        key, version = await service.resolve_feature_object(
            conn, principals["owner"], result.dataset_id
        )

    assert version == 1
    store = ObjectStore(
        endpoint="localhost:9000",
        access_key="minioadmin",
        secret_key="minioadmin",
        use_ssl=False,
    )
    with connect(store) as duck:
        count = duck.execute(
            f"SELECT count(*) FROM read_parquet('s3://{BUCKET}/{key}')"
        ).fetchone()[0]

    assert count == result.feature_count == 50


# --- tabular ----------------------------------------------------------------


async def test_csv_ingest_requires_the_column_mapping(
    engine: AsyncEngine,
    principals: dict[str, Principal],
    fixtures: dict[str, Path],
    storage: object,
) -> None:
    """Never sniffed. Sniffing swaps latitude and longitude often enough to
    put a map in the wrong hemisphere."""
    async with principal_session(engine, principals["owner"]) as conn:
        with pytest.raises(UnsupportedFormat) as excinfo:
            await ingest_upload(
                conn,
                principals["owner"],
                fixtures["coords_swapped"],
                IngestOptions(name="Unmapped", srid_override=4326),
                storage=storage,
                bucket=BUCKET,
            )

    message = str(excinfo.value)
    assert "explicit column mapping" in message
    assert "never guessed" in message


async def test_csv_ingest_requires_a_crs(
    engine: AsyncEngine,
    principals: dict[str, Principal],
    fixtures: dict[str, Path],
    storage: object,
) -> None:
    """A plain table carries no CRS, so one has to be stated."""
    async with principal_session(engine, principals["owner"]) as conn:
        with pytest.raises(MissingCRS) as excinfo:
            await ingest_upload(
                conn,
                principals["owner"],
                fixtures["coords_swapped"],
                IngestOptions(
                    name="No CRS", x_column="lon", y_column="lat", z_column="porosity"
                ),
                storage=storage,
                bucket=BUCKET,
            )

    assert "carries no coordinate reference system" in str(excinfo.value)
    assert "4326" in str(excinfo.value), "name the answer for lon/lat"


async def test_csv_ingest_carries_the_swap_warning_through(
    engine: AsyncEngine,
    people: dict[str, UUID],
    principals: dict[str, Principal],
    fixtures: dict[str, Path],
    storage: object,
) -> None:
    """A warning from the reader has to survive to the uploader.

    Losing it at the service boundary would leave the detection working and
    useless.
    """
    async with principal_session(engine, principals["owner"]) as conn:
        result = await ingest_upload(
            conn,
            principals["owner"],
            fixtures["coords_swapped"],
            IngestOptions(
                name="Swapped",
                owner_team_id=people["owner_team"],
                x_column="lon",
                y_column="lat",
                z_column="porosity",
                srid_override=4326,
            ),
            storage=storage,
            bucket=BUCKET,
        )

    assert any("wrong way round" in w for w in result.warnings)


# --- preview ----------------------------------------------------------------


def test_preview_proposes_a_mapping_without_applying_it(
    fixtures: dict[str, Path],
) -> None:
    """`11` §3.1. A proposal the user confirms is not a guess the software
    acts on, and the preview is what makes the strict rule usable."""
    result = preview(fixtures["coords_swapped"])

    assert result["format"] == "csv"
    assert result["columns"] == ["lon", "lat", "porosity"]
    assert result["proposed_mapping"] == {
        "x_column": "lon",
        "y_column": "lat",
        "z_column": "porosity",
    }
    assert "Neither is inferred" in result["detail"]


def test_preview_of_a_vector_file_reports_its_crs(fixtures: dict[str, Path]) -> None:
    result = preview(fixtures["valid_geopackage"])

    assert result["srid"] == TEXAS_CENTRAL
    assert result["geometry_kind"] == "linestring"
    assert result["feature_count"] == 3
