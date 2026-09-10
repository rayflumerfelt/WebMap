"""Applying feature edits to a versioned object. `09-editing.md` §13.

The behaviour worth pinning down is the unforgiving half: an edit against a
version that no longer holds the feature is refused rather than creating one,
and a delete of everything is refused rather than writing a layer that cannot
be told from a failed import.
"""

import json
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pytest
import shapely
from shapely.geometry.base import BaseGeometry

from webmap_io.edits import EmptyResult, FeatureChange, UnknownFeature, apply_edits
from webmap_io.parquet import write_features

# NAD83 / Texas Central (ftUS) — the Midland Basin working CRS, and the CRS
# these coordinates are in. Nothing here reprojects.
TEXAS_CENTRAL = 2277


def seed(path: Path, count: int = 4) -> Path:
    write_features(
        path,
        geometry=np.array(
            [shapely.Point(1_200_000.0 + i * 1_000.0, 6_800_000.0) for i in range(count)],
            dtype=object,
        ),
        props=[{"name": f"well-{i}", "depth": -8000 - i} for i in range(count)],
        srid=TEXAS_CENTRAL,
        ids=np.arange(1, count + 1, dtype=np.int64),
    )
    return path


def read(path: Path) -> dict[int, tuple[BaseGeometry, dict[str, object]]]:
    table = pq.read_table(path)
    geometries = shapely.from_wkb(table.column("geometry").to_pylist())
    return {
        int(feature_id): (geometry, json.loads(props))
        for feature_id, geometry, props in zip(
            table.column("id").to_pylist(),
            geometries,
            table.column("props").to_pylist(),
            strict=True,
        )
    }


def test_moves_a_geometry_and_leaves_the_rest(tmp_path: Path) -> None:
    source = seed(tmp_path / "v1.parquet")
    moved = shapely.Point(1_500_000.0, 6_900_000.0)

    apply_edits(
        source,
        tmp_path / "v2.parquet",
        [FeatureChange(feature_id=2, geometry=moved)],
        srid=TEXAS_CENTRAL,
    )

    features = read(tmp_path / "v2.parquet")
    assert features[2][0].equals(moved)
    assert features[1][0].equals(shapely.Point(1_200_000.0, 6_800_000.0))
    assert len(features) == 4


def test_keeps_properties_a_geometry_edit_did_not_touch(tmp_path: Path) -> None:
    """A drag is not a rename. Rebuilding the row from geometry alone would
    blank every attribute on the first vertex move."""
    source = seed(tmp_path / "v1.parquet")

    apply_edits(
        source,
        tmp_path / "v2.parquet",
        [FeatureChange(feature_id=1, geometry=shapely.Point(0.0, 0.0))],
        srid=TEXAS_CENTRAL,
    )

    assert read(tmp_path / "v2.parquet")[1][1] == {"name": "well-0", "depth": -8000}


def test_replaces_properties_whole_rather_than_merging(tmp_path: Path) -> None:
    """A merge cannot express clearing a field, and a field the user cleared
    coming back is worse than one they have to retype."""
    source = seed(tmp_path / "v1.parquet")

    apply_edits(
        source,
        tmp_path / "v2.parquet",
        [FeatureChange(feature_id=1, props={"name": "renamed"})],
        srid=TEXAS_CENTRAL,
    )

    assert read(tmp_path / "v2.parquet")[1][1] == {"name": "renamed"}


def test_deletes_a_feature(tmp_path: Path) -> None:
    source = seed(tmp_path / "v1.parquet")

    result = apply_edits(
        source,
        tmp_path / "v2.parquet",
        [FeatureChange(feature_id=3, deleted=True)],
        srid=TEXAS_CENTRAL,
    )

    assert result.feature_count == 3
    assert set(read(tmp_path / "v2.parquet")) == {1, 2, 4}


def test_last_state_wins_within_a_batch(tmp_path: Path) -> None:
    """§13 coalesces by feature id: a vertex drag fires dozens of changes and
    one request per change would overwhelm the API and make undo incoherent."""
    source = seed(tmp_path / "v1.parquet")

    apply_edits(
        source,
        tmp_path / "v2.parquet",
        [
            FeatureChange(feature_id=1, geometry=shapely.Point(1.0, 1.0)),
            FeatureChange(feature_id=1, geometry=shapely.Point(2.0, 2.0)),
        ],
        srid=TEXAS_CENTRAL,
    )

    assert read(tmp_path / "v2.parquet")[1][0].equals(shapely.Point(2.0, 2.0))


