"""Make ownership transfer impossible through an ordinary UPDATE.

Revision ID: 0002_owner_column
Revises: 0001_initial
Create Date: 2026-09-09

`03-auth-security.md` §3.3: "Transferring ownership is an explicit operation,
audited." The RLS policies from `02` §4 do not achieve that, and the gap is
not obvious.

The UPDATE policy's `WITH CHECK` re-evaluates the *new* row. For an editor the
qualifying clause is the grant subquery, which tests `object_type` and
`object_id` — neither of which changes when `owner_user_id` does. So the new
row still satisfies the policy, and an editor can quietly transfer ownership
of someone else's dataset to a third party. Found by
`tests/test_permissions_rls.py::test_an_editor_cannot_reassign_ownership`.

A policy cannot express "this column may not change": there is no OLD in a
`WITH CHECK`. Two tools can.

**Column privileges do not work here.** A column-level `REVOKE UPDATE (col)`
is inert while the role holds table-level UPDATE, and the fix — revoking the
table privilege and granting every other column individually — means every
future column needs a matching GRANT in its migration or writes to it fail at
runtime. That is a maintenance trap.

**A trigger does.** It compares OLD and NEW, needs no per-column upkeep, and
leaves one explicit door: a transaction-local setting the audited transfer
operation will set. Ordinary code cannot set it by accident, and grepping for
the setting name finds every legitimate transfer.

The block applies to owners too. An owner transferring their own object is
legitimate, but it should still go through the audited operation rather than
a bare column write.

**The setting alone is not enough to transfer**, and that is worth knowing
before you try. RLS also refuses: the UPDATE policy's `WITH CHECK` requires
the *new* row to be owned by the acting principal or granted to them, and a
row handed to someone else satisfies neither. So implementing transfer means
both opening this trigger and extending those policies — deliberately two
decisions rather than one, since between them they are the only way an
object changes hands. Transfer is not a Phase 1 deliverable, so neither has
been done; this migration installs the block, not the door.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_owner_column"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Same list as the RLS block in 0001.
OWNABLE_TABLES = (
    "project",
    "dataset",
    "style_template",
    "palette",
    "map_session",
    "render",
)

#: The transaction-local escape for the audited transfer operation. Named
#: alongside the other `webmap.*` settings so it is obvious it belongs to the
#: same mechanism as the RLS context.
TRANSFER_SETTING = "webmap.allow_ownership_transfer"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION webmap_forbid_ownership_change()
        RETURNS trigger AS $$
        BEGIN
            IF current_setting('{TRANSFER_SETTING}', true) = 'on' THEN
                RETURN NEW;
            END IF;
            IF NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id
               OR NEW.owner_team_id IS DISTINCT FROM OLD.owner_team_id THEN
                RAISE EXCEPTION
                    'Ownership of %.% cannot be changed by a direct UPDATE. '
                    'Transferring ownership is an explicit, audited operation '
                    '(03-auth-security.md 3.3). If you are implementing that '
                    'operation, set {TRANSFER_SETTING} transaction-locally.',
                    TG_TABLE_NAME, OLD.id
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table in OWNABLE_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER {table}_forbid_ownership_change
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION webmap_forbid_ownership_change()
            """
        )


def downgrade() -> None:
    for table in OWNABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_forbid_ownership_change ON {table}")
    op.execute("DROP FUNCTION IF EXISTS webmap_forbid_ownership_change()")
