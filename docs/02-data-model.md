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
   3857. A geologist working Midland Basin gets EPSG:2277 (NAD83 / Texas Central, ftUS)
   because that's what their data is in, not because we guessed.
3. Reprojection happens at defined boundaries only: on ingest (storage → nothing, we keep it),
   before analysis (storage → analysis), before tiling (storage → 4326 → 3857). Never
   ad-hoc mid-algorithm.
4. Vertical units are tracked separately from horizontal. A grid can be feet-vertical on
   meters-horizontal. See `dataset.vertical_unit`.

`webmap_geo.crs` owns `pyproj` and is the only module permitted to import it
(`adr/0003-geoprocessing-owns-crs.md`). `CrsContext` is a **thin wrapper over
it, not a parallel implementation** — it holds the validation that belongs at
the boundary preparing arrays for analysis, and delegates every transform.

```python
# python/webmap_core/crs.py

from dataclasses import dataclass

from webmap_geo import crs as geo_crs
# Re-exported by the module that owns pyproj. Naming the type here is not
# owning the dependency; importing pyproj directly would be.
from webmap_geo.crs import Transformer
from webmap_geo.frame import AnalysisFrame

WGS84 = geo_crs.WGS84
WEB_MERCATOR = geo_crs.WEB_MERCATOR


@dataclass(frozen=True)
class CrsContext:
    """Explicit CRS context threaded through every geoprocessing call.

    Constructing this is the only sanctioned way to obtain a transformer.
    Direct pyproj use outside webmap_geo.crs is an import-linter error.
    """
    storage_srid: int
    analysis_srid: int

    def __post_init__(self) -> None:
        if geo_crs.is_geographic(self.analysis_srid):
            raise ValueError(
                f"analysis_srid={self.analysis_srid} is geographic. "
                "Analysis requires a projected CRS — distances and areas in "
                "degrees are not meaningful. Choose a UTM zone or State Plane "
                "zone appropriate to the data extent."
            )

    @property
    def storage_to_analysis(self) -> Transformer:
        return geo_crs.transformer(self.storage_srid, self.analysis_srid)

    @property
    def analysis_to_storage(self) -> Transformer:
        return geo_crs.transformer(self.analysis_srid, self.storage_srid)

    @property
    def frame(self) -> AnalysisFrame:
        """The frame to hand webmap_geo entry points.

        Metadata declaring what the arrays are already in. It never causes a
        transformation — this context performs those, at the boundary.
        """
        return geo_crs.frame_for(self.analysis_srid)
```

The transformer cache lives in `webmap_geo.crs`, once. A second `lru_cache`
here would be a second implementation with its own `always_xy` decision, which
is exactly the divergence `adr/0003` exists to prevent.

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

### Capabilities

Object permission answers *may I read or change this thing*. It does not answer *may I publish
globally, manage a team, or create a user* — those are not about any one object, and `adr/0010`
§2 keeps them on a separate axis rather than folding them into a rank that would then have to
argue with grants.

Capabilities come from two columns: `app_user.is_global_admin` and `team_member.role`.

| Capability | Who |
|---|---|
| Create, edit, delete own layers, basemaps and maps | any active user |
| View and duplicate anything visible to them | any active user |
| Grant access to an object they own | the owner |
| Publish to a team (`visibility = 'team'`) | a member of that team |
| **Publish globally (`visibility = 'org'`)** | **a global administrator** |
| Manage membership of a locally-managed team | that team's administrator, or a global one |
| Set team defaults | that team's administrator, or a global one |
| Set global defaults; create and deactivate users | a global administrator |

Two consequences worth stating plainly:

- **Publishing globally is now restricted.** Any user could previously set
  `visibility = 'org'`.
- **There is no "team member" role.** It would differ from an ordinary user only by belonging
  to a team, which `team_member` already records, and a stored copy could disagree with it.

A capability never narrows object permission and object permission never grants a capability.
A global administrator holds no implicit read access to a private layer: administering the
deployment is not the same as being able to read everyone's work, and conflating them would
make the audit log's answer to "who saw this" much less useful.

---

