# 03 — Authentication and Security

> **This is the highest-schedule-risk document in the set.** The MCP OAuth flow against a
> corporate IdP is the piece most likely to take three times as long as estimated, and it gates
> every Claude interaction. Start it in Phase 1, not Phase 3.

---

## 1. Threat model

| Threat | Vector | Control |
|---|---|---|
| User reads data they aren't cleared for | Claude calls MCP with a service account | End-to-end identity propagation (§4) |
| Same, via tiles | Unauthenticated MVT/COG endpoints | Signed, scoped tile URLs (§6) |
| SSRF into internal network | Malicious style JSON given to headless browser | Style validation + request allowlist (§7) |
| Data exfiltration via render | Style points a source at an internal admin API; response appears in image | Same as above, plus network isolation |
| Token theft | Long-lived bearer tokens in browser storage | Short-lived access tokens, refresh in httpOnly cookie |
| Privilege escalation via grants | User grants themselves editor | Grants writable only by object owner (§3.3) |
| Destructive overwrite | Claude overwrites a source shapefile | Never write in place; versioned outputs (§8) |
| Prompt injection via data | Malicious content in a dataset attribute reaches Claude | Treat all dataset content as untrusted data (§9) |

---

## 2. Human authentication (browser)

Standard OIDC Authorization Code + PKCE against the corporate IdP.

```
Browser ──▶ /auth/login ──▶ IdP ──▶ /auth/callback ──▶ session cookie
```

- Access token: JWT, 15 min lifetime, held in memory by the SPA.
- Refresh token: httpOnly, Secure, SameSite=Lax cookie. Never readable by JS.
- Group claims map to `team.idp_group_id`. Team membership is synced on every login —
  the directory is the source of truth, `team_member` is a cache.

```python
# apps/api/auth/oidc.py

from authlib.integrations.starlette_client import OAuth
from strata_core.settings import settings

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

### 3.1 Two layers, both required

1. **Application layer** — an explicit permission check in the service function. This is where
   good error messages come from.
2. **Row-level security** — a database backstop. Makes the failure mode "no rows" instead of
   "wrong user's rows" when someone forgets layer 1.

Never rely on only one. RLS alone gives unhelpful 404s; application checks alone fail open on
the one query someone forgets.

### 3.2 Permission resolution

```python
# python/strata_core/permissions.py

from enum import IntEnum
from uuid import UUID


class Permission(IntEnum):
    """Ordered so comparisons work: OWNER > EDITOR > VIEWER > NONE."""
    NONE = 0
    VIEWER = 1
    EDITOR = 2
    OWNER = 3


class Principal:
    """The authenticated actor. Constructed once per request, never mutated."""

    def __init__(self, user_id: UUID, team_ids: frozenset[UUID], channel: str):
        self.user_id = user_id
        self.team_ids = team_ids
        self.channel = channel  # 'web' | 'claude'


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
- Transferring ownership is an explicit operation, audited.

### 3.4 Setting RLS context

```python
# apps/api/db/session.py

from contextlib import asynccontextmanager


@asynccontextmanager
async def principal_session(engine, principal: Principal):
    """Every DB session used to serve a request must go through this.

    Direct engine.connect() outside this helper is a lint error — see CLAUDE.md.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('strata.user_id', :uid, true)"),
            {"uid": str(principal.user_id)},
        )
        await conn.execute(
            text("SELECT set_config('strata.team_ids', :tids, true)"),
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

### 4.1 The problem

Remote MCP servers authenticate via OAuth 2.1, and the specification expects **Dynamic Client
Registration** (RFC 7591) so a client can register itself without an administrator
pre-provisioning it. Most corporate IdPs — Entra ID, Okta, Ping — do not expose DCR by default,
and enabling it is frequently blocked by security policy.

### 4.2 The resolution

Stand up `strata-auth`, our own authorization server, which:

- **Supports DCR.** Claude registers as a client against us.
- **Federates upstream** to the corporate IdP via standard OIDC for actual user
  authentication. We never handle credentials.
- **Issues our own access tokens** scoped to Strata resources, carrying the user's identity.

```
Claude                strata-auth              Corporate IdP
  │                        │                        │
  ├─GET /.well-known/──────▶                        │
  │  oauth-authorization-server                     │
  │◀──metadata (registration_endpoint present)──────┤
  ├─POST /register (DCR)───▶                        │
  │◀──client_id────────────┤                        │
  ├─GET /authorize────────▶│                        │
  │                        ├─redirect (OIDC)───────▶│
  │                        │◀──user authenticates───┤
  │◀──authorization code───┤                        │
  ├─POST /token───────────▶│                        │
  │◀──access + refresh─────┤                        │
  ├─MCP calls w/ Bearer───▶ strata-mcp              │
