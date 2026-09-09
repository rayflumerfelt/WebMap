# Architecture Decision Records

One file per decision that contradicts or extends the specifications in `docs/`.
Naming: `NNNN-short-title.md`. Format and an example are in `CLAUDE.md` §10.

| # | Decision | Status |
|---|---|---|
| [0001](0001-single-user-deployment.md) | Single-user deployment; remove federated auth, grants, and RLS | **Superseded by 0007** |
| [0002](0002-duckdb-data-plane.md) | DuckDB and GeoParquet for the data plane; Postgres for control only | Accepted · amended 2026-09-08 |
| [0003](0003-geoprocessing-owns-crs.md) | `webmap_geo` owns coordinate transformation | Accepted |
| [0004](0004-geoprocessing-owns-geometry.md) | All geometry operations belong to the geoprocessing module | Accepted |
| [0005](0005-single-editor-persistence.md) | Copy-on-write feature persistence | Accepted · amended 2026-09-08 |
| [0006](0006-render-image-delivery.md) | Renders return image content blocks, sized for the conversation | Accepted |
| [0007](0007-multi-user-directory-sso.md) | Multi-user deployment with directory SSO | Accepted |
| [0008](0008-local-stdio-mcp.md) | The MCP server runs locally over stdio | Accepted · amended 2026-09-08 |
| [0009](0009-offline-identity-seam.md) | A verifier seam so identity is testable without the directory | Accepted |

## Reading order

**0007 and 0008 are the current architecture.** 0007 supersedes 0001 and restores the
authorization model; 0008 is what makes that affordable, by removing the OAuth 2.1 / DCR
requirement rather than solving it.

0001 is kept rather than deleted. It records why the model was removed and what the removal
cost, which is the context 0007 answers — and it named its own reversal trigger, "a second
regular user," which is exactly what happened.

Three decisions are independent of user count and were untouched by the reversal: **0003**
(CRS ownership), **0004** (geometry consolidation), **0006** (render image delivery). Two
survived with amendments recorded in their own files: **0002** — its single-writer objection
was overstated, since DuckDB here is a query engine over immutable Parquet rather than a
database file — and **0005**, where copy-on-write stays and optimistic concurrency returns on
the version pointer alone.
