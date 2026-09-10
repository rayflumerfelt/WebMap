"""The ownership-change trigger reaches `layer` and `basemap`.

Revision ID: 0006_protect_layer_ownership
Revises: 0005_user_default_basemaps
Create Date: 2026-09-10

Revision 0002 put `webmap_forbid_ownership_change` on every ownable table there
was, and `adr/0010`'s `layer` and `basemap` arrived two revisions later without
it. So the two objects users actually share — the ones the whole sharing model
exists for — were the two whose ownership an ordinary UPDATE could change.

Nothing exploited it, because the only writer is `services.layers` and that
never touches the ownership columns. That is exactly why it is worth fixing
rather than noting: the guard exists so a *future* writer cannot, and a guard
with two holes in it is a guard nobody can rely on.

The list is deliberately not shared with revision 0002. A migration is a record
of what happened at a point in time, and importing a constant from another one
would make an old migration's behaviour change when the constant does.
"""

from __future__ import annotations

from alembic import op

revision = "0006_protect_layer_ownership"
down_revision = "0005_user_default_basemaps"
branch_labels = None
depends_on = None

#: The ownable tables `adr/0010` added, which revision 0002 could not know
#: about.
LATE_OWNABLE = ("layer", "basemap")


def upgrade() -> None:
    for table in LATE_OWNABLE:
        # `webmap_forbid_ownership_change` is created by revision 0002 and is
        # not redefined here: one function, several triggers, so a change to
        # the rule reaches every table at once.
        op.execute(
            f"""
            CREATE TRIGGER {table}_forbid_ownership_change
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION webmap_forbid_ownership_change()
            """
        )


def downgrade() -> None:
    for table in LATE_OWNABLE:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_forbid_ownership_change ON {table}")
