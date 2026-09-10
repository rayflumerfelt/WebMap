# 0001 — Single-user deployment; remove federated auth, grants, and RLS

## Status

**Superseded by [[0007-multi-user-directory-sso]] — 2026-09-08.** Accepted 2026-09-08 and reversed the same day, once a second regular user appeared — which is the trigger this ADR named for its own reversal. Kept rather than deleted: it records what the removal cost, which is the context 0007 answers.

## Context

`00-overview.md` §4 described the deployment as one company, 50–200 users across
multiple business units, a corporate OIDC provider, and cross-BU sharing as a
first-class feature. Nearly every security and data-model decision in the spec set
followed from that premise:

- `webmap-auth`, our own OAuth 2.1 authorization server with Dynamic Client
  Registration, federating upstream to the corporate IdP (`03` §4). `03`'s own
  preamble calls this "the highest-schedule-risk document in the set."
- An ownership + visibility + explicit-grant model with `team`, `team_member`, and
  `access_grant` tables (`02` §2, §3.2, §3.3).
- Row-level security on every ownable table as an authorization backstop, plus a
  startup assertion that the app role lacks `BYPASSRLS` (`02` §4, `03` §3).
- End-to-end identity propagation through MCP, jobs, and the render service
  (`03` §5), with per-team quotas (`10` §7) and a two-year audit log (`02` §3.12).

That premise is not correct for the actual near-term deployment, which is a single
operator with no corporate identity provider and no one to share with. Roadmap
Phase 1 — four to six weeks — is almost entirely the machinery above.

Building it anyway would mean carrying the cost of a multi-tenant authorization
system, and its schedule risk, to protect data from a population of one.

## Decision

Target a single-user deployment. Specifically:

1. **Remove `webmap-auth` entirely.** No authorization server, no DCR, no upstream
   federation. The MCP server authenticates with a static bearer token from the
   local secret store; the SPA runs against a local session.
2. **Remove the grant model.** `team`, `team_member`, `access_grant`, the
   `visibility_t` and `grant_role_t` enums, and the `owner_team_id` /
   `visibility` columns all go. `owner_user_id` is retained as a single-valued
   column so lineage and audit records name an actor.
3. **Remove row-level security** and the `BYPASSRLS` startup assertion. There is no
   second principal to isolate against, and `webmap.user_id` /
   `webmap.team_ids` session configuration goes with it.
4. **Remove per-team quotas.** A concurrency cap on the worker remains, to stop a
   runaway job set from exhausting local memory — that is resource hygiene, not
   authorization.
5. **Reduce the audit log** to job and destructive-operation records, kept for
   provenance rather than compliance.

**What is explicitly retained**, because it defends against hostile *data* rather
than hostile users, and the threat is unchanged:

- SSRF controls in the render service (`03` §7) — style validation, the Playwright
  `page.route` allowlist, and network isolation. A crafted style document reaching
  a headless browser inside the network is a live risk with one user or a hundred.
- Treating dataset content as untrusted (`03` §9). A partner-supplied shapefile with
  `../../etc/passwd` as a layer name is a real file that arrives regardless of how
  many people work here.
- Never writing in place, and soft deletes (`03` §8). Losing a partner-delivered
  shapefile is unrecoverable whether or not anyone else could have read it.

## Consequences

Roadmap Phase 1 collapses from four to six weeks to roughly one, and the project's
largest identified schedule risk is removed rather than mitigated.

`03-auth-security.md` loses §2 through §6 and is retitled to reflect that what
remains is data security, not identity.

**Reintroducing multi-user access later is a real project, not a flag.** The
retained `owner_user_id` means lineage and audit records survive the transition, and
service functions keep taking an explicit actor argument so the signature does not
change — but the schema, the policies, and the authorization server would all have
to be built at that point. This decision accepts that cost in exchange for not
paying it now. Revisit if a second regular user appears.

Related: [[0002-duckdb-data-plane]] removes PostGIS, which was partly justified by
RLS as an authorization backstop.
