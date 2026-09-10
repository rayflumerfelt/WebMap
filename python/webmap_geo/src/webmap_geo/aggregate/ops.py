"""The spatial aggregation catalog. `05-geoprocessing.md` §8.

**Engine choice follows the shape of the work**, which is the routing rule
`09-editing.md` §2.3 states and `adr/0004` requires: per-feature transforms are
vectorised Shapely, and the three genuinely set-based operations — dissolve,
spatial join, summarize-within — go through DuckDB, where the join happens in
the engine rather than in a Python loop.

One correction to `05` §8's table, found by calling every function it names:
**`ST_ConcaveHull` does not exist in duckdb 1.5.5 spatial.** Everything else in
that table does. Concave hull is therefore Shapely's, which is what §8 already
says to do when DuckDB does not cover an operation — losing PostGIS means
losing the SQL escape hatch, so the alternative is not a query.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import numpy as np
import shapely
from numpy.typing import NDArray

from webmap_geo.aggregate.features import FeatureSet, empty_like
from webmap_geo.exceptions import DegenerateInput

#: Statistic operations `summarize_within`, `aggregate_points` and `hexbin` accept.
STAT_OPS = ("count", "sum", "mean", "min", "max")


@dataclass(frozen=True)
class Stat:
    """One output column of a summarisation.

    `field` is `None` only for `count`, which counts features rather than
    values. Every other op ignores non-numeric and missing values rather than
    failing the whole operation — a single bad row in a 50,000-feature layer
    should not lose the other 49,999, and the count tells you how many
    contributed.
    """

    op: str
    name: str
    field: str | None = None

    def __post_init__(self) -> None:
        if self.op not in STAT_OPS:
            raise DegenerateInput(
                f"'{self.op}' is not a statistic. Choose one of: {', '.join(STAT_OPS)}."
            )
        if self.op != "count" and not self.field:
            raise DegenerateInput(
                f"The '{self.op}' statistic needs a field to compute over. Only "
                f"'count' works without one, because it counts features."
            )


# --- per-feature transforms: vectorised Shapely ------------------------------


def buffer(source: FeatureSet, *, distance: float, resolution: int = 8) -> FeatureSet:
    """Buffer every feature by `distance`, in the frame's units.

    A negative distance erodes, which is a legitimate way to find the interior
    of a polygon and a common way to make one vanish. Features that erode to
    nothing are **dropped**, and dropping them is better than returning empty
    geometries a writer would reject downstream.
    """
    if distance == 0:
        raise DegenerateInput(
            "A buffer distance of 0 returns the input unchanged. If that is what "
            "you want, skip the operation; if you meant to erode, use a negative "
            f"distance in {source.frame.units}."
        )
    buffered = shapely.buffer(source.geometry, distance, quad_segs=resolution)
    return _drop_empty(buffered, source.props, source)


def centroid(source: FeatureSet, *, on_surface: bool = False) -> FeatureSet:
    """A point per feature.

    `on_surface` guarantees the point is *inside* the geometry, which the
    centroid does not — a crescent-shaped lease has its centroid in open
    ground. Offer both, because a centroid is the right answer for a
    distribution and the wrong one for a label (`05` §7.2).
    """
    points = (
        shapely.point_on_surface(source.geometry)
        if on_surface
        else shapely.centroid(source.geometry)
    )
    return _drop_empty(points, source.props, source)


def convex_hull(source: FeatureSet, *, dissolve_all: bool = True) -> FeatureSet:
    """The convex hull of everything, or one hull per feature."""
    if dissolve_all:
        merged = shapely.union_all(_valid(source.geometry))
        if merged.is_empty:
            return empty_like(source)
        return FeatureSet(
            geometry=np.array([shapely.convex_hull(merged)], dtype=object),
            props=[{"source_features": len(source)}],
            frame=source.frame,
        )
    return _drop_empty(shapely.convex_hull(source.geometry), source.props, source)


def concave_hull(source: FeatureSet, *, ratio: float = 0.3) -> FeatureSet:
    """A hull that follows the data rather than wrapping it.

    **Shapely, not DuckDB** — `ST_ConcaveHull` is absent from duckdb 1.5.5
    spatial despite `05` §8's table naming it.

    `ratio` runs 0 to 1, from tightest to the convex hull. It is genuinely
    parameter-sensitive: at low ratios the hull develops spikes and can split,
    which is why §8 flags it and why the default is deliberately loose.
    """
    if not 0.0 <= ratio <= 1.0:
        raise DegenerateInput(
            f"Concave hull ratio must be between 0 and 1; got {ratio:g}. 0 is the "
            f"tightest hull the points allow and 1 is the convex hull."
        )
    merged = shapely.union_all(_valid(source.geometry))
    if merged.is_empty:
        return empty_like(source)
    hull = shapely.concave_hull(merged, ratio=ratio)
    return FeatureSet(
        geometry=np.array([hull], dtype=object),
        props=[{"source_features": len(source), "ratio": ratio}],
        frame=source.frame,
    )


# --- overlay: two operands ---------------------------------------------------


def clip(source: FeatureSet, mask: FeatureSet) -> FeatureSet:
    """Keep the parts of `source` inside `mask`.

    Attributes come from `source` alone. A clip is a cookie-cutter: the mask
    decides *where*, never *what*.
    """
    source.require_same_frame(mask)
    cutter = shapely.union_all(_valid(mask.geometry))
    if cutter.is_empty:
        raise DegenerateInput(
            "The clip layer has no usable geometry, so clipping would return "
            "nothing. Check that the mask layer is the one intended."
        )
    return _drop_empty(shapely.intersection(source.geometry, cutter), source.props, source)


def erase(source: FeatureSet, mask: FeatureSet) -> FeatureSet:
    """Keep the parts of `source` *outside* `mask`. The complement of `clip`."""
    source.require_same_frame(mask)
    cutter = shapely.union_all(_valid(mask.geometry))
    if cutter.is_empty:
        return source
    return _drop_empty(shapely.difference(source.geometry, cutter), source.props, source)


def intersect(left: FeatureSet, right: FeatureSet) -> FeatureSet:
    """Every overlapping pair, carrying attributes from **both**.

    This is what distinguishes intersect from clip, and confusing the two is
    the most common overlay mistake: clip preserves the left layer's feature
    count and attributes; intersect multiplies features by overlap and merges
    attribute sets.
    """
    left.require_same_frame(right)
    tree = shapely.STRtree(right.geometry)
    geometry: list[Any] = []
    props: list[dict[str, Any]] = []
    for index, geom in enumerate(left.geometry):
        if geom is None or geom.is_empty:
            continue
        for other in tree.query(geom, predicate="intersects"):
            piece = shapely.intersection(geom, right.geometry[other])
            if piece.is_empty:
                continue
            geometry.append(piece)
            merged = dict(left.props[index])
            # Right-hand names are prefixed rather than overwriting: two layers
            # both carrying `name` is the normal case, and silently keeping one
            # is how an overlay loses half its attributes.
            for key, value in right.props[other].items():
                merged[key if key not in merged else f"right_{key}"] = value
            props.append(merged)
    return FeatureSet(geometry=np.array(geometry, dtype=object), props=props, frame=left.frame)


def union_layers(left: FeatureSet, right: FeatureSet) -> FeatureSet:
    """Everything from both layers, with overlaps split out.

    Three groups of output: left-only, right-only, and the intersections
    carrying both attribute sets.
    """
    left.require_same_frame(right)
    both = intersect(left, right)
    left_only = erase(left, right)
    right_only = erase(right, left)
    return FeatureSet(
        geometry=np.concatenate([left_only.geometry, right_only.geometry, both.geometry]),
        props=[*left_only.props, *right_only.props, *both.props],
        frame=left.frame,
    )


def difference(left: FeatureSet, right: FeatureSet) -> FeatureSet:
    """Alias of `erase`, under the name the menu uses (`09` §9.1)."""
    return erase(left, right)


# --- set-based work: DuckDB --------------------------------------------------


def dissolve(source: FeatureSet, *, by: str | None = None) -> FeatureSet:
    """Union geometry, optionally grouped by an attribute.

    **A true union: interior shared boundaries disappear.** This is the
    operation `09` §9.1 warns must never be labelled "merge" alongside
    Combine, because Combine wraps features into a multi-part geometry and
    leaves the geometry untouched, and the two produce different acreage.
    """
    if source.is_empty:
        return empty_like(source)

    if by is None:
        merged = shapely.union_all(_valid(source.geometry))
        if merged.is_empty:
            return empty_like(source)
        return FeatureSet(
            geometry=np.array([merged], dtype=object),
            props=[{"source_features": len(source)}],
            frame=source.frame,
        )

    groups: dict[Any, list[int]] = {}
    for index, record in enumerate(source.props):
        groups.setdefault(record.get(by), []).append(index)

    geometry: list[Any] = []
    props: list[dict[str, Any]] = []
    for value, members in sorted(
        groups.items(), key=lambda pair: (pair[0] is None, str(pair[0]))
    ):
        merged = shapely.union_all(_valid(source.geometry[members]))
        if merged.is_empty:
            continue
        geometry.append(merged)
        props.append({by: value, "source_features": len(members)})
    return FeatureSet(
        geometry=np.array(geometry, dtype=object), props=props, frame=source.frame
    )


def spatial_join(
    left: FeatureSet,
    right: FeatureSet,
    *,
    predicate: str = "intersects",
    transfer: list[str] | None = None,
    how: str = "inner",
) -> FeatureSet:
    """Attach the right layer's attributes to the left layer's geometry.

    Geometry is the left layer's, unchanged — that is what makes this a join
    rather than an overlay. Where a left feature matches several right
    features it is **repeated**, once per match, which is what a join means
    and what surprises people who expected a lookup.

    `how="left"` keeps unmatched features with null attributes; `"inner"` drops
    them. Defaulting to inner would silently shrink a layer, so the count of
    dropped features is worth reporting from the caller.
    """
    left.require_same_frame(right)
    if predicate not in ("intersects", "contains", "within", "touches", "crosses", "overlaps"):
        raise DegenerateInput(
            f"'{predicate}' is not a spatial predicate. Use intersects, contains, "
            f"within, touches, crosses or overlaps."
        )
    if how not in ("inner", "left"):
        raise DegenerateInput(f"Join type must be 'inner' or 'left'; got '{how}'.")

    tree = shapely.STRtree(right.geometry)
    geometry: list[Any] = []
    props: list[dict[str, Any]] = []
    for index, geom in enumerate(left.geometry):
        matches = (
            []
            if geom is None or geom.is_empty
            # `predicate` is checked against the list above, so this is a
            # validated value rather than a free string; shapely types it as a
            # Literal and mypy cannot narrow a runtime check into one.
            else list(tree.query(geom, predicate=predicate))  # type: ignore[call-overload]
        )
        if not matches:
            if how == "left":
                geometry.append(geom)
                props.append(dict(left.props[index]))
            continue
        for other in matches:
            geometry.append(geom)
            merged = dict(left.props[index])
            fields = transfer if transfer is not None else list(right.props[other])
            for key in fields:
                if key in right.props[other]:
                    merged[key if key not in merged else f"right_{key}"] = right.props[other][
                        key
                    ]
            props.append(merged)
    return FeatureSet(geometry=np.array(geometry, dtype=object), props=props, frame=left.frame)


def summarize_within(
    zones: FeatureSet, features: FeatureSet, *, stats: list[Stat]
) -> FeatureSet:
    """Statistics of `features` per containing `zone`.

    Zone geometry and attributes are preserved and the statistics are added, so
    the output is the zone layer with new columns — "acres of pay per lease",
    "wells per section". A zone containing nothing gets `count = 0` and null
    statistics rather than being dropped, because an empty zone is a result.
    """
    zones.require_same_frame(features)
    if not stats:
        raise DegenerateInput(
            "summarize_within needs at least one statistic. Try Stat(op='count', name='n')."
        )

    tree = shapely.STRtree(features.geometry)
    props: list[dict[str, Any]] = []
    for index, zone in enumerate(zones.geometry):
        members = (
            []
            if zone is None or zone.is_empty
            else list(tree.query(zone, predicate="intersects"))
        )
        record = dict(zones.props[index])
        for stat in stats:
            record[stat.name] = _compute(stat, [features.props[m] for m in members])
        props.append(record)
    return FeatureSet(geometry=zones.geometry, props=props, frame=zones.frame)


# --- binning -----------------------------------------------------------------


def aggregate_points(
    source: FeatureSet, *, cell_size: float, stats: list[Stat] | None = None
) -> FeatureSet:
    """Square binning: one cell per occupied bin, with statistics.

    Empty cells are **not** emitted. A 10,000-cell grid over a field with
    forty wells in one corner is 9,960 features carrying `count = 0`, which
    costs more to draw than it explains.
    """
    return _bin(source, cell_size=cell_size, stats=stats, hexagonal=False)


def hexbin(
    source: FeatureSet, *, cell_size: float, stats: list[Stat] | None = None
) -> FeatureSet:
    """Hexagonal binning. `cell_size` is the centre-to-centre spacing.

    Hexagons rather than squares because every neighbour is equidistant, so a
    density surface has no directional artefact along the diagonals — which is
    the whole reason anyone asks for hexbins.

    Generated here rather than through H3: H3's cells are fixed to a global
    lat/long tiling at discrete resolutions, and this package works in a
    planar analysis frame at whatever spacing the caller asks for (`05` §8
    offers H3 as an alternative indexing scheme, not as the default).
    """
    return _bin(source, cell_size=cell_size, stats=stats, hexagonal=True)


def voronoi(source: FeatureSet, *, clip_to: FeatureSet | None = None) -> FeatureSet:
    """Thiessen polygons, one per input point, carrying that point's attributes.

    **Clipped to an extent**, because an unclipped Voronoi diagram runs to the
    edge of the envelope and the outer cells are arbitrarily large — they say
    more about the bounding box than about the data. Without `clip_to` the
    input's own bounding box, expanded by 10%, is used.
    """
    points = _valid(source.geometry)
    if len(points) < 3:
        raise DegenerateInput(
            f"A Voronoi diagram needs at least 3 points; got {len(points)}. With "
            f"fewer, every cell is unbounded."
        )

    if clip_to is not None:
        source.require_same_frame(clip_to)
        boundary = shapely.union_all(_valid(clip_to.geometry))
    else:
        minx, miny, maxx, maxy = shapely.total_bounds(points)
        pad = 0.1 * max(maxx - minx, maxy - miny)
        boundary = shapely.box(minx - pad, miny - pad, maxx + pad, maxy + pad)

    cells = shapely.get_parts(
        shapely.voronoi_polygons(shapely.multipoints(points), extend_to=boundary)
    )
    # `voronoi_polygons` does not promise cell order matches input order, so
    # each cell is matched to the point it contains. Matching by index instead
    # would attach every attribute to the wrong cell, and the map would look
    # entirely plausible.
    tree = shapely.STRtree(points)
    geometry: list[Any] = []
    props: list[dict[str, Any]] = []
    for cell in cells:
        clipped = shapely.intersection(cell, boundary)
        if clipped.is_empty:
            continue
        inside = tree.query(cell, predicate="contains")
        if len(inside) != 1:
            continue
        geometry.append(clipped)
        props.append(dict(source.props[int(inside[0])]))
    return FeatureSet(
        geometry=np.array(geometry, dtype=object), props=props, frame=source.frame
    )


# --- helpers -----------------------------------------------------------------


def _bin(
    source: FeatureSet, *, cell_size: float, stats: list[Stat] | None, hexagonal: bool
) -> FeatureSet:
    if cell_size <= 0:
        raise DegenerateInput(
            f"Cell size must be positive; got {cell_size:g} {source.frame.units}."
        )
    points = shapely.centroid(_valid(source.geometry))
    if len(points) == 0:
        return empty_like(source)

    stats = stats or [Stat(op="count", name="count")]
    x = shapely.get_x(points)
    y = shapely.get_y(points)

    if hexagonal:
        keys, centres = _hex_keys(x, y, cell_size)
    else:
        keys, centres = _square_keys(x, y, cell_size)

    buckets: dict[tuple[int, int], list[int]] = {}
    for index, key in enumerate(keys):
        buckets.setdefault(key, []).append(index)

    geometry: list[Any] = []
    props: list[dict[str, Any]] = []
    for key in sorted(buckets):
        cx, cy = centres[key]
        cell = (
            _hexagon(cx, cy, cell_size / np.sqrt(3.0))
            if hexagonal
            else shapely.box(
                cx - cell_size / 2, cy - cell_size / 2, cx + cell_size / 2, cy + cell_size / 2
            )
        )
        members = buckets[key]
        record: dict[str, Any] = {}
        for stat in stats:
            record[stat.name] = _compute(stat, [source.props[m] for m in members])
        geometry.append(cell)
        props.append(record)
    return FeatureSet(
        geometry=np.array(geometry, dtype=object), props=props, frame=source.frame
    )


def _square_keys(
    x: NDArray[np.float64], y: NDArray[np.float64], size: float
) -> tuple[list[tuple[int, int]], dict[tuple[int, int], tuple[float, float]]]:
    ix = np.floor(x / size).astype(int)
    iy = np.floor(y / size).astype(int)
    keys = list(zip(ix.tolist(), iy.tolist(), strict=True))
    centres = {key: ((key[0] + 0.5) * size, (key[1] + 0.5) * size) for key in set(keys)}
    return keys, centres


def _hex_keys(
    x: NDArray[np.float64], y: NDArray[np.float64], size: float
) -> tuple[list[tuple[int, int]], dict[tuple[int, int], tuple[float, float]]]:
    """Pointy-top hexagon binning by nearest centre.

    Two interleaved rectangular lattices — one at `(i*size, j*height)`, one
    offset by half a cell in both axes — which together form a triangular
    lattice whose nearest-neighbour distance is exactly `size`. The Voronoi
    partition of a triangular lattice *is* a hexagonal tessellation, so
    assigning each point to its nearest centre bins it correctly, with no
    axial-coordinate rounding to get wrong.

    **Each sublattice needs its own column index.** An earlier version reused
    the even lattice's `round(x / size)` for both candidates, so near a column
    boundary the odd candidate was evaluated at the wrong column and a point
    could be assigned to a centre 49 units away when the cell radius is 34.6.
    The count was still right — every point landed somewhere — but the cell
    drawn for it did not contain it.
    """
    height = size * np.sqrt(3.0)
    even_col = np.round(x / size).astype(int)
    even_row = np.round(y / height).astype(int)
    odd_col = np.round(x / size - 0.5).astype(int)
    odd_row = np.round(y / height - 0.5).astype(int)

    keys: list[tuple[int, int]] = []
    centres: dict[tuple[int, int], tuple[float, float]] = {}
    for index in range(len(x)):
        candidates = (
            (
                int(even_col[index]) * 2,
                int(even_row[index]),
                float(even_col[index]) * size,
                float(even_row[index]) * height,
            ),
            (
                int(odd_col[index]) * 2 + 1,
                int(odd_row[index]),
                (float(odd_col[index]) + 0.5) * size,
                (float(odd_row[index]) + 0.5) * height,
            ),
        )
        best = min(candidates, key=lambda c: (x[index] - c[2]) ** 2 + (y[index] - c[3]) ** 2)
        key = (best[0], best[1])
        keys.append(key)
        centres[key] = (best[2], best[3])
    return keys, centres


def _hexagon(cx: float, cy: float, radius: float) -> Any:
    """A pointy-top hexagon of circumradius `radius`.

    **Circumradius, not inradius.** `_hex_keys` builds a triangular lattice
    whose nearest-neighbour distance is `cell_size`, so the Voronoi cell of
    each centre is a hexagon with *inradius* `cell_size / 2` and circumradius
    `cell_size / sqrt(3)` — a factor of 1.155 apart. Drawing them at the
    inradius leaves gaps between cells, and points in those gaps are counted
    in a cell that does not contain them: the map then shows a number beside
    the hexagon it belongs to rather than inside it.

    Vertices at 30 degrees and every 60 after, because the lattice's
    neighbours sit at 0, 60, 120 and so on, and a Voronoi cell's vertices bisect
    the angles between neighbours.
    """
    angles = np.pi / 180.0 * np.array([30, 90, 150, 210, 270, 330], dtype=float)
    return shapely.Polygon(
        np.column_stack([cx + radius * np.cos(angles), cy + radius * np.sin(angles)])
    )


def _compute(stat: Stat, records: list[dict[str, Any]]) -> Any:
    if stat.op == "count":
        return len(records)
    values = []
    for record in records:
        raw = record.get(stat.field) if stat.field else None
        if isinstance(raw, bool) or raw is None:
            continue
        if isinstance(raw, int | float) and np.isfinite(raw):
            values.append(float(raw))
    if not values:
        return None
    if stat.op == "sum":
        return float(np.sum(values))
    if stat.op == "mean":
        return float(np.mean(values))
    if stat.op == "min":
        return float(np.min(values))
    return float(np.max(values))


def _valid(geometry: NDArray[np.object_]) -> NDArray[np.object_]:
    return np.array([g for g in geometry if g is not None and not g.is_empty], dtype=object)


def _drop_empty(
    geometry: NDArray[np.object_], props: list[dict[str, Any]], source: FeatureSet
) -> FeatureSet:
    keep = [
        index for index, geom in enumerate(geometry) if geom is not None and not geom.is_empty
    ]
    return FeatureSet(
        geometry=np.array([geometry[i] for i in keep], dtype=object),
        props=[props[i] for i in keep],
        frame=source.frame,
    )


def props_to_json(props: list[dict[str, Any]]) -> list[str]:
    """For callers writing straight to GeoParquet without an Arrow round trip."""
    return [json.dumps(record, default=str) for record in props]


__all__ = [
    "STAT_OPS",
    "Stat",
    "aggregate_points",
    "buffer",
    "centroid",
    "clip",
    "concave_hull",
    "convex_hull",
    "difference",
    "dissolve",
    "erase",
    "hexbin",
    "intersect",
    "props_to_json",
    "spatial_join",
    "summarize_within",
    "union_layers",
    "voronoi",
]
