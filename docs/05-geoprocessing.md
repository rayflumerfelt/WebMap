# 05 — Geoprocessing

Package: `python/webmap_geo`. Pure computation. **No database, no HTTP, no framework
imports.** Takes NumPy arrays and Shapely geometries, returns NumPy arrays and Shapely
geometries. This isolation is what makes it testable against Surfer reference outputs and
separately versionable.

---

## 1. Dependencies

```toml
[project]
name = "webmap-geo"
requires-python = ">=3.12"
dependencies = [
    "duckdb>=1.5",         # data plane: GeoParquet reads, spatial ops, MVT
    "numpy>=2.0",
    "scipy>=1.14",
    "shapely>=2.0",
    "gstools>=1.6",        # variogram models, random fields
    "scikit-gstat>=1.0",   # experimental variogram estimation, binning
    "contourpy>=1.3",      # contour extraction (matplotlib's engine)
    "triangle>=20230923",  # Shewchuk constrained Delaunay
    "pyamg>=5.2",          # algebraic multigrid for sparse solves
    "pyproj>=3.6",
]
```

Deliberately **not** used: PyKrige (global solve only — a moving neighbourhood is not
optional at this scale), verde (no fault constraints, and we need the mesh anyway).

---

## 2. Coordinate reference systems

`webmap_geo.crs` owns `pyproj`. It is the only module in the repository permitted to import
it, and `webmap_core.crs.CrsContext` is a thin wrapper over this one rather than a parallel
implementation. See `adr/0003-geoprocessing-owns-crs.md` for why the dependency points this
way round.

### 2.1 `AnalysisFrame`

Every public entry point takes one. It **declares** the planar frame the caller's arrays are
already in — it does not cause a transformation.

```python
# python/webmap_geo/src/webmap_geo/frame.py

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class AnalysisFrame:
    """The planar frame the caller's coordinates are already expressed in.

    Metadata, not an instruction. Nothing in webmap_geo reads this to decide
    whether to reproject — arrays arrive in the analysis CRS or the caller has
    a bug. It is carried into diagnostics and lineage so a stored grid can say
    what frame produced it, and it appears in error messages so "range 4200"
    is never ambiguous about its units.

    Validation that the srid is projected rather than geographic happens in
    webmap_core.crs.CrsContext, at the boundary that prepares the arrays.
    """
    srid: int
    units: Literal["m", "ft", "usft"]
```

### 2.2 Where transformation is allowed

Owning `pyproj` does not license calling it mid-algorithm. `CLAUDE.md` §3.1 rule 3 is
unchanged: reprojection happens at defined boundaries only. Inside this package there are
exactly two legitimate callers, and both run before any solver:

1. **Reconciling constraint and control-point datasets** that arrive in different storage
   CRSs. Faults from one source and picks from another must be in one frame before the mesh
   is built.
2. **Converting `GridSpec.bbox`** — documented in EPSG:4326 (`02-data-model.md` §5) — into
   analysis-CRS grid bounds, since `cell_size` is in analysis-CRS units.

Anything else is a bug. A transformer call inside `minimum_curvature`, a kriging neighbourhood
search, or a contour walk means coordinates were not in the frame they claimed to be.

### 2.3 Enforcement

`import-linter`, not a pygrep hook — the rule is about module graphs, which grep cannot see.

```toml
[[tool.importlinter.contracts]]
name = "webmap_geo is a leaf"
type = "forbidden"
source_modules = ["webmap_geo"]
forbidden_modules = ["webmap_core", "webmap_io", "fastapi", "sqlalchemy", "pydantic"]

[[tool.importlinter.contracts]]
name = "pyproj is confined to webmap_geo.crs"
type = "forbidden"
source_modules = ["webmap_geo.interpolate", "webmap_geo.mesh", "webmap_geo.faults",
                  "webmap_geo.variogram", "webmap_geo.contour", "webmap_geo.aggregate",
                  "webmap_core", "webmap_io"]
forbidden_modules = ["pyproj"]
```

`pyproj` is deliberately absent from the first contract. It is a legitimate `webmap_geo`
dependency now; the second contract is what keeps it in one module.

---

## 3. The fault constraint problem

This is the differentiator. No Python library supports it.

### 3.1 Two constraint types, different physics

