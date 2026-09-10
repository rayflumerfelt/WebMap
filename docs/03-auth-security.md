# 03 — Authentication and Security

> **Two things changed since this document was first written**, both recorded in `adr/`.
>
> The authorization model is back after a single-user detour — see
> `adr/0007-multi-user-directory-sso.md`. Deployment is an internal server used by several
> employees, authenticating against their Windows credentials.
>
> `webmap-auth` is **not**. The OAuth 2.1 authorization server with Dynamic Client
> Registration existed so `claude.ai` could authenticate to a remote MCP server. The MCP
> server now runs locally on each workstation over stdio, so nothing needs one — see
> `adr/0008-local-stdio-mcp.md` and §4. That removes what this document previously called
> the highest schedule risk in the set.

> **Implementation note.** The OIDC authorization-code flow (§2) and the credential
> broker (§4.3) are written but have never run against a directory — development has no
> route to one. `adr/0009-offline-identity-seam.md` puts a verifier seam at the narrowest
> point so everything downstream of token verification is exercised offline by the same
> code production runs. What remains untested is the acquisition call itself, and the
> three things most likely to be wrong are named in that ADR: the audience value, the
> groups claim name, and whether groups arrive as object ids or display names.

---

## 1. Threat model

Two classes of threat, and they are unrelated. Conflating them is how one gets neglected.

**Threats from people** — several employees share this system, and not everyone should see
everything.

| Threat | Vector | Control |
|---|---|---|
| User reads data they aren't cleared for | Missing permission check on a service function | Application check + RLS backstop (§3) |
| Same, via MCP | Claude calls a tool as the wrong identity | Identity propagation, no service account (§5) |
| Same, via tiles | Unauthenticated tile endpoints | Signed, scoped tile URLs (§6) |
| Same, via feature objects | Object storage reachable directly | API is the only reader; see `02-data-model.md` §4.1 |
| Privilege escalation via grants | User grants themselves editor | Grants writable only by object owner (§3.3) |
| Token theft | Long-lived bearer tokens in browser storage | Short-lived access tokens, refresh in httpOnly cookie |

**Threats from data** — datasets arrive from partners, vendors, and regulators. Nobody
authored them with this system in mind, and some were authored by someone who was. These are
unchanged by user count and were correct even during the single-user detour.

| Threat | Vector | Control |
|---|---|---|
| SSRF into the internal network | Malicious style JSON given to the headless browser | Style validation + request allowlist (§7) |
| Data exfiltration via render | A style points a source at an internal endpoint; the response appears in the image | Same, plus network isolation (§7.3) |
| Prompt injection via data | Attribute values reach Claude through tool responses | Treat dataset content as untrusted (§9) |
| Path traversal | A layer named `../../etc/passwd`, or a crafted share URI | Filename sanitization (§9), share-root confinement (`11` §2.2) |
| Destructive overwrite | An edit writes back to a partner-delivered source file | Never write in place; versioned outputs (§8) |

---

## 2. Human authentication (browser)

Two modes, selected by `WEBMAP_IDENTITY_MODE` (`adr/0010` §1). Both terminate in the same
session cookie and the same `Principal`, so everything downstream of authentication is
identical.

| Mode | Users come from | Team membership |
|---|---|---|
| `directory` | OIDC upsert on login | Reconciled from group claims for any team with `idp_group_id` |
| `managed` | Created by a global administrator | Managed in WebMap |

**A team with a null `idp_group_id` is locally managed in either mode**, which is what makes
"mostly directory, plus an ad-hoc project team" expressible without a second switch. A
membership write against a synced team is refused and names the group, because accepting it
and letting the next sign-in silently undo it is the failure the distinction exists to
prevent.

`managed` mode needs local credentials, which is genuinely new attack surface — §11 carries
the checklist. It should not be built until a deployment without a directory is actually
wanted; `directory` mode needs none of it.

### 2.1 Directory mode

Standard OIDC Authorization Code + PKCE against the corporate IdP.

```
Browser ──▶ /auth/login ──▶ IdP ──▶ /auth/callback ──▶ session cookie
```

- Access token: JWT, 15 min lifetime, held in memory by the SPA.
- Refresh token: httpOnly, Secure, SameSite=Lax cookie. Never readable by JS.
- Group claims map to `team.idp_group_id`. Team membership is synced on every login —
  the directory is the source of truth, `team_member` is a cache.

