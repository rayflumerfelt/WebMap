"""Every route is registered, and every data route depends on a principal.

The endpoint suites in this directory drive a running container, which is the
right way to test what a *request* gets. It is the wrong way to catch a router
that was never included or a handler that forgot `ScopedConn` — those show up
as a 404 or as a route with no data path at all, in a suite that skips entirely
when the stack is down.

So this file builds the app in-process and inspects it. No database, no
network, no container: it runs in every suite, including the one a developer
runs before starting Docker.

`03-auth-security.md` §5 calls the dependency chain the single most important
control in the document, and its whole design is that *a route which wants data
asks for `ScopedConn`, and there is no way to obtain one without a verified
principal*. That is a claim about the dependency graph, so it can be checked by
reading the graph — which is what the second test does.
"""

from __future__ import annotations

from typing import Any

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from webmap_api.dependencies import get_principal
from webmap_api.main import create_app
from webmap_api.routes.tiles import tile_principal

#: Every dependency that yields a verified `Principal`.
#:
#: Two, not one. `tile_principal` exists because MapLibre cannot attach a
#: bearer header to a tile request, so `03-auth-security.md` §4.4 mints a
#: scoped token per dataset instead — and it still runs the permission check on
#: every request, so revocation takes effect immediately rather than at token
#: expiry. Both produce the same `Principal`; they differ only in where the
#: credential arrives.
#:
#: A third entry here is a security decision. Keeping the set explicit is what
#: makes adding one a visible edit rather than a passing test.
PRINCIPAL_SOURCES = {get_principal, tile_principal}

#: Routes that legitimately reach no user data, and why.
#:
#: Adding to this set is also a security decision, which is why it is a
#: literal rather than a heuristic on the path.
PUBLIC: dict[str, str] = {
    "/health": "liveness; touches nothing",
    "/health/ready": "readiness; one SELECT 1, no user data",
    "/auth/dev/token": "issues a token — there is no principal yet, by definition",
    "/auth/dev/login": "the same, through the browser",
    "/auth/login": "starts the OIDC flow",
    "/auth/callback": "completes the OIDC flow",
    "/auth/logout": "clears a cookie",
    # SDF glyph ranges are font files, identical for every user and derived
    # from open fonts. Requiring a principal would mean a signed URL per
    # fontstack for data that carries nothing about anybody.
    "/static/glyphs": "lists the built font stacks; no user data",
    "/static/glyphs/{fontstack}/{codepoints}.pbf": "font glyphs; no user data",
}


def _api_routes(node: Any) -> list[APIRoute]:
    """Every `APIRoute` in the app, however deeply included.

    FastAPI wraps `include_router` results in an internal node rather than
    flattening them onto `app.routes`, so a one-level scan finds only the
    handlers declared inline — which in this app is the two health checks. The
    first version of this file did exactly that and passed while asserting
    nothing.
    """
    found: list[APIRoute] = []
    for route in getattr(node, "routes", []):
        if isinstance(route, APIRoute):
            found.append(route)
        elif hasattr(route, "original_router"):
            # `include_router` stores an `_IncludedRouter` that carries no
            # `.routes` of its own; the handlers hang off the router that was
            # included. Every router in this app declares its full prefix, so
            # the paths there are the paths served.
            found.extend(_api_routes(route.original_router))
        else:
            found.extend(_api_routes(route))
    return found


def _dependency_calls(dependant: Dependant) -> set[Any]:
    """Every callable in a route's dependency tree, transitively.

    Transitive because `get_scoped_conn` is what a data route asks for, and the
    principal is one level below it. A check that only looked at the route's
    own dependencies would report every data route as unauthenticated.
    """
    calls: set[Any] = set()
    pending = list(dependant.dependencies)
    while pending:
        node = pending.pop()
        if node.call is not None:
            calls.add(node.call)
        pending.extend(node.dependencies)
    return calls


def test_the_adr_0010_routes_are_registered() -> None:
    """A router that is written and never included is a 404 nobody finds until
    the frontend is wired to it."""
    routes = _api_routes(create_app())
    paths = {(route.path, method) for route in routes for method in route.methods}

    expected = {
        ("/api/v1/layers", "GET"),
        ("/api/v1/layers", "POST"),
        ("/api/v1/layers/{layer_id}", "GET"),
        ("/api/v1/layers/{layer_id}", "PATCH"),
        ("/api/v1/layers/{layer_id}", "DELETE"),
        ("/api/v1/layers/{layer_id}/duplicate", "POST"),
        ("/api/v1/layers/{layer_id}/basemaps", "GET"),
        ("/api/v1/basemaps", "GET"),
        ("/api/v1/basemaps", "POST"),
        ("/api/v1/basemaps/from-map", "POST"),
        ("/api/v1/basemaps/{basemap_id}", "DELETE"),
        ("/api/v1/basemaps/{basemap_id}/layers", "GET"),
        ("/api/v1/basemaps/{basemap_id}/layers", "PUT"),
        ("/api/v1/preferences", "GET"),
        ("/api/v1/preferences/default-basemap", "GET"),
        ("/api/v1/preferences/default-basemap", "PUT"),
        ("/api/v1/preferences/teams/{team_id}/default-basemap", "PUT"),
        ("/api/v1/preferences/global/default-basemap", "PUT"),
        ("/api/v1/palettes", "GET"),
        ("/api/v1/palettes/import", "POST"),
        ("/api/v1/palettes/{palette_id}", "GET"),
        ("/api/v1/features/{dataset_id}/summary", "GET"),
        ("/api/v1/jobs/clip", "POST"),
        ("/api/v1/jobs/label-anchors", "POST"),
    }
    assert expected <= paths, sorted(expected - paths)


def test_the_route_set_is_not_trivially_small() -> None:
    """The control for the test above and the one below.

    Both are set comparisons over `_api_routes`, and both would pass on an
    empty list — which is precisely the bug the first draft of this file had.
    """
    assert len(_api_routes(create_app())) > 40


def test_every_data_route_depends_on_a_principal() -> None:
    """`03-auth-security.md` §5, read off the dependency graph.

    Not a proxy for the runtime check — it *is* the control, stated as a
    property of the wiring. A handler that reaches data without a principal
    either cannot get a session at all or got one some other way, and the
    second is the security bug `CLAUDE.md` §3.2 forbids.

    The tile routes satisfy this through `tile_principal` rather than
    `get_principal`, and that is not a loophole: it re-runs the permission
    check on every request even when a scoped token was presented, so a
    revoked grant stops serving tiles at once instead of at token expiry.
    """
    offenders = []
    for route in _api_routes(create_app()):
        if route.path in PUBLIC:
            continue
        if not PRINCIPAL_SOURCES & _dependency_calls(route.dependant):
            offenders.append(f"{sorted(route.methods)} {route.path}")

    assert not offenders, (
        "These routes do not depend on a verified principal. Either they are "
        "missing `CurrentPrincipal`/`ScopedConn`, or they are genuinely public "
        f"and belong in PUBLIC in this file: {offenders}"
    )
