"""`user_preferences` gets `default_basemaps`, and loses the pre-0010 shape.

Revision ID: 0005_user_default_basemaps
Revises: 0004_layers_basemaps
Create Date: 2026-09-10

Revision 0004 created `team_preferences` and `global_preferences` with a
`default_basemaps` column and asserted `user_preferences` with `CREATE TABLE IF
NOT EXISTS`. **The table already existed**, so the `IF NOT EXISTS` did nothing
at all and the user tier kept the initial schema's columns — which do not
include `default_basemaps`. Resolution read the user tier first and failed on
every call with `column "default_basemaps" does not exist`, which is as loud a
failure as this class of bug ever gets and still only showed up when something
finally read the column.

The lesson is narrow and worth stating: `CREATE TABLE IF NOT EXISTS` asserts
that a table *exists*, never that it has the shape written underneath. Use it
only for a table whose columns are already guaranteed, and add columns with
`ALTER TABLE … ADD COLUMN IF NOT EXISTS` as this revision does.

**`default_basemap_layers` is dropped rather than converted, and the data in it
is not recoverable by any mechanical rule.** It held a JSONB array of *dataset*
ids — the pre-`adr/0010` idea of a basemap, and exactly the limitation the ADR
was written to remove. A basemap is now an ordered set of `layer` rows, and a
layer carries an owner, a visibility, a presentation and its symbology. Turning
one user's dataset-id list into layers would mean inventing all four for every
entry: guessing `presentation` for a dataset that could legitimately be drawn
as a filled grid or as contours, and guessing wrong produces a map that looks
plausible and is not what the user had, with nothing in the record to say so.
Better to drop it and have people pick their default basemap once.
"""

from __future__ import annotations

from alembic import op

revision = "0005_user_default_basemaps"
down_revision = "0004_layers_basemaps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE user_preferences ADD COLUMN IF NOT EXISTS "
        "default_basemaps JSONB NOT NULL DEFAULT '{}'::jsonb"
    )
    # See the module docstring: this cannot be converted, only discarded.
    op.execute("ALTER TABLE user_preferences DROP COLUMN IF EXISTS default_basemap_layers")


def downgrade() -> None:
    """Restores the column, empty.

    A downgrade cannot bring the lists back — `upgrade` did not keep them, and
    keeping them in a shadow column would be a second source of truth for the
    thing this revision exists to replace. Restoring the *column* is what makes
    the initial schema's shape valid again so an older revision can run.
    """
    op.execute(
        "ALTER TABLE user_preferences ADD COLUMN IF NOT EXISTS "
        "default_basemap_layers JSONB NOT NULL DEFAULT '[]'::jsonb"
    )
    op.execute("ALTER TABLE user_preferences DROP COLUMN IF EXISTS default_basemaps")
