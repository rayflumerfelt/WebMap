"""Reading control points out of a stored point layer.

Everything here is about a surface that looks right and is built on less data
than the person reading it believes. A layer of 1,200 wells where 340 have no
value in the gridded column produces a perfectly plausible map from 860
points, and nothing on the map says so — so the reader's job is as much to
count what it dropped as to return what it kept.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely

from webmap_geo.control import MIN_CONTROL_POINTS, read_control_points
from webmap_geo.exceptions import DegenerateInput
from webmap_geo.frame import AnalysisFrame

TEXAS = AnalysisFrame(srid=2277, units="usft")

# Roughly the Midland Basin working extent in EPSG:2277 feet.
EXTENT = (1_500_000.0, 10_400_000.0, 1_560_000.0, 10_440_000.0)


def write_layer(
    path: Path,
    geometry: Sequence[object],
    props: list[dict[str, object]],
    *,
    geoparquet: bool = True,
) -> str:
    """A layer in the shape the ingest pipeline writes.

    `geoparquet=True` writes the `geo` file metadata a real object carries, so
    DuckDB's spatial extension hands the geometry column back as GEOMETRY.
    Without it the column comes back as a BLOB of WKB, and the two need
    different SQL.

    **The default was the wrong way round once**, and the cost was specific: a
    reader written against BLOB-only fixtures passed every test here and failed
    on every ingested layer with "No function matches the given name and
    argument types". It was caught end to end against a seeded dataset. Both
    forms are exercised now, and the realistic one is the default.
    """
    table = pa.table(
        {
            "id": pa.array(range(len(props)), type=pa.int64()),
            "geometry": pa.array(
                shapely.to_wkb(np.asarray(geometry, dtype=object)), type=pa.binary()
            ),
            "props": pa.array([json.dumps(p) for p in props], type=pa.string()),
        }
    )
    if geoparquet:
        table = table.replace_schema_metadata(
            {
                "geo": json.dumps(
                    {
                        "version": "1.1.0",
                        "primary_column": "geometry",
                        "columns": {
                            "geometry": {
                                "encoding": "WKB",
                                "geometry_types": ["Point"],
                                "crs": None,
                            }
                        },
                    }
                )
            }
        )
    pq.write_table(table, path)
    return str(path)


@pytest.fixture(scope="module")
def wells(tmp_path_factory: pytest.TempPathFactory) -> str:
    """1,200 picks, 200 of which never got a porosity reading.

    Sparse-by-campaign rather than sparse-at-random: a field gets added
    partway through a programme, and the wells drilled before it keep their
    null forever.
    """
    rng = np.random.default_rng(20260909)
    count = 1_200
    xs = rng.uniform(EXTENT[0], EXTENT[2], count)
    ys = rng.uniform(EXTENT[1], EXTENT[3], count)

    geometry = [shapely.Point(x, y) for x, y in zip(xs, ys, strict=True)]
    props: list[dict[str, object]] = []
    for i in range(count):
        record: dict[str, object] = {
            "well_name": f"Well {i:04d}",
            "tvdss_ft": -8_200.0 - i,
            "operator": "Acme" if i % 3 else "Other",
        }
        if i >= 200:
            record["porosity"] = round(4.0 + (i % 180) / 10, 1)
        props.append(record)

    path = tmp_path_factory.mktemp("control") / "wells.parquet"
    return write_layer(path, list(geometry), props)


# --- the happy path ----------------------------------------------------------


def test_returns_coordinates_and_values_ready_for_interpolate(wells: str) -> None:
    control = read_control_points(wells, "porosity", TEXAS)

    assert control.coords.shape == (1_000, 2)
    assert control.values.shape == (1_000,)
    assert control.coords.dtype == np.float64
    assert np.isfinite(control.values).all()


def test_the_frame_is_carried_not_applied(wells: str) -> None:
    """`05` §2.2. The frame declares what the stored coordinates already are;
    a reader that reprojected would be a transformation mid-pipeline."""
    control = read_control_points(wells, "porosity", TEXAS)

    assert control.frame == TEXAS
    xmin, ymin, xmax, ymax = control.bounds()
    assert EXTENT[0] <= xmin < xmax <= EXTENT[2], "coordinates were moved"
    assert EXTENT[1] <= ymin < ymax <= EXTENT[3]


# --- what it dropped ---------------------------------------------------------


def test_rows_with_no_value_are_counted_and_named(wells: str) -> None:
    """**The point of this module.** 200 wells without a porosity is a
    seventeen-percent difference in the data behind the surface, and the map
    looks identical either way."""
    control = read_control_points(wells, "porosity", TEXAS)

    assert control.n_dropped_no_value == 200
    message = " ".join(control.warnings())
    assert "200" in message
    assert "porosity" in message
    assert "17%" in message, "a share, not only a count — 200 of what?"


def test_a_layer_with_nothing_missing_warns_about_nothing(wells: str) -> None:
    """Warnings a reader learns to ignore are worse than none."""
    control = read_control_points(wells, "tvdss_ft", TEXAS)

    assert control.n_dropped == 0
    assert control.warnings() == []


def test_a_text_column_is_dropped_like_a_null_one(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A CSV import turns "N/A" into text in an otherwise numeric column, and
    the consequence for the reader is identical: no number for this row."""
    points = [shapely.Point(EXTENT[0] + i * 100.0, EXTENT[1]) for i in range(10)]
    props: list[dict[str, object]] = [
        {"porosity": "N/A" if i < 4 else 12.0 + i} for i in range(10)
    ]
    path = write_layer(tmp_path_factory.mktemp("text") / "mixed.parquet", points, props)

    control = read_control_points(path, "porosity", TEXAS)

    assert len(control) == 6
    assert control.n_dropped_no_value == 4


