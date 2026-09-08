# 0008 — The MCP server runs locally over stdio

## Status

Accepted — 2026-09-08. Amends `01-architecture.md` §2.2 and `04-mcp-server.md` §1, §3.

## Context

`04-mcp-server.md` specified Streamable HTTP, stateless JSON, mounted at `/mcp` on the
`webmap-api` ASGI app — "not stdio (this is a remote server)". Remote MCP servers authenticate
with OAuth 2.1, and the specification expects Dynamic Client Registration so a client can
register itself without an administrator.

That single requirement drove the most expensive component in the original plan.
`03-auth-security.md` opened by calling itself "the highest-schedule-risk document in the set,"
and `12-roadmap.md` Phase 1 put four to six weeks behind standing up `webmap-auth`: an OAuth
server supporting DCR, federating upstream to the corporate IdP, because Entra ID, Okta and
Ping do not expose DCR by default and enabling it is frequently blocked by security policy.

[[0007-multi-user-directory-sso]] brings back SSO against Windows credentials. Integrated
Windows Authentication is Kerberos/SPNEGO — the browser presents a ticket because the machine
is domain-joined. `claude.ai` cannot do that from outside the domain. So the remote-MCP path
would require rebuilding `webmap-auth` essentially as originally specified.

But the deployment is internal-only (`00-overview.md` §7), and the people using it are at
domain-joined Windows workstations. If Claude is already running *on that workstation* —
Claude Code or Claude Desktop — the whole problem dissolves.

## Decision

**The MCP server runs locally, on each geologist's workstation, over stdio.** It is a thin
client of `webmap-api`, authenticating to it as the logged-in user.

```
workstation                              internal server
┌─────────────────────────┐              ┌──────────────────────┐
│ Claude Code / Desktop   │              │  webmap-api          │
│         │ stdio         │   HTTPS +    │  - enforces authz    │
│         ▼               │   user's     │  - owns the database │
│ webmap-mcp (local)      │──identity───▶│  - RLS backstop      │
│  - no DB connection     │              └──────────────────────┘
│  - no authz logic       │
│  - formats responses    │              browser ──OIDC──▶ webmap-api
└─────────────────────────┘
```

Three properties follow, and all three matter:

1. **No authorization server.** Nothing needs an OAuth token for a remote MCP endpoint, so
   `webmap-auth`, DCR, and upstream federation are not built. This is the single largest cost
   removal available in the plan.
2. **The local process holds no authority.** It runs where the user can edit it, so it is
   trusted with nothing. It has no database connection, no service credential, and no
   permission logic. Every call goes over HTTPS to `webmap-api` carrying the user's identity,
   and the API resolves permissions against the live grant model exactly as it does for the
   SPA.
3. **Windows credentials work directly.** With Entra ID, MSAL's Windows broker obtains a token
   silently for the logged-in account. With on-prem AD, `httpx-gssapi` over SSPI obtains a
   Kerberos ticket for the API's SPN. Either way there is no prompt and no stored password.

Transport becomes **stdio**, not Streamable HTTP. Tool names, schemas, annotations, pagination,
error vocabulary, and evaluations are unchanged — this is a transport and deployment decision,
not a redesign of the tool surface.

## Consequences

**`01-architecture.md` §2.2's co-location rationale changes shape.** It argued that MCP must
share the API's ASGI app because splitting them "means duplicating authorization logic, which
is the single most dangerous thing to duplicate." That concern is now satisfied more strongly
rather than abandoned: the local server does not duplicate authorization logic because it has
none. What it duplicates is response formatting, which is safe to have in two places.

**The cost is reach.** Claude must run on a domain-joined machine that can see the internal
network. `claude.ai` in a browser on a phone will not work, and neither will a laptop off the
VPN. Given `00-overview.md` §7 already forbids public internet exposure, this costs nothing
today — but it is the thing to check before anyone promises mobile access.

**Deployment gains a per-workstation step.** The MCP server has to be installed and registered
in each user's Claude client config. That is a packaging task the previous design did not have,
and it should be a single command. With ~10 users it is trivial; at 200 it would want an
installer and a managed config, which is the point at which the remote-MCP path with a
pre-provisioned confidential client becomes worth revisiting.

**Would change our mind.** Access needed from outside the domain, or a user count high enough
that per-workstation installation becomes an operational burden. The fallback is not the full
`webmap-auth` build — it is a pre-provisioned confidential OAuth client per environment, which
the original spec already named as its own fallback and which costs a manual registration step
rather than an authorization server.
