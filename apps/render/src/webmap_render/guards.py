"""Request interception. `03-auth-security.md` §7.2 — layer two of three.

Layer one (`security.py`) validates the style before it is dispatched. This is
defence in depth, and it catches what a validator structurally cannot:

- **Redirects.** A validator sees the URL in the document; the browser follows
  wherever that URL leads. An allowlisted host answering `302` to a metadata
  endpoint defeats validation entirely and is stopped only here.
- **URLs the style did not contain.** A stylesheet's `@import`, a script's
  `fetch`, a font file named inside a glyph range.

Two implementation decisions here are load-bearing and neither is obvious.

**Resources are fetched by this handler, not continued.** The shell loads from
`file://`, so its origin is `null` and every request to the API is
cross-origin; attaching an Authorization header to a continued request makes it
preflighted, the API answers no OPTIONS, and the browser blocks it. That
presented as every tile failing with "Failed to fetch (0)" and a blank map, and
no `data:`-based test could have caught it. Fetching here also means the page
never issues a credentialed request at all — the token is not merely absent
from page JavaScript (§7.2) but absent from anything the page could observe.

**Redirects are followed by this handler too, one hop at a time.** Letting
`route.fetch` follow them internally would resolve the target without this
handler ever seeing its host, which is precisely the attack §7.2 names.
Fulfilling the 3xx and letting the browser follow does not work either: a
`null`-origin fetch will not follow a fulfilled redirect, so the request simply
fails and the target host never appears in `failed_requests` — safe, but it
records nothing an operator could act on. Following here checks every hop and
names the one that was refused.
"""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.async_api import APIResponse, Page, Route

from webmap_core.logging import get_logger
from webmap_render.security import BLOCKED_NETWORKS

log = get_logger(__name__)

#: Schemes the shell itself needs. `file:` loads the shell and its bundled
#: MapLibre; `data:` and `blob:` are how MapLibre hands worker source and
#: decoded images to itself, and blocking them breaks tile decoding rather
#: than blocking anything a caller controls.
LOCAL_SCHEMES = frozenset({"file", "data", "blob", "about"})

#: More than this and the chain is a loop, or an attempt to exhaust the check.
#: Browsers allow twenty; a tile service needing more than five is broken.
MAX_REDIRECTS = 5


async def install_guards(
    page: Page,
    allowed: frozenset[str],
    auth_token: str,
    failed: list[dict[str, Any]],
) -> None:
    """Intercept every request the page makes.

    `failed` accumulates what was refused, so a hole in the map can be
    explained rather than guessed at (`06-rendering.md` §5.1). A blocked
    request is a *recorded* failure, not a silent one — the whole point of the
    list is that a geologist can tell "the data really is sparse there" from
    "the tile server was down", and "someone tried to make this map fetch the
    metadata service" is a third thing worth knowing.
    """

    async def handler(route: Route) -> None:
        url = route.request.url
        parsed = urlparse(url)

        if parsed.scheme in LOCAL_SCHEMES:
            await route.continue_()
            return

        try:
            response = await _fetch_following_redirects(route, url, allowed, auth_token, failed)
        except Exception as exc:
            # Recorded and aborted, never swallowed. A handler that raises
            # leaves the route unresolved, and an unresolved route hangs the
            # page until the render times out — thirty seconds spent to produce
            # a blank image and no explanation of what went wrong.
            failed.append({"url": url, "reason": "fetch_failed", "message": str(exc)[:200]})
            log.warning("render_fetch_failed", url=url[:200], error=type(exc).__name__)
            await route.abort()
            return

        if response is None:
            await route.abort()
            return

        await route.fulfill(
            response=response,
            headers={
                **response.headers,
                # The response was fetched by us, from a host on the allowlist,
                # for a page with no origin. Declaring it readable is what lets
                # the `null`-origin shell consume it; it grants nothing to
                # anyone else, because nothing else runs in this context.
                "access-control-allow-origin": "*",
            },
        )

    await page.route("**/*", handler)


async def _fetch_following_redirects(
    route: Route,
    url: str,
    allowed: frozenset[str],
    auth_token: str,
    failed: list[dict[str, Any]],
) -> APIResponse | None:
    """Fetch `url`, checking the allowlist at every hop.

    Returns None when a hop was refused — the caller aborts. The refusal is
    recorded against the URL that was actually refused rather than the one the
    style named, so an operator reading `failed_requests` learns where the
    chain went, not merely that it failed.
    """
    current = url

    for hop in range(MAX_REDIRECTS + 1):
        host = urlparse(current).hostname
        if host is None or not _is_allowed(host, allowed):
            failed.append({"url": current, "reason": "blocked_host"})
            log.warning("render_request_blocked", host=host, url=current[:200], hop=hop)
            return None

        response = await route.fetch(
            url=current,
            headers={**route.request.headers, "Authorization": f"Bearer {auth_token}"},
            # Never followed internally: that would resolve the target without
            # this loop ever checking its host.
            max_redirects=0,
        )

        if not 300 <= response.status < 400:
            return response

        location = response.headers.get("location")
        if not location:
            # A redirect with nowhere to go. Returned as-is rather than chased;
            # the page can make of it what it will.
            return response

        # Relative locations are ordinary, and must be resolved against the
        # current URL — an unresolved relative hop parses as having no host and
        # would be refused as though it were hostile.
        current = urljoin(current, location)

    failed.append({"url": url, "reason": "too_many_redirects"})
    log.warning("render_redirect_chain_too_long", url=url[:200])
    return None


def _is_allowed(host: str, allowed: frozenset[str]) -> bool:
    """A host is allowed only if it is named *and* not a blocked address.

    Both checks, in that order. The allowlist alone is not enough: an
    allowlisted name can resolve to anything, and a literal IP that happens to
    be in the allowlist would otherwise skip the network check entirely.
    """
    if host not in allowed:
        return False

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # A name rather than a literal. Resolution happens in the browser, and
        # the network isolation of §7.3 is what stops a name resolving
        # somewhere it should not — this layer cannot see that far.
        return True

    return not any(address in network for network in BLOCKED_NETWORKS)


__all__ = ["LOCAL_SCHEMES", "MAX_REDIRECTS", "install_guards"]