| Type | Value across it | Gradient across it | Carries Z? | Example |
|---|---|---|---|---|
| **Fault** (hard) | Discontinuous | Discontinuous | No | Sealing normal fault with 200 ft throw |
| **Breakline** (soft) | Continuous | Discontinuous | Yes | Channel axis, structural hinge, ridge crest |

Surfer uses the same distinction. Getting it wrong is not subtle: treating a fault as a
breakline smears throw across it; treating a breakline as a fault tears a surface that should
be continuous.

### 3.2 Why naive approaches fail

**Masking after interpolation** — grid normally, then blank cells near faults. Wrong: the
interpolation already used points from the far side, so values near the fault are contaminated
before you mask anything.

**Euclidean neighborhoods with a post-hoc filter** — search normally, discard points whose
straight line crosses a fault. Better, but leaves holes where a fault-bounded compartment has
too few remaining points, and produces discontinuities in the *number* of neighbors that show
up as artifacts.

**The correct approach** is that distance itself must be fault-aware. Two points on opposite
sides of a sealing fault are not 500 ft apart for interpolation purposes; they are
disconnected, or connected only by a path around the fault tip.

### 3.3 Architecture: constrained mesh as the substrate

Build a constrained Delaunay triangulation once, with fault segments as constrained edges.
Everything else runs on that mesh.

```
control points + fault polylines
        ↓
  fault network validation and cleaning
        ↓
  constrained Delaunay triangulation (triangle)
        ↓
  ┌─────────────┬──────────────┬────────────────┐
  │ mesh-based  │ fault-aware  │ mesh-based     │
  │ DSI solve   │ path distance│ minimum        │
  │             │ for kriging  │ curvature      │
  └─────────────┴──────────────┴────────────────┘
        ↓
  sample mesh onto output grid → COG
```

Faults become discontinuities in mesh *connectivity*. Any algorithm operating on mesh
adjacency inherits the constraint without special-casing.

---

## 4. Fault network preprocessing

Raw fault polylines from a geologist's interpretation are almost never triangulation-ready.
Cleaning is a required step with clear diagnostics, not a silent fix-up.

```python
# python/webmap_geo/src/webmap_geo/faults/network.py

from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np
from shapely.geometry import LineString, Point


class ConstraintKind(StrEnum):
    FAULT = "fault"
    BREAKLINE = "breakline"


@dataclass(frozen=True)
class Constraint:
    """A single fault trace or breakline in analysis-CRS coordinates."""
    geometry: LineString
    kind: ConstraintKind
    name: str | None = None
    z_values: np.ndarray | None = None   # required for breaklines

    def __post_init__(self) -> None:
        if self.kind is ConstraintKind.BREAKLINE and self.z_values is None:
            raise ValueError(
                f"Breakline '{self.name}' has no z_values. Breaklines carry "
                "their own elevations — a breakline without Z is either a "
                "fault (use kind='fault') or incomplete data."
            )


@dataclass
class ValidationReport:
    """Everything wrong with a fault network, in terms a geologist can act on."""
    crossing_pairs: list[dict] = field(default_factory=list)
    dangles: list[dict] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)
    zero_length: list[dict] = field(default_factory=list)
    self_intersections: list[dict] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not any([
            self.crossing_pairs, self.dangles, self.duplicates,
            self.zero_length, self.self_intersections,
        ])


def validate_network(
    constraints: list[Constraint], snap_tolerance: float
) -> ValidationReport:
    """Identify problems that prevent triangulation.

    snap_tolerance is in analysis-CRS units. A reasonable default is
    half the median control-point spacing.

    Does not modify anything. Cleaning is a separate, explicit step so the
    geologist sees what changed.
    """
    ...


def clean_network(
    constraints: list[Constraint],
    report: ValidationReport,
    snap_tolerance: float,
) -> tuple[list[Constraint], list[str]]:
    """Apply fixes. Returns cleaned constraints and a human-readable changelog.

    Fixes applied, in order:
      1. Drop zero-length segments.
      2. Split self-intersecting lines at their crossings.
      3. Insert shared nodes at crossings between different faults.
      4. Snap dangling ends within tolerance to the nearest fault.
      5. Merge exact duplicates.

    Dangles beyond tolerance are NOT extended — an unresolved dangle means
    the compartment is open there, which may be geologically correct. Report
    it and let the geologist decide.
    """
```

> **Design rule.** Never silently repair a fault network. The distinction between "this fault
> tips out here" and "this fault trace is incomplete" is geological judgment, not a
> preprocessing decision.

