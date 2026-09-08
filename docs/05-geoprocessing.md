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

Deliberately **not** used: PyKrige (global solve only, no barriers), verde (no barriers, and
we need the mesh anyway).

---

## 2. The fault constraint problem

This is the differentiator. No Python library supports it.

### 2.1 Two constraint types, different physics

| Type | Value across it | Gradient across it | Carries Z? | Example |
|---|---|---|---|---|
| **Fault** (hard) | Discontinuous | Discontinuous | No | Sealing normal fault with 200 ft throw |
| **Breakline** (soft) | Continuous | Discontinuous | Yes | Channel axis, structural hinge, ridge crest |

Surfer uses the same distinction. Getting it wrong is not subtle: treating a fault as a
breakline smears throw across it; treating a breakline as a fault tears a surface that should
be continuous.

### 2.2 Why naive approaches fail

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

### 2.3 Architecture: constrained mesh as the substrate

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

## 3. Fault network preprocessing

Raw fault polylines from a geologist's interpretation are almost never triangulation-ready.
Cleaning is a required step with clear diagnostics, not a silent fix-up.

```python
# python/webmap_geo/faults.py

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

## 4. Constrained triangulation

```python
# python/webmap_geo/mesh.py

import numpy as np
import triangle as tr


@dataclass(frozen=True)
class ConstrainedMesh:
    vertices: np.ndarray        # (n, 2) float64, analysis CRS
    triangles: np.ndarray       # (m, 3) int32
    segments: np.ndarray        # (k, 2) int32 — constrained edges
    segment_kind: np.ndarray    # (k,) — 0 = fault, 1 = breakline
    vertex_z: np.ndarray | None  # (n,) known values, NaN where unknown

    def edge_is_blocked(self, v0: int, v1: int) -> bool:
        """True if traversal between these vertices crosses a hard fault."""
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

---

## 5. Interpolation methods

### 5.1 Minimum curvature — the fast path

Briggs (1974). What Surfer produces by default, and what geologists expect for structure maps.
Implemented directly because no library does it with fault awareness.

```python
# python/webmap_geo/interpolate/minimum_curvature.py

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

### 5.2 Ordinary and universal kriging

Two hard requirements at this scale: local neighborhoods, and fault-aware distance.

```python
# python/webmap_geo/interpolate/kriging.py

import numpy as np
from scipy.spatial import cKDTree


