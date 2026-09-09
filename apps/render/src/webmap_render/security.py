"""SSRF prevention. `03-auth-security.md` §7 — the most important section.

A MapLibre style document is a structure full of URLs, and we hand it to a
browser running inside the network. Without controls, a crafted style points
a source at an internal endpoint or a cloud metadata service and the response
appears in the rendered image.

Three layers, all required. This module is layer one — validation before
dispatch. Layer two is `page.route` interception (which also catches
redirects), and layer three is network isolation of the render container.

Implemented in Phase 0 rather than Phase 3 because the alternative is a
render path that works without it, and then a change that has to remember to
add it.
"""

import ipaddress
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

#: Only WebMap's own tile, sprite, and glyph endpoints. Populated from
#: settings at startup; the module-level default is the local stack.
DEFAULT_ALLOWED_HOSTS = frozenset(
    {
        "tiles.webmap.internal",
        "titiler.webmap.internal",
        "static.webmap.internal",
    }
)

#: Networks that must never be reachable even if a hostname resolving to them
#: somehow reaches the allowlist. 169.254.0.0/16 is the one that matters —
#: it is where every cloud metadata service lives.
BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("169.254.0.0/16"),  # link-local / cloud metadata
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fd00::/8"),
    ipaddress.ip_network("fe80::/10"),
)


class StyleRejected(Exception):
    """A style document names a host the renderer will not fetch."""


def collect_style_urls(style: dict[str, Any]) -> list[str]:
    """Every place a MapLibre style can name a URL.

    Missing one of these is how a validator passes a style that then fetches
    something. The list is: sprite, glyphs, and per-source `url`, `data`, and
    `tiles`.
    """
    urls: list[str] = []
    for key in ("sprite", "glyphs"):
        value = style.get(key)
        if isinstance(value, str):
            urls.append(value)

    sources = style.get("sources")
    if isinstance(sources, dict):
        for source in sources.values():
            if not isinstance(source, dict):
                continue
            for key in ("url", "data"):
                value = source.get(key)
                # `data` may be inline GeoJSON rather than a URL. An object is
                # not a fetch and is left alone; a string is.
                if isinstance(value, str):
                    urls.append(value)
            tiles = source.get("tiles")
            if isinstance(tiles, list):
                urls.extend(t for t in tiles if isinstance(t, str))
    return urls


def validate_style(
    style: dict[str, Any], allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_HOSTS
) -> None:
    """Reject a style that references anything outside the allowlist.

    Note what is *not* attempted here: DNS resolution of each host to check it
    against BLOCKED_NETWORKS. That check is racy by construction — the name
    can resolve differently between validation and fetch — so the allowlist is
    the control and `page.route` is what enforces it at fetch time. Literal
    IP addresses are checked, because those cannot be re-resolved.
    """
    for url in collect_style_urls(style):
        parsed = urlparse(url)

        # Inline data and relative references never leave the page.
        if parsed.scheme in ("data", "") and not parsed.netloc:
            continue

        if parsed.scheme not in ("http", "https"):
            raise StyleRejected(
                f"Style references URL scheme '{parsed.scheme}:' which the "
                f"renderer will not fetch. Only http and https to "
                f"WebMap-served endpoints are permitted."
            )

        host = parsed.hostname
        if host is None:
            raise StyleRejected(f"Style references a URL with no host: {url!r}")

        literal = _as_ip(host)
        if literal is not None and _is_blocked(literal):
            raise StyleRejected(
                f"Style references the internal address {host}. Tile, sprite, "
                f"and glyph sources must be named by their WebMap hostname, "
                f"not by address."
            )

        if host not in allowed_hosts:
            raise StyleRejected(
                f"Style references disallowed host '{host}'. Only WebMap-served "
                f"tile, sprite, and glyph endpoints may be rendered. Styles are "
                f"assembled server-side from validated layer references "
                f"(06-rendering.md §6) — a client-supplied source URL is never "
                f"passed through."
            )


def _as_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def _is_blocked(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(address in network for network in _networks_for(address))


def _networks_for(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> Iterator[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    for network in BLOCKED_NETWORKS:
        if network.version == address.version:
            yield network