---

## 5. Constrained triangulation

```python
# python/webmap_geo/src/webmap_geo/mesh/constrained.py

import numpy as np
import triangle as tr


@dataclass(frozen=True)
class ConstrainedMesh:
    vertices: np.ndarray        # (n, 2) float64, analysis CRS
    triangles: np.ndarray       # (m, 3) int32
    segments: np.ndarray        # (k, 2) int32 — constrained edges
    segment_kind: np.ndarray    # (k,) — 0 = fault, 1 = breakline
    vertex_z: np.ndarray | None  # (n,) known values, NaN where unknown

    split_vertices: frozenset[int]  # copies made by the fault split

    def adjacency(self) -> dict[int, list[int]]:
        """Vertex -> neighbours. Faults separate the graph topologically."""
        ...


def build_mesh(
    points: np.ndarray,           # (n, 2) control point locations
    constraints: list[Constraint],
    bbox: tuple[float, float, float, float],
    max_area: float | None = None,
) -> ConstrainedMesh:
    """Constrained Delaunay triangulation with faults as constrained edges.

    Uses Shewchuk's triangle via the 'p' (planar straight line graph) flag
    plus 'q' for quality and 'a' for area constraint.

    Constrained edges are guaranteed present in the output triangulation,
    which is exactly the property we need: no triangle spans a fault, so
    no mesh-based operation can interpolate across one.

    max_area caps triangle size so the mesh resolves the output grid. Set to
    roughly (cell_size ** 2) * 2. Omitting it produces a mesh too coarse to
    sample accurately.
    """
    vertices, segments, seg_kind = _assemble_pslg(points, constraints, bbox)
    flags = "pq30"
    if max_area is not None:
        flags += f"a{max_area:.6g}"
    result = tr.triangulate(
        {"vertices": vertices, "segments": segments}, flags
    )
    ...
```

**Performance.** 500k points with fault constraints triangulates in seconds. Not the
bottleneck.

**A vertex on a fault has to be split, not merely marked.** An earlier draft gave
`ConstrainedMesh` an `edge_is_blocked(v0, v1)` predicate — "true if traversal between these
vertices crosses a hard fault" — and that predicate can never be true. A constrained Delaunay
triangulation guarantees no edge crosses a constrained edge, which is the whole point of the
`p` flag; paths cross a fault through the fault's *own vertices*, which the triangles on both
sides share. Measured on a 60-point domain with one sealing fault, 38 of the 40 vertices lying
on the fault were adjacent to both sides, so edge blocking stopped nothing at all.

`build_mesh` therefore splits every vertex on a hard fault into one copy per fan of incident
triangles, the standard treatment for a crack in a mesh. The two sides then share no vertex
and the separation is topological, so nothing downstream has to remember to check. A fault
**tip** has a single fan and is not split — which is correct, because a tip is exactly where
the two sides do connect.

**Constraints are clipped to the domain.** Fault traces legitimately run past the area of
interest, and `triangle` keeps their outside vertices while triangulating nothing around them,
leaving isolated vertices that then read as one-vertex fault compartments. Before clipping, one
sealing fault over a 60-point domain reported 4 compartments of sizes [233, 204, 1, 1]; after,
2 of [233, 204]. Sealing is preserved, because the trace still meets the boundary.

**What uses the mesh.** Fault network validation and compartment labelling. It was also built
to carry barrier-aware kriging, which §6.2 explains was measured and dropped — the
triangulation stands on its own and is where a future fault-aware interpolator would start.

---

## 6. Interpolation methods

### 6.1 Minimum curvature — the fast path

Briggs (1974). What Surfer produces by default, and what geologists expect for structure maps.
Implemented directly because no library does it with fault awareness.

