"""Fault compartments over a constrained mesh. `05-geoprocessing.md` §5.

A **compartment** is a region bounded by faults (`CLAUDE.md` §13): two points
share one exactly when a path between them exists that crosses no sealing
fault. On a mesh whose fault vertices have been split, that is just a
connected component — the barrier is topology, so this is a graph traversal
rather than a geometric test.

Worth reporting before anyone contours a map. A compartment holding no control
points at all is a region whose surface came entirely from the smoothness
term, and it draws with the same colours and the same contour interval as the
well-controlled part.

**This module used to carry Dijkstra path distance** for barrier-aware kriging
neighbourhood search. That was measured and dropped — `05` §6.2 records the
numbers — and the search went with it rather than remaining as unused
machinery, which would suggest kriging honours faults when it does not.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from webmap_geo.mesh.constrained import ConstrainedMesh


def compartment_of(
    mesh: ConstrainedMesh, adjacency: dict[int, list[int]] | None = None
) -> NDArray[np.int32]:
    """Label each vertex with its fault compartment.

    Connected components of the mesh adjacency. Because `build_mesh` splits
    every vertex lying on a hard fault into one copy per side, two vertices
    are connected here exactly when a fault-free path joins them — no edge
    filtering is involved, and none would work (`05` §5).
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


def assign_compartments(
    mesh: ConstrainedMesh,
    points: NDArray[np.floating],
    labels: NDArray[np.int32] | None = None,
) -> NDArray[np.int32]:
    """Which fault compartment each point falls in.

    **Located by containing triangle, not by nearest vertex or centroid.**
    Two shortcuts fail here and both fail worst right at a fault, which is the
    only place the answer is interesting:

    - A vertex on a sealing fault exists twice after the split, once per side
      at the same coordinate, so a nearest-vertex lookup for a point just west
      of a fault can return the eastern copy.
    - A centroid sits at a triangle's middle, not near its edges. Measured: a
      point 1 ft west of a fault took an *eastern* triangle as its nearest
      centroid and was filed in the wrong compartment, while points 400 ft
      west were fine.

    So the nearest centroids are candidates, not answers: each is tested for
    actual containment by barycentric sign, and the first that contains the
    point wins. A point outside the mesh entirely — beyond the domain — falls
    back to the nearest candidate, which is the best available answer for
    something that is not in any triangle.
    """
    from scipy.spatial import cKDTree

    vertex_labels = labels if labels is not None else compartment_of(mesh)
    corners = mesh.vertices[mesh.triangles]
    centroids = corners.mean(axis=1)
    query = np.asarray(points, dtype=float)

    # Enough candidates to cover the triangles around a point without
    # searching them all. A fan around a vertex is rarely wider than this, and
    # the fallback covers the rest.
    candidates = min(12, len(centroids))
    _, nearest = cKDTree(centroids).query(query, k=candidates)
    nearest = np.atleast_2d(nearest.reshape(len(query), -1))

    chosen = nearest[:, 0].copy()
    for row, point in enumerate(query):
        for candidate in nearest[row]:
            if _contains(corners[candidate], point):
                chosen[row] = candidate
                break

    return np.asarray(vertex_labels[mesh.triangles[chosen, 0]], dtype=np.int32)


def _contains(triangle: NDArray[np.float64], point: NDArray[np.float64]) -> bool:
    """Whether a triangle contains a point, by barycentric sign.

    A point exactly on a shared edge is reported as inside both triangles; the
    caller takes the first, which is what makes a point sitting precisely on a
    fault land on one side rather than nowhere.
    """
    a, b, c = triangle
    area = (b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])
    if area == 0.0:
        return False

    u = ((b[0] - point[0]) * (c[1] - point[1]) - (c[0] - point[0]) * (b[1] - point[1])) / area
    v = ((c[0] - point[0]) * (a[1] - point[1]) - (a[0] - point[0]) * (c[1] - point[1])) / area
    w = 1.0 - u - v
    return bool(u >= -1e-12 and v >= -1e-12 and w >= -1e-12)


__all__ = ["assign_compartments", "compartment_of"]
