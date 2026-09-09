"""Tests for the GeoParquet feature writer.

The pruning test is a Phase 0 acceptance criterion (`12-roadmap.md`): the
seed dataset's object must prune row groups on a tile-extent predicate,
because "an unsorted write silently defeats it". Silently is the operative
word — an unsorted layer is correct and merely slow, on every tile request,
forever. Nothing else in the system notices.
"""

import json
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pyarrow.parquet as pq
import pytest
import shapely

from webmap_io.parquet import row_group_bounds, write_features

# NAD83 / Texas Central (ftUS). The Midland Basin working CRS.
TEXAS_CENTRAL = 2277

# Roughly the seed extent in EPSG:2277 feet.
EXTENT = (1_100_000.0, 6_600_000.0, 1_600_000.0, 7_000_000.0)


def scattered_points(n: int, seed: int = 20260908) -> npt.NDArray[np.object_]:
    rng = np.random.default_rng(seed)
    points = shapely.points(
        rng.uniform(EXTENT[0], EXTENT[2], n), rng.uniform(EXTENT[1], EXTENT[3], n)
    )
    # shapely.points returns a scalar for n == 1; every caller here wants an
    # array, so normalise rather than making each one handle both.
    return np.atleast_1d(np.asarray(points, dtype=object))


def test_round_trips_through_pyarrow(tmp_path: Path) -> None:
    geometry = scattered_points(1_000)
    props = [{"well": f"W-{i:04d}", "porosity": 0.1} for i in range(1_000)]

    result = write_features(
        tmp_path / "v1.parquet", geometry=geometry, props=props, srid=TEXAS_CENTRAL
    )
    table = pq.read_table(result.path)

    assert result.feature_count == 1_000
    assert table.num_rows == 1_000
    assert table.column_names == ["id", "geometry", "props", "updated_at", "bbox"]


def test_carries_geoparquet_metadata_so_the_object_is_self_describing(
    tmp_path: Path,
) -> None:
    """A .parquet handed to someone else must be readable without this database.

    That was not true of a row in a per-dataset feature table
    (`02-data-model.md` §3.5.1), and it is the reason the CRS travels in the
    file metadata rather than only in the registry.
    """
    result = write_features(
        tmp_path / "v1.parquet",
        geometry=scattered_points(100),
        props=[{} for _ in range(100)],
        srid=TEXAS_CENTRAL,
    )

    meta = json.loads(pq.read_schema(result.path).metadata[b"geo"])
    column = meta["columns"]["geometry"]

    assert meta["version"].startswith("1.1")
    assert meta["primary_column"] == "geometry"
    assert column["encoding"] == "WKB"
    assert column["geometry_types"] == ["Point"]
    assert column["crs"]["id"]["code"] == TEXAS_CENTRAL
    # The covering declaration is what tells a reader the bbox struct is
    # authoritative rather than an ordinary attribute column.
    assert column["covering"]["bbox"]["xmin"] == ["bbox", "xmin"]


def test_ids_are_stable_across_the_written_order(tmp_path: Path) -> None:
    """Sorting reorders rows; it must not renumber features.

    `id` is the edit identity of a feature and is stable across versions
    (`02` §3.5.1). If the writer assigned ids after sorting, an edit that
    changed one geometry would silently renumber everything downstream of it.
    """
    geometry = scattered_points(500)
    ids = np.arange(1000, 1500, dtype=np.int64)

    result = write_features(
        tmp_path / "v1.parquet",
        geometry=geometry,
        props=[{"n": int(i)} for i in ids],
        srid=TEXAS_CENTRAL,
        ids=ids,
    )
    table = pq.read_table(result.path)

    written_ids = table.column("id").to_numpy()
    written_props = [json.loads(p)["n"] for p in table.column("props").to_pylist()]

    assert sorted(written_ids) == list(ids)
    # Each row's props must still belong to that row's id after the sort.
    assert written_ids.tolist() == written_props


def test_geometry_survives_the_sort_attached_to_its_row(tmp_path: Path) -> None:
    """The bug this catches: sorting one column and not the others."""
    geometry = scattered_points(300)
    result = write_features(
        tmp_path / "v1.parquet",
        geometry=geometry,
        props=[{"x": float(shapely.get_x(g))} for g in geometry],
        srid=TEXAS_CENTRAL,
    )
    table = pq.read_table(result.path)

    written = shapely.from_wkb(table.column("geometry").to_pylist())
    claimed = [json.loads(p)["x"] for p in table.column("props").to_pylist()]

    assert shapely.get_x(written) == pytest.approx(claimed)


def test_bbox_column_matches_the_geometry(tmp_path: Path) -> None:
    """The predicate filters on bbox, so a stale bbox drops real features."""
    geometry = shapely.linestrings(
        [[[1_200_000.0, 6_700_000.0], [1_210_000.0, 6_720_000.0]]] * 1
    )
    result = write_features(
        tmp_path / "v1.parquet",
        geometry=np.array([geometry[0]]),
        props=[{}],
        srid=TEXAS_CENTRAL,
    )

    bbox = pq.read_table(result.path).column("bbox").to_pylist()[0]

    assert bbox == {
        "xmin": 1_200_000.0,
        "ymin": 6_700_000.0,
        "xmax": 1_210_000.0,
        "ymax": 6_720_000.0,
    }