```python
# python/webmap_geo/src/webmap_geo/interpolate/minimum_curvature.py

import numpy as np
import scipy.sparse as sp
from pyamg import smoothed_aggregation_solver


def minimum_curvature(
    points: np.ndarray,          # (n, 2)
    values: np.ndarray,          # (n,)
    grid: GridDefinition,
    constraints: list[Constraint] | None = None,
    tension: float = 0.0,        # 0 = pure minimum curvature, 1 = harmonic
    max_iterations: int = 10_000,
    tolerance: float = 1e-5,
) -> np.ndarray:
    """Minimum-curvature gridding after Briggs (1974).

    Minimizes the integral of squared curvature subject to passing through
    (or near) the data points. Produces the smooth, geologically plausible
    surfaces geologists expect from Surfer.

    Fault handling: the biharmonic finite-difference stencil is modified at
    cells whose stencil would span a hard fault. Blocked neighbours are
    removed and the stencil is re-derived with a free-edge (natural)
    boundary condition, which is the correct physical analogue — the surface
    is unconstrained at a fault, not clamped.

    Scales with grid cells, not input points. A 2000x2000 grid is a
    4M-unknown sparse system, solved in seconds with algebraic multigrid.

    tension blends toward a harmonic (Laplace) solution, reducing overshoot
    near steep gradients. Surfer's 'internal tension' parameter.
    """
    A, b = _assemble_biharmonic(grid, points, values, constraints, tension)
    ml = smoothed_aggregation_solver(A.tocsr())
    z = ml.solve(b, tol=tolerance, maxiter=max_iterations, accel="cg")
    return z.reshape(grid.ny, grid.nx)


def _assemble_biharmonic(grid, points, values, constraints, tension):
    """Build the sparse system.

    Interior cells get the 13-point biharmonic stencil. Cells adjacent to a
    fault get a reduced stencil. Data cells get an equality constraint row.

    The fault mask is computed once by rasterizing constraint geometries onto
    the grid edges — an edge between two cells is blocked if a hard fault
    segment crosses it.
    """
    ...
```

### 6.2 Ordinary and universal kriging

> **The geostatistics subsystem is specified in `13-kriging.md`.** This section is the
> interpolator a structure map needs, in the form `webmap_interpolate` dispatches to. `13`
> specifies the rest of the family — simple, universal/KED, indicator and block kriging,
> regression kriging, and regression indicator kriging with a global parametric trend — along
> with declustering, covariate screening, local CDFs, attribution diagnostics and
> spatially-blocked validation.

One hard requirement at this scale: local neighborhoods.

```python
# python/webmap_geo/src/webmap_geo/interpolate/kriging.py

import numpy as np
from scipy.spatial import cKDTree


def ordinary_kriging(
    points: np.ndarray,
    values: np.ndarray,
    grid: GridDefinition,
    variogram: FittedVariogram,
    n_neighbors: int = 48,
    max_radius: float | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Ordinary kriging with a moving neighbourhood.

    Returns (estimate, variance), both shaped (ny, nx).

    WHY LOCAL: ordinary kriging solves an (n+1) x (n+1) system per estimate.
    Global kriging at n=500,000 is a 500,001-square dense system — roughly
    2 TB and O(n^3) to factor. Impossible. Moving neighbourhoods reduce this
    to n_neighbors-square systems, solved per grid node. Standard practice.

    NOT FAULT-AWARE. Distance here is Euclidean, so a kriged surface is
    continuous across every fault. Use minimum curvature for a faulted
    structure map; see "Kriging does not honour faults" below.

    ANISOTROPY is applied by transforming coordinates into the variogram's
    principal frame before the search, so the neighbourhood is elliptical.
    """
    ...
```

#### Kriging does not honour faults

**Kriging is Euclidean.** Supplying a fault network to `webmap_interpolate` with
`method="ordinary_kriging"` produces a warning, not a barrier: the surface is continuous
across every fault, and a geologist reading it will see throw smeared into a smooth ramp under
a fault line the map draws on top. **Minimum curvature is the fault-aware method** — its
finite-difference stencil drops links blocked by a hard fault (§6.1), which is a genuine
discontinuity and is what a faulted structure map needs.

An earlier draft of this section specified barrier-aware kriging: neighbour search by Dijkstra
path distance on the constrained mesh of §5, in the manner of ArcGIS's kriging-with-barriers.
It was removed rather than shipped, for two measured reasons.

*It is too slow.* A multi-source k-nearest search over the mesh — already the fast
formulation, one pass rather than one Dijkstra per node — took 14 s on a mesh of 8,855
vertices. A 1000×1000 grid needs a mesh of roughly two million, which extrapolates to about
55 minutes against the 5-minute budget in §10. The compartment-major frontier caching this
section used to propose is an optimisation on a constant factor, not on that gap.