```python
# apps/api/src/webmap_api/auth/oidc.py

from authlib.integrations.starlette_client import OAuth
from webmap_core.settings import settings

oauth = OAuth()
oauth.register(
    name="corp",
    server_metadata_url=settings.oidc_discovery_url,
    client_id=settings.oidc_client_id,
    client_secret=settings.oidc_client_secret,
    client_kwargs={"scope": "openid profile email groups"},
)


async def sync_user_from_claims(db, claims: dict) -> AppUser:
    """Upsert user and reconcile team membership from directory groups.

    The directory is authoritative. A user removed from a group in the IdP
    loses team access on their next login — do not require manual cleanup.
    """
    user = await upsert_user(
        db,
        subject=claims["sub"],
        email=claims["email"],
        display_name=claims.get("name", claims["email"]),
    )
    group_ids = claims.get("groups", [])
    await reconcile_team_membership(db, user.id, group_ids)
    return user
```

---

## 3. Authorization

### 3.0 Two axes, not one rank

**Object permission** — may I read or change this thing — resolves as owner → grant →
visibility scope, and is what §3.1 and §3.2 describe.

**Capability** — may I publish globally, manage a team, create a user — is a separate axis,
stored as `app_user.is_global_admin` and `team_member.role`. `02-data-model.md` §2 lists them.

`adr/0010` §2 explains why they are not one four-rank role: a plain user holding an `editor`
grant on a colleague's layer resolves differently under each model, and only one of them can
be authoritative. Capabilities decide what you may *do*; grants decide what you may do it
*to*. A global administrator therefore has no implicit read access to a private layer.

### 3.1 Two layers, both required

1. **Application layer** — an explicit permission check in the service function. This is where
   good error messages come from.
2. **Row-level security** — a database backstop. Makes the failure mode "no rows" instead of
   "wrong user's rows" when someone forgets layer 1.

Never rely on only one. RLS alone gives unhelpful 404s; application checks alone fail open on
the one query someone forgets.

### 3.2 Permission resolution

```python
# python/webmap_core/src/webmap_core/permissions.py

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from uuid import UUID


class Permission(IntEnum):
    """Ordered so comparisons work: OWNER > EDITOR > VIEWER > NONE."""
    NONE = 0
    VIEWER = 1
    EDITOR = 2
    OWNER = 3


@dataclass(frozen=True)
class Principal:
    """The authenticated actor. Constructed once per request, never mutated.

    Frozen, so "never mutated" is enforced rather than asserted — CLAUDE.md
    §4.2 asks for frozen dataclasses for internal value objects, and this one
    is threaded into RLS context and job payloads where a late mutation would
    change who a query runs as.
    """

    user_id: UUID
    team_ids: frozenset[UUID]
    channel: Channel  # 'web' | 'claude' | 'worker'


async def effective_permission(db, principal: Principal, obj) -> Permission:
    if obj.owner_user_id == principal.user_id:
        return Permission.OWNER

    grant_role = await lookup_grant(db, obj, principal)
    if grant_role == "editor":
        return Permission.EDITOR

    base = Permission.NONE
    if obj.visibility == "org":
        base = Permission.VIEWER
    elif obj.visibility == "team" and obj.owner_team_id in principal.team_ids:
        base = Permission.VIEWER

    if grant_role == "viewer":
        base = max(base, Permission.VIEWER)
    return base


async def require(db, principal: Principal, obj, level: Permission) -> None:
    actual = await effective_permission(db, principal, obj)
    if actual < level:
        raise PermissionDenied(
            f"You have {actual.name.lower()} access to "
            f"'{getattr(obj, 'name', obj.id)}' but {level.name.lower()} is required. "
            f"Ask {await owner_display_name(db, obj)} to grant access."
        )
```

The error message names the owner. Claude can then tell the geologist exactly who to ask,
which turns a dead end into a next step.

### 3.3 Grant rules

- Only an object's **owner** may create or revoke grants on it.
- A grant can widen access, never narrow it. There is no "deny" grant.
- Granting `editor` on a dataset does **not** grant permission to delete it. Delete is
  owner-only.
- Transferring ownership is an explicit operation, audited. `migration 0002` forbids it
  through an ordinary UPDATE and exposes `webmap.allow_ownership_transfer` as the
  transaction-local escape the operation sets.

