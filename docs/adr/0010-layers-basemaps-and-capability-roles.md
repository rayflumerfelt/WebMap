# 0010 — Layers and basemaps as shared objects; roles as capabilities

## Status

Accepted — 2026-09-09. **Amends [[0007-multi-user-directory-sso]]** on identity provisioning.
Extends `02-data-model.md` §3 and `03-auth-security.md` §3.

## Context

Four requirements arrived together, and they interact:

1. A **map** is a set of layers with one *active* layer. Only the active layer is editable;
   Auto Zoom fits the active layer, Zoom to Extents fits all of them.
2. A **basemap** is a named collection of layers. Layers are independent of it — two basemaps
   can share the same layer. An operation's output is added to a basemap as the active layer.
3. Users, teams and the deployment each get **default basemaps**, per kind of layer, resolving
   user → team → global.
4. Four **roles**: User, Team Member, Team Administrator, Global Administrator, with team
   administrators managing their own membership and global administrators managing users.

Three of these collide with decisions already made.

**Requirement 4 contradicts [[0007]] outright.** `03-auth-security.md` §2 reconciles
`team_member` from IdP group claims on every login, and `02-data-model.md` §3.2 says teams
"mirror the corporate directory. Never a source of truth." A Team Administrator adding a
member in WebMap would see them removed at that person's next sign-in — silently, and with no
error anyone could act on. The same applies to a Global Administrator creating users: the
`app_user` row is upserted from the OIDC `sub`, so a deleted user returns on next login.

**Requirement 4 also overlaps the grant model.** `access_grant` already answers "who may edit
this?" per object. A four-rank role answers it globally. Both cannot be authoritative, and a
plain User holding an `editor` grant on a colleague's layer resolves differently under each.

**Requirement 2 has no home in the schema.** A basemap today is
`user_preferences.default_basemap_layers` — a JSONB list of dataset ids in one user's
preferences. It cannot be named, shared, or referenced by two maps.

Confirmed with the requester: the deployment **may run without a corporate IdP**, and wants
directory and manual identity as an option; layers and basemaps **must be shareable**;
users are **deactivated, never deleted**; and a layer belongs to no basemap in particular.

## Decision

### 1. Identity provisioning is a deployment mode, and teams carry it individually

`WEBMAP_IDENTITY_MODE` is `directory` or `managed`.

- **`directory`** — OIDC as [[0007]] specifies. `app_user` is upserted from claims; a team
  with `idp_group_id` set has its membership reconciled on every login.
- **`managed`** — WebMap is authoritative. A Global Administrator creates users with local
  credentials; there is no reconciliation.

**A team with a null `idp_group_id` is locally managed in either mode.** That is what makes
"mostly directory, plus an ad-hoc project team" expressible without a second switch, and it
means the authority question is answered per team rather than per deployment.

Membership writes against a directory-synced team are **refused**, naming the group:

> `Permian Basin` membership comes from the directory group `sg-permian-geo` and is
> reconciled at every sign-in, so a change here would be undone. Ask the directory owner to
> change the group, or create a WebMap-managed team.

Refusing is the point. Accepting the write and letting reconciliation quietly undo it is the
failure this ADR exists to prevent.

### 2. Roles grant capabilities; grants govern objects

Two axes, deliberately not merged:

- **Capabilities** are what a role lets you *do*: publish globally, manage a team, create
  users. Stored as `app_user.is_global_admin` and `team_member.role ∈ (member, admin)`.
- **Object access** stays exactly as [[0007]] restored it: owner → grant → visibility scope.

`team_member.role` is per-team on purpose, because "administrator of Permian, member of
Delaware" is ordinary and a single four-value enum cannot say it.

| Capability | Who |
|---|---|
| Create, edit, delete own layers, basemaps and maps | any active user |
| View and duplicate anything visible to them | any active user |
| Grant access to an object they own | owner |
| Publish to a team (`visibility = 'team'`) | a member of that team |
| **Publish globally (`visibility = 'org'`)** | **global administrator only** |
| Manage membership of a locally-managed team | that team's administrator, or global |
| Set team defaults | that team's administrator, or global |
| Set global defaults, create and deactivate users | global administrator |

**"Team Member" is not stored.** The requirement distinguishes it from "User" only by
belonging to a team, which `team_member` already records. A stored role would be a second
source of truth able to disagree with membership.