*The cheap approximation is not obviously worse.* Restricting each node to control in its own
fault compartment is exact for a sealing fault and costs no more than unfaulted kriging. Its
only error is at a fault **tip**, where it uses straight-line distance and the true path wraps
around. Measured over 18,800 neighbour pairs on a tipping-fault domain, path/straight distance
was 1.056 at the median, 1.175 at p99, and above 2× for 0.03% of pairs — and the ~5% median
gap is itself an artefact of walking triangle edges rather than a real detour, so mesh path
distance carries a bias of its own.

That leaves compartment restriction as the natural design if kriging is made fault-aware
later. It is not implemented today, and this section does not describe it as though it were.

### 6.3 Variogram fitting

Kriging without variogram analysis is kriging with made-up parameters. Both an automatic path
(for Claude) and an interactive path (for the geologist) are required.

> **`13-kriging.md` §7 extends this in three ways, and supersedes it in one.**
>
> Extends: nested structures and Matérn beside the single-structure `FittedVariogram` below;
> directional variograms and variogram maps; equal-count lag binning with pair counts
> returned, because a variogram plot without them invites trust in a tail built from nine
> pairs.
>
> Supersedes: **the anisotropy detection described here accepts whatever ellipse the eight
> azimuths produce.** `13` §7.6 accepts one only if the range ratio is significant against a
> bootstrap null, and reports the p-value — an anisotropy azimuth quoted without one looks
> like a measurement.
>
> And a rule this section does not have: WLS is the right fitter for a variogram of **raw
> data**, and the wrong one for the residual of a fitted trend, where it underestimates sill
> and range self-reinforcingly. See [`adr/0011`](adr/0011-reml-for-trend-residual-variograms.md).

```python
# python/webmap_geo/src/webmap_geo/variogram/model.py

import numpy as np
from dataclasses import dataclass


@dataclass(frozen=True)
class FittedVariogram:
    model: str
    nugget: float
    sill: float
    range_: float
    anisotropy_ratio: float = 1.0
    anisotropy_angle: float = 0.0     # azimuth of major axis, deg CW from N
    fit_residual: float = 0.0
    n_pairs_used: int = 0

    def gamma(self, h: np.ndarray) -> np.ndarray:
        """Semivariance at lag distance h."""
        ...


def estimate_experimental(
    points: np.ndarray,
    values: np.ndarray,
    n_lags: int = 20,
    max_lag: float | None = None,
    subsample: int = 20_000,
    declustering: bool = True,
    rng: np.random.Generator | None = None,
) -> ExperimentalVariogram:
    """Binned experimental variogram.

    SUBSAMPLING IS MANDATORY at our scale. All-pairs on 500,000 points is
    1.25e11 distances. We subsample to `subsample` points (default 20,000,
    giving 2e8 pairs) using declustered weights so dense well clusters do
    not dominate.

    DECLUSTERING matters more than people expect. Well control is clustered
    by development history, not by geology. Without cell declustering, the
    variogram is dominated by short lags within pads and reports a nugget
    that is really just clustering.

    rng is threaded through explicitly so results are reproducible — see
    CLAUDE.md on determinism.
    """


def fit(
    experimental: ExperimentalVariogram,
    model: str | None = None,
    detect_anisotropy: bool = True,
) -> FittedVariogram:
    """Fit a model to the experimental variogram.

    model=None fits all candidate models and returns the best by weighted
    least squares residual, weighting short lags more heavily (they matter
    most for kriging weights).

    Anisotropy detection computes directional variograms in 8 azimuths and
    fits an ellipse to the ranges. Reports ratio and azimuth.
    """
```

### 6.4 Cubic spline and the rest

- **Cubic spline** — `scipy.interpolate.RBFInterpolator` with a thin-plate or cubic kernel on
  the constrained mesh vertices, then mesh sampling. Fast, smooth, can overshoot; warn when
  output range exceeds input range by more than 20%.
- **IDW** — trivial, and Euclidean like kriging: it does not honour barriers.
  Produces bull's-eyes; offer it, do not default to it.
- **Nearest** — diagnostic only. Useful for checking data coverage.

### 6.5 Method dispatch

```python
# python/webmap_geo/src/webmap_geo/interpolate/__init__.py

def interpolate(request: InterpolationSpec) -> InterpolationResult:
    """Single entry point. Validates, dispatches, returns grid + diagnostics.

    Diagnostics returned regardless of method:
      - output value range vs input value range (overshoot detection)
      - fraction of grid cells with no control point within the search radius
      - per-compartment control point counts when faults are used
      - cross-validation RMSE (leave-one-out on a subsample)

    Those diagnostics feed the render metadata and the caption. A grid with
    40% of its area extrapolated should say so.
    """
```