## 3. Schema

### 3.1 Extensions and enums

```sql
-- No PostGIS. Geometry lives in the data plane (GeoParquet + COG on object
-- storage, queried by DuckDB in-process). See adr/0002-duckdb-data-plane.md.
CREATE EXTENSION IF NOT EXISTS pgcrypto;      -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS pg_trgm;       -- dataset name search
CREATE EXTENSION IF NOT EXISTS citext;        -- app_user.email (§3.2)

CREATE TYPE visibility_t     AS ENUM ('private', 'team', 'org');
CREATE TYPE grant_role_t     AS ENUM ('viewer', 'editor');
CREATE TYPE dataset_kind_t   AS ENUM ('vector', 'grid', 'pointset', 'fault_network');
CREATE TYPE geometry_kind_t  AS ENUM ('point', 'linestring', 'polygon', 'mixed');
CREATE TYPE connector_kind_t AS ENUM ('upload', 'fileshare', 'postgis', 'derived');
CREATE TYPE sync_state_t     AS ENUM ('pending', 'syncing', 'ready', 'failed', 'stale');
CREATE TYPE job_state_t      AS ENUM ('queued', 'running', 'succeeded', 'failed', 'cancelled');
CREATE TYPE constraint_kind_t AS ENUM ('fault', 'breakline');
CREATE TYPE length_unit_t    AS ENUM ('m', 'ft', 'usft');
CREATE TYPE team_role_t      AS ENUM ('member', 'admin');

-- How a layer is drawn, which is NOT its dataset_kind (adr/0010). A
-- colour-filled grid and a contour map of the same surface are both
-- dataset_kind='grid' rendered two ways, and contours are a derived
-- dataset_kind='vector'. Default basemaps are keyed on this, so conflating the
-- two would make "my default for contour maps" unexpressible.
CREATE TYPE presentation_t   AS ENUM (
    'vector', 'filled_grid', 'contour', 'filled_contour', 'hillshade', 'points'
);
```

### 3.2 Identity

Users and teams mirror the corporate directory **when there is one**. `adr/0010` makes that a
mode rather than an assumption:

- `WEBMAP_IDENTITY_MODE = directory` — OIDC as `03-auth-security.md` §2 describes. `app_user`
  is upserted from claims, and a team with `idp_group_id` set has its membership reconciled at
  every login. The directory is authoritative and `team_member` is a cache.
- `WEBMAP_IDENTITY_MODE = managed` — WebMap is authoritative. A global administrator creates
  users with local credentials; nothing reconciles.

**A team with a null `idp_group_id` is locally managed in either mode.** That is what makes
"mostly directory, plus an ad-hoc project team" expressible without a second switch — the
authority question is answered per team, not per deployment.

A membership write against a directory-synced team is **refused**, naming the group, because
accepting it and letting the next sign-in quietly undo it is the failure the mode exists to
prevent.

```sql
CREATE TABLE app_user (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subject         TEXT NOT NULL UNIQUE,      -- OIDC 'sub', or 'local|<uuid>'
    email           CITEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    -- Deactivation, never deletion (adr/0010). Their objects stay owned by
    -- them and visible per visibility and grants: deleting a departing
    -- geologist's team-visible layer breaks every colleague's map that
    -- references it, and lineage.created_by would point at nothing.
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    -- A capability, not an object permission (adr/0010 §2). Publishing
    -- globally, managing any team, and creating users all check this.
    is_global_admin BOOLEAN NOT NULL DEFAULT FALSE,
    -- Argon2id. NULL in directory mode, where there is no local credential.
    -- See 03-auth-security.md §11 for storage, lockout and reset.
    password_hash   TEXT,
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
    -- Per team, because "administrator of Permian, member of Delaware" is
    -- ordinary and a single global role cannot say it. In directory mode this
    -- is set locally even for a synced team: the directory supplies who is in
    -- the team, not who administers it here.
    role            team_role_t NOT NULL DEFAULT 'member',
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
--   deleted_at     TIMESTAMPTZ            -- soft delete, 30 days (03 §8)

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

-- One grant per (object, grantee). Without this, two rows can name the same
-- grantee with different roles and a revoke removes only one of them.
CREATE UNIQUE INDEX ON access_grant (object_type, object_id, grantee_user_id)
    WHERE grantee_user_id IS NOT NULL;
CREATE UNIQUE INDEX ON access_grant (object_type, object_id, grantee_team_id)
    WHERE grantee_team_id IS NOT NULL;
```