Publishing globally becoming administrator-only is a **behaviour change**: today any user may
set `visibility = 'org'`.

### 3. `layer` and `basemap` become ownable entities

A **layer** is a dataset plus how it is drawn — the thing a user names, shares and duplicates.
A **basemap** is an ordered collection of layers, many-to-many, so two basemaps share a layer
rather than copying it.

Both carry the standard ownable columns (`owner_user_id`, `owner_team_id`, `visibility`,
`deleted_at`) and are therefore covered by the existing grant model and RLS policies with no
new authorization code. That is the main reason for making them tables rather than JSONB.

A layer carries a `presentation` — `vector`, `filled_grid`, `contour`, `filled_contour` and so
on. **This is not `dataset_kind`.** A colour-filled grid and a contour map of the same surface
are the same `dataset_kind = 'grid'`, drawn two ways, and contours are a derived `vector`
dataset. Default basemaps are keyed on presentation, so conflating the two would make "my
default for contour maps" unexpressible.

Soft-deleting a layer that a basemap still references is **refused**, naming the basemaps.
Shared objects need the shared-ness to be visible at the moment it costs something.

### 4. Default basemap resolution

At each tier, the presentation-specific default is tried before the general one, and only then
does resolution descend:

```
user.defaults[presentation] → user.defaults[*]
  → team.defaults[presentation] → team.defaults[*]
    → global.defaults[presentation] → global.defaults[*]
      → no basemap
```

A user who set a general default meant it to beat a team's, which is why the tier is exhausted
before descending rather than matching presentation across all three first.

**Ties between teams resolve alphabetically by `team.slug`**, and the UI names the team the
default came from. Most-recently-updated was the alternative and is worse: a colleague editing
a preference would change someone else's map with no visible cause. If deterministic-but-
arbitrary proves annoying, `user_preferences.primary_team_id` is the fix, and it can be added
without changing the rule above.

### 5. Users are deactivated, never deleted

`app_user.is_active = false`. They cannot sign in. **Their objects remain**, owned by them and
still visible per their visibility and grants — which is the entire point: deleting a
departing geologist's team-visible layer breaks every colleague's map that references it.

Removing a user from a team deletes the `team_member` row and touches nothing they own, as the
requirement states. Objects they published to that team stay owned by them and keep
`owner_team_id`; they simply lose their own team-visibility route to them.

Deactivation shows what the account owns before confirming, and is audited. `lineage.created_by`
and `audit_event.actor_user_id` keep pointing at a real row, which an erasable user would not.

## Consequences

**Local password authentication is new security surface, and it is the largest risk here.**
`managed` mode needs `app_user.password_hash`, Argon2id, per-account lockout, and an
administrator-initiated reset path — there may be no mail server to send a reset link.
[[0009-offline-identity-seam]]'s verifier seam is where it belongs: a third verifier alongside
OIDC and dev, so the surface stays behind one interface. `03-auth-security.md` §11's checklist
gains rows for storage, lockout and reset. **None of this is needed for a `directory`
deployment**, and it should not be built until a `managed` one is actually wanted.

**`map_session.layers` changes shape**, from inline layer dictionaries to
`[{layer_id, opacity?, visible?, z}]` alongside `basemap_id` and `active_layer_id`. The
document already carries `schema_version`, so the migration path exists; every ad-hoc layer a
session holds now needs a `layer` row, which is what makes "duplicate this into my layers" a
row copy rather than a new concept.

**Duplicating a layer copies the registration, not the data.** GeoParquet and COG objects are
immutable ([[0005-single-editor-persistence]]), so a duplicate references the same object with
its own row, its own owner, and `visibility = 'private'` regardless of the source's. Copying a
two-gigabyte COG because someone clicked Duplicate is avoidable and would be surprising.

**"Only the active layer can be edited" is a user-interface affordance and must never be an
authorization boundary.** The API checks permission on every request regardless of what the
client considers active. Written down because the rule reads like a security control and is
not one.

**Roadmap.** Layers, basemaps and defaults are Phase 5 work (`12-roadmap.md`), alongside the
styling and editing surface they belong to. The capability roles are smaller and can land with
them. `managed` identity mode is its own increment and should not be bundled with either.

**What this does not change.** The two-layer authorization model, RLS on every ownable table,
identity propagation through `Principal` and `JobContext`, and the quota model all stand
exactly as [[0007]] restored them. This ADR adds objects and capabilities on top; it removes
nothing.
