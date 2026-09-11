"""Writing features out, and reading them back. `11-file-io.md` §1, §7.

Every test here is a round trip, because the only question worth asking of a
writer is whether the file it produced says what the data said. A shapefile that
writes without error and loses a column is the failure this format is famous for.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
import shapely

from webmap_io.export import zip_shapefile
from webmap_io.read import read_vector
from webmap_io.write import write_features

TEXAS_CENTRAL = 2277

POINTS = np.array(
    [shapely.Point(1_500_000.0 + index * 500.0, 10_400_000.0) for index in range(3)],
    dtype=object,
)
POINT_PROPS: list[dict[str, Any]] = [
    {"well": "Smith 1H", "net_pay": 42.5, "is_producer": True},
    {"well": "Smith 2H", "net_pay": 51.0, "is_producer": True},
    {"well": "Jones 1", "net_pay": None, "is_producer": False},
]

POLYGONS = np.array(
    [
        shapely.box(1_500_000.0, 10_400_000.0, 1_501_000.0, 10_401_000.0),
        shapely.box(1_501_000.0, 10_400_000.0, 1_502_000.0, 10_401_000.0),
    ],
    dtype=object,
)
POLYGON_PROPS: list[dict[str, Any]] = [{"lease": "A"}, {"lease": "B"}]


class TestGeoPackage:
    def test_round_trips_geometry_and_attributes(self, tmp_path: Path) -> None:
        path = tmp_path / "wells.gpkg"

        write_features(path, geometry=POINTS, props=POINT_PROPS, srid=TEXAS_CENTRAL, fmt="gpkg")
        back = read_vector(path)

        assert back.feature_count == 3
        assert back.srid == TEXAS_CENTRAL
        assert back.attributes["well"] == ["Smith 1H", "Smith 2H", "Jones 1"]

    def test_keeps_a_long_field_name(self, tmp_path: Path) -> None:
        """The whole reason §4.2 recommends it over shapefile."""
        path = tmp_path / "wells.gpkg"

        write_features(
            path,
            geometry=POINTS,
            props=[{"porosity_average": 0.1} for _ in range(3)],
            srid=TEXAS_CENTRAL,
            fmt="gpkg",
        )

        assert "porosity_average" in read_vector(path).attributes

    def test_a_numeric_column_comes_back_numeric(self, tmp_path: Path) -> None:
        """An object column reaches the driver as text, and then every sum the
        recipient tries on it fails."""
        path = tmp_path / "wells.gpkg"

        write_features(path, geometry=POINTS, props=POINT_PROPS, srid=TEXAS_CENTRAL, fmt="gpkg")
        values = read_vector(path).attributes["net_pay"]

        assert isinstance(values[0], float)
        assert values[0] == pytest.approx(42.5)


class TestShapefile:
    def test_writes_its_sidecars(self, tmp_path: Path) -> None:
        path = tmp_path / "leases.shp"

        write_features(
            path, geometry=POLYGONS, props=POLYGON_PROPS, srid=TEXAS_CENTRAL, fmt="shapefile"
        )

        written = {file.suffix.lower() for file in tmp_path.iterdir()}
        assert {".shp", ".shx", ".dbf", ".prj"} <= written

    def test_the_field_name_matches_what_the_warning_promised(self, tmp_path: Path) -> None:
        """`plan_export` tells a user their column will arrive as `porosity_a`.
        This is the test that makes that sentence true."""
        path = tmp_path / "leases.shp"

        write_features(
            path,
            geometry=POLYGONS,
            props=[{"porosity_average": 0.1}, {"porosity_average": 0.2}],
            srid=TEXAS_CENTRAL,
            fmt="shapefile",
        )

        assert "porosity_a" in read_vector(path).attributes

    def test_colliding_names_both_survive(self, tmp_path: Path) -> None:
        """Silent data loss is the documented consequence of not doing this."""
        path = tmp_path / "leases.shp"

        write_features(
            path,
            geometry=POLYGONS,
            props=[
                {"porosity_average": 0.1, "porosity_amplitude": 9.0},
                {"porosity_average": 0.2, "porosity_amplitude": 8.0},
            ],
            srid=TEXAS_CENTRAL,
            fmt="shapefile",
        )
        back = read_vector(path)

        assert len([key for key in back.attributes if key.startswith("porosity")]) == 2

    def test_zips_to_something_readable(self, tmp_path: Path) -> None:
        """Users forward exactly what they are given, so what they are given has
        to be complete."""
        write_features(
            tmp_path / "leases.shp",
            geometry=POLYGONS,
            props=POLYGON_PROPS,
            srid=TEXAS_CENTRAL,
            fmt="shapefile",
        )

        archive = zip_shapefile(tmp_path, tmp_path / "leases.zip", stem="leases")

        import zipfile

        extracted = tmp_path / "out"
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(extracted)
        assert read_vector(extracted / "leases.shp").feature_count == 2

    def test_keeps_the_crs(self, tmp_path: Path) -> None:
        """Without the `.prj` there is no CRS, and `CLAUDE.md` §3.1 forbids the
        recipient's tool from inferring one — so it guesses, and the guess is
        usually WGS84."""
        path = tmp_path / "leases.shp"

        write_features(
            path, geometry=POLYGONS, props=POLYGON_PROPS, srid=TEXAS_CENTRAL, fmt="shapefile"
        )

        assert read_vector(path).srid == TEXAS_CENTRAL


class TestGeoJson:
    def test_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "wells.geojson"

        write_features(
            path, geometry=POINTS, props=POINT_PROPS, srid=TEXAS_CENTRAL, fmt="geojson"
        )

        assert read_vector(path).feature_count == 3


class TestCsv:
    def test_points_get_x_and_y_columns(self, tmp_path: Path) -> None:
        """The file a geologist actually asked for: one they can open in a
        spreadsheet and hand to somebody who will re-import it."""
        path = tmp_path / "wells.csv"

        write_features(path, geometry=POINTS, props=POINT_PROPS, srid=TEXAS_CENTRAL, fmt="csv")
        lines = path.read_text(encoding="utf-8").splitlines()

        assert lines[0].startswith("x,y,")
        assert lines[1].startswith("1500000.0,10400000.0,")

    def test_lines_and_polygons_become_wkt(self, tmp_path: Path) -> None:
        path = tmp_path / "leases.csv"

        write_features(
            path, geometry=POLYGONS, props=POLYGON_PROPS, srid=TEXAS_CENTRAL, fmt="csv"
        )
        lines = path.read_text(encoding="utf-8").splitlines()

        assert lines[0].startswith("geometry,")
        assert "POLYGON" in lines[1]

    def test_writes_every_row(self, tmp_path: Path) -> None:
        path = tmp_path / "wells.csv"

        write_features(path, geometry=POINTS, props=POINT_PROPS, srid=TEXAS_CENTRAL, fmt="csv")

        assert len(path.read_text(encoding="utf-8").splitlines()) == 4


class TestParquet:
    def test_round_trips_through_the_internal_format(self, tmp_path: Path) -> None:
        path = tmp_path / "wells.parquet"

        write_features(
            path, geometry=POINTS, props=POINT_PROPS, srid=TEXAS_CENTRAL, fmt="parquet"
        )

        import pyarrow.parquet as pq

        assert pq.read_table(path).num_rows == 3


class TestUnknownFormat:
    def test_names_the_formats_that_exist(self, tmp_path: Path) -> None:
        from webmap_io.exceptions import UnsupportedFormat

        with pytest.raises(UnsupportedFormat, match="Available: csv, geojson"):
            write_features(
                tmp_path / "x.dwg",
                geometry=POINTS,
                props=POINT_PROPS,
                srid=TEXAS_CENTRAL,
                fmt="dwg",
            )
