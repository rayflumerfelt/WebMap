# 02 — Data Model

Implementation depth. The DDL here is the contract; Alembic migrations must match it.

---

## 1. Coordinate reference systems

Three CRS roles. Conflating them is the most likely source of silently wrong output in this
system. Every function that touches coordinates must be explicit about which it expects.

| Role | Meaning | Where |
|---|---|---|
| **Storage CRS** | The CRS the data arrived in. Preserved on ingest. | `dataset.storage_srid` |
| **Analysis CRS** | Projected CRS in which all geoprocessing runs. | `project.analysis_srid` |
| **Display CRS** | Always EPSG:3857. Conversion at the last step. | Implicit |

**Hard rules.**

1. Interpolation, variogram estimation, distance calculation, area calculation, and buffering
   run **only** in the analysis CRS. Never in geographic coordinates. A variogram range in
   decimal degrees is meaningless and anisotropy in degrees is worse.
2. `project.analysis_srid` is **required and explicit**. Never inferred, never defaulted to
   3857. A geologist working Midland Basin gets EPSG:32013 (NAD83 Texas Central) because
   that's what their data is in, not because we guessed.
3. Reprojection happens at defined boundaries only: on ingest (storage → nothing, we keep it),
   before analysis (storage → analysis), before tiling (storage → 4326 → 3857). Never
   ad-hoc mid-algorithm.
4. Vertical units are tracked separately from horizontal. A grid can be feet-vertical on
   meters-horizontal. See `dataset.vertical_unit`.

```python
# python/webmap_core/crs.py

from dataclasses import dataclass
from pyproj import CRS, Transformer
from functools import lru_cache

WGS84 = 4326
WEB_MERCATOR = 3857


@dataclass(frozen=True)
class CrsContext:
    """Explicit CRS context threaded through every geoprocessing call.

    Constructing this is the only sanctioned way to obtain a transformer.
    Direct pyproj use outside this module is a lint error.
    """
    storage_srid: int
    analysis_srid: int

    def __post_init__(self) -> None:
        if CRS.from_epsg(self.analysis_srid).is_geographic:
            raise ValueError(
                f"analysis_srid={self.analysis_srid} is geographic. "
                "Analysis requires a projected CRS — distances and areas in "
                "degrees are not meaningful. Choose a UTM zone or State Plane "
                "zone appropriate to the data extent."
            )

    @property
    def storage_to_analysis(self) -> Transformer:
        return _transformer(self.storage_srid, self.analysis_srid)

    @property
    def analysis_to_storage(self) -> Transformer:
        return _transformer(self.analysis_srid, self.storage_srid)


@lru_cache(maxsize=256)
def _transformer(src: int, dst: int) -> Transformer:
    return Transformer.from_crs(
        CRS.from_epsg(src), CRS.from_epsg(dst), always_xy=True
    )
```

---

## 2. Authorization model

Objects are owned, scoped, and optionally granted. There is no partition key.

```
visibility:
  private  → owner only (plus explicit grants)
  team     → members of owner_team_id (plus explicit grants)
  org      → all authenticated users
```

Explicit grants layer on top and can name a user or a team, with a role of `viewer` or
`editor`. Grants can widen access but never narrow it below the visibility scope.

Effective permission for principal `P` on object `O`:

```
if O.owner_user_id == P:                       → owner  (full control)
if grant exists (O, P|P.teams) with role R:    → R
if O.visibility == 'org':                      → viewer
if O.visibility == 'team' and O.owner_team_id in P.teams: → viewer
otherwise                                      → none
```

RLS is enabled as a backstop, with policies written against this model. The application
connects as a role that **cannot** bypass RLS.

---

## 3. Schema

### 3.1 Extensions and enums

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_raster;
CREATE EXTENSION IF NOT EXISTS pgcrypto;      -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS pg_trgm;       -- dataset name search

