# 0002 — DuckDB and GeoParquet for the data plane; Postgres for control only

## Status

Accepted — 2026-09-08

## Context

`01-architecture.md` §4.3 made PostgreSQL 16 + PostGIS 3.4 the system of record for
everything: the dataset registry, per-dataset feature tables in the `feat` schema,
and the spatial operations themselves. Three things justified PostGIS specifically:

1. `ST_AsMVT` serving dynamic vector tiles from live, editable data via Martin
   (`06` §7).
2. The spatial aggregation catalog (`05` §8) — buffer, dissolve, clip, overlay,
   spatial join, voronoi, hexbin — implemented as "thin wrappers over PostGIS."
3. GIST indexes for tile bbox queries and snapping.

Two findings moved this. First, DuckDB's spatial extension now covers all three —
verified against duckdb 1.5.5: `ST_AsMVT`, `ST_AsMVTGeom`, `ST_TileEnvelope`,
`ST_Transform`, RTREE indexes, and every function named in the `05` §8 catalog.
Second, [[0001-single-user-deployment]] removed row-level security, which was the
one PostGIS-adjacent capability DuckDB has no answer for.

Separately, `05` §8's PostGIS wrappers were a standing violation of the rule that
all geometry operations belong in the geoprocessing module
([[0004-geoprocessing-owns-geometry]]) — the aggregation catalog executed as SQL in
the database, not in `webmap_geo`.

DuckDB's real limitation is that it is a single-writer engine and not an OLTP store.
That rules it out for the control plane and, before
[[0005-single-editor-persistence]], would have ruled it out for feature storage too.

## Decision

Split the two planes and give each the right engine.

**Control plane — PostgreSQL, without PostGIS.** Dataset registry, projects,
sessions, palettes, style templates, preferences, jobs, and lineage. Small
transactional rows. Nothing left here is spatial except `dataset.bbox_4326`, which
is four floats.

**Data plane — DuckDB embedded in the geoprocessing module, over GeoParquet and
COG on object storage.** Feature geometry, gridded values, tile generation, and the
aggregation catalog. DuckDB runs in-process; it is a library, not a service.

Consequences for the service topology:

- **Martin is removed.** MVT is generated in-process from DuckDB `ST_AsMVT` rather
  than by a separate tile service reading Postgres.
- **The `feat` schema is removed.** Per-dataset feature tables become versioned
  GeoParquet objects; `dataset` carries a `parquet_key` in place of
  `feature_table`.
- **TiTiler is retained.** Dynamic colormap application over COG is a genuine win —
  changing a palette stays a URL parameter change, not a regrid.

Postgres is kept over SQLite for the control plane despite its small size: Alembic
against SQLite needs batch mode for nearly every `ALTER TABLE`, which is recurring
friction against the roadmap's "migrations apply and roll back cleanly" criterion.
One small container is cheaper than that.

## Consequences

The local stack drops from six services to four — app, worker, TiTiler, MinIO —
plus the render container.

Aggregation moves into `webmap_geo` as in-process DuckDB rather than SQL issued at a
database, which satisfies [[0004-geoprocessing-owns-geometry]] structurally instead
of by discipline.

**GeoParquet replaces a mutable table with immutable objects.** That is what makes
[[0005-single-editor-persistence]] work, and it aligns with `03` §8's "never write
in place" more directly than the previous design did — but it means edits are
copy-on-write rather than `UPDATE`, and the version pointer lives in Postgres.

Losing PostGIS means losing an escape hatch: any operation not covered by DuckDB
spatial has to be written against Shapely in `webmap_geo` rather than reached for in
SQL. The `05` §8 catalog was checked function-by-function before this was accepted;
anything added later needs the same check.

Restoring PostGIS is possible but not free — the tile path, the aggregation
implementations, and the ingest writer would all move back. Trigger to reconsider:
concurrent multi-writer editing becomes a requirement, which would also reopen
[[0005-single-editor-persistence]].

---

## Amendment — 2026-09-08

**Status unchanged: Accepted.** The multi-user reversal in
[[0007-multi-user-directory-sso]] does *not* reopen this decision, and the "would change our
mind" clause above overstated the risk.

That clause said concurrent multi-writer editing would reopen this, because DuckDB is a
single-writer engine. That is true of a DuckDB *database file*, and this design does not use
one. DuckDB is an embedded query engine over immutable GeoParquet on object storage: a request
opens a connection, reads, and closes. There is no shared mutable DuckDB state, so many
concurrent readers across many processes are fine.

Write concurrency is handled by Postgres, not DuckDB. Two users editing a layer each write a
new Parquet object — harmless, since objects are immutable and separately named — and then
contend on `dataset.version`, which is a single-row `UPDATE`. See the amendment to
[[0005-single-editor-persistence]].

**Corrected trigger to reconsider:** an operation DuckDB spatial cannot express, or a working
set large enough that reading Parquet per request stops being viable. Not concurrency.