---

## 7. Contouring

```python
# python/webmap_geo/src/webmap_geo/contour/lines.py

import contourpy
import numpy as np
from shapely.geometry import LineString


def contour_grid(
    grid: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    levels: np.ndarray,
    smoothing: float = 0.0,
    min_length: float | None = None,
) -> list[ContourLine]:
    """Extract contour lines from a gridded surface.

    Uses contourpy's serial algorithm with corner_mask to respect NaN cells
    (fault-blanked areas and extrapolation masks).

    SMOOTHING: raw contours follow grid cell boundaries and look angular.
    Chaikin corner-cutting at low smoothing, spline fitting at higher
    values. Above 0.5 the contour can drift measurably off the grid value it
    claims to represent — the API caps it and the render metadata records it.

    min_length drops fragments shorter than this (in analysis-CRS units).
    Prevents the scatter of tiny closed contours around noise. Default to
    3 * cell_size.
    """
    gen = contourpy.contour_generator(
        x=x, y=y, z=grid, corner_mask=True, chunk_size=0
    )
    ...


def auto_levels(vmin: float, vmax: float, target_count: int = 15) -> np.ndarray:
    """Choose a contour interval a geologist would choose.

    Snaps to 1, 2, 2.5, 5 x 10^n. A range of 4.1-21.8 gives an interval of
    1.0 (18 contours), not 1.18 (15 contours). Geologists read intervals off
    the legend and expect round numbers.
    """
```

Contour output carries attributes: `value`, `is_index` (every Nth for heavier styling), and
`closed`. Index contours drive label placement in the style.

### 7.1 Filled bands

```python
# python/webmap_geo/src/webmap_geo/contour/bands.py

def contour_bands(
    surface: NDArray[np.floating],
    grid: GridDefinition,
    levels: NDArray[np.floating] | None = None,
    *,
    smoothing: float = 0.0,
    min_area: float | None = None,
) -> list[ContourBand]:
    """Fill the intervals between contour levels, as polygons."""
```

**A colour-filled grid renders these bands; it does not produce them.** The difference is
whether anything can be measured or exported: area per band answers "how much of this lease is
above the spill point", which is usually the question a filled map is drawn to ask. A PNG
answers nothing.

`n` levels give up to `n + 1` bands. **Both ends are closed** — the outermost run to the
surface's own extremes — because an unfilled margin is indistinguishable from no-data, which is
the confusion the extrapolation reporting exists to prevent. Those two bands carry
`is_open_ended`, so a legend says "below 8,600" rather than claiming a floor the data lacks.

One band per interval, as a multipolygon, rather than one feature per connected part: the band
is what a legend entry names and what an area is totalled against, and a structure map's
8,600 ft interval is routinely a dozen disjoint pieces nobody wants listed separately.

NaN stays unfilled. A fault-blanked compartment becomes a hole in the band rather than ground
coloured as though it had been interpolated.

**Lines and bands are the same curve, not two curves that agree closely.** They come from one
level list and are smoothed by one function (`contour/smooth.py`), and the tests assert the
maximum departure of a contour from its band edge is **exactly zero** at every smoothing level
— not a tolerance, which would let them drift a fraction of a cell apart and leave a coloured
fringe along every contour.

Areas are checked against a closed form rather than a golden file: on a cone `z = r`, the band
between `r = 200` and `r = 400` is `pi * (400^2 - 200^2)`, met to within 0.5% — the bound being
the chord error of marching squares over a 10 ft cell, not slack.

### 7.2 Label anchors

Polygon label anchors are computed here rather than left to the renderer, because they are
geometry (`adr/0004`) and because MapLibre's own placement is per-tile.

```python
# python/webmap_geo/src/webmap_geo/label.py

@dataclass(frozen=True)
class LabelAnchor:
    point: Point
    method: str          # 'centroid' or 'pole'
    clearance: float     # distance to the nearest edge, in the frame's units


def label_anchors(
    geometries: Sequence[BaseGeometry],
    frame: AnalysisFrame,
    *,
    tolerance_ratio: float = 0.01,
) -> list[LabelAnchor | None]:
    """One representative interior point per feature."""
```