**Transfer is how a departed colleague's private work is recovered** (`adr/0010` §5), and it
is deliberately not a read:

- A global administrator may transfer the objects of a **deactivated** user to a named owner.
  The new owner then has ordinary access; the administrator never reads the object.
- **Only deactivated users.** An active user's work is shareable by its owner alone.
  Transfer-to-self on an active user's objects would be read access under another name, which
  is precisely the guarantee this preserves.
- Someone on long leave is deactivated first — two audited steps, and the deactivation is
  itself visible.
- The audit record carries actor, source owner, target owner, object and a stated reason.

### 3.4 Setting RLS context

```python
# python/webmap_core/src/webmap_core/db/session.py

from contextlib import asynccontextmanager


@asynccontextmanager
async def principal_session(engine, principal: Principal):
    """Every DB session used to serve a request must go through this.

    Direct engine.connect() outside this helper is a lint error — see CLAUDE.md.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('webmap.user_id', :uid, true)"),
            {"uid": str(principal.user_id)},
        )
        await conn.execute(
            text("SELECT set_config('webmap.team_ids', :tids, true)"),
            {"tids": "{" + ",".join(str(t) for t in principal.team_ids) + "}"},
        )
        yield conn
```

`set_config(..., true)` makes it transaction-local, so it cannot leak across pooled
connections. Using `SET` without the local flag is a serious bug — verify in review.

### 3.5 Startup assertion

```python
async def assert_rls_enforced(engine) -> None:
    """Fail loudly at boot if the app role can bypass RLS."""
    async with engine.connect() as conn:
        row = await conn.execute(text(
            "SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ))
        if row.scalar():
            raise RuntimeError(
                "Application DB role has BYPASSRLS. All row-level security is "
                "inert. Refusing to start."
            )
```

---

## 4. MCP authentication

### 4.1 There is no OAuth flow

The MCP server runs **locally on each geologist's workstation, over stdio**, and is a thin
client of `webmap-api`. See `adr/0008-local-stdio-mcp.md`.

```
workstation                              internal server
┌─────────────────────────┐              ┌──────────────────────┐
│ Claude Code / Desktop   │              │  webmap-api          │
│         │ stdio         │   HTTPS +    │  - enforces authz    │
│         ▼               │   user's     │  - owns the database │
│ webmap-mcp (local)      │──identity───▶│  - RLS backstop      │
│  - no DB connection     │              └──────────────────────┘
│  - no authz logic       │
└─────────────────────────┘
```

Remote MCP servers authenticate with OAuth 2.1 and the specification expects Dynamic Client
Registration, which Entra ID, Okta and Ping do not expose by default. That requirement is what
made a bespoke authorization server necessary. Moving the server onto the workstation removes
the requirement rather than solving it.

### 4.2 The local server is trusted with nothing

It runs where the user can read and modify it. Therefore:

- **No database connection.** It speaks HTTPS to `webmap-api` and nothing else.
- **No service credential.** Nothing it holds grants more than the user already has.
- **No permission logic.** Every check happens at the API, against the live grant model.
- **No secret worth stealing.** Tokens are acquired per-session from the OS credential broker
  and are the user's own.

> A local MCP process that enforces a permission check is a bug, not a defence. The user owns
> that process. Treat every request arriving at `webmap-api` from it as if the user typed it
> by hand — because they can.

### 4.3 Acquiring the user's identity

With **Entra ID** (expected): MSAL's Windows broker (WAM) obtains a token silently for the
logged-in Windows account. No prompt, no stored password, no device-code dance.

With **on-prem Active Directory** (fallback): `httpx-gssapi` over SSPI obtains a Kerberos
ticket for the API's SPN using the logged-in user's credentials.

Either way the geologist sees nothing — they open Claude and the tools work — and
`webmap-api` receives a verifiable assertion of who is calling.

### 4.4 Token requirements

- **Audience-bound.** Tokens must carry `aud` naming the WebMap API. Reject tokens issued for
  anything else, so a token stolen from another service cannot be replayed here.
- **Short-lived.** 30 minutes, with silent renewal through the broker.
- **Carry identity, not privilege.** The token says who the user is. What they may do is
  resolved per request against the live grant model, so revoking access takes effect
  immediately rather than at token expiry.

### 4.5 Environment guardrail