CREATE TYPE visibility_t     AS ENUM ('private', 'team', 'org');
CREATE TYPE grant_role_t     AS ENUM ('viewer', 'editor');
CREATE TYPE dataset_kind_t   AS ENUM ('vector', 'grid', 'pointset', 'fault_network');
CREATE TYPE geometry_kind_t  AS ENUM ('point', 'linestring', 'polygon', 'mixed');
CREATE TYPE connector_kind_t AS ENUM ('upload', 'fileshare', 'postgis', 'derived');
CREATE TYPE sync_state_t     AS ENUM ('pending', 'syncing', 'ready', 'failed', 'stale');
CREATE TYPE job_state_t      AS ENUM ('queued', 'running', 'succeeded', 'failed', 'cancelled');
CREATE TYPE constraint_kind_t AS ENUM ('fault', 'breakline');
CREATE TYPE length_unit_t    AS ENUM ('m', 'ft', 'usft');
```

### 3.2 Identity

Users and teams mirror the corporate directory. Never a source of truth — synced from OIDC
claims on login.

```sql
CREATE TABLE app_user (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subject         TEXT NOT NULL UNIQUE,      -- OIDC 'sub'
    email           CITEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ
);

CREATE TABLE team (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug            TEXT NOT NULL UNIQUE,      -- 'permian-asset', 'exploration'
    display_name    TEXT NOT NULL,
    idp_group_id    TEXT UNIQUE,               -- directory group mapped to this team
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE team_member (
    team_id         UUID NOT NULL REFERENCES team(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    PRIMARY KEY (team_id, user_id)
);
CREATE INDEX ON team_member (user_id);
```

### 3.3 Ownership mixin

Every ownable table repeats this block. Defined once as a SQL macro in the migration helper.

```sql
-- Applied to: project, dataset, style_template, palette, map_session, render
--   owner_user_id  UUID NOT NULL REFERENCES app_user(id)
--   owner_team_id  UUID REFERENCES team(id)
--   visibility     visibility_t NOT NULL DEFAULT 'team'
--   created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
--   updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()

CREATE TABLE access_grant (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    object_type     TEXT NOT NULL,             -- 'dataset' | 'project' | ...
    object_id       UUID NOT NULL,
    grantee_user_id UUID REFERENCES app_user(id) ON DELETE CASCADE,
    grantee_team_id UUID REFERENCES team(id) ON DELETE CASCADE,
    role            grant_role_t NOT NULL,
    granted_by      UUID NOT NULL REFERENCES app_user(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(grantee_user_id, grantee_team_id) = 1)
);
CREATE INDEX ON access_grant (object_type, object_id);
CREATE INDEX ON access_grant (grantee_user_id);
CREATE INDEX ON access_grant (grantee_team_id);
```

### 3.4 Project

The container that fixes the analysis CRS and units.

```sql
CREATE TABLE project (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    description     TEXT,

    analysis_srid   INTEGER NOT NULL,          -- MUST be projected; validated in app
    horizontal_unit length_unit_t NOT NULL,
    vertical_unit   length_unit_t NOT NULL,
    -- Depth increases downward (true) vs elevation increases upward (false).
    -- Geologists disagree about this constantly; make it explicit per project.
    depth_positive_down BOOLEAN NOT NULL DEFAULT TRUE,

    default_extent  GEOMETRY(Polygon, 4326),

    owner_user_id   UUID NOT NULL REFERENCES app_user(id),
    owner_team_id   UUID REFERENCES team(id),
    visibility      visibility_t NOT NULL DEFAULT 'team',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 3.5 Dataset registry

**This is the linchpin.** When a geologist says "show me this data," Claude needs a referent.
Datasets are named, searchable, permissioned handles. They store *references*, not bytes.

```sql
CREATE TABLE dataset (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id          UUID REFERENCES project(id) ON DELETE SET NULL,

    name                TEXT NOT NULL,
    description         TEXT,
    kind                dataset_kind_t NOT NULL,
    geometry_kind       geometry_kind_t,        -- NULL for grids

    -- Source
    connector           connector_kind_t NOT NULL,
    source_uri          TEXT,                   -- smb://..., postgis://..., s3://...
    source_checksum     TEXT,
    sync_state          sync_state_t NOT NULL DEFAULT 'pending',
    synced_at           TIMESTAMPTZ,
    sync_error          TEXT,

    -- Spatial
    storage_srid        INTEGER NOT NULL,
    bbox_4326           GEOMETRY(Polygon, 4326),

    -- Vector payload
    feature_table       TEXT,                   -- 'feat.ds_<uuid_hex>'
    feature_count       BIGINT,
    attribute_schema    JSONB,                  -- [{name, type, nullable, description}]

    -- Grid payload
    cog_key             TEXT,                   -- object storage key
    grid_nx             INTEGER,
    grid_ny             INTEGER,
    grid_cell_size      DOUBLE PRECISION,       -- in analysis CRS units
    value_min           DOUBLE PRECISION,
    value_max           DOUBLE PRECISION,
    value_unit          TEXT,                   -- 'ft', '%', 'mD'
    vertical_unit       length_unit_t,

    -- Metadata for Claude's captions
    data_vintage        DATE,
    caption             TEXT,                   -- generated one-liner

    owner_user_id       UUID NOT NULL REFERENCES app_user(id),
    owner_team_id       UUID REFERENCES team(id),
    visibility          visibility_t NOT NULL DEFAULT 'team',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT vector_has_table CHECK (
        kind NOT IN ('vector','pointset','fault_network') OR feature_table IS NOT NULL),
    CONSTRAINT grid_has_cog CHECK (
        kind <> 'grid' OR cog_key IS NOT NULL)
);

CREATE INDEX ON dataset USING GIST (bbox_4326);
CREATE INDEX ON dataset USING GIN (name gin_trgm_ops);
CREATE INDEX ON dataset (project_id, kind);
CREATE INDEX ON dataset (owner_user_id);
```

Vector features live in per-dataset tables in the `feat` schema, created dynamically on ingest:

```sql
CREATE SCHEMA IF NOT EXISTS feat;

-- Template, instantiated per dataset as feat.ds_<uuid_hex>
CREATE TABLE feat.ds_TEMPLATE (
    id          BIGSERIAL PRIMARY KEY,
    geom        GEOMETRY NOT NULL,             -- typed + SRID-constrained at creation
    -- 4326 projection for tiling and bbox queries, maintained by PostGIS
    geom_4326   GEOMETRY GENERATED ALWAYS AS (ST_Transform(geom, 4326)) STORED,
    props       JSONB NOT NULL DEFAULT '{}'::jsonb,
    version     INTEGER NOT NULL DEFAULT 1,    -- optimistic locking
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  UUID REFERENCES app_user(id)
);
CREATE INDEX ON feat.ds_TEMPLATE USING GIST (geom);
CREATE INDEX ON feat.ds_TEMPLATE USING GIST (geom_4326);
CREATE INDEX ON feat.ds_TEMPLATE USING GIN (props jsonb_path_ops);
```

Rationale for table-per-dataset over one wide features table: independent geometry type and
SRID constraints, independent indexes and statistics, cheap `DROP TABLE` on delete, and no
single index hotspot across millions of unrelated features.

### 3.6 Fault networks

Faults and breaklines are constraints on interpolation, not ordinary vector layers. They get
their own metadata because the two behave differently.

```sql
CREATE TABLE fault_network (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_id      UUID NOT NULL UNIQUE REFERENCES dataset(id) ON DELETE CASCADE,
    -- Preprocessing state: raw fault polylines usually need cleaning before
    -- they can be used as triangulation constraints.
    is_validated    BOOLEAN NOT NULL DEFAULT FALSE,
    validation_report JSONB,   -- {intersections: [...], dangles: [...], duplicates: [...]}
    validated_at    TIMESTAMPTZ
);
```

Per-feature constraint semantics live in the feature `props`:

```jsonc
{
  "constraint_kind": "fault",   // 'fault' = hard, value discontinuous across it
                                // 'breakline' = soft, value continuous,
                                //   gradient discontinuous; carries its own Z
  "name": "Big Lake Fault",
  "z_values": null              // required for breaklines, null for faults
}
```

### 3.7 Styles, palettes, preferences

```sql
CREATE TABLE palette (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    -- Continuous ramp or discrete classes
    is_continuous   BOOLEAN NOT NULL DEFAULT TRUE,
    -- [{position: 0.0, color: "#2166ac"}, ...] positions normalized 0-1
    stops           JSONB NOT NULL,
    -- 'linear' | 'discrete'
    interpolation   TEXT NOT NULL DEFAULT 'linear',
    source_format   TEXT,                      -- 'clr' | 'cpt' | 'native'

    owner_user_id   UUID NOT NULL REFERENCES app_user(id),
    owner_team_id   UUID REFERENCES team(id),
    visibility      visibility_t NOT NULL DEFAULT 'org',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE style_template (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,             -- 'Fault Traces — Company Standard'
    description     TEXT,
    -- Applies to datasets of this kind/geometry
    applies_to_kind dataset_kind_t NOT NULL,
    applies_to_geometry geometry_kind_t,
    schema_version  INTEGER NOT NULL DEFAULT 1,
    -- The high-level symbology spec; compiles to MapLibre layers.
    -- See 08-styling-palettes.md for the schema.
    symbology       JSONB NOT NULL,

    owner_user_id   UUID NOT NULL REFERENCES app_user(id),
    owner_team_id   UUID REFERENCES team(id),
    visibility      visibility_t NOT NULL DEFAULT 'team',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-geologist defaults applied to every new map.
CREATE TABLE user_preferences (
    user_id             UUID PRIMARY KEY REFERENCES app_user(id) ON DELETE CASCADE,
    schema_version      INTEGER NOT NULL DEFAULT 1,
    -- Ordered list of dataset_ids / basemap ids always shown beneath data layers
    default_basemap_layers JSONB NOT NULL DEFAULT '[]'::jsonb,
    default_palette_id  UUID REFERENCES palette(id),
    default_project_id  UUID REFERENCES project(id),
    preferred_units     JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Fallback style templates by dataset kind
    default_templates   JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 3.8 Map sessions

The saved state Claude creates and the browser loads. Stores dataset **references**, never
copies — otherwise sessions balloon and go stale.

```sql
CREATE TABLE map_session (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    short_code      TEXT NOT NULL UNIQUE,      -- URL-friendly, e.g. 'k3n8fq'
    project_id      UUID REFERENCES project(id) ON DELETE SET NULL,
    name            TEXT,
    schema_version  INTEGER NOT NULL DEFAULT 1,

    -- [{dataset_id, style_template_id?, symbology_override?, opacity, visible, z}]
    layers          JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- {center: [lon, lat], zoom, bearing, pitch} OR {bbox: [...]}
    view            JSONB NOT NULL,
    -- Set when Claude created this session, for conversational continuity
    created_by_claude BOOLEAN NOT NULL DEFAULT FALSE,

    owner_user_id   UUID NOT NULL REFERENCES app_user(id),
    owner_team_id   UUID REFERENCES team(id),
    visibility      visibility_t NOT NULL DEFAULT 'private',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ                -- NULL = permanent
);
CREATE INDEX ON map_session (short_code);
CREATE INDEX ON map_session (owner_user_id, updated_at DESC);
```

### 3.9 Renders

Persisted image artifacts. Claude references these by ID across turns and across slides.

```sql
CREATE TABLE render (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      UUID REFERENCES map_session(id) ON DELETE SET NULL,
    job_id          UUID,

    image_key       TEXT NOT NULL,             -- object storage key
    width           INTEGER NOT NULL,
    height          INTEGER NOT NULL,
    scale_factor    INTEGER NOT NULL DEFAULT 2,
    size_preset     TEXT,                      -- 'slide_full' | 'slide_half' | ...
    format          TEXT NOT NULL DEFAULT 'png',

    -- The exact style used. Reproducibility.
    style_json      JSONB NOT NULL,
    extent_4326     GEOMETRY(Polygon, 4326) NOT NULL,

    -- Everything Claude needs to write a caption without inventing anything.
    metadata        JSONB NOT NULL,
    caption         TEXT,

    -- Requests the renderer blocked or that 404'd. Distinguishes
    -- "the data is genuinely sparse here" from "the tile server was down".
    failed_requests JSONB NOT NULL DEFAULT '[]'::jsonb,

    owner_user_id   UUID NOT NULL REFERENCES app_user(id),
    owner_team_id   UUID REFERENCES team(id),
    visibility      visibility_t NOT NULL DEFAULT 'private',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON render (session_id, created_at DESC);
```

`render.metadata` shape:

```jsonc
{
  "title": "Wolfcamp A Porosity",
  "layers": [{"dataset_id": "...", "name": "...", "kind": "grid"}],
  "value_range": {"min": 4.1, "max": 21.8, "unit": "%"},
  "method": "ordinary_kriging",
  "method_params": {
    "variogram_model": "exponential", "range": 4200, "sill": 12.4,
    "nugget": 1.1, "anisotropy_ratio": 1.8, "anisotropy_angle": 35,
    "neighbors": 48, "faults_honored": true
  },
  "grid": {"cell_size": 250, "nx": 812, "ny": 640, "unit": "ft"},
  "crs": {"analysis_srid": 32038, "display_srid": 3857},
  "extent": [-102.9, 31.6, -101.4, 32.5],
  "data_vintage": "2026-07-31",
  "feature_counts": {"control_points": 1847, "faults": 23}
}
```

### 3.10 Provenance

Reproducibility, re-runnability, and Claude being able to answer "how was this made?"

```sql
CREATE TABLE lineage (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    output_dataset_id UUID NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
    operation       TEXT NOT NULL,             -- 'krige' | 'contour' | 'buffer' | ...
    -- Full parameter set, sufficient to re-run identically
    parameters      JSONB NOT NULL,
    input_dataset_ids UUID[] NOT NULL,
    webmap_geo_version TEXT NOT NULL,          -- pinned algorithm package version
    job_id          UUID,
    created_by      UUID NOT NULL REFERENCES app_user(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON lineage (output_dataset_id);
CREATE INDEX ON lineage USING GIN (input_dataset_ids);
```

### 3.11 Jobs

```sql
CREATE TABLE job (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            TEXT NOT NULL,             -- 'krige' | 'render' | 'sync' | ...
    state           job_state_t NOT NULL DEFAULT 'queued',
    parameters      JSONB NOT NULL,
    progress        DOUBLE PRECISION NOT NULL DEFAULT 0.0,   -- 0..1
    progress_message TEXT,
    result          JSONB,
    error           TEXT,
    error_kind      TEXT,                      -- machine-readable for retry logic
    requested_by    UUID NOT NULL REFERENCES app_user(id),
    queued_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ
);
CREATE INDEX ON job (requested_by, queued_at DESC);
CREATE INDEX ON job (state) WHERE state IN ('queued', 'running');
```

### 3.12 Audit log

Required for enterprise. Distinct from lineage.

```sql
CREATE TABLE audit_event (
    id              BIGSERIAL PRIMARY KEY,
    actor_user_id   UUID REFERENCES app_user(id),
    -- 'claude' when the action came through MCP, 'web' from the SPA
    actor_channel   TEXT NOT NULL,
    action          TEXT NOT NULL,             -- 'dataset.read' | 'dataset.delete' | ...
    object_type     TEXT,
    object_id       UUID,
    detail          JSONB,
    ip_address      INET,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON audit_event (actor_user_id, created_at DESC);
CREATE INDEX ON audit_event (object_type, object_id, created_at DESC);
```

---

## 4. Row-level security

```sql
ALTER TABLE dataset ENABLE ROW LEVEL SECURITY;

-- The app sets these per request/session; see 03-auth-security.md
--   SET LOCAL webmap.user_id = '<uuid>';
--   SET LOCAL webmap.team_ids = '{<uuid>,<uuid>}';

CREATE POLICY dataset_read ON dataset FOR SELECT
USING (
    owner_user_id = current_setting('webmap.user_id')::uuid
    OR visibility = 'org'
    OR (visibility = 'team'
        AND owner_team_id = ANY(current_setting('webmap.team_ids')::uuid[]))
    OR EXISTS (
        SELECT 1 FROM access_grant g
        WHERE g.object_type = 'dataset' AND g.object_id = dataset.id
          AND (g.grantee_user_id = current_setting('webmap.user_id')::uuid
               OR g.grantee_team_id = ANY(current_setting('webmap.team_ids')::uuid[]))
    )
);

CREATE POLICY dataset_write ON dataset FOR UPDATE
USING (
    owner_user_id = current_setting('webmap.user_id')::uuid
    OR EXISTS (
        SELECT 1 FROM access_grant g
        WHERE g.object_type = 'dataset' AND g.object_id = dataset.id
          AND g.role = 'editor'
          AND (g.grantee_user_id = current_setting('webmap.user_id')::uuid
               OR g.grantee_team_id = ANY(current_setting('webmap.team_ids')::uuid[]))
    )
);

CREATE POLICY dataset_delete ON dataset FOR DELETE
USING (owner_user_id = current_setting('webmap.user_id')::uuid);
```

Repeat for `project`, `style_template`, `palette`, `map_session`, `render`.

> **The application database role must not have `BYPASSRLS`.** Verify in a startup assertion
> and fail loudly. Migrations run as a separate privileged role.

---

## 5. Pydantic models

`python/webmap_core/models.py`. These are the API and MCP contract.

```python
from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Visibility(StrEnum):
    PRIVATE = "private"
    TEAM = "team"
    ORG = "org"


class DatasetKind(StrEnum):
    VECTOR = "vector"
    GRID = "grid"
    POINTSET = "pointset"
    FAULT_NETWORK = "fault_network"


class GeometryKind(StrEnum):
    POINT = "point"
    LINESTRING = "linestring"
    POLYGON = "polygon"
    MIXED = "mixed"


class SyncState(StrEnum):
    PENDING = "pending"
    SYNCING = "syncing"
    READY = "ready"
    FAILED = "failed"
    STALE = "stale"


class LengthUnit(StrEnum):
    M = "m"
    FT = "ft"
    USFT = "usft"


Bbox = Annotated[list[float], Field(min_length=4, max_length=4)]
"""[west, south, east, north] in EPSG:4326."""


class WebMapModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",          # Catch typos in Claude-supplied params loudly
        use_enum_values=False,
        populate_by_name=True,
    )


class AttributeField(WebMapModel):
    name: str
    type: Literal["string", "integer", "number", "boolean", "date", "datetime"]
    nullable: bool = True
    description: str | None = None


class DatasetSummary(WebMapModel):
    """Compact form returned by list/search. Keep small — Claude reads many."""
    id: UUID
    name: str
    kind: DatasetKind
    geometry_kind: GeometryKind | None = None
    feature_count: int | None = None
    bbox_4326: Bbox | None = None
    sync_state: SyncState
    synced_at: datetime | None = None
    caption: str | None = None


class DatasetDetail(DatasetSummary):
    """Full form. Includes everything Claude needs for an accurate caption."""
    description: str | None = None
    project_id: UUID | None = None
    storage_srid: int
    attribute_schema: list[AttributeField] | None = None
    value_min: float | None = None
    value_max: float | None = None
    value_unit: str | None = None
    vertical_unit: LengthUnit | None = None
    grid_nx: int | None = None
    grid_ny: int | None = None
    grid_cell_size: float | None = None
    data_vintage: date | None = None
    owner: str                                   # display name
    visibility: Visibility
    created_at: datetime
    updated_at: datetime
    lineage: LineageRecord | None = None


class LineageRecord(WebMapModel):
    operation: str
    parameters: dict[str, Any]
    input_dataset_ids: list[UUID]
    webmap_geo_version: str
    created_at: datetime


# --- Interpolation ------------------------------------------------------

class VariogramModel(StrEnum):
    SPHERICAL = "spherical"
    EXPONENTIAL = "exponential"
    GAUSSIAN = "gaussian"
    MATERN = "matern"
    LINEAR = "linear"


class VariogramSpec(WebMapModel):
    model: VariogramModel = VariogramModel.EXPONENTIAL
    range: float | None = Field(
        None, gt=0,
        description="Correlation range in analysis-CRS units. None = auto-fit.",
    )
    sill: float | None = Field(None, gt=0)
    nugget: float | None = Field(None, ge=0)
    anisotropy_ratio: float = Field(
        1.0, ge=1.0,
        description="Major/minor axis ratio. 1.0 = isotropic.",
    )
    anisotropy_angle: float = Field(
        0.0, ge=-180, le=180,
        description="Azimuth of major axis, degrees clockwise from north.",
    )


class GridSpec(WebMapModel):
    cell_size: float | None = Field(
        None, gt=0,
        description="Cell size in analysis-CRS units. None = derived from "
                    "data density (median nearest-neighbour distance / 2).",
    )
    bbox: Bbox | None = Field(
        None, description="Output extent in EPSG:4326. None = data extent + 5% margin.",
    )
    max_cells: int = Field(4_000_000, le=16_000_000)

    @model_validator(mode="after")
    def _check(self) -> GridSpec:
        if self.bbox and (self.bbox[0] >= self.bbox[2] or self.bbox[1] >= self.bbox[3]):
            raise ValueError("bbox must be [west, south, east, north] with west<east, south<north")
        return self


class InterpolationMethod(StrEnum):
    ORDINARY_KRIGING = "ordinary_kriging"
    UNIVERSAL_KRIGING = "universal_kriging"
    MINIMUM_CURVATURE = "minimum_curvature"
    CUBIC_SPLINE = "cubic_spline"
    IDW = "idw"
    NEAREST = "nearest"


class InterpolationRequest(WebMapModel):
    dataset_id: UUID
    value_field: str
    method: InterpolationMethod = InterpolationMethod.ORDINARY_KRIGING
    grid: GridSpec = GridSpec()
    variogram: VariogramSpec | None = None      # kriging only
    fault_dataset_id: UUID | None = Field(
        None,
        description="Fault network to honor. Features with "
                    "constraint_kind='fault' block interpolation; "
                    "'breakline' allows continuity but breaks gradient.",
    )
    n_neighbors: int = Field(48, ge=8, le=256)
    max_search_radius: float | None = Field(None, gt=0)
    detrend: Literal["none", "linear", "quadratic"] = "none"
    declustering: bool = Field(
        True, description="Cell-declustering weights for variogram estimation. "
                          "Strongly recommended with clustered well control.",
    )
    output_name: str


# --- Rendering ----------------------------------------------------------

class SizePreset(StrEnum):
    SLIDE_FULL = "slide_full"       # 16:9 full bleed,  2560x1440 @2x
    SLIDE_HALF = "slide_half"       # half slide,       1280x1440 @2x
    SLIDE_QUARTER = "slide_quarter" # quarter,          1280x720  @2x
    SQUARE = "square"               #                   1600x1600 @2x
    THUMBNAIL = "thumbnail"         #                   640x360   @1x


class RenderMetadata(WebMapModel):
    title: str
    value_range: dict[str, Any] | None = None
    method: str | None = None
    method_params: dict[str, Any] | None = None
    grid: dict[str, Any] | None = None
    crs: dict[str, int]
    extent: Bbox
    data_vintage: date | None = None
    feature_counts: dict[str, int] = Field(default_factory=dict)


class RenderResult(WebMapModel):
    render_id: UUID
    url: str
    width: int
    height: int
    metadata: RenderMetadata
    caption: str
    session_url: str | None = None
    warnings: list[str] = Field(default_factory=list)
```

---

## 6. Schema versioning

`user_preferences`, `style_template`, `map_session`, and `palette` all carry
`schema_version`. Migrating unversioned JSON blobs is miserable; pay the small cost now.

Rules:

- Loaders dispatch on `schema_version` and upgrade in memory.
- Writers always write the current version.
- A background migration job rewrites old rows opportunistically.
- Never delete an upgrade path. `v1 → v2 → v3` chains are fine.

```python
# python/webmap_core/versioning.py

from typing import Any, Callable

_UPGRADES: dict[tuple[str, int], Callable[[dict], dict]] = {}


def upgrader(kind: str, from_version: int):
    def deco(fn: Callable[[dict], dict]):
        _UPGRADES[(kind, from_version)] = fn
        return fn
    return deco


def upgrade(kind: str, doc: dict[str, Any], target: int) -> dict[str, Any]:
    version = doc.get("schema_version", 1)
    while version < target:
        fn = _UPGRADES.get((kind, version))
        if fn is None:
            raise ValueError(f"No upgrade path for {kind} v{version} -> v{version + 1}")
        doc = fn(doc)
        version += 1
        doc["schema_version"] = version
    return doc
```
