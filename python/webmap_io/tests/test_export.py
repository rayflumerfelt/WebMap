"""Export loss reporting. `11-file-io.md` §4.2.

Every one of §4.1's shapefile constraints loses data quietly. These tests are
about the warning arriving *before* the write, and about it being true — a
warning that predicted the wrong field name would be worse than none, because
it would be believed.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from webmap_io.export import (
    ExportWarning,
    plan_export,
    shapefile_field_names,
    zip_shapefile,
)


def field(name: str, kind: str = "text") -> dict[str, str]:
    return {"name": name, "type": kind}


def codes(warnings: list[ExportWarning]) -> set[str]:
    return {warning.code for warning in warnings}


class TestShapefileWarnings:
    def test_reports_truncation_with_the_name_it_will_get(self) -> None:
        """ "They will not notice until the partner asks about it, and by then
        the file has been forwarded twice." """
        warnings = plan_export([field("porosity_average")], "polygon", "shapefile")

        truncation = next(w for w in warnings if w.code == "field_truncation")
        assert "porosity_average -> porosity_a" in truncation.message
        assert truncation.affected == ["porosity_average"]

    def test_reports_a_collision_before_it_silently_loses_a_column(self) -> None:
        warnings = plan_export(
            [field("porosity_average"), field("porosity_amplitude")], "polygon", "shapefile"
        )

        collision = next(w for w in warnings if w.code == "field_collision")
        assert "porosity_average, porosity_amplitude" in collision.message

    def test_warns_that_numeric_nulls_become_zero(self) -> None:
        """A blank that arrives as 0 reads as a measurement rather than a gap,
        which on a porosity column is a different map."""
        warnings = plan_export([field("net_pay", "number")], "polygon", "shapefile")

        assert "null_to_zero" in codes(warnings)

    def test_warns_that_a_mixed_layer_splits(self) -> None:
        assert "geometry_split" in codes(plan_export([], "mixed", "shapefile"))

    def test_says_nothing_about_splitting_a_single_type(self) -> None:
        assert "geometry_split" not in codes(plan_export([], "polygon", "shapefile"))

    def test_warns_about_size_before_the_write_fails(self) -> None:
        """The 2 GB limit fails during the write, which is the worst moment to
        learn about it."""
        warnings = plan_export([], "point", "shapefile", feature_count=4_000_000)

        assert "size_risk" in codes(warnings)

    def test_always_offers_geopackage(self) -> None:
        """§4.2: offer it in the same dialog, with a one-line reason. None of
        the constraints above apply to it."""
        assert "prefer_gpkg" in codes(plan_export([], "polygon", "shapefile"))


class TestOtherFormats:
    def test_geopackage_loses_nothing_worth_saying(self) -> None:
        warnings = plan_export([field("porosity_average")], "mixed", "gpkg")

        assert warnings == []

    def test_geojson_loses_nothing_worth_saying(self) -> None:
        assert plan_export([field("porosity_average")], "polygon", "geojson") == []

    def test_csv_says_the_crs_does_not_travel(self) -> None:
        """The failure this prevents is a recipient plotting State Plane feet as
        degrees, which produces a map that looks like a dot."""
        assert "csv_geometry" in codes(plan_export([], "point", "csv"))

    def test_csv_says_lines_become_wkt(self) -> None:
        assert "csv_geometry_wkt" in codes(plan_export([], "linestring", "csv"))

    def test_an_unknown_format_names_the_ones_that_exist(self) -> None:
        with pytest.raises(ValueError, match="Available: csv, geojson"):
            plan_export([], "point", "dwg")


class TestFieldNames:
    def test_a_short_name_is_untouched(self) -> None:
        assert shapefile_field_names(["value"]) == {"value": "value"}

    def test_a_long_name_is_truncated_to_ten(self) -> None:
        assert shapefile_field_names(["porosity_average"]) == {"porosity_average": "porosity_a"}

    def test_a_collision_is_suffixed_and_stays_within_the_limit(self) -> None:
        assigned = shapefile_field_names(["porosity_average", "porosity_amplitude"])

        assert assigned["porosity_average"] == "porosity_a"
        assert assigned["porosity_amplitude"] == "porosity_1"
        assert all(len(name) <= 10 for name in assigned.values())

    def test_names_are_unique_case_insensitively(self) -> None:
        """DBF field names are not case-sensitive to every reader, and two that
        differ only in case are a collision on the ones that matter."""
        assigned = shapefile_field_names(["Porosity_Average", "porosity_average"])

        assert len({name.lower() for name in assigned.values()}) == 2

    def test_the_same_schema_produces_the_same_names(self) -> None:
        """A recipient joining two exports on a field name depends on this far
        more than they know."""
        names = ["porosity_average", "porosity_amplitude", "porosity_anomaly"]

        assert shapefile_field_names(names) == shapefile_field_names(names)

    def test_the_warning_and_the_writer_agree(self) -> None:
        """The one property that makes the warning worth printing: it predicts
        what the writer will actually do."""
        names = ["porosity_average", "porosity_amplitude"]
        warnings = plan_export([field(name) for name in names], "polygon", "shapefile")
        assigned = shapefile_field_names(names)

        truncation = next(w for w in warnings if w.code == "field_truncation")
        assert assigned["porosity_average"] in truncation.message


class TestZipping:
    def test_bundles_every_sidecar(self, tmp_path: Path) -> None:
        """A `.cpg` left behind turns attribute text into mojibake on a reader
        that trusts it."""
        for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
            (tmp_path / f"leases{suffix}").write_bytes(b"x")
        archive = tmp_path / "leases.zip"

        zip_shapefile(tmp_path, archive, stem="leases")

        with zipfile.ZipFile(archive) as bundle:
            assert set(bundle.namelist()) == {
                "leases.shp",
                "leases.shx",
                "leases.dbf",
                "leases.prj",
                "leases.cpg",
            }

    def test_leaves_another_layer_alone(self, tmp_path: Path) -> None:
        """A mixed layer exports as several shapefiles into one directory, and
        each zip holds its own."""
        for stem in ("leases", "wells"):
            for suffix in (".shp", ".dbf"):
                (tmp_path / f"{stem}{suffix}").write_bytes(b"x")

        zip_shapefile(tmp_path, tmp_path / "leases.zip", stem="leases")

        with zipfile.ZipFile(tmp_path / "leases.zip") as bundle:
            assert all(name.startswith("leases") for name in bundle.namelist())

    def test_says_so_when_there_is_no_shapefile_to_zip(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="failed earlier than this"):
            zip_shapefile(tmp_path, tmp_path / "empty.zip", stem="nothing")
