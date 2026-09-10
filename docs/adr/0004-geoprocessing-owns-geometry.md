# 0004 — All geometry operations belong to the geoprocessing module

## Status

Accepted — 2026-09-08

## Context

The spec set claimed a clean separation between computation and presentation but did
not enforce one. Three places broke it:

- **`05-geoprocessing.md` §8** — the entire spatial aggregation catalog was "thin
  wrappers over PostGIS." Buffer, dissolve, clip, intersect, union, difference,
  spatial join, summarize-within, voronoi, and hexbin executed as SQL against the
  database, not in `webmap_geo`.
- **`06-rendering.md` §7** — `ST_AsMVTGeom` performed simplification, clipping, and
  coordinate quantization inside a Postgres function. That is display preprocessing
  living in the database.
- **`08-styling-palettes.md` §3** — symbology compiles to MapLibre Style JSON in two
  implementations, TypeScript and Python, kept honest by shared test vectors.

The first two mean "where does geometry get transformed?" has three answers
depending on which operation you ask about, and the answer is discoverable only by
reading the implementation. That is the condition under which a CRS bug hides: `05`
§7's own closing rule — "every operation runs in the project analysis CRS" — is
enforced by nothing when the operation is a SQL string.

## Decision

**All operations on geometry and gridded values happen in `webmap_geo`.** MapLibre
receives display-ready geometry and a compiled style, and performs no geometric
computation of its own.

Concretely:

1. The `05` §8 aggregation catalog is implemented in `webmap_geo` over in-process
   DuckDB and Shapely, not as SQL issued at a database
   ([[0002-duckdb-data-plane]] makes this the natural implementation rather than a
   sacrifice).
2. Tile geometry preparation — simplification, clipping, quantization — moves out of
   a database function and into the module.
3. Contouring, triangulation, interpolation, and validation stay where they already
   are, in `webmap_geo`. These were never in violation.

**One deliberate carve-out: style compilation is appearance, not geoprocessing.**
Compiling a `Symbology` model to Style JSON operates on colors, class breaks, and
palette stops — not on coordinates. It stays in `packages/style-model` and
`webmap_core.style`. Folding it into the geoprocessing module on the strength of
"it's preprocessing for display" would drag the ramp editor, palette import, and
classification UI in with it, and `webmap_geo` would stop being a computation
package.

The dividing line is **coordinates**: if an operation reads or writes geometry, it
belongs to `webmap_geo`. If it reads or writes appearance, it does not.

## Consequences

The rule becomes structural rather than aspirational. With aggregation running
in-process, there is no SQL path for a geometry operation to take, so `05` §8's
analysis-CRS rule is enforced by the `AnalysisFrame` threading of
[[0003-geoprocessing-owns-crs]] instead of by review.

Large-layer overlays now load geometry into the process rather than staying in the
database. DuckDB reads GeoParquet columnar and lazily, so this is not the naive
round-trip it would have been against Postgres — but it is a different performance
profile, and the `05` §10 targets should be re-measured against it rather than
assumed to carry over.

Losing the SQL escape hatch means any operation DuckDB spatial does not cover has to
be written against Shapely. That check was done for the existing catalog; it is now
a standing requirement for anything added.

`06-rendering.md` §7's "dynamic, no build step" property is retained — tiles are
still generated per request from current data, just in-process rather than by
Martin.