The local server points at exactly one API base URL, configured per install. A development
install points at localhost; a production install points at the internal server. Never make
the target switchable at runtime — a tool call that deletes a dataset does not care which
environment it landed in.

---

## 5. Identity propagation

**The single most important control in this document.**

When Claude calls `webmap_list_datasets`, the query must execute as the requesting geologist.
If any part of the chain uses a service account, you have built a system where any user can
ask Claude for data they are not cleared to see, and the audit log will show a service
principal instead of a person.

```
Claude → stdio → webmap-mcp (local, on the user's workstation)
       → HTTPS + the user's OS-brokered token → webmap-api
       → Principal(user_id, team_ids, channel='claude')
       → principal_session(engine, principal)   # RLS context set
       → service function with explicit permission check
       → worker job carrying requested_by=user_id
       → render service with a render-scoped token carrying that identity
```

Note where the chain begins. The local MCP server asserts nothing — it forwards a token the
OS broker issued for the logged-in Windows account, and `webmap-api` verifies it. A local
process cannot be a link in a trust chain (§4.2); it is a client of one.

### 5.1 Workers must carry identity

Jobs run asynchronously, after the request is gone. The identity must travel with the job
payload.

```python
# apps/worker/src/webmap_worker/runtime.py

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class JobContext:
    """Every job payload embeds this. There is no such thing as an
    anonymous job in this system."""
    job_id: UUID
    requested_by: UUID
    team_ids: frozenset[UUID]

    def principal(self) -> Principal:
        return Principal(self.requested_by, self.team_ids, channel="worker")


async def run_with_identity(ctx: JobContext, fn, *args, **kwargs):
    async with principal_session(engine, ctx.principal()) as conn:
        return await fn(conn, *args, **kwargs)
```

> A job that resolves datasets without a `JobContext` is a security bug, not a style issue.
> Reject in review.

---

## 6. Tile and asset authorization

Tile endpoints are hit thousands of times during a single pan. The temptation to leave them
open for performance is exactly how data leaks.

**Approach: short-TTL signed URLs, minted by the API after a permission check.**

```python
# python/webmap_core/src/webmap_core/signing.py

import hmac, hashlib, time, base64
from uuid import UUID


def mint_tile_token(
    dataset_id: UUID, user_id: UUID, ttl_seconds: int = 900, secret: bytes = ...
) -> str:
    """Scope: one dataset, one user, 15 minutes.

    Not a general-purpose token. A token for dataset A cannot fetch dataset B,
    so a leaked tile URL exposes exactly one already-authorized layer.
    """
    expires = int(time.time()) + ttl_seconds
    payload = f"{dataset_id}:{user_id}:{expires}"
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(f"{payload}:{sig.hex()}".encode()).decode()


def verify_tile_token(token: str, dataset_id: UUID, secret: bytes) -> UUID:
    """Returns the user_id, or raises. Constant-time comparison."""
    raw = base64.urlsafe_b64decode(token).decode()
    ds, uid, expires, sig_hex = raw.rsplit(":", 3)
    if UUID(ds) != dataset_id:
        raise InvalidToken("Token is not valid for this dataset")
    if int(expires) < time.time():
        raise InvalidToken("Token expired")
    expected = hmac.new(secret, f"{ds}:{uid}:{expires}".encode(), hashlib.sha256)
    if not hmac.compare_digest(bytes.fromhex(sig_hex), expected.digest()):
        raise InvalidToken("Signature mismatch")
    return UUID(uid)
```

TiTiler sits behind an auth proxy in `webmap-api` that verifies the token before forwarding.
It is never exposed directly.

Vector tiles have no second process to proxy — `adr/0002-duckdb-data-plane.md` removed Martin,
and MVT is now generated in-process by the API from the dataset's GeoParquet object
(`06-rendering.md` §7). That shortens the path but does not relax the control: the MVT endpoint
verifies the same token through the same code, before the object is opened. It has to, because
this is the data plane, where the API is the *sole* enforcement point and RLS is not behind it
(`02-data-model.md` §4.1).

### 6.1 The render service

`mint_tile_token` scopes a token to **one dataset and one user**, which is right for the
browser — a leaked tile URL exposes exactly one already-authorized layer — but wrong for a
render, which is multi-layer by definition. One token cannot fetch three layers.

