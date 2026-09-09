"""Initial schema: identity, registry, styling, sessions, jobs, provenance.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-08

The DDL in `02-data-model.md` §3 is the contract and this migration matches
it. It is written as explicit SQL rather than SQLAlchemy operations because
the spec is SQL: a hand-translation to `op.create_table` would be a second
representation of the schema, free to drift from the first.

`citext`, the `deleted_at` columns, the INSERT policies, `WITH CHECK` on
UPDATE, and `FORCE ROW LEVEL SECURITY` were all missing from earlier revisions
of `02` and are now specified there, each with its rationale. This file
implements the spec; it does not argue with it.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Tables carrying the ownership block from `02` §3.3, and therefore the RLS
#: policies from §4. Adding an ownable table without adding it here leaves it
#: unprotected — `assert_policies_present` at API startup is the backstop that
#: catches that omission.
OWNABLE_TABLES = (
    "project",
    "dataset",
    "style_template",
    "palette",
    "map_session",
    "render",
)

ENUMS = (
    "visibility_t",
    "grant_role_t",
    "dataset_kind_t",
    "geometry_kind_t",
    "connector_kind_t",
    "sync_state_t",
    "job_state_t",
    "constraint_kind_t",
    "length_unit_t",
)

TABLES_IN_DROP_ORDER = (
    "audit_event",
    "job",
    "lineage",
    "render",
    "map_session",
    "user_preferences",
    "style_template",
    "palette",
    "fault_network",
    "dataset_version",
    "dataset",
    "project",
    "access_grant",
    "team_member",
    "team",
    "app_user",
)


def upgrade() -> None:
    _extensions()
    _enums()
    _identity()
    _grants()
    _project()
    _dataset()
    _faults()
    _styles()
    _sessions()
    _renders()
    _provenance()
    _jobs()
    _audit()
    _row_level_security()


def downgrade() -> None:
    for table in TABLES_IN_DROP_ORDER:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for enum in ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {enum}")
    # Extensions are intentionally left in place. They are database-wide and
    # may be in use by something else; dropping them on downgrade is a wider
    # blast radius than the migration created.


# --- 02 §3.1 ---------------------------------------------------------------


def _extensions() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")  # gen_random_uuid()
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")  # dataset name search
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")  # app_user.email (02 §3.2)


def _enums() -> None:
    op.execute("CREATE TYPE visibility_t AS ENUM ('private', 'team', 'org')")
    op.execute("CREATE TYPE grant_role_t AS ENUM ('viewer', 'editor')")
    op.execute(
        "CREATE TYPE dataset_kind_t AS ENUM ('vector', 'grid', 'pointset', 'fault_network')"
    )
    op.execute(
        "CREATE TYPE geometry_kind_t AS ENUM ('point', 'linestring', 'polygon', 'mixed')"
    )
    op.execute(
        "CREATE TYPE connector_kind_t AS ENUM ('upload', 'fileshare', 'postgis', 'derived')"
    )
    op.execute(
        "CREATE TYPE sync_state_t AS ENUM ('pending', 'syncing', 'ready', 'failed', 'stale')"
    )
    op.execute(
        "CREATE TYPE job_state_t AS ENUM "
        "('queued', 'running', 'succeeded', 'failed', 'cancelled')"
    )
    op.execute("CREATE TYPE constraint_kind_t AS ENUM ('fault', 'breakline')")
    op.execute("CREATE TYPE length_unit_t AS ENUM ('m', 'ft', 'usft')")


# --- 02 §3.2 ---------------------------------------------------------------


def _identity() -> None:
    op.execute(
        """
        CREATE TABLE app_user (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            subject         TEXT NOT NULL UNIQUE,      -- OIDC 'sub'
            email           CITEXT NOT NULL UNIQUE,
            display_name    TEXT NOT NULL,
            is_active       BOOLEAN NOT NULL DEFAULT TRUE,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_seen_at    TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE TABLE team (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            slug            TEXT NOT NULL UNIQUE,
            display_name    TEXT NOT NULL,
            idp_group_id    TEXT UNIQUE,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE team_member (
            team_id         UUID NOT NULL REFERENCES team(id) ON DELETE CASCADE,
            user_id         UUID NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
            PRIMARY KEY (team_id, user_id)
        )
        """
    )
    op.execute("CREATE INDEX team_member_user_idx ON team_member (user_id)")


# --- 02 §3.3 ---------------------------------------------------------------


def _grants() -> None:
    op.execute(
        """
        CREATE TABLE access_grant (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            object_type     TEXT NOT NULL,
            object_id       UUID NOT NULL,
            grantee_user_id UUID REFERENCES app_user(id) ON DELETE CASCADE,
            grantee_team_id UUID REFERENCES team(id) ON DELETE CASCADE,
            role            grant_role_t NOT NULL,
            granted_by      UUID NOT NULL REFERENCES app_user(id),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            CHECK (num_nonnulls(grantee_user_id, grantee_team_id) = 1)
        )
        """
    )
    op.execute("CREATE INDEX access_grant_object_idx ON access_grant (object_type, object_id)")
    op.execute("CREATE INDEX access_grant_user_idx ON access_grant (grantee_user_id)")
    op.execute("CREATE INDEX access_grant_team_idx ON access_grant (grantee_team_id)")
    # One grant per (object, grantee). Without this, two rows can name the
    # same grantee with different roles and "revoke" removes only one of them.
    op.execute(
        """
        CREATE UNIQUE INDEX access_grant_unique_user
            ON access_grant (object_type, object_id, grantee_user_id)
            WHERE grantee_user_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX access_grant_unique_team
            ON access_grant (object_type, object_id, grantee_team_id)
            WHERE grantee_team_id IS NOT NULL
        """
    )


# --- 02 §3.4 ---------------------------------------------------------------


def _project() -> None:
    op.execute(
        """
        CREATE TABLE project (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            slug            TEXT NOT NULL UNIQUE,
            name            TEXT NOT NULL,
            description     TEXT,

            analysis_srid   INTEGER NOT NULL,
            horizontal_unit length_unit_t NOT NULL,
            vertical_unit   length_unit_t NOT NULL,
            depth_positive_down BOOLEAN NOT NULL DEFAULT TRUE,

            default_extent  DOUBLE PRECISION[4],

            owner_user_id   UUID NOT NULL REFERENCES app_user(id),
            owner_team_id   UUID REFERENCES team(id),
            visibility      visibility_t NOT NULL DEFAULT 'team',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at      TIMESTAMPTZ
        )
        """
    )


# --- 02 §3.5 ---------------------------------------------------------------


def _dataset() -> None:
    op.execute(
        """
        CREATE TABLE dataset (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id          UUID REFERENCES project(id) ON DELETE SET NULL,

            name                TEXT NOT NULL,
            description         TEXT,
            kind                dataset_kind_t NOT NULL,
            geometry_kind       geometry_kind_t,

            connector           connector_kind_t NOT NULL,
            source_uri          TEXT,
            source_checksum     TEXT,
            sync_state          sync_state_t NOT NULL DEFAULT 'pending',
            synced_at           TIMESTAMPTZ,
            sync_error          TEXT,

            storage_srid        INTEGER NOT NULL,
            bbox_4326           DOUBLE PRECISION[4],

            parquet_key         TEXT,
            version             INTEGER NOT NULL DEFAULT 1,
            feature_count       BIGINT,
            attribute_schema    JSONB,

            cog_key             TEXT,
            grid_nx             INTEGER,
            grid_ny             INTEGER,
            grid_cell_size      DOUBLE PRECISION,
            value_min           DOUBLE PRECISION,
            value_max           DOUBLE PRECISION,
            value_unit          TEXT,
            vertical_unit       length_unit_t,

            data_vintage        DATE,
            caption             TEXT,

            owner_user_id       UUID NOT NULL REFERENCES app_user(id),
            owner_team_id       UUID REFERENCES team(id),
            visibility          visibility_t NOT NULL DEFAULT 'team',
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at          TIMESTAMPTZ,

            CONSTRAINT vector_has_parquet CHECK (
                kind NOT IN ('vector','pointset','fault_network')
                OR parquet_key IS NOT NULL),
            CONSTRAINT grid_has_cog CHECK (
                kind <> 'grid' OR cog_key IS NOT NULL)
        )
        """
    )
    op.execute("CREATE INDEX dataset_name_trgm_idx ON dataset USING GIN (name gin_trgm_ops)")
    op.execute("CREATE INDEX dataset_project_kind_idx ON dataset (project_id, kind)")
    op.execute("CREATE INDEX dataset_owner_idx ON dataset (owner_user_id)")

    # 02 §3.5.1. Prior versions are retained; `dataset.version` names the
    # current one and advancing that pointer is the atomic commit
    # (adr/0005-single-editor-persistence.md).
    op.execute(
        """
        CREATE TABLE dataset_version (
            dataset_id   UUID NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
            version      INTEGER NOT NULL,
            parquet_key  TEXT NOT NULL,
            feature_count BIGINT,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_by   UUID REFERENCES app_user(id),
            PRIMARY KEY (dataset_id, version)
        )
        """
    )
    # The retention job thins by age (02 §3.5.1); it scans on this.
    op.execute("CREATE INDEX dataset_version_created_idx ON dataset_version (created_at)")


# --- 02 §3.6 ---------------------------------------------------------------


def _faults() -> None:
    op.execute(
        """
        CREATE TABLE fault_network (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            dataset_id      UUID NOT NULL UNIQUE REFERENCES dataset(id) ON DELETE CASCADE,
            is_validated    BOOLEAN NOT NULL DEFAULT FALSE,
            validation_report JSONB,
            validated_at    TIMESTAMPTZ
        )
        """
    )


# --- 02 §3.7 ---------------------------------------------------------------


def _styles() -> None:
    op.execute(
        """
        CREATE TABLE palette (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name            TEXT NOT NULL,
            is_continuous   BOOLEAN NOT NULL DEFAULT TRUE,
            stops           JSONB NOT NULL,
            interpolation   TEXT NOT NULL DEFAULT 'linear',
            source_format   TEXT,
            schema_version  INTEGER NOT NULL DEFAULT 1,

            owner_user_id   UUID NOT NULL REFERENCES app_user(id),
            owner_team_id   UUID REFERENCES team(id),
            visibility      visibility_t NOT NULL DEFAULT 'team',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at      TIMESTAMPTZ,

            CONSTRAINT palette_interpolation CHECK (
                interpolation IN ('linear', 'discrete'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE style_template (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name            TEXT NOT NULL,
            description     TEXT,
            applies_to_kind dataset_kind_t NOT NULL,
            applies_to_geometry geometry_kind_t,
            schema_version  INTEGER NOT NULL DEFAULT 1,
            symbology       JSONB NOT NULL,

            owner_user_id   UUID NOT NULL REFERENCES app_user(id),
            owner_team_id   UUID REFERENCES team(id),
            visibility      visibility_t NOT NULL DEFAULT 'team',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at      TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE TABLE user_preferences (
            user_id             UUID PRIMARY KEY REFERENCES app_user(id) ON DELETE CASCADE,
            schema_version      INTEGER NOT NULL DEFAULT 1,
            default_basemap_layers JSONB NOT NULL DEFAULT '[]'::jsonb,
            default_palette_id  UUID REFERENCES palette(id),
            default_project_id  UUID REFERENCES project(id),
            preferred_units     JSONB NOT NULL DEFAULT '{}'::jsonb,
            default_templates   JSONB NOT NULL DEFAULT '{}'::jsonb,
            -- Panel widths and collapsed state persist per user across
            -- sessions (07-frontend.md §5.2 and the Phase 2 criterion).
            panel_layout        JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


# --- 02 §3.8 ---------------------------------------------------------------


def _sessions() -> None:
    op.execute(
        """
        CREATE TABLE map_session (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            short_code      TEXT NOT NULL UNIQUE,
            project_id      UUID REFERENCES project(id) ON DELETE SET NULL,
            name            TEXT,
            schema_version  INTEGER NOT NULL DEFAULT 1,

            layers          JSONB NOT NULL DEFAULT '[]'::jsonb,
            view            JSONB NOT NULL,
            created_by_claude BOOLEAN NOT NULL DEFAULT FALSE,

            owner_user_id   UUID NOT NULL REFERENCES app_user(id),
            owner_team_id   UUID REFERENCES team(id),
            visibility      visibility_t NOT NULL DEFAULT 'team',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at      TIMESTAMPTZ,
            expires_at      TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX map_session_short_code_idx ON map_session (short_code)")
    op.execute(
        "CREATE INDEX map_session_owner_idx ON map_session (owner_user_id, updated_at DESC)"
    )


# --- 02 §3.9 ---------------------------------------------------------------


def _renders() -> None:
    op.execute(
        """
        CREATE TABLE render (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id      UUID REFERENCES map_session(id) ON DELETE SET NULL,
            job_id          UUID,

            image_key       TEXT NOT NULL,
            width           INTEGER NOT NULL,
            height          INTEGER NOT NULL,
            scale_factor    INTEGER NOT NULL DEFAULT 2,
            size_preset     TEXT,
            format          TEXT NOT NULL DEFAULT 'png',

            style_json      JSONB NOT NULL,
            extent_4326     DOUBLE PRECISION[4] NOT NULL,

            metadata        JSONB NOT NULL,
            caption         TEXT,

            failed_requests JSONB NOT NULL DEFAULT '[]'::jsonb,

            owner_user_id   UUID NOT NULL REFERENCES app_user(id),
            owner_team_id   UUID REFERENCES team(id),
            visibility      visibility_t NOT NULL DEFAULT 'team',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at      TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX render_session_idx ON render (session_id, created_at DESC)")


# --- 02 §3.10 --------------------------------------------------------------


def _provenance() -> None:
    op.execute(
        """
        CREATE TABLE lineage (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            output_dataset_id UUID NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
            operation       TEXT NOT NULL,
            parameters      JSONB NOT NULL,
            input_dataset_ids UUID[] NOT NULL,
            webmap_geo_version TEXT NOT NULL,
            job_id          UUID,
            created_by      UUID NOT NULL REFERENCES app_user(id),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX lineage_output_idx ON lineage (output_dataset_id)")
    op.execute("CREATE INDEX lineage_inputs_idx ON lineage USING GIN (input_dataset_ids)")


# --- 02 §3.11 --------------------------------------------------------------


def _jobs() -> None:
    op.execute(
        """
        CREATE TABLE job (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            kind            TEXT NOT NULL,
            state           job_state_t NOT NULL DEFAULT 'queued',
            parameters      JSONB NOT NULL,
            progress        DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            progress_message TEXT,
            result          JSONB,
            error           TEXT,
            error_kind      TEXT,
            requested_by    UUID NOT NULL REFERENCES app_user(id),
            queued_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            started_at      TIMESTAMPTZ,
            finished_at     TIMESTAMPTZ,

            CONSTRAINT job_progress_range CHECK (progress >= 0.0 AND progress <= 1.0)
        )
        """
    )
    op.execute("CREATE INDEX job_requested_by_idx ON job (requested_by, queued_at DESC)")
    op.execute(
        "CREATE INDEX job_active_idx ON job (state) WHERE state IN ('queued', 'running')"
    )


# --- 02 §3.12 --------------------------------------------------------------


def _audit() -> None:
    op.execute(
        """
        CREATE TABLE audit_event (
            id              BIGSERIAL PRIMARY KEY,
            actor_user_id   UUID REFERENCES app_user(id),
            actor_channel   TEXT NOT NULL,
            action          TEXT NOT NULL,
            object_type     TEXT,
            object_id       UUID,
            detail          JSONB,
            ip_address      INET,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX audit_actor_idx ON audit_event (actor_user_id, created_at DESC)")
    op.execute(
        "CREATE INDEX audit_object_idx ON audit_event (object_type, object_id, created_at DESC)"
    )


# --- 02 §4 -----------------------------------------------------------------


def _row_level_security() -> None:
    """RLS as the backstop. `02-data-model.md` §4, `03-auth-security.md` §3.1.

    The application sets `webmap.user_id` and `webmap.team_ids` per
    transaction via `principal_session`. `current_setting` is called *without*
    the missing_ok flag deliberately: a query that reaches these tables
    without a principal should raise loudly, not quietly return zero rows and
    look like an empty result set.

    Note what these policies do not reach: feature geometry lives in
    GeoParquet objects on object storage, where Postgres has no jurisdiction.
    For the data plane the API is the sole enforcement point (§4.1).
    """
    for table in OWNABLE_TABLES:
        visible = f"""
            owner_user_id = current_setting('webmap.user_id')::uuid
            OR visibility = 'org'
            OR (visibility = 'team'
                AND owner_team_id = ANY(current_setting('webmap.team_ids')::uuid[]))
            OR EXISTS (
                SELECT 1 FROM access_grant g
                WHERE g.object_type = '{table}' AND g.object_id = {table}.id
                  AND (g.grantee_user_id = current_setting('webmap.user_id')::uuid
                       OR g.grantee_team_id
                           = ANY(current_setting('webmap.team_ids')::uuid[]))
            )
        """
        writable = f"""
            owner_user_id = current_setting('webmap.user_id')::uuid
            OR EXISTS (
                SELECT 1 FROM access_grant g
                WHERE g.object_type = '{table}' AND g.object_id = {table}.id
                  AND g.role = 'editor'
                  AND (g.grantee_user_id = current_setting('webmap.user_id')::uuid
                       OR g.grantee_team_id
                           = ANY(current_setting('webmap.team_ids')::uuid[]))
            )
        """

        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # Policies do not apply to the table owner unless forced, and the
        # migration role owns these tables. Without FORCE, a bug that runs
        # application queries on the migration connection bypasses everything.
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

        op.execute(f"CREATE POLICY {table}_read ON {table} FOR SELECT USING ({visible})")

        op.execute(
            f"""
            CREATE POLICY {table}_insert ON {table} FOR INSERT
            WITH CHECK (owner_user_id = current_setting('webmap.user_id')::uuid)
            """
        )

        op.execute(
            f"""
            CREATE POLICY {table}_write ON {table} FOR UPDATE
            USING ({writable}) WITH CHECK ({writable})
            """
        )

        op.execute(
            f"""
            CREATE POLICY {table}_delete ON {table} FOR DELETE
            USING (owner_user_id = current_setting('webmap.user_id')::uuid)
            """
        )
