"""The Phase 4 solver stack must be installed.

Not a test of third-party behaviour (`CLAUDE.md` §6.4 rules that out) — a test
that `05-geoprocessing.md` §1's dependency list is actually satisfied by a
plain `uv sync`.

It earns its place because of the window it covers. Nothing in Phases 0-3
imports these, so a packaging mistake that removes them produces no failure
anywhere until someone starts Phase 4 — the longest and highest-risk phase —
and hits `ModuleNotFoundError` in the middle of debugging a kriging result.
That happened once already: declaring them under an optional `solvers` extra
meant `uv sync --all-extras` installed none of them, because `--all-extras`
resolves the root project's extras and not a workspace member's.

Delete this file once the solvers have real callers with real tests.
"""

import importlib

import pytest

#: 05-geoprocessing.md §1, with what each one is load-bearing for. Every
#: dependency in this package is reviewed (`CLAUDE.md` §7.2), so the list is
#: short and each entry has a reason.
REQUIRED = {
    "duckdb": "data plane: GeoParquet reads, spatial ops, MVT",
    "numpy": "arrays in, arrays out",
    "scipy": "sparse solves, cKDTree neighbourhood search",
    "shapely": "geometry in, geometry out",
    "gstools": "variogram models, random fields",
    "skgstat": "experimental variogram estimation, binning",
    "contourpy": "contour extraction",
    "triangle": "Shewchuk constrained Delaunay — the fault constraint substrate",
    "pyamg": "algebraic multigrid for the minimum-curvature solve",
    "pyproj": "coordinate transformation, confined to webmap_geo.crs",
}


@pytest.mark.parametrize(("module", "why"), sorted(REQUIRED.items()))
def test_declared_dependency_is_importable(module: str, why: str) -> None:
    try:
        importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - only on a broken install
        pytest.fail(
            f"{module} is not installed, but webmap_geo needs it for: {why}. "
            f"It is declared in python/webmap_geo/pyproject.toml per "
            f"05-geoprocessing.md §1 — run `uv sync`. If it was moved to an "
            f"optional extra, move it back: a workspace member's extras are "
            f"not installed by `uv sync --all-extras`.\n  {exc}"
        )