Shapely in, Shapely out — no geopandas, which `webmap_geo` does not depend on and must not
(`CLAUDE.md` §3.5). Assembling these into a dataset is the caller's job.

**Area centroid where it falls inside the polygon; the pole of inaccessibility where it does
not.** The pole is the centre of the largest inscribed circle — the point furthest from any
edge, and so the one with the most room for text. A crescent-shaped lease, or a township with a
lake over its middle, has its centroid outside its own material, and a label there sits on open
ground. Shapely ships `polylabel`, so this costs no new dependency.

**One anchor per feature, on its largest part.** A multipolygon lease gets one label, not one
per sliver.

**The result is aligned with the input, gaps included.** A geometry with no polygonal area
yields `None` rather than being dropped: the caller zips these back onto features by position,
and a shorter list shifts every label after the first empty one onto the wrong feature — a map
whose names are all correct and all in the wrong places, which reads as a data problem rather
than as this.

`clearance` is measured against the **boundary**, holes included. A ring of land around a lake
is a long way from the outside and a few feet from the water; reporting the first tells the
caller there is room for a label that will sit in the lake.

`tolerance_ratio` is relative to `sqrt(area)` rather than absolute, because a lease and a basin
differ by four orders of magnitude and one fixed tolerance either costs seconds on the small
one or returns a corner of the large one.

The output is a point dataset in its own right, so an anchor can be inspected, moved by hand
and exported with the map. `08-styling-palettes.md` §2.4 covers what the style does with it.

---

## 8. Spatial aggregation

Runs **in-process** in `webmap_geo.aggregate`, over DuckDB and Shapely. Not the hard part,
but breadth matters for adoption.

Previously these were "thin wrappers over PostGIS" — SQL issued at a database. That put
geometry operations outside the geoprocessing module, which meant "where does geometry get
transformed?" had a different answer depending on which operation you asked about, and the
analysis-CRS rule below was enforced by nothing. See
`adr/0004-geoprocessing-owns-geometry.md`.

```python
# python/webmap_geo/src/webmap_geo/aggregate/__init__.py

@dataclass(frozen=True)
class FeatureSet:
    geometry: NDArray[np.object_]      # shapely
    props: list[dict[str, Any]]
    frame: AnalysisFrame


def aggregate(op: str, inputs: list[FeatureSet], **params) -> FeatureSet:
    """Dispatch a spatial aggregation.

    Arrays arrive already in their frame. The AnalysisFrame is what makes the
    analysis-CRS rule checkable rather than aspirational: `distance` is in
    frame.units, and the frame is echoed into the lineage record, so a buffer
    that was run in degrees is visible after the fact instead of merely wrong.
    """
```

**The frame travels on the value rather than beside it.** An earlier sketch of this signature
took `frame` as a separate argument and `pyarrow.Table` operands. Carrying it on the
`FeatureSet` is what lets an overlay between two layers in different frames *raise* —
`FrameMismatch` — instead of returning an empty result, which is the shape the bug takes:
coordinates in different frames do not overlap, so the honest-looking answer is "these layers
do not touch". `FeatureSet.to_arrow()` and `.from_arrow()` keep the Arrow boundary for callers
that want it; the common path writes straight to `webmap_io.write_features`, which already
takes Shapely and dicts.

| Operation | Implementation | Notes |
|---|---|---|
| buffer | `ST_Buffer` | Distance in `frame.units` |
| dissolve | `ST_Union_Agg` grouped | |
| clip | `ST_Intersection` | |
| intersect / union / difference | DuckDB overlay | |
| spatial_join | `ST_Intersects` + attribute transfer | Predicate configurable |
| summarize_within | `ST_Contains` + aggregate | Stats per containing polygon |
| aggregate_points | binning + stats | |
| centroid | `ST_Centroid` / `ST_PointOnSurface` | Offer both; `PointOnSurface` guarantees inside |
| convex_hull | `ST_ConvexHull` | |
| concave_hull | **Shapely** `concave_hull` | Not in DuckDB — see below. Param sensitive; expose ratio |
| voronoi | `ST_VoronoiDiagram` | Clip to extent |
| hexbin | generated grid + join | Offer H3 as an alternative indexing scheme |
| erase / difference | Shapely difference | The complement of clip; `09` §9 exposes it as Erase |
| union (two layers) | clip + erase + intersect | Left-only, right-only and the shared piece, split |

