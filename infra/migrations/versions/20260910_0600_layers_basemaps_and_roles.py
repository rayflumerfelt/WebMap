"""Layers, basemaps, capability roles and the three preference tiers.

Revision ID: 0004_layers_basemaps
Revises: 0003_team_visibility
Create Date: 2026-09-10

`adr/0010` added the objects users actually share. `02-data-model.md` §3.7a and
§3.8 have specified them since; nothing created them, so five of the twenty-one
tables that document defines have never existed.

Four things this migration gets right that are easy to get wrong:

**`layer` and `basemap` are ownable, so they get the full RLS treatment.** Not
just `ENABLE ROW LEVEL SECURITY` but `FORCE`, and all four policies — read,
insert, update, delete — mirroring `OWNABLE_TABLES` exactly. A new ownable
table with three of the four policies is a table with a hole in it, and the
hole is invisible until someone finds it.

**`basemap_layer` is not ownable and must not be.** It is a join row with no
owner of its own; its visibility is entirely the basemap's. Giving it an
`owner_user_id` would let a layer be attached to someone else's basemap by
whoever owns the layer, which is the reverse of what `adr/0010` §3 says.

**`ON DELETE RESTRICT` on `basemap_layer.layer_id` is the point of the model.**
Two basemaps sharing one layer is what `adr/0010` is for, and the sharing has
to become visible at the moment it costs something — when someone tries to
delete the layer — rather than afterwards, when another person's map has
quietly lost it. `07` §6.1 requires the refusal to name the basemaps.

**`global_preferences` is one row, enforced by the primary key.** A `BOOLEAN
PRIMARY KEY DEFAULT TRUE CHECK (id)` admits exactly one. A second row would
make "the global default" ambiguous with nothing to arbitrate it, and a
`LIMIT 1` in application code is not a constraint.

`app_user.is_global_admin` and `team_member.role` are the capability columns
from `adr/0010` §2 — a separate axis from per-object grants, which is why they
are columns rather than rows in `access_grant`.
"""

from __future__ import annotations

from alembic import op

revision = "0004_layers_basemaps"
down_revision = "0003_team_visibility"
branch_labels = None
depends_on = None

#: The new ownable tables. Kept as a tuple so the policy loop below cannot
#: drift from the list, which is how a table ends up with partial RLS.
NEW_OWNABLE = ("layer", "basemap")


def upgrade() -> None:
    _capability_columns()
    _presentation_enum()
    _layers_and_basemaps()
    _preferences()
    _row_level_security()


def downgrade() -> None:
    for table in reversed(NEW_OWNABLE):
        for action in ("read", "insert", "write", "remove"):
            op.execute(f"DROP POLICY IF EXISTS {table}_{action} ON {table}")

    op.execute("DROP TABLE IF EXISTS global_preferences")
    op.execute("DROP TABLE IF EXISTS team_preferences")
    # **`user_preferences` is NOT dropped.** The initial schema created it;
    # this revision only asserted it with IF NOT EXISTS. A downgrade that
    # removed it would take a table it never made, and with it every user's
    # saved defaults — the classic destructive-downgrade bug, and the reason
    # `CLAUDE.md` §7.4 asks for migrations to be rolled back in testing.
    op.execute("DROP TABLE IF EXISTS basemap_layer")
    op.execute("DROP TABLE IF EXISTS basemap")
    op.execute("DROP TABLE IF EXISTS layer")

    op.execute("DROP TYPE IF EXISTS presentation_t")

    op.execute("ALTER TABLE team_member DROP COLUMN IF EXISTS role")
    op.execute("DROP TYPE IF EXISTS team_role_t")
    op.execute("ALTER TABLE app_user DROP COLUMN IF EXISTS is_global_admin")


# --- capabilities -------------------------------------------------------------


def _capability_columns() -> None:
    """`adr/0010` §2: role capabilities, a separate axis from object grants.

    Defaulting `is_global_admin` to FALSE means an existing deployment gains no
    administrators from this migration. Someone has to be promoted
    deliberately, which is the right direction for a column that grants the
    ability to publish org-wide.
    """
    op.execute(
        "ALTER TABLE app_user ADD COLUMN IF NOT EXISTS "
        "is_global_admin BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute(
        "DO $$ BEGIN "
        "CREATE TYPE team_role_t AS ENUM ('member', 'admin'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
    )
    op.execute(
        "ALTER TABLE team_member ADD COLUMN IF NOT EXISTS "
        "role team_role_t NOT NULL DEFAULT 'member'"
    )


def _presentation_enum() -> None:
    """How a layer is *drawn*, which is not the same as what it *is*.

    `02` §3.1: a grid can be a filled surface or a set of contours, and
    contours are themselves a derived vector dataset. Default basemaps are
    keyed on this, so conflating it with `dataset_kind_t` would make "my
    default for contour maps" inexpressible.
    """
    op.execute(
        "DO $$ BEGIN "
        "CREATE TYPE presentation_t AS ENUM "
        "('vector', 'filled_grid', 'contour', 'filled_contour', 'hillshade', 'points'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
    )


# --- the objects --------------------------------------------------------------


