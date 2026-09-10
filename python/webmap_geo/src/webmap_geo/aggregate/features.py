"""The value object every aggregation takes and returns.

`05-geoprocessing.md` §8 sketched this as a `pyarrow.Table`. A frozen dataclass
carrying Shapely geometry and plain dicts is what the rest of this package
already uses — `ControlPoints`, `ContourBand`, `LabelAnchor` — and it is what
`webmap_io.write_features` consumes, so the common path involves no conversion
at all. `to_arrow` and `from_arrow` keep the Arrow boundary the specification
asked for, for the callers that want it.

**The frame travels with the features.** Every operation here is a distance,
area or containment question, and `05` §8's rule is that all of them run in the
project analysis CRS. Carrying the frame on the value rather than passing it
alongside is what makes a mismatch a `FrameMismatch` rather than a wrong answer
in degrees.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import numpy as np
import shapely
from numpy.typing import NDArray

from webmap_geo.exceptions import DegenerateInput, FrameMismatch
from webmap_geo.frame import AnalysisFrame


@dataclass(frozen=True)
class FeatureSet:
    """Geometry and attributes for one layer, in one analysis frame."""

    geometry: NDArray[np.object_]
    props: list[dict[str, Any]]
    frame: AnalysisFrame

    def __post_init__(self) -> None:
        if len(self.geometry) != len(self.props):
            raise DegenerateInput(
                f"A feature set has {len(self.geometry)} geometries and "
                f"{len(self.props)} property records. They are zipped by position, "
                f"so a mismatch would attach every attribute after the first gap to "
                f"the wrong feature."
            )

    def __len__(self) -> int:
        return len(self.geometry)

    @property
    def is_empty(self) -> bool:
        return len(self.geometry) == 0

    def require_same_frame(self, other: FeatureSet) -> None:
        """Both operands of an overlay must be in one frame.

        Not a formality: an intersection between arrays in different frames
        returns an empty result rather than an error, because the coordinates
        simply do not overlap. That reads as "these layers do not touch".
        """
        if self.frame != other.frame:
            raise FrameMismatch(
                f"These layers are in different analysis frames — "
                f"{self.frame.describe()} and {other.frame.describe()}. Overlaying "
                f"them would silently return nothing, because the coordinates do "
                f"not occupy the same space. Reproject one before this call."
            )

    def to_arrow(self) -> Any:
        """WKB geometry and JSON props, the shape `webmap_io` reads and writes."""
        import pyarrow as pa

        return pa.table(
            {
                "geometry": pa.array(
                    [None if g is None else shapely.to_wkb(g) for g in self.geometry],
                    type=pa.binary(),
                ),
                "props": pa.array(
                    [json.dumps(p, default=str) for p in self.props], type=pa.string()
                ),
            }
        )

    @classmethod
    def from_arrow(cls, table: Any, frame: AnalysisFrame) -> FeatureSet:
        raw = table.column("geometry").to_pylist()
        geometry = np.array(
            [None if g is None else shapely.from_wkb(g) for g in raw], dtype=object
        )
        if "props" in table.column_names:
            props = [json.loads(p) if p else {} for p in table.column("props").to_pylist()]
        else:
            props = [{} for _ in raw]
        return cls(geometry=geometry, props=props, frame=frame)


def empty_like(source: FeatureSet) -> FeatureSet:
    return FeatureSet(geometry=np.array([], dtype=object), props=[], frame=source.frame)


__all__ = ["FeatureSet", "empty_like", "read_feature_set"]


def read_feature_set(
    parquet_key: str,
    frame: AnalysisFrame,
    store: Any = None,
    *,
    where: str | None = None,
    limit: int | None = None,
) -> FeatureSet:
    """Load a whole feature layer out of GeoParquet.

    The counterpart of `webmap_geo.control.read_control_points`, which reads
    only coordinates and one numeric column because that is all interpolation
    needs. Aggregation needs the geometry itself and every attribute, so this
    returns WKB and the props JSON and reconstitutes both.

    **Geometry comes back as WKB in both encodings**, which is what makes this
    work against a real GeoParquet object *and* a hand-built fixture. A
    GeoParquet file written by the ingest pipeline gives DuckDB a `GEOMETRY`
    column; one without the `geo` metadata gives a `BLOB`. `control.py`
    records what happens when a reader assumes one of those — it passed
    fourteen unit tests over fixtures and failed on every real dataset.

    `limit` is a guard, not a feature: an operation that would materialise
    millions of geometries in Python should fail with a number rather than
    consume the worker.
    """
    from webmap_geo.control import _geometry_expression
    from webmap_geo.dataplane import connect

    predicate = f" WHERE {where}" if where else ""
    cap = f" LIMIT {int(limit)}" if limit else ""

    with connect(store) as conn:
        geometry_expr = _geometry_expression(conn, parquet_key)
        rows = conn.execute(
            f"""
            SELECT ST_AsWKB({geometry_expr}) AS wkb, props
            FROM read_parquet($key){predicate}{cap}
            """,
            {"key": parquet_key},
        ).fetchall()

    geometry: list[Any] = []
    props: list[dict[str, Any]] = []
    for wkb, raw in rows:
        if wkb is None:
            continue
        geometry.append(shapely.from_wkb(bytes(wkb)))
        props.append(json.loads(raw) if raw else {})

    return FeatureSet(geometry=np.array(geometry, dtype=object), props=props, frame=frame)
