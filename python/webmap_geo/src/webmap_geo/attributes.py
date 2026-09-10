"""Attribute reads from the data plane. `07-frontend.md` §10.

Separate from `tiles.py` because it reads **no geometry**. That is the whole
point: an attribute table needs values and a row count, and pulling geometry it
will never draw is the difference between a page that arrives instantly and one
that transfers megabytes of coordinates to display a column of porosities.

It lives in `webmap_geo` regardless, because DuckDB and the object store are
confined here (`01-architecture.md` §3.2) — the boundary is about which package
may open a GeoParquet file, not only about which may compute on geometry.

**Paging is why this exists.** The GeoJSON endpoint refuses above 5,000
features rather than truncating (`06-rendering.md` §7.1), which is right for a
map source and useless for a table: it left a 500k-feature layer with no
attribute view at all. A page is honest about being a page.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from webmap_geo.dataplane import ObjectStore, connect
from webmap_geo.exceptions import DegenerateInput

#: One screen of a dense table is about forty rows; a page of 500 covers a fast
#: scroll without a round trip. Above a few thousand the JSON itself becomes the
#: cost, which is what the cap prevents.
DEFAULT_PAGE = 500
MAX_PAGE = 5_000


@dataclass(frozen=True)
class AttributePage:
    """One page of attributes, and enough context to place it.

    `total` is the whole layer, not the page — a table that shows 500 rows and
    says nothing about the other 499,500 is a table someone draws a conclusion
    from.
    """

    items: list[dict[str, Any]]
    total: int
    offset: int
    limit: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


def feature_attributes(
    parquet_key: str,
    store: ObjectStore | None = None,
    *,
    offset: int = 0,
    limit: int = DEFAULT_PAGE,
    order_by: str | None = None,
    descending: bool = False,
) -> AttributePage:
    """A page of a layer's attributes, without its geometry.

    Ordered by `id` when no column is named. **An unordered page is not a
    page**: Parquet gives no ordering guarantee across scans, so paging without
    an ORDER BY can return the same row twice and skip another — and the reader
    would have no way to tell.

    `order_by` is a column name from the layer's own attribute schema. It is
    interpolated into SQL, so it is validated against the columns the file
    actually has rather than trusted.
    """
    if offset < 0:
        raise DegenerateInput(f"Page offset must not be negative; got {offset}.")
    limit = max(1, min(limit, MAX_PAGE))

    with connect(store) as conn:
        counted = conn.execute(
            "SELECT count(*) FROM read_parquet($key)", {"key": parquet_key}
        ).fetchone()
        if counted is None:
            # `count(*)` always returns a row; None here means the file could
            # not be opened at all, and a zero-row table would be the wrong
            # thing to report for a missing one.
            raise DegenerateInput(
                f"Could not read {parquet_key}. The dataset is registered, so "
                f"this is a storage problem rather than a permission one."
            )
        total = int(counted[0])

        direction = "DESC" if descending else "ASC"

        # **The ordering is total, and every expression carries its own
        # direction.** Two things went wrong here that the tests caught:
        #
        # - `ORDER BY a, b DESC` applies DESC to `b` alone, so a descending
        #   sort came back ascending. SQL attaches direction per expression,
        #   not to the clause.
        # - Tiebreaking on the same column twice is not a tiebreak. 1,200 rows
        #   over 180 distinct porosities means constant ties, and a page
        #   boundary landing inside one duplicated a row and skipped another.
        #   `id` last makes the order total, which is what paging requires.
        #
        # NULLS LAST in both directions: a descending sort of a sparse column
        # that opens with a screen of blanks looks broken, and the reader
        # wanted the largest values, not the missing ones.
        terms = [f"id {direction}"]
        if order_by is not None:
            # The keys of the props map, which is the layer's attribute schema
            # as stored. Read from the file rather than from the registry so a
            # page cannot be sorted by a column the file does not have. Looked
            # up only when there is a column to validate — it is a scan of the
            # props column, and an unsorted page has nothing to check.
            available = attribute_names(conn, parquet_key)
            if order_by not in available:
                raise DegenerateInput(
                    f"'{order_by}' is not an attribute of this layer. Available "
                    f"attributes: {', '.join(sorted(available)) or 'none'}."
                )
            terms = [
                # Numeric first: a numeric column sorted as text puts 100
                # before 20, which reads as a broken table rather than as a
                # sort-order decision. `try_cast` yields NULL for a text value,
                # which the second term then orders.
                f"try_cast(props->>'{order_by}' AS DOUBLE) {direction} NULLS LAST",
                f"props->>'{order_by}' {direction} NULLS LAST",
                "id ASC",
            ]
        order_clause = ", ".join(terms)

        rows = conn.execute(
            f"""
            SELECT id, props
            FROM read_parquet($key)
            ORDER BY {order_clause}
            LIMIT $limit OFFSET $offset
            """,
            {"key": parquet_key, "limit": limit, "offset": offset},
        ).fetchall()

    items = [{"id": row[0], **(json.loads(row[1]) if row[1] else {})} for row in rows]
    return AttributePage(items=items, total=total, offset=offset, limit=limit)


#: How many distinct values the category table will list.
#:
#: `07` §6.2 says "the top few hundred". Two hundred rows is already more than
#: anybody assigns colours to by hand, and each carries a colour picker — past
#: this the dialog is the bottleneck rather than the query.
MAX_CATEGORIES = 200

#: Past this many distinct values, enumeration is **refused** rather than
#: truncated.
#:
#: `07` §6.2: "the endpoint refuses to enumerate a column above a cardinality
#: threshold rather than hanging the dialog". A well-name column on 500k
#: features has 500k distinct values and no useful colour mapping, and the
#: honest answer is to say so — a truncated list of 200 well names looks like a
#: list somebody could finish.
MAX_DISTINCT = 5_000

#: Bins in the histogram drawn under a ramp. Enough to show a bimodal
#: distribution or a spike at zero at any panel width; more would be narrower
#: than a pixel in a 280 px panel.
HISTOGRAM_BINS = 40

#: How much of a column has to cast for `kind='auto'` to call it numeric.
#:
#: Not all of it: one `N/A` in a porosity column is a data-entry slip, and
#: demoting the column to text over it takes the ramp away from a layer that
#: plainly has one. Deliberately high all the same — a column that is 90%
#: numbers and 10% words is a mixed column, and a ramp would hide the words.
NUMERIC_THRESHOLD = 0.98


@dataclass(frozen=True)
class CategorySummary:
    """Distinct values of a text column, most common first."""

    values: list[tuple[str, int]]
    #: Distinct values beyond the ones returned. They take the *Other* colour,
    #: and the dialog says how many there are.
    remaining: int
    #: `(distinct, limit)` when the column was past `MAX_DISTINCT` and not
    #: enumerated at all.
    refused: tuple[int, int] | None = None


@dataclass(frozen=True)
class NumericSummary:
    """A numeric column's shape, for the ramp editor's underlay."""

    minimum: float
    maximum: float
    counts: list[int]
    #: Rows whose value is null or does not cast. Reported rather than dropped
    #: silently: "why does my histogram hold 900 wells when the layer has
    #: 1,200" has an answer, and it is usually a column added mid-campaign.
    missing: int


def attribute_summary(
    parquet_key: str,
    column: str,
    store: ObjectStore | None = None,
    *,
    kind: str = "auto",
    max_categories: int = MAX_CATEGORIES,
    bins: int = HISTOGRAM_BINS,
) -> CategorySummary | NumericSummary:
    """Summarise one column for the formatting dialog.

    A text column gives distinct values with counts; a numeric one gives a
    range and a histogram. `kind='auto'` decides by trying to cast, because a
    column of numbers stored as strings is ordinary in a shapefile and reading
    it as categories offers a picker with 1,200 entries where a ramp was
    wanted.

    **The column name is bound, not interpolated.** It is validated against the
    file's own attribute names first — a name that is not there is a mistake
    worth a message — and then passed as a parameter, so a props key containing
    a quote is data rather than syntax.
    """
    with connect(store) as conn:
        available = attribute_names(conn, parquet_key)
        if column not in available:
            listed = sorted(available)
            raise DegenerateInput(
                f"'{column}' is not a column of this layer. It has: "
                f"{', '.join(listed[:20])}{'…' if len(listed) > 20 else ''}."
            )

        resolved = kind if kind != "auto" else _infer_kind(conn, parquet_key, column)
        if resolved == "number":
            return _numeric_summary(conn, parquet_key, column, bins)
        return _category_summary(conn, parquet_key, column, max_categories)


def _infer_kind(conn: Any, parquet_key: str, column: str) -> str:
    row = conn.execute(
        """
        SELECT
            count(*) FILTER (WHERE value IS NOT NULL) AS present,
            count(*) FILTER (WHERE TRY_CAST(value AS DOUBLE) IS NOT NULL) AS numeric
        FROM (SELECT props ->> $col AS value FROM read_parquet($key))
        """,
        {"key": parquet_key, "col": column},
    ).fetchone()
    present, numeric = int(row[0] or 0), int(row[1] or 0)
    return "number" if present > 0 and numeric / present >= NUMERIC_THRESHOLD else "text"


def _category_summary(
    conn: Any, parquet_key: str, column: str, max_categories: int
) -> CategorySummary:
    """Distinct values with counts, most common first.

    **The distinct count is taken before the values are.** One extra aggregate,
    and it is what makes the refusal possible: enumerating 500,000 values to
    discover there are 500,000 of them is the hang the refusal exists to
    prevent.
    """
    distinct = int(
        conn.execute(
            "SELECT count(DISTINCT props ->> $col) FROM read_parquet($key)",
            {"key": parquet_key, "col": column},
        ).fetchone()[0]
        or 0
    )
    if distinct > MAX_DISTINCT:
        return CategorySummary(values=[], remaining=distinct, refused=(distinct, MAX_DISTINCT))

    rows = conn.execute(
        """
        SELECT props ->> $col AS value, count(*) AS n
        FROM read_parquet($key)
        WHERE props ->> $col IS NOT NULL
        GROUP BY 1
        ORDER BY n DESC, value ASC
        LIMIT $cap
        """,
        {"key": parquet_key, "col": column, "cap": int(max_categories)},
    ).fetchall()

    values = [(str(row[0]), int(row[1])) for row in rows]
    return CategorySummary(values=values, remaining=max(0, distinct - len(values)))


def _numeric_summary(conn: Any, parquet_key: str, column: str, bins: int) -> NumericSummary:
    row = conn.execute(
        """
        SELECT min(value), max(value),
               count(*) FILTER (WHERE value IS NULL) AS missing
        FROM (SELECT TRY_CAST(props ->> $col AS DOUBLE) AS value FROM read_parquet($key))
        """,
        {"key": parquet_key, "col": column},
    ).fetchone()

    low_raw, high_raw, missing = row[0], row[1], int(row[2] or 0)
    if low_raw is None or high_raw is None:
        raise DegenerateInput(
            f"'{column}' has no numeric values. Check the column name, and that "
            f"the values are numbers rather than text with units in them."
        )
    low, high = float(low_raw), float(high_raw)

    if high <= low:
        # A constant column. One bin holding everything is the honest picture,
        # and the alternative — dividing by a zero width — is a crash.
        total = int(
            conn.execute(
                """
                SELECT count(*) FROM read_parquet($key)
                WHERE TRY_CAST(props ->> $col AS DOUBLE) IS NOT NULL
                """,
                {"key": parquet_key, "col": column},
            ).fetchone()[0]
            or 0
        )
        return NumericSummary(minimum=low, maximum=high, counts=[total], missing=missing)

    # Binned in SQL rather than by pulling the column into Python: a 500,000-row
    # column is 4 MB of doubles over the wire to compute forty integers.
    rows = conn.execute(
        """
        SELECT bin, count(*) AS n FROM (
            SELECT least($bins - 1, CAST(floor((value - $low) / $width) AS INTEGER)) AS bin
            FROM (SELECT TRY_CAST(props ->> $col AS DOUBLE) AS value FROM read_parquet($key))
            WHERE value IS NOT NULL
        )
        GROUP BY bin ORDER BY bin
        """,
        {
            "key": parquet_key,
            "col": column,
            "bins": int(bins),
            "low": low,
            "width": (high - low) / bins,
        },
    ).fetchall()

    counts = [0] * bins
    for bin_index, count in rows:
        index = int(bin_index)
        if 0 <= index < bins:
            counts[index] = int(count)
    return NumericSummary(minimum=low, maximum=high, counts=counts, missing=missing)


def attribute_names(conn: Any, parquet_key: str) -> set[str]:
    """Every attribute name present anywhere in the file.

    Read from the data rather than from the Parquet schema, because attributes
    live inside a JSON `props` column — the schema knows there is a map, not
    what is in it.

    **The whole column, not a sample.** This sampled the first 200 rows until
    a layer of 1,200 wells where porosity was added 200 wells into the
    programme reported that it had no porosity at all: attributes are sparse
    *by campaign*, so a field added partway through is absent from exactly the
    rows a head sample reads. The consequence was a column that could not be
    sorted on, and — once gridding used this — a surface that could not be
    built from a column holding a thousand values.

    Pushed into DuckDB rather than done in Python: measured at 76 ms over
    500,000 rows against 1 ms for the 200-row sample that gave the wrong
    answer. Callers pay it only when they have a column name to validate.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT unnest(json_keys(props)) AS name
        FROM read_parquet($key)
        WHERE props IS NOT NULL
        """,
        {"key": parquet_key},
    ).fetchall()
    return {str(row[0]) for row in rows}


__all__ = [
    "DEFAULT_PAGE",
    "HISTOGRAM_BINS",
    "MAX_CATEGORIES",
    "MAX_DISTINCT",
    "MAX_PAGE",
    "NUMERIC_THRESHOLD",
    "AttributePage",
    "CategorySummary",
    "NumericSummary",
    "attribute_names",
    "attribute_summary",
    "feature_attributes",
]