**On `deleted_at`.** Deletion is soft for 30 days (`03-auth-security.md` §8),
then hard. The column is deliberately **not** filtered by the RLS policies in
§4: deleted datasets must remain resolvable by lineage records so provenance
chains do not break. Excluding them is the service layer's job, per query.
Adding `AND deleted_at IS NULL` to a read policy would break every lineage
chain that references a deleted input, and would do it silently.

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

    -- [west, south, east, north] in EPSG:4326. Four floats, not a geometry —
    -- the control plane has no PostGIS.
    default_extent  DOUBLE PRECISION[4],

    owner_user_id   UUID NOT NULL REFERENCES app_user(id),
    owner_team_id   UUID REFERENCES team(id),
    visibility      visibility_t NOT NULL DEFAULT 'team',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ
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
    bbox_4326           DOUBLE PRECISION[4],    -- [w, s, e, n]

    -- Vector payload. Features are versioned GeoParquet objects, not rows;
    -- parquet_key names the CURRENT version. See §3.5.1 and adr/0005.
    parquet_key         TEXT,                   -- 'features/ds_<hex>/v<N>.parquet'
    version             INTEGER NOT NULL DEFAULT 1,
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
    deleted_at          TIMESTAMPTZ,

    CONSTRAINT vector_has_parquet CHECK (
        kind NOT IN ('vector','pointset','fault_network') OR parquet_key IS NOT NULL),
    CONSTRAINT grid_has_cog CHECK (
        kind <> 'grid' OR cog_key IS NOT NULL)
);

