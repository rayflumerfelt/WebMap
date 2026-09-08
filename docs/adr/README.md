# Architecture Decision Records

One file per decision that contradicts or extends the specifications in `docs/`.
Naming: `NNNN-short-title.md`. Format and an example are in `CLAUDE.md` §10.

| # | Decision | Status |
|---|---|---|
| [0001](0001-single-user-deployment.md) | Single-user deployment; remove federated auth, grants, and RLS | Accepted |
| [0002](0002-duckdb-data-plane.md) | DuckDB and GeoParquet for the data plane; Postgres for control only | Accepted |
| [0003](0003-geoprocessing-owns-crs.md) | `webmap_geo` owns coordinate transformation | Accepted |
| [0004](0004-geoprocessing-owns-geometry.md) | All geometry operations belong to the geoprocessing module | Accepted |
| [0005](0005-single-editor-persistence.md) | Copy-on-write feature persistence, single editor per layer | Accepted |
| [0006](0006-render-image-delivery.md) | Renders return image content blocks, sized for the conversation | Accepted |

0001 through 0005 are interlocking — 0001 removes the authorization model, which is
what makes 0002 viable, which is what makes 0004 free and 0005 natural. Reversing
any one of them should be checked against the others.
