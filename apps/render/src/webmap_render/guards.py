"""Request interception. `03-auth-security.md` §7.2 — layer two of three.

Layer one (`security.py`) validates the style before it is dispatched. This is
defence in depth, and it catches what a validator structurally cannot:

- **Redirects.** A validator sees the URL in the document; the browser follows
  wherever that URL leads. An allowlisted host answering `302` to a metadata
  endpoint defeats validation entirely and is stopped only here.
- **URLs the style did not contain.** A stylesheet's `@import`, a script's
  `fetch`, a font file named inside a glyph range.

**The auth token is injected here, in this closure.** It is deliberately not
passed into the page's JavaScript: the shell has no use for it, and anything
the style manages to load could read it there.
"""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Page, Route

from webmap_core.logging import get_logger
from webmap_render.security import BLOCKED_NETWORKS

log = get_logger(__name__)

#: Schemes the shell itself needs. `file:` loads the shell and its bundled
#: MapLibre; `data:` and `blob:` are how MapLibre hands worker source and
#: decoded images to itself, and blocking them breaks tile decoding rather
#: than blocking anything a caller controls.
LOCAL_SCHEMES = frozenset({"file", "data", "blob", "about"})


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

        host = parsed.hostname
        if host is None or not _is_allowed(host, allowed):
            failed.append({"url": url, "reason": "blocked_host"})
            log.warning("render_request_blocked", host=host, url=url[:200])
            await route.abort()
            return

        # Only WebMap's own endpoints reach here, so the token goes only to
        # them. Carried on the request rather than in the page (§7.2).
        headers = {**route.request.headers, "Authorization": f"Bearer {auth_token}"}
        await route.continue_(headers=headers)

    await page.route("**/*", handler)


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


__all__ = ["LOCAL_SCHEMES", "install_guards"]