CREATE INDEX ON dataset USING GIN (name gin_trgm_ops);
CREATE INDEX ON dataset (project_id, kind);
CREATE INDEX ON dataset (owner_user_id);
```

#### 3.5.1 Feature storage

Vector features are **versioned GeoParquet objects on object storage**, not rows in the
control plane. `dataset.parquet_key` and `dataset.version` name the current one; advancing
that pointer is the atomic commit for an edit (`adr/0005-single-editor-persistence.md`).

Because objects are immutable, two concurrent editors cannot corrupt each other's writes —
they produce two separately-named objects. They contend only on the pointer, which is one row
and one optimistic `UPDATE`; see `09-editing.md` §5.1.

```
features/ds_<uuid_hex>/v1.parquet      <- superseded, retained
features/ds_<uuid_hex>/v2.parquet      <- superseded, retained
features/ds_<uuid_hex>/v3.parquet      <- dataset.parquet_key, dataset.version = 3
```

Schema of each object:

| Column | Type | Notes |
|---|---|---|
| `id` | `INT64` | Stable across versions; the edit identity of a feature |
| `geometry` | GeoParquet WKB | In the dataset's `storage_srid` |
| `props` | JSON string | Attribute values |
| `updated_at` | `TIMESTAMP` | |

GeoParquet metadata carries the CRS, so an object is self-describing — a `.parquet` handed
to someone else does not need this database to be readable, which is not true of a row in a
`feat.ds_*` table.

```sql
CREATE TABLE dataset_version (
    dataset_id   UUID NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
    version      INTEGER NOT NULL,
    parquet_key  TEXT NOT NULL,
    feature_count BIGINT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by   UUID REFERENCES app_user(id),
    PRIMARY KEY (dataset_id, version)
);
```

**Retention.** Every version for 30 days, matching the soft-delete window in
`03-auth-security.md` §8, then thinned to daily. Copy-on-write means storage grows with edit
count; this is the cost of the model and it needs a scheduled job, not good intentions.

**Why an object per dataset rather than one wide table:** independent geometry type and CRS
per dataset, cheap delete, no index hotspot across unrelated features — the same reasons the
previous table-per-dataset design gave — plus DuckDB reads Parquet columnar and predicate-
pushed, so a tile query touching two columns does not pay for the attribute payload.

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
    visibility      visibility_t NOT NULL DEFAULT 'team',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ
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
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ
);

-- Defaults applied to every new map, in three tiers (adr/0010 §4). The
-- columns are identical at each tier so resolution is one shape repeated,
-- not three special cases.
CREATE TABLE user_preferences (
    user_id             UUID PRIMARY KEY REFERENCES app_user(id) ON DELETE CASCADE,
    schema_version      INTEGER NOT NULL DEFAULT 1,
    -- {"*": <basemap_id>, "contour": <basemap_id>, ...} keyed on
    -- presentation_t, plus "*" for the general default.
    default_basemaps    JSONB NOT NULL DEFAULT '{}'::jsonb,
    default_palette_id  UUID REFERENCES palette(id),
    default_project_id  UUID REFERENCES project(id),
    preferred_units     JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Fallback style templates by dataset kind
    default_templates   JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Set by a team administrator. Same shape as the tier above it.
CREATE TABLE team_preferences (
    team_id             UUID PRIMARY KEY REFERENCES team(id) ON DELETE CASCADE,
    schema_version      INTEGER NOT NULL DEFAULT 1,
    default_basemaps    JSONB NOT NULL DEFAULT '{}'::jsonb,
    default_palette_id  UUID REFERENCES palette(id),
    preferred_units     JSONB NOT NULL DEFAULT '{}'::jsonb,
    default_templates   JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Set by a global administrator. One row, enforced: a second would make
-- "the global default" ambiguous with nothing to arbitrate it.
CREATE TABLE global_preferences (
    id                  BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    schema_version      INTEGER NOT NULL DEFAULT 1,
    default_basemaps    JSONB NOT NULL DEFAULT '{}'::jsonb,
    default_palette_id  UUID REFERENCES palette(id),
    preferred_units     JSONB NOT NULL DEFAULT '{}'::jsonb,
    default_templates   JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**Resolution order** (`adr/0010` §4). At each tier the presentation-specific default is tried
before the general one, and only then does resolution descend:

```
user.default_basemaps[presentation] → user.default_basemaps["*"]
  → team.default_basemaps[presentation] → team.default_basemaps["*"]
    → global.default_basemaps[presentation] → global.default_basemaps["*"]
      → no basemap
```

A user who set a general default meant it to beat a team's, which is why the tier is exhausted
before descending rather than matching presentation across all three first.

Where a user belongs to several teams that each set a default, **the tie resolves
alphabetically by `team.slug`** and the interface names the team it came from.
Most-recently-updated was the alternative and is worse: a colleague editing a preference would
change someone else's map with no visible cause.

### 3.7a Layers and basemaps

A **layer** is a dataset plus how it is drawn — the thing a geologist names, shares and
duplicates. A **basemap** is an ordered collection of layers. The relationship is many-to-many:
layers are independent of any basemap, and two basemaps share a layer rather than copying it
(`adr/0010` §3).

Both carry the ownership mixin of §3.3, so the grant model and the RLS policies of §4 cover
them with no new authorization code. That is the reason they are tables rather than JSONB in
preferences, which is where a basemap used to live and where it could not be named or shared.

```sql
CREATE TABLE layer (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    description     TEXT,
    dataset_id      UUID NOT NULL REFERENCES dataset(id),

    -- How it is drawn. NOT dataset_kind — see the presentation_t comment in
    -- §3.1. This is the key default basemaps are looked up on.
    presentation    presentation_t NOT NULL,
    style_template_id UUID REFERENCES style_template(id),
    -- Overrides the template, or stands alone when there is none.
    symbology       JSONB,
    schema_version  INTEGER NOT NULL DEFAULT 1,
    default_opacity DOUBLE PRECISION NOT NULL DEFAULT 1.0
        CHECK (default_opacity BETWEEN 0.0 AND 1.0),

    owner_user_id   UUID NOT NULL REFERENCES app_user(id),
    owner_team_id   UUID REFERENCES team(id),
    visibility      visibility_t NOT NULL DEFAULT 'private',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ
);
CREATE INDEX ON layer (dataset_id);

