"""Path distance across a faulted domain. `05-geoprocessing.md` §6.2.

Kriging weights points by how far away they are. **With a sealing fault
between them, "how far away" is not the straight-line distance** — it is the
distance you would have to travel going around the fault tip, and for a point
on the far side of a fault that runs off the edge of the data, it is infinite.

Getting this wrong does not produce an obviously broken map. It produces a
smooth surface that smears throw across a sealing fault while the map draws
the fault on top of it: geologically wrong, entirely plausible, and the sort
of thing that ends up in a partner deck.

This is the same principle as ArcGIS's kriging-with-barriers, and it is
expensive: Dijkstra per grid node against a cKDTree query. `05` §6.2 puts it
at 5-20x slower, which is why gridding is an async job.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from webmap_geo.mesh.constrained import ConstrainedMesh

#: How far a search may wander, as a multiple of the requested radius, before
#: it is abandoned. A point reachable only by a path four times its straight
#: line distance is on the other side of the field, not next door — and the
#: variogram was fitted on straight-line lags, so a weight derived from such a
#: path means nothing.
MAX_DETOUR = 4.0


@dataclass(frozen=True)
class Neighbourhood:
    """The control points reachable from one target, and how far away they are.

    `distances` are *path* distances, which is what the variogram must be
    evaluated at. A point that is 800 ft away in a straight line but 6,400 ft
    around a fault tip is correctly a distant neighbour, and one that is
    unreachable is absent rather than infinitely weighted.
    """

    indices: NDArray[np.intp]
    distances: NDArray[np.float64]

    def __len__(self) -> int:
        return len(self.indices)


def path_distances(
    mesh: ConstrainedMesh,
    source_vertex: int,
    targets: set[int],
    *,
    max_distance: float | None = None,
    adjacency: dict[int, list[int]] | None = None,
) -> dict[int, float]:
    """Dijkstra from one vertex, over mesh edges that no fault blocks.

    Returns only the targets actually reached. **Absence is the answer** for a
    point behind a sealing fault: reporting it at infinite distance would
    invite a caller to include it with a tiny weight, and the correct weight
    is not small but absent.

    Terminates as soon as every target is settled, which is what keeps this
    affordable — the frontier rarely has to cover the whole mesh.
    """
    graph = adjacency if adjacency is not None else mesh.adjacency()
    vertices = mesh.vertices

    settled: dict[int, float] = {}
    remaining = set(targets)
    queue: list[tuple[float, int]] = [(0.0, source_vertex)]
    best: dict[int, float] = {source_vertex: 0.0}

    while queue and remaining:
        distance, vertex = heapq.heappop(queue)
        if vertex in settled:
            continue
        settled[vertex] = distance
        remaining.discard(vertex)

        if max_distance is not None and distance > max_distance:
            # Everything still queued is at least this far, so nothing
            # reachable within the radius remains.
            break

        for neighbour in graph.get(vertex, ()):
            if neighbour in settled:
                continue
            step = float(np.hypot(*(vertices[neighbour] - vertices[vertex])))
            candidate = distance + step
            if max_distance is not None and candidate > max_distance:
                continue
            if candidate < best.get(neighbour, np.inf):
                best[neighbour] = candidate
                heapq.heappush(queue, (candidate, neighbour))

    return {vertex: settled[vertex] for vertex in targets if vertex in settled}


def barrier_aware_neighbours(
    mesh: ConstrainedMesh,
    control_vertices: NDArray[np.intp],
    source_vertex: int,
    k: int,
    *,
    max_radius: float | None = None,
    adjacency: dict[int, list[int]] | None = None,
) -> Neighbourhood:
    """The `k` nearest control points by path distance.

    `max_radius` bounds the search. It is widened by `MAX_DETOUR` internally,
    because the radius a caller states is a straight-line one — a well 800 ft
    away around a fault tip is still a legitimate neighbour, and clipping the
    search at 800 ft of *path* would drop it. Beyond the detour factor the
    path has stopped meaning anything the variogram can price.
    """
    targets = {int(v) for v in control_vertices}
    limit = max_radius * MAX_DETOUR if max_radius is not None else None

    reached = path_distances(
        mesh, source_vertex, targets, max_distance=limit, adjacency=adjacency
    )
    if not reached:
        return Neighbourhood(
            indices=np.empty(0, dtype=np.intp), distances=np.empty(0, dtype=np.float64)
        )

    ordered = sorted(reached.items(), key=lambda item: item[1])[:k]
    return Neighbourhood(
        indices=np.asarray([vertex for vertex, _ in ordered], dtype=np.intp),
        distances=np.asarray([distance for _, distance in ordered], dtype=np.float64),
    )


def compartment_of(
    mesh: ConstrainedMesh, adjacency: dict[int, list[int]] | None = None
) -> NDArray[np.int32]:
    """Label each vertex with its fault compartment.

    Connected components of the unblocked adjacency. Two vertices share a
    label exactly when a path between them exists that crosses no sealing
    fault — which is the definition of a compartment, and the thing worth
    reporting before anyone contours a region with no wells in it.

    Used to order grid nodes for the frontier caching `05` §5.2 describes:
    nodes in one compartment share most of their neighbourhood, and nodes in
    different compartments share none of it.
    """
    graph = adjacency if adjacency is not None else mesh.adjacency()
    labels = np.full(mesh.n_vertices, -1, dtype=np.int32)
    label = 0

    for start in range(mesh.n_vertices):
        if labels[start] != -1:
            continue
        stack = [start]
        labels[start] = label
        while stack:
            vertex = stack.pop()
            for neighbour in graph.get(vertex, ()):
                if labels[neighbour] == -1:
                    labels[neighbour] = label
                    stack.append(neighbour)
        label += 1

    return labels


def nearest_vertex(mesh: ConstrainedMesh, point: NDArray[np.floating]) -> int:
    """The mesh vertex closest to a point, for entering the graph.

    A grid node is not a mesh vertex, so a search from it has to start
    somewhere. The nearest vertex is the honest entry: the alternative — the
    containing triangle's three corners — is more accurate and costs a point
    location per node, which at a million nodes is the whole budget.
    """
    from scipy.spatial import cKDTree

    _, index = cKDTree(mesh.vertices).query(np.asarray(point, dtype=float))
    return int(index)


__all__ = [
    "MAX_DETOUR",
    "Neighbourhood",
    "barrier_aware_neighbours",
    "compartment_of",
    "nearest_vertex",
    "path_distances",
]
