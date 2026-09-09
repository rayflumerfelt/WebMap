"""Reading control points out of a stored point layer.

Interpolation takes `(n, 2)` coordinates and `(n,)` values. Getting them out
of a GeoParquet object is a geometry read, so it lives here rather than in
`webmap_core` (`adr/0004-geoprocessing-owns-geometry.md`).

**Nothing here reprojects.** The `AnalysisFrame` declares the frame the stored
coordinates are already in; if that is not the analysis frame, the caller has
a bug that `05` §2.2 says to fix at the boundary, not inside a reader.

What this module mostly does is *count what it dropped*. A control set that
silently loses 340 of 1,200 wells to a null porosity column produces a
perfectly plausible surface built on a third less data, and nothing about the
finished map says so.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from webmap_geo.attributes import attribute_names
from webmap_geo.dataplane import ObjectStore, connect
from webmap_geo.exceptions import DegenerateInput
from webmap_geo.frame import AnalysisFrame

#: Below this, a variogram cannot be estimated and a kriged surface is mostly
#: the mean. `05` §6.2 puts the practical floor higher; this is the hard one.
MIN_CONTROL_POINTS = 3


@dataclass(frozen=True)
class ControlPoints:
    """Coordinates, values, and an account of what did not make it.

    `n_dropped_*` are not statistics for their own sake. They feed the job's
    warnings and the grid's caption, which is the only place a reader of the
    finished map will learn that a third of the wells had no value in the
    column that was gridded.
    """

    coords: NDArray[np.float64]
    values: NDArray[np.float64]
    frame: AnalysisFrame
    value_column: str
    #: Rows whose value column was null or non-numeric.
    n_dropped_no_value: int
    #: Rows whose geometry was not a point — a line or polygon in a layer
    #: registered as a pointset.
    n_dropped_not_a_point: int
    #: Points sharing a location with another. Not removed: two picks at one
    #: surface location is a real thing, and averaging them is a geological
    #: decision this module has no standing to make. Reported because it is
    #: also what a duplicated import looks like.
    n_coincident: int

    def __len__(self) -> int:
        return len(self.values)

    @property
    def n_dropped(self) -> int:
        return self.n_dropped_no_value + self.n_dropped_not_a_point

    def bounds(self) -> tuple[float, float, float, float]:
        """(xmin, ymin, xmax, ymax) of the control, in the analysis frame."""
        return (
            float(self.coords[:, 0].min()),
            float(self.coords[:, 1].min()),
            float(self.coords[:, 0].max()),
            float(self.coords[:, 1].max()),
        )

    def warnings(self) -> list[str]:
        """Sentences, not counts. These reach a caption and a chat reply."""
        messages: list[str] = []
        total = len(self.values) + self.n_dropped

        if self.n_dropped_no_value:
            share = self.n_dropped_no_value / total
            messages.append(
                f"{self.n_dropped_no_value:,} of {total:,} points ({share:.0%}) had "
                f"no numeric '{self.value_column}' and were not gridded. The "
                f"surface is built from {len(self.values):,} points, and it looks "
                f"the same either way."
            )
        if self.n_dropped_not_a_point:
            messages.append(
                f"{self.n_dropped_not_a_point:,} features were not points and were "
                f"skipped. Interpolation needs point control — a line or polygon "
                f"in a pointset layer usually means the wrong layer was chosen."
            )
        if self.n_coincident:
            messages.append(
                f"{self.n_coincident:,} points share a location with another. That "
                f"is ordinary for deviated wells picking the same surface, and it "
                f"is also what a doubled import looks like — worth a glance if you "
                f"did not expect it."
            )
        return messages

    def describe(self) -> str:
        return (
            f"{len(self.values):,} control points, {self.value_column} "
            f"{self.values.min():g} to {self.values.max():g}, "
            f"{self.frame.describe()}"
        )


def read_control_points(
    parquet_key: str,
    value_column: str,
    frame: AnalysisFrame,
    store: ObjectStore | None = None,
    *,
    where: str | None = None,
) -> ControlPoints:
    """Load `(coords, values)` from a stored point layer.

    `value_column` is an attribute name, interpolated into SQL and therefore
    validated against the columns the file actually has — the same rule
    `attributes.feature_attributes` follows. The error lists what is
    available, because "no such column" with nothing else is the least useful
    thing to hand back to a conversation trying to grid something.

    `where` is an optional SQL predicate over `props`, for gridding a subset
    (one formation out of a layer that holds several). It is *not* user text:
    callers build it from validated inputs.
    """
    with connect(store) as conn:
        available = attribute_names(conn, parquet_key)
        if value_column not in available:
            raise DegenerateInput(
                f"'{value_column}' is not an attribute of this layer. Available: "
                f"{', '.join(sorted(available)) or 'none'}. Interpolation needs a "
                f"numeric column — pick one of those, or check that the layer that "
                f"was chosen is the one holding the picks."
            )

        predicate = f" AND ({where})" if where else ""

        # **Two queries, because ST_X raises on a non-point** rather than
        # returning NULL — a line in a pointset layer would abort the read
        # instead of being counted. Guarding with a CASE would work only as
        # long as DuckDB evaluates it lazily, which is not something to build
        # a data read on.
        tally = conn.execute(
            f"""
            SELECT ST_GeometryType(ST_GeomFromWKB(geometry)) = 'POINT' AS is_point,
                   count(*) AS n
            FROM read_parquet($key)
            WHERE 1 = 1{predicate}
            GROUP BY 1
            """,
            {"key": parquet_key},
        ).fetchall()
        counts = {bool(row[0]): int(row[1]) for row in tally}

        rows = conn.execute(
            f"""
            SELECT ST_X(geom) AS x,
                   ST_Y(geom) AS y,
                   try_cast(props->>'{value_column}' AS DOUBLE) AS value
            FROM (
                SELECT ST_GeomFromWKB(geometry) AS geom, props
                FROM read_parquet($key)
                WHERE 1 = 1{predicate}
            )
            WHERE ST_GeometryType(geom) = 'POINT'
            """,
            {"key": parquet_key},
        ).fetchall()

    if not counts:
        raise DegenerateInput(
            f"No features came back from {parquet_key}"
            + (f" matching {where}. " if where else ". ")
            + "There is nothing to interpolate."
        )

    return _assemble(rows, counts.get(False, 0), value_column, frame)


def _assemble(
    rows: list[tuple[float, float, float | None]],
    not_a_point: int,
    value_column: str,
    frame: AnalysisFrame,
) -> ControlPoints:
    """Split the point rows into what is usable and what was lost.

    Geometry and values are counted separately because they fail for different
    reasons and want different fixes: a line in a pointset layer means the
    wrong layer, and a null value means the wrong column.
    """
    # A null value column and a non-numeric one are the same failure to a
    # reader: the column does not hold a number for this row.
    usable = [row for row in rows if row[2] is not None]
    no_value = len(rows) - len(usable)
    total = len(rows) + not_a_point

    if len(usable) < MIN_CONTROL_POINTS:
        raise DegenerateInput(
            f"Only {len(usable)} of {total:,} features have both a point "
            f"geometry and a numeric '{value_column}' — too few to interpolate. "
            f"{no_value:,} had no numeric value in that column and "
            f"{not_a_point:,} were not points. Check the column name and the "
            f"layer's geometry type."
        )

    coords = np.asarray([(row[0], row[1]) for row in usable], dtype=np.float64)
    values = np.asarray([row[2] for row in usable], dtype=np.float64)

    finite = np.isfinite(values) & np.isfinite(coords).all(axis=1)
    # NaN reaches here from a stored NaN rather than from a null, which
    # `try_cast` would have made None. Same consequence, same counter.
    no_value += int((~finite).sum())
    coords, values = coords[finite], values[finite]

    if len(values) < MIN_CONTROL_POINTS:
        raise DegenerateInput(
            f"Only {len(values)} control points have finite coordinates and "
            f"values. A coordinate that is not finite usually means the layer "
            f"was written in a geographic CRS and reprojected badly."
        )

    return ControlPoints(
        coords=coords,
        values=values,
        frame=frame,
        value_column=value_column,
        n_dropped_no_value=no_value,
        n_dropped_not_a_point=not_a_point,
        n_coincident=_count_coincident(coords),
    )


def _count_coincident(coords: NDArray[np.float64]) -> int:
    """How many points share a location with an earlier one.

    Exact equality rather than a tolerance: this is looking for a doubled
    import and for wells that genuinely share a surface location, both of
    which produce bit-identical coordinates. A tolerance would instead
    measure well spacing, which is a different question.
    """
    unique = np.unique(coords, axis=0)
    return int(len(coords) - len(unique))


__all__ = ["MIN_CONTROL_POINTS", "ControlPoints", "read_control_points"]
