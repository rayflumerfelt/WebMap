# 0005 — Copy-on-write feature persistence, single editor per layer

## Status

Accepted — 2026-09-08

## Context

`09-editing.md` §5.1 specified optimistic locking against mutable per-dataset
tables:

```sql
UPDATE feat.ds_9f3a... SET geom = ..., version = version + 1
WHERE id = :feature_id AND version = :expected_version
```

Zero rows returned meant a concurrent edit, and the API returned 409 with server
state so the client could show a diff and let the user choose. `09` §6 then cleared
the redo stack on conflict, because replaying forward over someone else's change
produces a state nobody authored.

That machinery exists to answer one question: what happens when two people edit the
same layer. Under [[0001-single-user-deployment]] there is no second person, and the
requirement is explicitly that one editor holds a layer at a time.

[[0002-duckdb-data-plane]] independently moved feature storage from mutable
Postgres tables to GeoParquet objects, which are immutable. Those two facts together
change what the persistence model should be, rather than merely making the old one
unnecessary.

## Decision

**Feature edits are copy-on-write.** A flushed edit batch writes a new versioned
GeoParquet object; the previous version is retained. The `dataset` row in Postgres
holds a pointer to the current version, and advancing that pointer is the atomic
commit.

- **No optimistic locking, no version column on features, no 409 conflict path.**
  A layer is held by one editor.
- **`dataset.parquet_key` plus `dataset.version`** name the current object. Version
  history is a table of prior keys, so any earlier state is directly addressable.
- **The batch remains the unit of undo** (`09` §5.2, §6 are otherwise unchanged) —
  and undo now has a durable counterpart, since the prior version is a real object
  rather than a reconstruction from inverted commands.

This satisfies `03` §8's "never write in place" more directly than the previous
design did. Under the old model that rule applied only to *source* files on a share;
the working copy in `feat.*` was mutated freely. Now it holds all the way down.

## Consequences

Every edit costs a rewrite of the layer's Parquet object rather than an `UPDATE` of
the affected rows. For the layer sizes in `09` §8 — editing is capped at 5,000
features in the viewport — that is cheap. For a 5M-feature layer it would not be,
which is why the working-set mechanism in `09` §8 stays: edits apply to a bounded
viewport extract, and only that extract is rewritten.

Storage grows with edit count. A retention policy is required — keep every version
for 30 days to match the soft-delete window in `03` §8, then thin to daily. This is
new work the previous design did not need.

**Reintroducing concurrent editing is a real project.** It would need either a
locking service over the version pointer or a merge strategy over Parquet objects,
and reopening it would also reopen [[0002-duckdb-data-plane]], since a
multi-writer store is exactly what DuckDB is not. Trigger to reconsider: a second
regular editor, which is the same trigger as
[[0001-single-user-deployment]].

The E2E test that `09` §9 calls the one that matters most — edit a fault, re-grid,
confirm the surface changed at the fault — is unaffected and still the highest-value
test in the editing surface.

---

## Amendment — 2026-09-08

**Copy-on-write is retained. The single-editor assumption is not**, following
[[0007-multi-user-directory-sso]].

The storage model was the good half of this decision and it survives intact: a flushed batch
writes a new versioned GeoParquet object, and advancing `dataset.version` is the commit.
Immutable objects mean two concurrent editors cannot corrupt each other's writes — they
produce two separate objects.

What returns is **optimistic concurrency on the version pointer**, and only there:

```sql
UPDATE dataset
SET parquet_key = :new_key, version = version + 1, updated_at = now()
WHERE id = :dataset_id AND version = :expected_version
RETURNING version;
```

Zero rows means someone else committed while this batch was in flight. The API returns 409
with the current version so the client can rebase or surface a diff. Never silently overwrite.

This is less machinery than the per-feature optimistic locking this ADR originally removed —
one row, one column, one comparison per commit, rather than a version column on every feature
and a conflict path per row. The immutable objects do the hard part.

`EditHistory.onFlushFailed()` covers this case already; it does not need a separate
`onConflict()`.

**Unchanged:** the retention rule (every version 30 days, then thinned to daily), the 5,000-
feature viewport working set that keeps rewrites cheap, and the crash-safety property that a
half-written object is invisible until the pointer moves.