def ordinary_kriging(
    points: np.ndarray,
    values: np.ndarray,
    grid: GridDefinition,
    variogram: FittedVariogram,
    constraints: list[Constraint] | None = None,
    n_neighbors: int = 48,
    max_radius: float | None = None,
    mesh: ConstrainedMesh | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Ordinary kriging with a moving neighbourhood.

    Returns (estimate, variance), both shaped (ny, nx).

    WHY LOCAL: ordinary kriging solves an (n+1) x (n+1) system per estimate.
    Global kriging at n=500,000 is a 500,001-square dense system — roughly
    2 TB and O(n^3) to factor. Impossible. Moving neighbourhoods reduce this
    to n_neighbors-square systems, solved per grid node. Standard practice;
    it also composes correctly with the fault constraint because the
    neighbourhood search is exactly where barrier awareness applies.

    FAULT HANDLING: when constraints are supplied, neighbour search uses
    path distance on the constrained mesh rather than Euclidean distance.
    Points separated by a sealing fault are either unreachable (excluded) or
    reachable only around the fault tip (correctly downweighted). This is
    the same principle as ArcGIS's kriging-with-barriers.

    ANISOTROPY is applied by transforming coordinates into the variogram's
    principal frame before the search, so the neighbourhood is elliptical.
    """
    if constraints and mesh is None:
        raise ValueError(
            "Fault constraints supplied without a mesh. Build one with "
            "build_mesh() — barrier-aware distance requires the mesh "
            "adjacency structure."
        )
    ...
```

Fault-aware neighbor search:

```python
def _neighbors_with_barriers(
    mesh: ConstrainedMesh,
    target_xy: np.ndarray,
    point_vertex_ids: np.ndarray,
    k: int,
    max_radius: float | None,
) -> tuple[np.ndarray, np.ndarray]:
    """k nearest control points by path distance on the constrained mesh.

    Dijkstra from the target's containing triangle, traversing only mesh
    edges not blocked by a hard fault. Terminates when k control points are
    reached or max_radius is exceeded.

    Cost: O(E log V) per grid node, with E bounded locally by max_radius.
    Expensive relative to a cKDTree query — expect 5-20x slower kriging with
    faults than without. This is why gridding is an async job.

    OPTIMISATION: grid nodes within the same fault compartment and close
    together share most of their neighbourhood. Process nodes in
    compartment-major order and cache the Dijkstra frontier.
    """
```

**Universal kriging** adds a low-order polynomial trend, fitted by GLS and subtracted before
ordinary kriging of the residuals. Use when the data has regional dip — common for structure
maps across a basin margin.

### 5.3 Variogram fitting

Kriging without variogram analysis is kriging with made-up parameters. Both an automatic path
(for Claude) and an interactive path (for the geologist) are required.

```python
# python/webmap_geo/variogram.py

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

### 5.4 Cubic spline and the rest

- **Cubic spline** — `scipy.interpolate.RBFInterpolator` with a thin-plate or cubic kernel on
  the constrained mesh vertices, then mesh sampling. Fast, smooth, can overshoot; warn when
  output range exceeds input range by more than 20%.
- **IDW** — trivial, but honor barriers via mesh path distance when constraints are present.
  Produces bull's-eyes; offer it, do not default to it.
- **Nearest** — diagnostic only. Useful for checking data coverage.

### 5.5 Method dispatch

```python
# python/webmap_geo/interpolate/__init__.py

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

## 6. Contouring

```python
# python/webmap_geo/contour.py

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

---

## 7. Spatial aggregation

Thin wrappers over PostGIS and Shapely. Not the hard part, but breadth matters for adoption.

| Operation | Implementation | Notes |
|---|---|---|
| buffer | PostGIS `ST_Buffer` | Analysis CRS only |
| dissolve | `ST_Union` grouped | |
| clip | `ST_Intersection` | |
| intersect / union / difference | PostGIS overlay | |
| spatial_join | `ST_Intersects` + attribute transfer | Predicate configurable |
| summarize_within | `ST_Contains` + aggregate | Stats per containing polygon |
| aggregate_points | binning + stats | |
| centroid | `ST_Centroid` / `ST_PointOnSurface` | Offer both; `PointOnSurface` guarantees inside |
| convex_hull | `ST_ConvexHull` | |
| concave_hull | `ST_ConcaveHull` | Param sensitive; expose target percent |
| voronoi | `ST_VoronoiPolygons` | Clip to extent |
| hexbin | generated grid + join | Offer H3 as an alternative indexing scheme |

**Rule:** every operation runs in the project analysis CRS. Buffering in EPSG:4326 produces
distances in degrees, which vary with latitude and are never what anyone wanted.

---

## 8. Determinism and reproducibility

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

## 9. Performance targets

Measured on 8 vCPU, 32 GB.

| Operation | Input | Target | Notes |
|---|---|---|---|
| Variogram fit | 500k pts (20k subsample) | < 5 s | |
| Minimum curvature, no faults | 1000×1000 grid | < 10 s | AMG-bound |
| Minimum curvature, with faults | 1000×1000, 50 faults | < 25 s | Stencil assembly cost |
| Ordinary kriging, no faults | 100k pts → 1000×1000 | < 60 s | cKDTree + local solve |
| Ordinary kriging, with faults | 100k pts → 1000×1000 | < 5 min | Dijkstra-bound; the expensive case |
| Triangulation | 500k pts + 100 faults | < 15 s | |
| Contouring | 2000×2000, 20 levels | < 5 s | |

Exceeding these is a bug, not a fact of life. Profile before optimizing; the fault-aware
neighbor search is the expected hotspot and the caching strategy in §5.2 is the first lever.