CREATE TABLE basemap (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    description     TEXT,

    owner_user_id   UUID NOT NULL REFERENCES app_user(id),
    owner_team_id   UUID REFERENCES team(id),
    visibility      visibility_t NOT NULL DEFAULT 'private',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ
);

CREATE TABLE basemap_layer (
    basemap_id      UUID NOT NULL REFERENCES basemap(id) ON DELETE CASCADE,
    layer_id        UUID NOT NULL REFERENCES layer(id) ON DELETE RESTRICT,
    z               INTEGER NOT NULL,          -- draw order, 0 at the bottom
    PRIMARY KEY (basemap_id, layer_id)
);
CREATE INDEX ON basemap_layer (layer_id);
```

`ON DELETE RESTRICT` on `layer_id` is deliberate, and so is the soft-delete rule beside it:
**soft-deleting a layer that a basemap still references is refused, naming the basemaps.** A
shared object needs its shared-ness to be visible at the moment it costs something, not
afterwards when someone else's map has quietly lost a layer.

`visibility` defaults to `private` on both, unlike the `team` default elsewhere. A layer is
made deliberately, often from an operation's output, and publishing it should be a decision
rather than what happens if nobody chooses.

**Duplicating** either copies the row, not the data. GeoParquet and COG objects are immutable
(`adr/0005`), so a duplicated layer references the same `dataset_id` with a new owner and
`visibility = 'private'` regardless of the source's. Copying a two-gigabyte COG because
someone clicked Duplicate is avoidable.

### 3.8 Map sessions

The saved state Claude creates and the browser loads — **the map** of `adr/0010` §1. Stores
references, never copies, otherwise sessions balloon and go stale.

A map is a basemap, the layers drawn over it, and exactly one **active layer**. The active
layer is what Auto Zoom fits and what the editing tools address; Zoom to Extents fits every
layer in the map.

```sql
CREATE TABLE map_session (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    short_code      TEXT NOT NULL UNIQUE,      -- URL-friendly, e.g. 'k3n8fq'
    project_id      UUID REFERENCES project(id) ON DELETE SET NULL,
    name            TEXT,
    -- 2 from adr/0010: `layers` holds layer_ids rather than inline dataset
    -- dictionaries. See §6 for the migration.
    schema_version  INTEGER NOT NULL DEFAULT 2,

    -- The layers beneath the data, resolved once at creation from the default
    -- basemap for the active layer's presentation (§3.7). Stored rather than
    -- re-resolved, so reopening a map a year later shows the map that was
    -- made and not whatever the defaults have become since.
    basemap_id      UUID REFERENCES basemap(id) ON DELETE SET NULL,
    -- [{layer_id, opacity?, visible?, z}] — the overrides are per-map, so two
    -- maps can show the same layer at different opacities without either
    -- editing the shared layer.
    layers          JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- The one layer edits address. Must appear in `layers`; a map with none
    -- is a map nothing can be edited on, which is a legitimate state.
    active_layer_id UUID REFERENCES layer(id) ON DELETE SET NULL,
    -- {center: [lon, lat], zoom, bearing, pitch} OR {bbox: [...]}
    view            JSONB NOT NULL,
    -- Set when Claude created this session, for conversational continuity
    created_by_claude BOOLEAN NOT NULL DEFAULT FALSE,

    owner_user_id   UUID NOT NULL REFERENCES app_user(id),
    owner_team_id   UUID REFERENCES team(id),
    visibility      visibility_t NOT NULL DEFAULT 'team',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ                -- NULL = permanent
);
CREATE INDEX ON map_session (short_code);
CREATE INDEX ON map_session (owner_user_id, updated_at DESC);
```

**"Only the active layer can be edited" is an interface affordance, not an authorization
boundary.** The API checks permission on every request regardless of what the client considers
active. Written down because the rule reads like a security control and is not one — a client
that set `active_layer_id` to a layer its user may only view must still be refused by the
service, and is.

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
    extent_4326     DOUBLE PRECISION[4] NOT NULL,   -- [w, s, e, n]

    -- Everything Claude needs to write a caption without inventing anything.
    metadata        JSONB NOT NULL,
    caption         TEXT,

    -- Requests the renderer blocked or that 404'd. Distinguishes
    -- "the data is genuinely sparse here" from "the tile server was down".
    failed_requests JSONB NOT NULL DEFAULT '[]'::jsonb,

    owner_user_id   UUID NOT NULL REFERENCES app_user(id),
    owner_team_id   UUID REFERENCES team(id),
    visibility      visibility_t NOT NULL DEFAULT 'team',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ
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
  "crs": {"analysis_srid": 2277, "display_srid": 3857},
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

The activity trail. Distinct from lineage, which is the durable provenance artifact
(§3.10) — lineage says how a dataset was *made*, audit says what *happened*.

```sql
CREATE TABLE audit_event (
    id              BIGSERIAL PRIMARY KEY,
    actor_user_id   UUID REFERENCES app_user(id),
    -- 'claude' when the action came through MCP, 'web' from the SPA.
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

Written for the events listed in `03-auth-security.md` §10 — authentication, dataset reads via
MCP, create/update/delete, grant changes, ownership transfer, export, render creation, and job
submission. `actor_channel` is what makes "what did Claude do on my behalf" answerable.

---

## 4. Row-level security

```sql
ALTER TABLE dataset ENABLE ROW LEVEL SECURITY;
-- Policies do not apply to a table's owner unless forced, and migrations run
-- as the role that owns these tables. Without FORCE, any query issued on the
-- migration connection bypasses every policy below.
ALTER TABLE dataset FORCE ROW LEVEL SECURITY;

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

-- With RLS enabled and no INSERT policy, every insert is denied and the
-- application cannot create anything. The WITH CHECK also makes "create an
-- object owned by someone else" impossible at the database.
CREATE POLICY dataset_insert ON dataset FOR INSERT
WITH CHECK (owner_user_id = current_setting('webmap.user_id')::uuid);

-- WITH CHECK as well as USING. USING decides which rows may be updated;
-- without WITH CHECK, Postgres reuses it for the NEW row — which lets an
-- editor rewrite owner_user_id to a third party and keep the row visible.
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
)
WITH CHECK (
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

Repeat all four policies for `project`, `style_template`, `palette`,
`map_session`, and `render`. The migration generates them from one list, so
adding an ownable table without adding it to that list leaves the table
unprotected — which is what the `assert_policies_present` startup check in
`03-auth-security.md` §3.5 exists to catch.

`current_setting` is called **without** the missing-ok flag deliberately. A
query that reaches these tables with no principal set should raise loudly
rather than quietly return zero rows and look like an empty result.

> **The application database role must not have `BYPASSRLS`.** Verify in a startup assertion
> and fail loudly. Migrations run as a separate privileged role.

### 4.1 What RLS does not cover

RLS protects the **control plane** — the registry rows describing a dataset. It does not
protect the **data plane**, because feature geometry lives in GeoParquet objects on object
storage and gridded values in COGs, and Postgres policies have no reach there
(`adr/0002-duckdb-data-plane.md`).

State this as a decision rather than discover it as a gap. `03-auth-security.md` §3.1 requires
two layers of authorization, and for feature *content* there is only one:

- **The enforcement point is the API.** Object storage is not reachable by users. It sits on
  the internal network with credentials only the API and worker hold, and every tile, export,
  and feature read is a permission check in a service function before the object is opened.
- **The backstop is that nothing else can resolve a key.** `dataset.parquet_key` is only
  readable through a row RLS already protects, so a user who cannot see the dataset row cannot
  learn the object name to ask for.

That is weaker than two independent layers and should be treated as such: a missing permission
check on a feature-reading endpoint is not caught by anything else. Endpoints that read the
data plane are the ones to review hardest, and the exhaustive permission tests required by
`CLAUDE.md` §6.1 apply to them first.

If this ever feels too thin, the fix is per-user credentials on object storage scoped by
prefix — not putting features back in Postgres.

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
