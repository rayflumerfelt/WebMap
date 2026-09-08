# 0007 — Multi-user deployment with directory SSO

## Status

Accepted — 2026-09-08. **Supersedes [[0001-single-user-deployment]].**

## Context

[[0001-single-user-deployment]] removed the authorization model on the premise that the
deployment was one operator with no identity provider. That premise was wrong, and it was
wrong in the way that ADR named as its own reversal trigger: *a second regular user*.

The actual deployment is:

- **Final:** an internal server, accessed by multiple employees.
- **Development and testing:** the author's local machine.
- **Authentication:** single sign-on against the users' Windows credentials.

So the authorization model comes back. What does *not* come back is the schedule risk that
made `03-auth-security.md` "the highest-schedule-risk document in the set" — that was OAuth 2.1
with Dynamic Client Registration, needed because `claude.ai` had to authenticate to a remote
MCP server. [[0008-local-stdio-mcp]] removes the requirement rather than solving it.

## Decision

Restore the model described in the original `02-data-model.md` §2 and `03-auth-security.md`
§2–§6, with one substitution and one subtraction.

**Restored in full:**

1. **Authentication.** OIDC authorization-code flow with PKCE against the corporate directory.
   Users and teams mirror the directory and are synced from claims on login; the directory is
   authoritative and `team_member` is a cache.
2. **The grant model.** `team`, `team_member`, `access_grant`, `visibility_t`, `grant_role_t`,
   and the `owner_team_id` / `visibility` columns on every ownable table. Effective permission
   resolves as owner → grant → visibility scope, exactly as originally specified.
3. **Row-level security** on every ownable table, with the app role lacking `BYPASSRLS` and a
   startup assertion that fails loudly if it is granted. Two layers, both required: an
   application check for good error messages, RLS as the backstop for the query someone
   forgets.
4. **Identity propagation.** `Principal`, `principal_session`, and a `JobContext` carrying
   `requested_by` and `team_ids` so a job resolves datasets as the user who asked for it.
5. **Per-user and per-team quotas**, replacing the bare resource limits of [[0001]].

**Substituted — identity provider.** Written against **OIDC**, expected to be Entra ID. One
protocol serves the browser SPA, the local MCP process, and group claims for team sync, with
no keytab and no SPN registration. MSAL's Windows broker gives the local process silent SSO
against the logged-in Windows account, so it is still Windows credentials from the user's side.

*Fallback if the organisation is pure on-prem AD with no Entra ID:* Kerberos/SPNEGO for the
browser, LDAP for group lookup, and `httpx-gssapi` via SSPI for the local MCP process.
Confirm which applies in week 1 — the same advice the original spec gave about DCR, for the
same reason.

**Subtracted — `webmap-auth`.** No authorization server, no Dynamic Client Registration, no
upstream federation. It existed solely so `claude.ai` could obtain a token for a remote MCP
server, and [[0008-local-stdio-mcp]] means nothing needs one. The SPA talks OIDC to the
corporate IdP directly.

## Consequences

**Roadmap Phase 1 comes back, but smaller than it was.** The original was 4–6 weeks and most
of it was `webmap-auth`. Restoring OIDC login, the permission model, RLS, and directory sync
is real work — call it 2–3 weeks — but the DCR-brokering risk that the original flagged for
week-1 escalation is gone entirely, not mitigated.

**Authorization is enforced at the API and nowhere else.** [[0008-local-stdio-mcp]] puts the
MCP server on the user's workstation, where it cannot be trusted to enforce anything. This is
a strengthening of `04-mcp-server.md` §1: the MCP layer now contains *no* authorization logic,
rather than sharing the API's.

**Three decisions from the single-user work survive untouched**, because they were never about
user count: [[0003-geoprocessing-owns-crs]], [[0004-geoprocessing-owns-geometry]], and
[[0006-render-image-delivery]]. So do the four defect fixes committed alongside them — the
EPSG codes, the MVT index defect, the legend off-by-one, and the pre-commit excludes.

**Two decisions survive with amendments**, recorded in their own files:
[[0002-duckdb-data-plane]] (its single-writer objection was overstated) and
[[0005-single-editor-persistence]] (optimistic concurrency returns on the version pointer).

**What was never in question:** the SSRF controls, the untrusted-dataset-content rules, and
the destructive-operation safety in `03-auth-security.md`. Those defend against hostile data
rather than hostile users, and they were correct under both premises. `03` keeps its
data-security sections and regains its identity sections around them.

The lesson worth recording: [[0001]] named its own reversal trigger, which is why this is a
superseding ADR and a forward diff rather than an archaeology exercise.
