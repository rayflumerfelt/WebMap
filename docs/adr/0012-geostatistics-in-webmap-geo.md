# 0012 — The geostatistics toolkit lives in `webmap_geo`, on WebMap's contracts

## Status

Accepted — 2026-09-09

## Context

`13-kriging.md` specifies a substantial body of work — variography, the kriging family,
regression kriging, regression indicator kriging, local CDFs, attribution diagnostics and
spatially-blocked validation. It arrived as a specification for a **standalone package**
(`kriging_toolkit`) with its own CRS model, its own grid type, its own run manifest, its own
flag system and its own figure rendering.

Adopted as written, WebMap would carry two of each: two ways to say what frame an array is
in, two grid definitions, two records of how a derived dataset was made, and two rendering
paths. Each pair would start identical and drift, and the drift would be discovered by a
geologist comparing a number in a report against a number on a map.

Four of the standalone spec's choices contradict rules this repository already enforces
mechanically:

| Standalone spec | WebMap rule |
|---|---|
| `SampleSet.crs` as an EPSG string, validated with `pyproj` in `prep/validate.py` | `pyproj` is confined to `webmap_geo.crs`, enforced by `import-linter`. `webmap_geo` entry points take an `AnalysisFrame`, which declares the frame the arrays are *already* in and never causes a transformation ([[0003-geoprocessing-owns-crs]]). |
| `seed: int` on every stochastic step | All randomness takes an explicit `numpy.random.Generator`. `np.random.seed` and friends are a pre-commit failure (`CLAUDE.md` §3.3). |
| `RunManifest`, a new record of every parameter, capable of replaying a run | Every derived dataset already gets a **lineage** record carrying `webmap_geo.__version__` and enough parameters to re-run and reproduce the identical grid — asserted by a test that re-runs and compares arrays. |
| `matplotlib` figures assembled inside the package | Rendering is MapLibre in the browser and MapLibre-through-Playwright on the server ([[0006-render-image-delivery]]). `webmap_geo` takes NumPy, Shapely and DuckDB in, and returns NumPy, Shapely and Arrow. |

None of these is a disagreement about geostatistics. They are all the same disagreement about
who owns a boundary, and WebMap has already answered each one.

## Decision

**The toolkit is implemented inside `webmap_geo`**, as new subpackages beside the existing
`variogram/` and `interpolate/`:

```
python/webmap_geo/src/webmap_geo/
  variogram/     # extended: nested structures, Matérn, REML, anisotropy significance
  interpolate/   # extended: SK, UK/KED, block, indicator
  trend/         # forms, sandboxed parser, GLS, the GLS↔REML loop
  cdf/           # LocalCDF, tails, order-relation correction
  estimators/    # rk, rik, sgs
  diag/          # attribution diagnostics — arrays and numbers, no figures
  validate/      # spatial splits, CRPS, PIT, reliability, baselines
  presets/       # petroleum; imported by nothing in the core
```

It reads and writes geometry and gridded values, which is what `CLAUDE.md` §3.5 and
[[0004-geoprocessing-owns-geometry]] put here, and it stays a leaf: no web framework, no
database, no `webmap_core`.

The four conflicts resolve toward WebMap in every case:

1. **Frame, not CRS.** `ControlPoints` and `GridDefinition` already carry an `AnalysisFrame`.
   The standalone spec's degenerate-coordinate check is worth keeping and moves to
   `webmap_core.crs.CrsContext`, the boundary that prepares the arrays — where a geographic
   SRID can be refused before anything is computed rather than sniffed from coordinate
   magnitudes afterwards.
2. **`Generator`, not `seed`.** Every stochastic step — declustering origin shifts, the
   anisotropy bootstrap, subsampling, SGS, multi-start perturbations — takes an explicit
   `numpy.random.Generator`. The **integer seed used to construct it** is what the lineage
   record stores, which is what makes a run replayable.
3. **The manifest is the lineage record.** `lineage.parameters` gains the structure the
   standalone spec wanted — auto-selected values, overrides paired with the auto value they
   displaced, fitted coefficients, variogram parameters, seeds, and the flag list. There is
   one record, and it is the one that already exists.
4. **No `matplotlib`.** Diagnostics return arrays and numbers with a stable `figure` key.
   The frontend draws them with ECharts (`07` §9.3), the render service draws map-shaped ones
   the same way it draws every other map, and the HTML report is assembled by
   `webmap_core`/render from those two sources.

**Two dependencies are added to `webmap_geo`, and both are load-bearing:**

- `pandas` — the covariate table. Covariates are named, heterogeneous columns that get
  selected, standardised, screened for collinearity, and carried alongside coordinates
  through declustering and subsetting. A structured NumPy array would be a worse DataFrame.
- `sympy` — the sandboxed trend-expression path. A user-supplied algebraic expression must be
  parsed against an allow-list and `lambdify`'d. The alternative is `eval`, which is not an
  alternative.

`matplotlib` is **not** added. `pysr` and `numba` are optional extras and never runtime
dependencies; `pysr` drags in Julia and exists only for offline trend discovery.

**`Flag` replaces `list[str]` warnings.** `InterpolationResult.warnings` is free text today.
It becomes a `FlagCollection` of `Flag(severity, code, msg, detail, figure)` with stable codes
from a registry. The job result document carries them, and the MCP job status renders them by
severity. This is a change to a contract two layers consume, and it is deliberate: for an MCP
tool the message *is* the interface (`CLAUDE.md` §8), and a caller cannot branch on prose.

## Consequences

**`webmap_geo`'s dependency list grows by two**, in a package where `CLAUDE.md` §7.2 says
every dependency is reviewed. The justification is above; a future reviewer should hold new
additions to the same bar rather than treating these as a precedent for loosening it.

**The standalone spec's `io/raster.py` COG writer is dropped.** `webmap_io` already writes
COGs, and `13` §14.4 hands grids back as arrays for the existing pipeline to store.

**Two subsystems now produce flags** — interpolation and the toolkit — so the registry in
`13` §16 is the single place codes are defined, and the existing free-text warnings in
`interpolate/dispatch.py` migrate to codes rather than living alongside them.

**The toolkit cannot be installed without WebMap.** That is the cost of not carrying two of
everything. If it ever needs to ship standalone, the seam is `AnalysisFrame` and
`GridDefinition` — both are small, dependency-free value objects — not a rewrite.

**`webmap_geo` gets materially larger**, which strengthens rather than weakens the case for
the `import-linter` contract that keeps it a leaf. A package this size with a database import
in it would be very hard to unpick later.
