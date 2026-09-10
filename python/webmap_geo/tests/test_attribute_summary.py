"""Column summaries for the formatting dialog. `07-frontend.md` §6.2.

Against a real GeoParquet file through DuckDB, because the interesting parts
are all in the SQL: the distinct count taken before the values, the cast that
decides text from number, and the binning that has to hold the top value in the
last bin rather than in a forty-first.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from webmap_geo.attributes import (
    HISTOGRAM_BINS,
    MAX_DISTINCT,
    CategorySummary,
    NumericSummary,
    attribute_summary,
)
from webmap_geo.exceptions import DegenerateInput


def a_layer(tmp_path: Path, props: Sequence[Mapping[str, object]]) -> str:
    """A minimal features file: an id and the props map the readers look in."""
    table = pa.table(
        {
            "id": pa.array(range(len(props)), type=pa.int64()),
            "props": pa.array([json.dumps(record) for record in props], type=pa.string()),
        }
    )
    path = tmp_path / "features.parquet"
    pq.write_table(table, path)
    return str(path)


# --- text columns ----------------------------------------------------------------


def test_categories_come_back_most_common_first(tmp_path: Path) -> None:
    """A formation column has six values that matter and forty that appear
    once; alphabetical order buries the six."""
    key = a_layer(
        tmp_path,
        [{"formation": "Wolfcamp"}] * 40
        + [{"formation": "Bone Spring"}] * 12
        + [{"formation": "Delaware"}] * 3,
    )
    summary = attribute_summary(key, "formation")

    assert isinstance(summary, CategorySummary)
    assert summary.values == [("Wolfcamp", 40), ("Bone Spring", 12), ("Delaware", 3)]
    assert summary.remaining == 0
    assert summary.refused is None


def test_the_list_is_capped_and_says_how_many_it_left_out(tmp_path: Path) -> None:
    """The uncounted values take the *Other* colour, and the dialog says how
    many there are — a short list with no note reads as a complete one."""
    key = a_layer(tmp_path, [{"well": f"API-{i:05d}"} for i in range(50)])
    summary = attribute_summary(key, "well", max_categories=10)

    assert isinstance(summary, CategorySummary)
    assert len(summary.values) == 10
    assert summary.remaining == 40


def test_a_high_cardinality_column_is_refused_rather_than_truncated(
    tmp_path: Path,
) -> None:
    """`07` §6.2. A truncated list of 200 well names looks like a list somebody
    could finish; the refusal says the column is the wrong one to colour by."""
    key = a_layer(tmp_path, [{"well": f"API-{i:06d}"} for i in range(MAX_DISTINCT + 5)])
    summary = attribute_summary(key, "well")

    assert isinstance(summary, CategorySummary)
    assert summary.values == []
    assert summary.refused == (MAX_DISTINCT + 5, MAX_DISTINCT)


def test_nulls_are_not_a_category(tmp_path: Path) -> None:
    """A missing formation is missing data, not a formation called nothing —
    and it is the *Other* colour's job, not a row's."""
    key = a_layer(
        tmp_path,
        [{"formation": "Wolfcamp"}] * 3 + [{"formation": None}] * 2 + [{}] * 2,
    )
    summary = attribute_summary(key, "formation")

    assert isinstance(summary, CategorySummary)
    assert summary.values == [("Wolfcamp", 3)]


# --- numeric columns --------------------------------------------------------------


def test_a_numeric_column_gives_a_range_and_a_histogram(tmp_path: Path) -> None:
    rng = np.random.default_rng(20260910)
    values = rng.normal(10.0, 2.0, size=500)
    key = a_layer(tmp_path, [{"porosity": float(value)} for value in values])

    summary = attribute_summary(key, "porosity")

    assert isinstance(summary, NumericSummary)
    assert summary.minimum == pytest.approx(float(values.min()))
    assert summary.maximum == pytest.approx(float(values.max()))
    assert len(summary.counts) == HISTOGRAM_BINS
    assert sum(summary.counts) == 500


def test_the_top_value_lands_in_the_last_bin(tmp_path: Path) -> None:
    """`floor((max - min) / width)` is exactly `bins`, so without the clamp the
    maximum falls in a forty-first bin that does not exist — and the value that
    defines the top of the ramp is the one dropped."""
    key = a_layer(tmp_path, [{"depth": float(value)} for value in range(0, 100)])
    summary = attribute_summary(key, "depth", bins=10)

    assert isinstance(summary, NumericSummary)
    assert sum(summary.counts) == 100
    assert summary.counts[-1] > 0


def test_numbers_stored_as_text_are_still_numbers(tmp_path: Path) -> None:
    """Ordinary in a shapefile. Read as categories it offers a picker with
    1,200 entries where a ramp was wanted."""
    key = a_layer(tmp_path, [{"depth": str(value)} for value in range(50)])
    summary = attribute_summary(key, "depth")

    assert isinstance(summary, NumericSummary)
    assert summary.minimum == 0.0


def test_one_bad_value_does_not_demote_the_column(tmp_path: Path) -> None:
    """A single `N/A` in a porosity column is a data-entry slip, not a reason
    to take the ramp away from a layer that plainly has one."""
    key = a_layer(
        tmp_path,
        [{"porosity": float(i)} for i in range(200)] + [{"porosity": "N/A"}],
    )
    summary = attribute_summary(key, "porosity")

    assert isinstance(summary, NumericSummary)
    assert summary.missing == 1


def test_a_mixed_column_is_read_as_text(tmp_path: Path) -> None:
    """90% numbers and 10% words is a mixed column, and a ramp would hide the
    words entirely."""
    key = a_layer(
        tmp_path,
        [{"grade": str(i)} for i in range(90)] + [{"grade": "unlogged"} for _ in range(10)],
    )
    summary = attribute_summary(key, "grade")
    assert isinstance(summary, CategorySummary)


def test_a_constant_column_does_not_divide_by_zero(tmp_path: Path) -> None:
    """One bin holding everything is the honest picture; the alternative is a
    crash on a layer where every well logged the same value."""
    key = a_layer(tmp_path, [{"depth": 100.0} for _ in range(20)])
    summary = attribute_summary(key, "depth")

    assert isinstance(summary, NumericSummary)
    assert summary.counts == [20]
    assert summary.minimum == summary.maximum == 100.0


def test_a_column_of_only_text_is_not_forced_numeric(tmp_path: Path) -> None:
    key = a_layer(tmp_path, [{"formation": "Wolfcamp"}] * 5)
    with pytest.raises(DegenerateInput, match="no numeric values"):
        attribute_summary(key, "formation", kind="number")


# --- the column itself ------------------------------------------------------------


def test_an_unknown_column_lists_what_the_layer_has(tmp_path: Path) -> None:
    key = a_layer(tmp_path, [{"formation": "Wolfcamp", "porosity": 12.0}])
    with pytest.raises(DegenerateInput, match="formation, porosity"):
        attribute_summary(key, "porosty")


def test_a_column_name_with_a_quote_in_it_is_data_not_syntax(tmp_path: Path) -> None:
    """The name is bound rather than interpolated, so a props key containing a
    quote reads as a column name — a real possibility for a shapefile field
    named from a spreadsheet header."""
    key = a_layer(tmp_path, [{"o'brien lease": "A"}] * 3)
    summary = attribute_summary(key, "o'brien lease")

    assert isinstance(summary, CategorySummary)
    assert summary.values == [("A", 3)]
