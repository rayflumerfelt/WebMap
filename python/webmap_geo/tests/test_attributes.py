"""Paged attribute reads. `07-frontend.md` §9.

The property that matters is that paging is *coherent*: every row appears
exactly once across the pages, and no row appears twice. Parquet gives no
ordering guarantee across scans, so a page without an ORDER BY can silently
duplicate and skip — and a reader paging through a table has no way to tell.
"""

from __future__ import annotations

import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from webmap_geo.attributes import MAX_PAGE, feature_attributes
from webmap_geo.exceptions import DegenerateInput


@pytest.fixture(scope="module")
def layer(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A synthetic layer with the shape the ingest pipeline writes: an id, a
    geometry column that must never be read here, and attributes in `props`."""
    count = 1_200
    path = tmp_path_factory.mktemp("attributes") / "features.parquet"
    table = pa.table(
        {
            "id": pa.array(range(count), type=pa.int64()),
            # Present so a query that forgets to exclude it would still work,
            # and so its absence from the result is meaningful.
            "geometry": pa.array([b"\x00" * 64] * count, type=pa.binary()),
            "props": pa.array(
                [
                    json.dumps(
                        {
                            "well_name": f"Well {i:04d}",
                            "porosity": round(4.0 + (i % 180) / 10, 1),
                            "operator": "Acme" if i % 3 else "Other",
                            # Sparse on purpose: a real campaign adds fields
                            # partway through.
                            **({"tvdss_ft": -8_200 - i} if i % 2 == 0 else {}),
                        }
                    )
                    for i in range(count)
                ]
            ),
        }
    )
    pq.write_table(table, path)
    return str(path)


# --- paging -----------------------------------------------------------------


def test_a_page_reports_the_whole_layer_not_the_page(layer: str) -> None:
    """A table showing 500 rows and saying nothing about the other 700 is a
    table someone draws a conclusion from."""
    page = feature_attributes(layer, offset=0, limit=100)

    assert len(page.items) == 100
    assert page.total == 1_200
    assert page.has_more is True


def test_paging_covers_every_row_exactly_once(layer: str) -> None:
    """**The property this module exists for.**

    Parquet gives no ordering guarantee across scans. Without an ORDER BY, two
    pages can contain the same row and omit another, and nothing about the
    result says so.
    """
    seen: list[int] = []
    offset = 0
    while True:
        page = feature_attributes(layer, offset=offset, limit=250)
        seen.extend(int(item["id"]) for item in page.items)
        if not page.has_more:
            break
        offset += len(page.items)

    assert len(seen) == 1_200
    assert len(set(seen)) == 1_200, "a row appeared on two pages"
    assert sorted(seen) == list(range(1_200)), "a row was skipped"


def test_the_last_page_is_short_and_says_it_is_last(layer: str) -> None:
    page = feature_attributes(layer, offset=1_100, limit=250)

    assert len(page.items) == 100
    assert page.has_more is False


def test_an_offset_past_the_end_is_empty_rather_than_an_error(layer: str) -> None:
    """A reader who scrolls past the end of a layer that shrank should see an
    empty table, not a failure."""
    page = feature_attributes(layer, offset=5_000, limit=100)

    assert page.items == []
    assert page.total == 1_200


def test_a_negative_offset_is_refused(layer: str) -> None:
    with pytest.raises(DegenerateInput, match="must not be negative"):
        feature_attributes(layer, offset=-1)


def test_an_oversized_page_is_clamped_rather_than_refused(layer: str) -> None:
    """Clamped, because a caller asking for too much wants as much as it can
    get — and refusing would make a UI bug into a blank table."""
    page = feature_attributes(layer, offset=0, limit=MAX_PAGE * 10)

    assert page.limit == MAX_PAGE


# --- content ----------------------------------------------------------------


def test_geometry_is_not_read(layer: str) -> None:
    """The whole point. Pulling geometry the table will never draw is the
    difference between an instant page and megabytes of coordinates."""
    page = feature_attributes(layer, limit=5)

    assert all("geometry" not in item for item in page.items)


def test_attributes_arrive_flat_with_their_id(layer: str) -> None:
    page = feature_attributes(layer, limit=1)

    (item,) = page.items
    assert item["id"] == 0
    assert item["well_name"] == "Well 0000"
    assert item["porosity"] == 4.0


def test_a_sparse_attribute_is_absent_rather_than_null(layer: str) -> None:
    """The table renders a missing key as an em dash either way; keeping the
    payload sparse is what makes a 500-row page small."""
    page = feature_attributes(layer, offset=1, limit=1)

    assert "tvdss_ft" not in page.items[0]


# --- sorting ----------------------------------------------------------------


def test_sorting_by_a_numeric_attribute_orders_numerically(layer: str) -> None:
    """A numeric column sorted as text puts 100 before 20, which reads as a
    broken table rather than as a sort-order decision."""
    page = feature_attributes(layer, limit=50, order_by="porosity", descending=True)

    values = [float(item["porosity"]) for item in page.items]
    assert values == sorted(values, reverse=True)
    assert values[0] == 21.9


def test_sorting_by_a_text_attribute_orders_lexically(layer: str) -> None:
    page = feature_attributes(layer, limit=10, order_by="well_name")

    names = [str(item["well_name"]) for item in page.items]
    assert names == sorted(names)


def test_a_sorted_page_is_still_coherent_across_pages(layer: str) -> None:
    """Sorting must not reintroduce the duplication an ORDER BY prevents — the
    tiebreak on the raw text is what keeps rows with equal values stable."""
    first = feature_attributes(layer, offset=0, limit=300, order_by="porosity")
    second = feature_attributes(layer, offset=300, limit=300, order_by="porosity")

    ids = [item["id"] for item in first.items] + [item["id"] for item in second.items]
    assert len(set(ids)) == 600


def test_sorting_by_an_unknown_attribute_lists_the_real_ones(layer: str) -> None:
    """The column name is interpolated into SQL, so it is validated against the
    file rather than trusted — and the message helps rather than just refusing."""
    with pytest.raises(DegenerateInput, match="well_name"):
        feature_attributes(layer, order_by="porsoity")


def test_a_sort_column_cannot_smuggle_sql(layer: str) -> None:
    """The validation above is what stands between a query parameter and the
    query. Asserted directly, because the reason it exists is not obvious from
    the happy path."""
    with pytest.raises(DegenerateInput):
        feature_attributes(layer, order_by="porosity'; DROP TABLE x; --")


def test_a_layer_with_no_attributes_pages_without_failing(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Geometry-only layers exist — a fault trace with nothing but a line."""
    path = tmp_path_factory.mktemp("bare") / "features.parquet"
    pq.write_table(
        pa.table(
            {
                "id": pa.array([1, 2], type=pa.int64()),
                "props": pa.array([None, None], type=pa.string()),
            }
        ),
        path,
    )

    page = feature_attributes(str(path), limit=10)

    assert page.total == 2
    assert page.items == [{"id": 1}, {"id": 2}]