def test_an_edit_after_a_delete_brings_the_feature_back(tmp_path: Path) -> None:
    """Which is what undoing a delete inside one session looks like by the
    time it reaches here."""
    source = seed(tmp_path / "v1.parquet")

    apply_edits(
        source,
        tmp_path / "v2.parquet",
        [
            FeatureChange(feature_id=2, deleted=True),
            FeatureChange(feature_id=2, geometry=shapely.Point(5.0, 5.0)),
        ],
        srid=TEXAS_CENTRAL,
    )

    assert read(tmp_path / "v2.parquet")[2][0].equals(shapely.Point(5.0, 5.0))


def test_refuses_an_unknown_feature_and_says_why(tmp_path: Path) -> None:
    """Ids come from the writer, so a client-chosen one could collide with an
    id this function would hand out later."""
    source = seed(tmp_path / "v1.parquet")

    with pytest.raises(UnknownFeature, match="reload the layer"):
        apply_edits(
            source,
            tmp_path / "v2.parquet",
            [FeatureChange(feature_id=99, geometry=shapely.Point(0.0, 0.0))],
            srid=TEXAS_CENTRAL,
        )


def test_refuses_to_write_an_empty_layer(tmp_path: Path) -> None:
    source = seed(tmp_path / "v1.parquet", count=2)

    with pytest.raises(EmptyResult, match="delete the dataset instead"):
        apply_edits(
            source,
            tmp_path / "v2.parquet",
            [
                FeatureChange(feature_id=1, deleted=True),
                FeatureChange(feature_id=2, deleted=True),
            ],
            srid=TEXAS_CENTRAL,
        )


def test_refuses_an_empty_batch(tmp_path: Path) -> None:
    """A new version identical to the current one would advance the pointer
    and invalidate every cached tile for nothing."""
    source = seed(tmp_path / "v1.parquet")

    with pytest.raises(ValueError, match="every cached tile for nothing"):
        apply_edits(source, tmp_path / "v2.parquet", [], srid=TEXAS_CENTRAL)


def test_a_delete_carries_no_new_state(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="send two changes"):
        FeatureChange(feature_id=1, geometry=shapely.Point(0.0, 0.0), deleted=True)


def test_leaves_updated_at_alone_for_untouched_features(tmp_path: Path) -> None:
    """The column exists to say which features a session changed. Stamping the
    whole layer on every edit makes it useless for that."""
    source = seed(tmp_path / "v1.parquet")
    before = {
        int(feature_id): stamp
        for feature_id, stamp in zip(
            pq.read_table(source).column("id").to_pylist(),
            pq.read_table(source).column("updated_at").to_pylist(),
            strict=True,
        )
    }

    apply_edits(
        source,
        tmp_path / "v2.parquet",
        [FeatureChange(feature_id=1, geometry=shapely.Point(0.0, 0.0))],
        srid=TEXAS_CENTRAL,
    )

    table = pq.read_table(tmp_path / "v2.parquet")
    after = {
        int(feature_id): stamp
        for feature_id, stamp in zip(
            table.column("id").to_pylist(),
            table.column("updated_at").to_pylist(),
            strict=True,
        )
    }
    assert after[2] == before[2]
    assert after[3] == before[3]
    assert after[1] >= before[1]


def test_the_new_object_is_still_spatially_sorted(tmp_path: Path) -> None:
    """The Hilbert sort is what makes row-group pruning work, and an edited
    version that skipped it would be correct and slow forever."""
    source = seed(tmp_path / "v1.parquet", count=8)

    result = apply_edits(
        source,
        tmp_path / "v2.parquet",
        # Move the first feature to the far end of the extent: if the writer
        # re-sorts, its row order changes; if the edit path wrote rows in id
        # order, it would not.
        [FeatureChange(feature_id=1, geometry=shapely.Point(1_900_000.0, 7_000_000.0))],
        srid=TEXAS_CENTRAL,
    )

    order = pq.read_table(result.path).column("id").to_pylist()
    assert order[-1] == 1