```

Use a maintained OAuth server implementation. Do not write token issuance from scratch.
Candidates: Authlib's authorization server components, Ory Hydra, or Keycloak configured as a
broker. Keycloak-as-broker is the lowest-code path if you already run it.

### 4.3 Required endpoints

| Endpoint | Purpose |
|---|---|
| `/.well-known/oauth-authorization-server` | RFC 8414 metadata |
| `/.well-known/oauth-protected-resource` | RFC 9728, on the MCP resource |
| `/register` | RFC 7591 dynamic client registration |
| `/authorize` | Authorization code + PKCE |
| `/token` | Token issuance and refresh |
| `/jwks.json` | Public keys for verification |

### 4.4 Token requirements

- **Audience-bound.** Access tokens must carry `aud` naming the Strata MCP resource. Reject
  tokens issued for anything else — this prevents a token stolen from another service being
  replayed here.
- **Short-lived.** 30 minutes for access tokens; refresh handles continuity.
- **Carry identity, not privilege.** The token says who the user is. It does not enumerate
  what they can do — that is resolved per request against the live grant model, so revoking
  access takes effect immediately rather than at token expiry.

### 4.5 Registration guardrail

Register separate OAuth clients per environment. A Claude connector configured against `dev`
must be structurally incapable of reaching `prod`. Enforce with distinct issuers and
audiences, not just distinct URLs.

---

## 5. Identity propagation

**The single most important control in this document.**

When Claude calls `strata_list_datasets`, the query must execute as the requesting geologist.
If any part of the chain uses a service account, you have built a system where any user can
ask Claude for data they are not cleared to see, and the audit log will show a service
principal instead of a person.

```
Claude → [Bearer: user token] → strata-mcp
       → Principal(user_id, team_ids, channel='claude')
       → principal_session(engine, principal)   # RLS context set
       → service function with explicit permission check
       → worker job carrying requested_by=user_id
       → render service with per-request scoped tile token
```

### 5.1 Workers must carry identity

Jobs run asynchronously, after the request is gone. The identity must travel with the job
payload.

```python
# apps/worker/tasks/base.py

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
# python/strata_core/signing.py

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

Martin and TiTiler sit behind an auth proxy in `strata-api` that verifies the token before
forwarding. Neither is exposed directly.

**Exception for the render service.** Because `strata-render` runs inside the trust boundary,
it can be issued a short-lived internal token and skip signed URLs — but it must still carry
the *user's* identity so that a render cannot access layers the requester cannot.

---

## 7. SSRF prevention in the render service

A MapLibre style document is a structure full of URLs, and we hand it to a browser inside the
corporate network. Without controls, a crafted style can point a source at an internal admin
endpoint or a cloud metadata service and the response appears in the rendered image.

**Three layers, all required.**

### 7.1 Style validation before dispatch

```python
# apps/render/security.py

from urllib.parse import urlparse

ALLOWED_HOSTS = frozenset({
    "tiles.strata.internal",
    "titiler.strata.internal",
    "static.strata.internal",
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
                f"Style references disallowed host '{host}'. Only Strata-served "
                f"tile, sprite, and glyph endpoints may be rendered."
            )
```

### 7.2 Request interception in Playwright

Defense in depth — catches anything the validator missed, including redirects.

```python
async def install_guards(page, allowed: frozenset[str], failed: list[dict]) -> None:
    async def handler(route):
        url = route.request.url
        host = urlparse(url).hostname
        if host not in allowed:
            failed.append({"url": url, "reason": "blocked_host"})
            await route.abort()
            return
        # Inject the requesting user's tile token so downstream auth holds.
        headers = {**route.request.headers, "Authorization": f"Bearer {tile_token}"}
        await route.continue_(headers=headers)

    await page.route("**/*", handler)
```

Note that this same hook solves the authorization problem — no signed URLs needed in the
render path, because we inject the header directly.

### 7.3 Network isolation

Render workers run in a network segment with egress permitted only to the tile, glyph, sprite,
and object-storage services. Not the database. Not the API. Not the internet.

**Structural rule:** never render a style document that arrived from a client verbatim. All
styles are assembled server-side from validated layer references. Client-supplied *symbology*
is accepted; client-supplied *source URLs* are not.

---

## 8. Destructive operation safety

- **Never write in place.** Editing a dataset sourced from a file share creates a new
  versioned output; the source is never modified. This is non-negotiable — a geologist losing
  a partner-delivered shapefile is unrecoverable.
- **Delete is soft** for 30 days, then hard. Deleted datasets remain resolvable by lineage
  records so provenance chains do not break.
- **MCP destructive tools require confirmation.** Tools that delete or overwrite are annotated
  `destructiveHint: true` and require an explicit `confirm: true` parameter. Claude must ask
  the user first.
- **Bulk operations are capped.** No MCP tool deletes more than one object per call.

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

---

## 10. Audit requirements

Every one of these emits an `audit_event`:

- Authentication (success and failure)
- Dataset read via MCP (not via tiles — too high volume)
- Dataset create, update, delete
- Grant create and revoke
- Ownership transfer
- Export and download
- Render creation
- Job submission

Records carry `actor_channel` distinguishing `web` from `claude`, so "what did Claude do on my
behalf" is answerable.

Retention: 2 years minimum. Confirm against corporate policy before launch.

---

## 11. Security checklist before production

- [ ] App DB role lacks `BYPASSRLS`; startup assertion in place
- [ ] RLS policies exist on every ownable table
- [ ] `set_config(..., true)` used everywhere (transaction-local)
- [ ] No service-account path from MCP to data
- [ ] Tile endpoints unreachable without a valid scoped token
- [ ] Style validation rejects non-allowlisted hosts
- [ ] Playwright `page.route` allowlist active and tested with a hostile style fixture
- [ ] Render workers network-isolated; verified by attempting egress in a test
- [ ] Separate OAuth clients and audiences per environment
- [ ] Token audience validation enforced
- [ ] Destructive MCP tools require `confirm: true`
- [ ] Audit events emitted for all actions in §10
- [ ] Secrets from the corporate secret manager, never environment files in the image
