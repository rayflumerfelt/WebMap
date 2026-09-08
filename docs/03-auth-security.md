# 03 — Data Security

> **Scope changed.** This document previously covered OIDC login, an OAuth 2.1
> authorization server with Dynamic Client Registration, a grant model, row-level
> security, and end-to-end identity propagation. All of that assumed the multi-user
> deployment described in `00-overview.md` §4. See `adr/0001-single-user-deployment.md`.
>
> What remains is the part that was never about users: defending against hostile
> **data**. A partner-supplied shapefile with a crafted layer name, and a style document
> handed to a headless browser, are exactly as dangerous with one operator as with two
> hundred.

---

## 1. Threat model

The actor is a file, not a person. Datasets arrive from partners, vendors, regulators,
and file shares; nobody authored them with this system in mind, and some were authored
by someone who was.

| Threat | Vector | Control |
|---|---|---|
| SSRF into the local network | Malicious style JSON given to the headless browser | Style validation + request allowlist (§3) |
| Data exfiltration via render | A style points a source at an internal endpoint; the response appears in the image | Same as above, plus network isolation (§3.3) |
| Prompt injection via data | Attribute values reach Claude through tool responses | Treat all dataset content as untrusted (§5) |
| Path traversal | A layer named `../../etc/passwd`, or a crafted share URI | Filename sanitization (§5), share-root confinement (`11` §2.2) |
| Destructive overwrite | An edit writes back to a partner-delivered source file | Never write in place; versioned outputs (§4) |
| Accidental data loss | A delete that turns out to have been wrong | Soft delete, 30 days (§4) |

**Not in the model:** one user reading another's data, privilege escalation, token theft
between principals, tenant isolation. There is one principal.

---

## 2. Access

Single operator, internal network or localhost. There is no identity provider, no login
flow, and no permission model.

- **The SPA** runs against a local session. No OIDC, no refresh-token rotation.
- **The MCP server** authenticates with a static bearer token read from the local secret
  store. It is an access gate, not an identity — it says "this caller may use this
  server," not "this caller is Alice."
- **Tile and asset endpoints** sit behind the same token, checked by the API. They are
  never exposed directly.
- **The render service** receives that token to inject on its own requests (§3.2).

`webmap_core` service functions still take an explicit actor argument. It resolves to the
single local user today, and it exists so lineage and audit records name someone — and so
that reintroducing real identity later is a change of implementation rather than of every
signature. See `adr/0001-single-user-deployment.md` for what that would cost.

**Secrets** come from the local secret store or the environment, never from a file baked
into an image. `gitleaks` runs in pre-commit (`CLAUDE.md` §11).

---

## 3. SSRF prevention in the render service

**This is the most important section in the document.** A MapLibre style document is a
structure full of URLs, and we hand it to a browser running inside the network. Without
controls, a crafted style can point a source at an internal endpoint or a cloud metadata
service, and the response appears in the rendered image.

**Three layers, all required.**

### 3.1 Style validation before dispatch

```python
# apps/render/security.py

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

### 3.2 Request interception in Playwright

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

### 3.3 Network isolation

Render workers run in a network segment with egress permitted only to the tile, glyph,
sprite, and object-storage services. Not the database. Not the API. Not the internet.

**Structural rule:** never render a style document that arrived from a client verbatim.
All styles are assembled server-side from validated layer references (`06` §6).
Client-supplied *symbology* is accepted; client-supplied *source URLs* are not.

---

## 4. Destructive operation safety

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

## 5. Treating dataset content as untrusted

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

## 6. Provenance and audit

Audit exists here for provenance — answering "how did this dataset come to exist, and what
produced it" — rather than for compliance. Records are written for:

- Dataset create, update, delete
- Export and download
- Job submission and completion
- Render creation

Records carry `actor_channel`, distinguishing `web` from `claude`, so "what did Claude do
on my behalf" stays answerable. That question is still worth answering with one user; it is
the difference between a map you made and a map an agent made for you.

Retention follows the soft-delete window: keep indefinitely for datasets that still exist,
and until the lineage chain is purged otherwise. Lineage records (`02-data-model.md` §3.10)
are the durable provenance artifact; the audit log is the activity trail.

---

## 7. Checklist before running against real data

- [ ] Style validation rejects non-allowlisted hosts
- [ ] Playwright `page.route` allowlist active and tested with a hostile style fixture
- [ ] Render workers network-isolated; verified by attempting egress in a test
- [ ] The auth token is injected in the route handler, never passed into page JS
- [ ] Destructive MCP tools require `confirm: true`
- [ ] Every `hostile/` fixture in `11` §8 fails with an actionable message
- [ ] Dataset names sanitized before use in any filesystem path
- [ ] Delete is soft; lineage survives it
- [ ] Secrets from the local secret store, never an env file in the image
- [ ] `gitleaks` passing in pre-commit