def _layers_and_basemaps() -> None:
    op.execute(
        """
        CREATE TABLE layer (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name              TEXT NOT NULL,
            description       TEXT,
            dataset_id        UUID NOT NULL REFERENCES dataset(id),

            presentation      presentation_t NOT NULL,
            style_template_id UUID REFERENCES style_template(id),
            symbology         JSONB,
            schema_version    INTEGER NOT NULL DEFAULT 1,
            default_opacity   DOUBLE PRECISION NOT NULL DEFAULT 1.0
                CHECK (default_opacity BETWEEN 0.0 AND 1.0),

            owner_user_id     UUID NOT NULL REFERENCES app_user(id),
            owner_team_id     UUID REFERENCES team(id),
            visibility        visibility_t NOT NULL DEFAULT 'private',
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at        TIMESTAMPTZ,

            -- The same backstop revision 0003 added to every other ownable
            -- table: a row marked team-visible with no team is private in all
            -- but name, and nothing errors or logs when it happens.
            CONSTRAINT layer_team_visibility_needs_a_team
                CHECK (visibility <> 'team' OR owner_team_id IS NOT NULL)
        )
        """
    )
    op.execute("CREATE INDEX ON layer (dataset_id)")
    op.execute("CREATE INDEX ON layer (owner_user_id)")

    op.execute(
        """
        CREATE TABLE basemap (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name            TEXT NOT NULL,
            description     TEXT,

            owner_user_id   UUID NOT NULL REFERENCES app_user(id),
            owner_team_id   UUID REFERENCES team(id),
            visibility      visibility_t NOT NULL DEFAULT 'private',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at      TIMESTAMPTZ,

            CONSTRAINT basemap_team_visibility_needs_a_team
                CHECK (visibility <> 'team' OR owner_team_id IS NOT NULL)
        )
        """
    )
    op.execute("CREATE INDEX ON basemap (owner_user_id)")

    op.execute(
        """
        CREATE TABLE basemap_layer (
            basemap_id  UUID NOT NULL REFERENCES basemap(id) ON DELETE CASCADE,
            -- RESTRICT, not CASCADE. Two basemaps sharing one layer is the
            -- point of `adr/0010`, and deleting the layer must be refused with
            -- the basemaps named (`07` §6.1) rather than silently emptying
            -- somebody else's map.
            layer_id    UUID NOT NULL REFERENCES layer(id) ON DELETE RESTRICT,
            z           INTEGER NOT NULL,
            PRIMARY KEY (basemap_id, layer_id)
        )
        """
    )
    op.execute("CREATE INDEX ON basemap_layer (layer_id)")


# --- preferences --------------------------------------------------------------


def _preferences() -> None:
    """Three tiers, resolved user → team → global (`adr/0010` §4).

    `default_basemaps` is JSONB keyed on `presentation_t` plus `"*"` for the
    general default, rather than a column per presentation. A new presentation
    would otherwise be a migration on three tables.
    """
    # Created by the initial schema. Asserted here rather than created, so a
    # deployment that somehow lacks it converges — and deliberately not dropped
    # on downgrade; see `downgrade`.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_preferences (
            user_id            UUID PRIMARY KEY REFERENCES app_user(id) ON DELETE CASCADE,
            schema_version     INTEGER NOT NULL DEFAULT 1,
            default_basemaps   JSONB NOT NULL DEFAULT '{}'::jsonb,
            default_palette_id UUID REFERENCES palette(id),
            default_project_id UUID REFERENCES project(id),
            preferred_units    JSONB NOT NULL DEFAULT '{}'::jsonb,
            default_templates  JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE team_preferences (
            team_id            UUID PRIMARY KEY REFERENCES team(id) ON DELETE CASCADE,
            schema_version     INTEGER NOT NULL DEFAULT 1,
            default_basemaps   JSONB NOT NULL DEFAULT '{}'::jsonb,
            default_palette_id UUID REFERENCES palette(id),
            preferred_units    JSONB NOT NULL DEFAULT '{}'::jsonb,
            default_templates  JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE global_preferences (
            -- One row, enforced. A second would make "the global default"
            -- ambiguous with nothing to arbitrate it, and a LIMIT 1 in
            -- application code is not a constraint.
            id                 BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
            schema_version     INTEGER NOT NULL DEFAULT 1,
            default_basemaps   JSONB NOT NULL DEFAULT '{}'::jsonb,
            default_palette_id UUID REFERENCES palette(id),
            preferred_units    JSONB NOT NULL DEFAULT '{}'::jsonb,
            default_templates  JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


# --- row level security --------------------------------------------------------


def _row_level_security() -> None:
    """The same four policies every other ownable table carries.

    Written as a loop over `NEW_OWNABLE` for the same reason the initial schema
    does: a new ownable table that gets three of the four policies has a hole
    in it, and the hole is invisible until someone finds it.

    The preference tables are deliberately **not** here. `user_preferences` is
    keyed on `user_id` and `team_preferences` on `team_id`, so they are not
    ownable objects with a visibility — they are settings, and the service
    layer scopes them. `global_preferences` is a single row every principal may
    read and only a global administrator may write, which is a capability check
    rather than a row policy.
    """
    for table in NEW_OWNABLE:
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
        op.execute(f"CREATE POLICY {table}_remove ON {table} FOR DELETE USING ({writable})")