# --- The acceptance criterion ----------------------------------------------


def _write_with_order(path: Path, geometry: np.ndarray, *, sorted_: bool) -> Path:
    """Write the same features sorted and unsorted, at a row-group size small
    enough that a 20k-feature layer produces many groups.

    The production writer targets 128 MB groups, which for a small test layer
    is one group and prunes nothing by construction. Forcing a small group
    size is what makes the *ordering* the variable under test rather than the
    layer size.
    """
    import pyarrow as pa

    from webmap_io import parquet as writer

    original = writer.MIN_ROWS_PER_GROUP
    try:
        writer.MIN_ROWS_PER_GROUP = 512
        writer.MAX_ROWS_PER_GROUP = 512
        if sorted_:
            return writer.write_features(
                path,
                geometry=geometry,
                props=[{} for _ in geometry],
                srid=TEXAS_CENTRAL,
            ).path

        # The unsorted control: same data, file order, same everything else.
        bounds = shapely.bounds(geometry)
        table = pa.table(
            {
                "id": pa.array(np.arange(len(geometry), dtype=np.int64)),
                "geometry": pa.array(shapely.to_wkb(geometry), type=pa.binary()),
                "props": pa.array(["{}"] * len(geometry), type=pa.string()),
                "updated_at": pa.array(
                    np.full(len(geometry), np.datetime64("now", "us")),
                    type=pa.timestamp("us"),
                ),
                "bbox": pa.StructArray.from_arrays(
                    [pa.array(bounds[:, i], type=pa.float64()) for i in range(4)],
                    names=["xmin", "ymin", "xmax", "ymax"],
                ),
            }
        )
        pq.write_table(table, path, row_group_size=512, write_statistics=True)
        return path
    finally:
        writer.MIN_ROWS_PER_GROUP = original
        writer.MAX_ROWS_PER_GROUP = 1_000_000


def _groups_touched(path: Path, tile: tuple[float, float, float, float]) -> int:
    """How many row groups a tile-extent predicate cannot skip.

    This is exactly the decision DuckDB makes from Parquet statistics for the
    `WHERE bbox.xmin <= ... AND bbox.xmax >= ...` predicate in
    `06-rendering.md` §7 — read from the statistics themselves rather than
    from an EXPLAIN plan whose format changes between releases.
    """
    west, south, east, north = tile
    return sum(
        1
        for xmin, ymin, xmax, ymax in row_group_bounds(path)
        if xmin <= east and xmax >= west and ymin <= north and ymax >= south
    )


def test_hilbert_sort_makes_row_groups_prune(tmp_path: Path) -> None:
    """Phase 0 acceptance: an unsorted write silently defeats pruning.

    Asserts the *difference* the sort makes, not an absolute number — the
    absolute count depends on group size and layer density, but the unsorted
    control reading essentially everything is the invariant.
    """
    geometry = scattered_points(20_000)
    # A tile covering about 1% of the layer extent, near one corner.
    width = (EXTENT[2] - EXTENT[0]) / 10
    height = (EXTENT[3] - EXTENT[1]) / 10
    tile = (EXTENT[0], EXTENT[1], EXTENT[0] + width, EXTENT[1] + height)

    sorted_path = _write_with_order(tmp_path / "sorted.parquet", geometry, sorted_=True)
    unsorted_path = _write_with_order(tmp_path / "unsorted.parquet", geometry, sorted_=False)

    total = len(row_group_bounds(sorted_path))
    touched_sorted = _groups_touched(sorted_path, tile)
    touched_unsorted = _groups_touched(unsorted_path, tile)

    assert total >= 20, "too few row groups for the comparison to mean anything"
    # In file order every group's bbox spans the whole layer, so every group
    # is read for every tile.
    assert touched_unsorted == total
    # Sorted, a 1%-area tile should touch a small fraction of groups.
    assert touched_sorted < total * 0.2, (
        f"Hilbert-sorted layer still reads {touched_sorted}/{total} row groups "
        f"for a 1% tile. Pruning is not working."
    )


def test_refuses_to_write_an_empty_object(tmp_path: Path) -> None:
    """An ingest that dropped every feature is a validation failure."""
    with pytest.raises(ValueError, match="dropped every feature"):
        write_features(
            tmp_path / "v1.parquet",
            geometry=np.array([], dtype=object),
            props=[],
            srid=TEXAS_CENTRAL,
        )


def test_mismatched_geometry_and_props_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="3 geometries and 2 property records"):
        write_features(
            tmp_path / "v1.parquet",
            geometry=scattered_points(3),
            props=[{}, {}],
            srid=TEXAS_CENTRAL,
        )


def test_row_group_bounds_refuses_a_file_with_no_covering_column(
    tmp_path: Path,
) -> None:
    """A layer written by something else must not silently report no bounds."""
    import pyarrow as pa

    path = tmp_path / "plain.parquet"
    pq.write_table(pa.table({"id": pa.array([1, 2, 3])}), path)

    with pytest.raises(ValueError, match="no bbox covering column"):
        row_group_bounds(path)
