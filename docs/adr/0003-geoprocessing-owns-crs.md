# 0003 — `webmap_geo` owns coordinate transformation

## Status

Accepted — 2026-09-08

## Context

Three rules in `CLAUDE.md` could not all hold at once:

- §3.1 — "All geoprocessing takes an explicit `CrsContext`. Direct `pyproj` use
  outside `webmap_core.crs` is a lint error."
- §3.5 — "`python/webmap_geo` imports no web framework, no database, no
  `webmap_core`."
- `05-geoprocessing.md` §1 — lists `pyproj` as a direct `webmap_geo` dependency.

`CrsContext` lives in `webmap_core.crs`. A package forbidden from importing
`webmap_core` cannot take a `CrsContext`, and a package whose dependency list
includes `pyproj` cannot honour a lint rule confining `pyproj` to `webmap_core`.

The collision surfaces in Phase 4, the longest and most expensive phase.

There is also a real requirement underneath it. Geoprocessing genuinely needs to
transform coordinates in at least two places: reconciling a fault dataset and a
control-point dataset that arrive in different storage CRSs, and converting
`GridSpec.bbox` — documented in EPSG:4326 — into analysis-CRS grid bounds when
`cell_size` is in analysis-CRS units.

Two resolutions were considered. Extracting `CrsContext` into a shared leaf package
importable by both would satisfy the letter of the rules, but it drags `pyproj` and
PROJ grid data into `webmap_geo`'s test environment — which is what
`01-architecture.md` §3.2 isolates `webmap_geo` to avoid — and buys nothing, because
the package would still not be permitted to transform.

## Decision

Invert the dependency. **`webmap_geo.crs` owns `pyproj`**, and
`webmap_core.crs.CrsContext` becomes a thin wrapper over it rather than a parallel
implementation.

This satisfies every constraint simultaneously:

- §3.5 still holds — `webmap_geo` imports nothing from `webmap_core`; the arrow now
  points the other way.
- §3.1's intent still holds — `pyproj` lives in exactly one module, which is now
  `webmap_geo.crs`.
- Geoprocessing gets the transforms it legitimately needs.

**§3.1 rule 3 is unchanged**: "reprojection happens at defined boundaries only,
never mid-algorithm." That rule governs *where you call* a transformer, not where
the code lives. Owning the capability does not license calling it inside a solver.

`webmap_geo` entry points take an `AnalysisFrame` — a frozen dataclass carrying
`srid` and `units` — declaring the planar frame the caller's arrays are *already*
in. It is metadata: stamped into diagnostics and lineage, never used to transform
implicitly. Validation that the frame is projected rather than geographic stays in
`CrsContext`, at the boundary that prepares the arrays.

Enforcement moves from a pygrep hook to an `import-linter` contract:

```toml
[[tool.importlinter.contracts]]
name = "webmap_geo is a leaf"
type = "forbidden"
source_modules = ["webmap_geo"]
forbidden_modules = ["webmap_core", "webmap_io", "fastapi", "sqlalchemy", "pydantic"]
```

`pyproj` is absent from that list deliberately — it is now a legitimate dependency,
confined to `webmap_geo.crs` by a separate contract.

## Consequences

Better layering on the merits: coordinate transformation is computation, and the
computation package should own it. `webmap_core` becomes a consumer of geospatial
primitives rather than a definer of them.

`webmap_geo` now depends on `pyproj`, so its test environment carries PROJ grid
data. That is a real cost against `01` §3.2's isolation rationale, accepted because
the alternative was an unsatisfiable rule.

`webmap_geo` uses frozen dataclasses, not Pydantic — `webmap_core.services`
translates the Pydantic `InterpolationRequest` into `webmap_geo`'s
`InterpolationSpec`, and that translation is where `CrsContext` is used. The
boundary is explicit and testable.

Because `webmap_geo` is versioned and pinned independently (`01` §3.2), a change to
CRS handling is now a `webmap_geo` version bump. That is the correct blast radius —
it was previously a `webmap_core` change that `webmap_geo` could not see.