def test_a_stored_nan_counts_as_a_missing_value(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A null and a NaN arrive by different routes — `try_cast` produces None
    for one and a float for the other — and mean the same thing here. Counting
    only the null would understate what was dropped."""
    points = [shapely.Point(EXTENT[0] + i * 100.0, EXTENT[1]) for i in range(10)]
    props: list[dict[str, object]] = [
        {"porosity": float("nan") if i < 3 else 12.0 + i} for i in range(10)
    ]
    path = write_layer(tmp_path_factory.mktemp("nan") / "nan.parquet", points, props)

    control = read_control_points(path, "porosity", TEXAS)

    assert len(control) == 7
    assert control.n_dropped_no_value == 3
    assert np.isfinite(control.values).all()


def test_non_point_geometry_is_reported_separately(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A line in a pointset layer means the wrong layer was chosen, and a null
    value means the wrong column. Two different fixes, so two counters."""
    geometry: list[object] = [shapely.Point(EXTENT[0] + i * 100.0, EXTENT[1]) for i in range(8)]
    geometry += [shapely.LineString([(EXTENT[0], EXTENT[1]), (EXTENT[2], EXTENT[3])])] * 2
    props: list[dict[str, object]] = [{"porosity": 12.0 + i} for i in range(10)]
    path = write_layer(tmp_path_factory.mktemp("mixed") / "geom.parquet", geometry, props)

    control = read_control_points(path, "porosity", TEXAS)

    assert len(control) == 8
    assert control.n_dropped_not_a_point == 2
    assert control.n_dropped_no_value == 0, "a line is not a missing value"
    assert "wrong layer" in " ".join(control.warnings())


def test_coincident_points_are_reported_but_not_removed(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Two picks at one surface location is ordinary for deviated wells.
    Averaging them is a geological decision a reader has no standing to make
    — but it is also what a doubled import looks like, so it gets said."""
    base = [shapely.Point(EXTENT[0] + i * 100.0, EXTENT[1]) for i in range(6)]
    geometry: list[object] = [*base, base[0], base[1]]
    props: list[dict[str, object]] = [{"porosity": 12.0 + i} for i in range(8)]
    path = write_layer(tmp_path_factory.mktemp("dup") / "dup.parquet", geometry, props)

    control = read_control_points(path, "porosity", TEXAS)

    assert len(control) == 8, "nothing was removed"
    assert control.n_coincident == 2
    assert "doubled import" in " ".join(control.warnings())


def test_a_plain_wkb_column_is_read_as_well_as_a_geoparquet_one(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """**The encoding that got this wrong.** DuckDB hands a GeoParquet
    geometry column back as GEOMETRY and a metadata-less one back as BLOB, and
    `ST_GeomFromWKB` accepts only the second. Both have to work: the first is
    what every ingested layer is, and the second is what an exported or
    hand-built file often is."""
    points = [shapely.Point(EXTENT[0] + i * 100.0, EXTENT[1]) for i in range(10)]
    props: list[dict[str, object]] = [{"porosity": 12.0 + i} for i in range(10)]
    plain = write_layer(
        tmp_path_factory.mktemp("plain") / "wkb.parquet", points, props, geoparquet=False
    )
    tagged = write_layer(
        tmp_path_factory.mktemp("tagged") / "geo.parquet", points, props, geoparquet=True
    )

    from_plain = read_control_points(plain, "porosity", TEXAS)
    from_tagged = read_control_points(tagged, "porosity", TEXAS)

    assert np.array_equal(from_plain.coords, from_tagged.coords)
    assert np.array_equal(from_plain.values, from_tagged.values)


def test_a_table_with_no_geometry_column_says_so(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    path = tmp_path_factory.mktemp("bare") / "attrs.parquet"
    pq.write_table(
        pa.table(
            {
                "id": pa.array([1, 2, 3], type=pa.int64()),
                "props": pa.array([json.dumps({"porosity": 12.0})] * 3),
            }
        ),
        path,
    )

    with pytest.raises(DegenerateInput, match="no `geometry` column"):
        read_control_points(str(path), "porosity", TEXAS)


# --- refusals ----------------------------------------------------------------


def test_an_unknown_column_lists_what_is_available(wells: str) -> None:
    """`CLAUDE.md` §8. "No such column" alone is the least useful thing to
    hand back to a conversation trying to grid something."""
    with pytest.raises(DegenerateInput) as excinfo:
        read_control_points(wells, "porsity", TEXAS)

    message = str(excinfo.value)
    assert "porsity" in message
    assert "porosity" in message, "the near-miss the user meant must be listed"
    assert "well_name" in message


def test_too_few_usable_points_says_which_failure_caused_it(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """ "Too few points" on a layer of 900 wells is baffling. Which of the two
    reasons applied is the whole message."""
    points = [shapely.Point(EXTENT[0] + i * 100.0, EXTENT[1]) for i in range(20)]
    props: list[dict[str, object]] = [{"porosity": None} for _ in range(20)]
    path = write_layer(tmp_path_factory.mktemp("empty") / "null.parquet", points, props)

    with pytest.raises(DegenerateInput) as excinfo:
        read_control_points(path, "porosity", TEXAS)

    message = str(excinfo.value)
    assert "20" in message, "the size of the layer that produced nothing"
    assert "no numeric value" in message


def test_the_hard_floor_is_three_points(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Two points define a plane's dip only along one line; there is no
    surface to fit."""
    points = [shapely.Point(EXTENT[0] + i * 100.0, EXTENT[1]) for i in range(2)]
    props: list[dict[str, object]] = [{"porosity": 12.0}, {"porosity": 13.0}]
    path = write_layer(tmp_path_factory.mktemp("two") / "two.parquet", points, props)

    with pytest.raises(DegenerateInput, match="too few to interpolate"):
        read_control_points(path, "porosity", TEXAS)

    assert MIN_CONTROL_POINTS == 3


def test_a_predicate_that_matches_nothing_says_so(wells: str) -> None:
    """Gridding one formation out of a layer holding several is the normal
    case, and a typo in the formation name would otherwise surface as "too few
    control points" — which reads as a data problem rather than a filter."""
    with pytest.raises(DegenerateInput, match="nothing to interpolate"):
        read_control_points(
            wells, "porosity", TEXAS, where="props->>'operator' = 'Nonexistent'"
        )


def test_a_predicate_selects_a_subset(wells: str) -> None:
    control = read_control_points(
        wells, "porosity", TEXAS, where="props->>'operator' = 'Other'"
    )

    assert 0 < len(control) < 1_000


# --- what a caption gets -----------------------------------------------------


def test_describe_names_the_column_the_range_and_the_frame(wells: str) -> None:
    """ "range 4200" is ambiguous about its units; the frame removes that."""
    described = read_control_points(wells, "tvdss_ft", TEXAS).describe()

    assert "1,200 control points" in described
    assert "tvdss_ft" in described
    assert "EPSG:2277 (usft)" in described