So the render path does not use signed per-dataset URLs. `webmap-render` runs inside the trust
boundary and is issued a short-lived **render-scoped token** carrying the requesting user's
identity:

```python
{
  "sub":       str(user_id),
  "teams":     [str(t) for t in team_ids],
  "render_id": str(render_id),
  "aud":       "webmap-tiles",
  "iss":       "webmap-api",
  "exp":       now + 300,        # renders finish in under 5 s (06 §11)
}
```

The tile proxy accepts either credential. For a signed per-dataset token it authorizes that
dataset directly; for a render token it builds a `Principal` and runs the ordinary
`require(..., Permission.VIEWER)` check per dataset. **One authorization code path, not two** —
which is what §3.1 asks for, and it means a render can never reach a layer its requester
cannot, even if style assembly has a bug.

The token is injected by the Playwright route handler (§7.2) and never enters the page's
JavaScript context.

---

## 7. SSRF prevention in the render service

**This is the most important section in the document.** A MapLibre style document is a
structure full of URLs, and we hand it to a browser running inside the network. Without
controls, a crafted style can point a source at an internal endpoint or a cloud metadata
service, and the response appears in the rendered image.

**Three layers, all required.**

### 7.1 Style validation before dispatch

```python
# apps/render/src/webmap_render/security.py

from urllib.parse import urlparse

ALLOWED_HOSTS = frozenset({
    "tiles.webmap.internal",
    "titiler.webmap.internal",
    "static.webmap.internal",
})

BLOCKED_NETWORKS = [
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / cloud metadata
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
]


def collect_style_urls(style: dict) -> list[str]:
    """Every place a MapLibre style can name a URL."""
    urls: list[str] = []
    if "sprite" in style:
        urls.append(style["sprite"])
    if "glyphs" in style:
        urls.append(style["glyphs"])
    for source in style.get("sources", {}).values():
        for key in ("url", "data"):
            if isinstance(source.get(key), str):
                urls.append(source[key])
        urls.extend(source.get("tiles", []))
    return urls


def validate_style(style: dict) -> None:
    for url in collect_style_urls(style):
        host = urlparse(url).hostname
        if host not in ALLOWED_HOSTS:
            raise StyleRejected(
                f"Style references disallowed host '{host}'. Only WebMap-served "
                f"tile, sprite, and glyph endpoints may be rendered."
            )
```

### 7.2 Request interception in Playwright

Defense in depth — catches anything the validator missed, including redirects.

```python
async def install_guards(
    page, allowed: frozenset[str], auth_token: str, failed: list[dict]
) -> None:
    async def handler(route):
        url = route.request.url
        host = urlparse(url).hostname
        if host not in allowed:
            failed.append({"url": url, "reason": "blocked_host"})
            await route.abort()
            return
        # Inject the API token so tile and glyph requests authenticate.
        headers = {**route.request.headers, "Authorization": f"Bearer {auth_token}"}
        await route.continue_(headers=headers)

    await page.route("**/*", handler)
```

The token is injected here, in the route handler's closure. It is **not** passed into the
page's JavaScript context — the shell has no need for it, and putting it there would
expose it to any script the style manages to load.

### 7.3 Network isolation

Render workers run in a network segment with egress permitted only to the tile, glyph,
sprite, and object-storage services. Not the database. Not the API. Not the internet.

**Structural rule:** never render a style document that arrived from a client verbatim.
All styles are assembled server-side from validated layer references (`06` §6).
Client-supplied *symbology* is accepted; client-supplied *source URLs* are not.

---

## 8. Destructive operation safety

- **Never write in place.** Editing a dataset sourced from a file share creates a new
  versioned output; the source is never modified. This is non-negotiable — a geologist
  losing a partner-delivered shapefile is unrecoverable.
- **Delete is soft** for 30 days, then hard. Deleted datasets remain resolvable by lineage
  records so provenance chains do not break.
- **MCP destructive tools require confirmation.** Tools that delete or overwrite are
  annotated `destructiveHint: true` and require an explicit `confirm: true` parameter.
  Claude must ask the user first.
- **Bulk operations are capped.** No MCP tool deletes more than one object per call.

Feature edits are copy-on-write against versioned GeoParquet objects
(`adr/0005-single-editor-persistence.md`), so "never write in place" now holds for the
working copy too, not only for the upstream source file.

---

## 9. Treating dataset content as untrusted

