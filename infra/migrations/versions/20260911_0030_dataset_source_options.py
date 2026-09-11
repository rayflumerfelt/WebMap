"""How to re-read a source. `11-file-io.md` §2.4.

A sync re-reads the upstream file, and for the commonest share format it cannot:
a CSV of picks has no intrinsic geometry, so `read_xyz` needs the X, Y and Z
columns named — and `11` §3.1 forbids sniffing them, because sniffing swaps
latitude and longitude often enough to put a mirrored map in a deck.

Those columns were chosen once, by a person, at ingest. Without somewhere to
keep them the choice is lost the moment the upload completes, and a
share-sourced CSV becomes a dataset that can never be refreshed — which is most
of what a share holds.

`source_options` sits beside `source_uri` and `source_checksum` because it is
the same kind of fact: what this dataset came from, and what it takes to read it
again. The alternative considered was the lineage record, which is the right
*shape* — `CLAUDE.md` §3.3 asks that lineage be sufficient to re-run — but an
uploaded dataset has no lineage row, and reading the latest one to find a column
mapping makes a sync depend on a table that exists for provenance.

Revision ID: 0007_dataset_source_options
Revises: 0006_protect_layer_ownership
"""

from __future__ import annotations

from alembic import op

revision = "0007_dataset_source_options"
down_revision = "0006_protect_layer_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE dataset ADD COLUMN IF NOT EXISTS source_options JSONB")
    op.execute(
        "COMMENT ON COLUMN dataset.source_options IS "
        "'Reader options needed to re-read source_uri: column mapping for XYZ/CSV, "
        "layer name for multi-layer formats, encoding. 11-file-io.md 2.4.'"
    )


def downgrade() -> None:
    # Dropping loses the column mapping, and with it the ability to sync any
    # CSV-sourced dataset. That is the honest consequence of going back, and it
    # is recoverable only by re-stating the mapping — which is why the upgrade
    # is additive and nothing depends on the column being present.
    op.execute("ALTER TABLE dataset DROP COLUMN IF EXISTS source_options")
