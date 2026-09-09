"""Refuse team visibility with no team.

Revision ID: 0003_team_visibility
Revises: 0002_owner_column
Create Date: 2026-09-09

The RLS clause from `02` §4 reads:

    visibility = 'team' AND owner_team_id = ANY(current_setting('webmap.team_ids')::uuid[])

A NULL `owner_team_id` matches nothing, so a row marked team-visible with no
team is **private in all but name**. Nothing errors and nothing logs: the
person sharing their work sees it succeed, their team sees nothing, and the
first anyone hears of it is someone asking why they cannot open a link.

Found while implementing map sessions
(`tests/test_session_endpoints.py::test_a_teammate_sees_the_whole_session` —
Ada's teammate Grace got a 404 on Ada's own team-visible session). It was not
session-specific: one dataset in the local stack, registered through the
ordinary upload path, already had it.

`webmap_core.services.ownable.resolve_owner_team` now fills the team in at
every creation path. This constraint is the backstop, and it is the part that
matters: a service that forgets to call the resolver fails at the write rather
than silently hiding a row.

**Existing violations are repaired downward, to 'private'.** That is what they
already are in effect, so nothing changes about who can read them. Guessing a
team from the owner's membership would be the other option and it widens
access on the strength of a guess — the wrong direction to be wrong in.
"""

from alembic import op

revision = "0003_team_visibility"
down_revision = "0002_owner_column"
branch_labels = None
depends_on = None

#: `02` §3.3. The same list as the RLS and trigger migrations; kept literal in
#: each so a migration always says what it did rather than what a shared
#: constant said at the time.
OWNABLE_TABLES = (
    "project",
    "dataset",
    "style_template",
    "palette",
    "map_session",
    "render",
)


def upgrade() -> None:
    for table in OWNABLE_TABLES:
        # Repair before constraining. Downward, to what the row already is.
        #
        # `02` §4 sets FORCE ROW LEVEL SECURITY, which subjects the table owner
        # to its own policies — so this migration cannot see the rows it needs
        # to repair. Lifting FORCE for the statement is the narrow way through:
        # the alternative, setting `webmap.user_id` to something, would make
        # the policy evaluate against a principal that does not exist and
        # silently update nothing. Restored immediately, inside the same
        # transaction, so a failure here leaves FORCE on.
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            UPDATE {table} SET visibility = 'private'
            WHERE visibility = 'team' AND owner_team_id IS NULL
            """
        )
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            ALTER TABLE {table} ADD CONSTRAINT {table}_team_visibility_needs_team
            CHECK (visibility <> 'team' OR owner_team_id IS NOT NULL)
            """
        )


def downgrade() -> None:
    # The repaired rows are not restored: 'private' is what they meant, and
    # re-marking them 'team' would recreate rows that claim a sharing they do
    # not have.
    for table in OWNABLE_TABLES:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {table}_team_visibility_needs_team")