Dataset attribute values, layer names, and file contents come from shapefiles authored
elsewhere. They reach Claude through tool responses.

- Never embed raw attribute text in a tool response in a position where it could read as
  instruction. Return it inside a clearly-labelled data field.
- Truncate long attribute values in list responses (200 chars).
- Do not execute or interpolate dataset content into SQL, shell commands, file paths, or
  style JSON. Parameterize everything.
- Sanitize dataset names used in filenames — path traversal via a layer called
  `../../etc/passwd` is a real shapefile you may receive.

The `hostile/` fixtures in `11-file-io.md` §8 are the test surface for this section. Every
one of them asserts on the *error message*, not just the failure.

---


---

## 10. Audit requirements

Every one of these emits an `audit_event` (`02-data-model.md` §3.12):

- Authentication (success and failure)
- Dataset read via MCP (not via tiles — too high volume)
- Dataset create, update, delete
- Grant create and revoke
- Ownership transfer, with source owner, target owner and the stated reason
- User deactivation and reactivation
- Export and download
- Render creation
- Job submission

Records carry `actor_channel` distinguishing `web` from `claude`, so "what did Claude do on my
behalf" is answerable — which matters more with several users than it did with one, because
the answer now also identifies *whose* Claude.

Retention: 2 years minimum. Confirm against corporate policy before launch.

---

## 11. Security checklist before production

**Identity and authorization**

- [ ] App DB role lacks `BYPASSRLS`; startup assertion in place
- [ ] RLS policies exist on every ownable table
- [ ] `set_config(..., true)` used everywhere (transaction-local)
- [ ] No service-account path from MCP to data
- [ ] The local MCP server holds no database connection and no permission logic
- [ ] Token audience validation enforced
- [ ] Every endpoint that reads the data plane has an explicit permission check —
      RLS does not cover it (`02-data-model.md` §4.1)
- [ ] Permission logic tested exhaustively: every visibility × grant × role combination
- [ ] Capability checks tested separately from object permission: a global administrator has
      no implicit read access to a private object (`adr/0010` §2)
- [ ] `visibility = 'org'` refused to a non-administrator
- [ ] A membership write against a directory-synced team is refused and names the group
- [ ] Ownership transfer is refused for an **active** user's objects — the loophole that would
      turn transfer into read access (`adr/0010` §5)
- [ ] A transfer emits an audit record naming source owner, target owner and reason
- [ ] The role that performs a transfer holds `BYPASSRLS` and **owns nothing else** — no
      table, no other function. The operation needs to read what RLS hides from everyone
      including the administrator running it, so the privilege is real; keeping it to two
      functions is what keeps it auditable. See `adr/0010` §5's implementation note for why
      the ordinary routes cannot work

**Local credentials — `managed` mode only.** None of this applies to a `directory`
deployment, which has no local credential to protect.

- [ ] Passwords hashed with Argon2id; parameters recorded and reviewed
- [ ] `password_hash` never leaves the database — not in an API response, a log line, or an
      audit detail blob
- [ ] Per-account lockout after repeated failures, and a rate limit on the login endpoint
- [ ] Reset is administrator-initiated and audited; a deployment may have no mail server, so
      a self-service emailed link cannot be the only path
- [ ] Timing of a failed login does not distinguish "no such user" from "wrong password"
- [ ] A deactivated user cannot authenticate, and their session is invalidated

**Tiles and rendering**

- [ ] Tile endpoints unreachable without a valid scoped token
- [ ] A render token authorizes per dataset through the same `require()` path as the browser
- [ ] Style validation rejects non-allowlisted hosts
- [ ] Playwright `page.route` allowlist active and tested with a hostile style fixture
- [ ] The auth token is injected in the route handler, never passed into page JS
- [ ] Render workers network-isolated; verified by attempting egress in a test

**Data**

- [ ] Destructive MCP tools require `confirm: true`
- [ ] Every `hostile/` fixture in `11` §8 fails with an actionable message
- [ ] Dataset names sanitized before use in any filesystem path
- [ ] Delete is soft; lineage survives it

**Operations**

- [ ] Audit events emitted for all actions in §10
- [ ] Secrets from the corporate secret manager, never environment files in the image
- [ ] Separate API base URLs per environment, fixed at install time (§4.5)
- [ ] `gitleaks` passing in pre-commit