**`ST_ConcaveHull` is the one that failed that check.** Every other function named above
exists in duckdb 1.5.5 spatial; concave hull does not, so it is Shapely's — which is exactly
what this paragraph prescribes, and worth recording because the table previously asserted
otherwise. **Anything added later needs the same check**, by calling it: losing PostGIS means
losing the SQL escape hatch, so an operation DuckDB does not cover has to be written against
Shapely here rather than reached for in a query.

**Engine choice follows the shape of the work** (the routing rule `09` §2.3 states): a
per-feature transform is vectorised Shapely, and the genuinely set-based operations — dissolve,
spatial join, summarize-within — do their matching through a prepared spatial index rather than
a Python loop over pairs.

**Rule:** every operation runs in the project analysis CRS. Buffering in EPSG:4326 produces
distances in degrees, which vary with latitude and are never what anyone wanted.

---

## 9. Determinism and reproducibility

Non-negotiable, because these outputs go into partner decks and get revisited a year later.

- **All randomness takes an explicit `numpy.random.Generator`.** No module-level global state,
  no bare `np.random.*`. The seed is recorded in the lineage record.
- **Iterative solvers record their convergence tolerance and iteration count.** A grid that hit
  the iteration cap without converging is flagged, not silently returned.
- **`webmap_geo.__version__` is written into every lineage record.** A change to the
  neighborhood search is a version bump, so old outputs remain explicable.
- **Reference fixtures.** `tests/fixtures/reference/` holds known inputs with Surfer and
  ArcGIS reference outputs where available. Regression tests assert agreement within a stated
  tolerance, and the tolerance is documented per method.

```python
# tests/test_minimum_curvature.py

def test_matches_surfer_reference():
    """Briggs minimum curvature against a Surfer 25 reference grid.

    Tolerance: 0.5% of value range. Exact agreement is not expected —
    Surfer's boundary conditions and convergence criteria differ — but
    systematic divergence indicates a bug in the stencil.
    """
    inp = load_fixture("midland_structure_1847pts")
    expected = load_reference_grid("midland_structure_surfer.grd")
    actual = minimum_curvature(inp.points, inp.values, inp.grid)
    rng = expected.max() - expected.min()
    assert np.nanmax(np.abs(actual - expected)) < 0.005 * rng
```

---

## 10. Performance targets

Measured on 8 vCPU, 32 GB.

| Operation | Input | Target | Notes |
|---|---|---|---|
| Variogram fit | 500k pts (20k subsample) | < 5 s | |
| Minimum curvature, no faults | 1000×1000 grid | < 10 s | AMG-bound |
| Ordinary kriging, no faults | 100k pts → 1000×1000 | < 60 s | cKDTree + local solve |
| **Minimum curvature, with faults** | **1.08M cells, 20 faults, 2k pts** | **< 5 min — measured 181 s** | Solver-bound; the expensive case |
| Triangulation | 500k pts + 100 faults | < 15 s | |
| Contouring | 2000×2000, 20 levels | < 5 s | |

**The faulted minimum-curvature row is measured, not estimated.** An earlier revision carried
*two* rows for that operation — 25 s and 5 min — for the same method and the same output size,
which cannot both be right when the method scales with grid cells rather than input points. It
was settled by running it: a 1,230 × 881 grid (1,083,630 cells) at 500 ft over the seeded
Midland Basin extent, with the 20-fault network and 2,000 control points, completed in
**181.5 s** wall clock end to end — job submission through solve to a registered COG — on a
developer workstation through Docker. The 25 s figure was wrong for the whole job by roughly
7×; the 5-minute budget holds with about 40% headroom.

Exceeding these is a bug, not a fact of life. Profile before optimizing; minimum curvature's
sparse solve is the expected hotspot. **§6.2's caching strategy is not the lever it once was** —
fault-aware neighbour search was measured and removed, and the compartment-major frontier
caching that section used to propose went with it.

**These need re-measuring, not assuming.** They were established against a design where
overlay and aggregation ran in PostGIS and features lived in database tables. Reading
GeoParquet through DuckDB is columnar, lazy, and predicate-pushed — a different profile, not
a strictly worse one, but different enough that carrying the numbers over unverified would be
guessing. Re-baseline them in Phase 4 against the seed dataset before treating a miss as a
regression.
