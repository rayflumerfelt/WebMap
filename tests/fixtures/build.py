"""Build the fixture files for the I/O tests. `11-file-io.md` §8.

Generated rather than committed. Two reasons: `CLAUDE.md` §7.5 says
`tests/fixtures/` is synthetic only, and a generator makes that checkable —
you can read what every byte is. Committed binaries would also drift out of
step with the readers with nothing to notice.

The `hostile/` set is not a collection of edge cases. Every one is something
a geologist will receive from a partner within the first month, and each
asserts on the *error message* rather than merely on failure: a bad message
here costs a support ticket every time.
"""

from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyogrio
import shapely

# NAD83 / Texas Central (ftUS), the Midland Basin working CRS.
TEXAS_CENTRAL = "EPSG:2277"
EXTENT = (1_500_000.0, 10_400_000.0, 2_060_000.0, 10_800_000.0)


def _points(n: int, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return shapely.points(
        rng.uniform(EXTENT[0], EXTENT[2], n), rng.uniform(EXTENT[1], EXTENT[3], n)
    )


def _write(
    path: Path,
    geometry: np.ndarray,
    fields: dict[str, list[Any]],
    *,
    crs: str | None = TEXAS_CENTRAL,
    driver: str = "ESRI Shapefile",
    **kwargs: Any,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pyogrio.write_arrow(
        pa.table({**fields, "wkb_geometry": shapely.to_wkb(geometry)}),
        path,
        driver=driver,
        geometry_name="wkb_geometry",
        geometry_type=_ogr_type(geometry),
        crs=crs,
        **kwargs,
    )
    return path


def _ogr_type(geometry: np.ndarray) -> str:
    by_id = {0: "Point", 1: "LineString", 3: "Polygon", 6: "MultiPolygon"}
    ids = {int(t) for t in shapely.get_type_id(geometry)}
    if len(ids) == 1:
        return by_id.get(next(iter(ids)), "Unknown")
    return "Unknown"


# --- valid ------------------------------------------------------------------


def valid_points(root: Path) -> Path:
    """A well-formed shapefile with a .prj. The control for every other case."""
    n = 50
    geometry = _points(n)
    return _write(
        root / "valid" / "points_nad83_texas_central.shp",
        geometry,
        {
            "well_name": [f"SYN {i}-1H" for i in range(n)],
            "porosity": [round(6.0 + i * 0.1, 2) for i in range(n)],
        },
    )


def valid_geopackage(root: Path) -> Path:
    """GeoPackage embeds its CRS, so there is no sidecar to lose."""
    traces = [
        shapely.LineString(
            [
                (1_500_000 + i * 20_000, 10_420_000),
                (1_530_000 + i * 20_000, 10_470_000),
            ]
        )
        for i in range(3)
    ]
    return _write(
        root / "valid" / "faults_texas_central.gpkg",
        np.asarray(traces, dtype=object),
        {
            "name": [f"Synthetic Fault {i + 1:02d}" for i in range(3)],
            "constraint_kind": ["fault", "fault", "breakline"],
        },
        driver="GPKG",
    )


# --- hostile ----------------------------------------------------------------


def shapefile_no_prj(root: Path) -> Path:
    """The commonest real failure: a shapefile zipped without its .prj.

    Must fail rather than assume a CRS. `11` §3: guessing is how data ends up
    300 km from where it belongs.
    """
    path = _write(
        root / "hostile" / "shapefile_no_prj" / "points.shp",
        _points(20),
        {"id": list(range(20))},
    )
    path.with_suffix(".prj").unlink(missing_ok=True)
    return path


def shapefile_latin1_attrs(root: Path) -> Path:
    """Attributes in cp1252 with no .cpg, the classic mojibake source.

    Names with accents are ordinary in this domain — operators, fields, and
    people. Reading them as UTF-8 without declaring the encoding produces
    replacement characters that then travel into every export.
    """
    n = 5
    path = _write(
        root / "hostile" / "shapefile_latin1_attrs" / "operators.shp",
        _points(n),
        {
            "operator": [
                "Pétrole Générale",
                "Ørsted Energi",
                "Müller Öl",
                "Ação Sul",
                "Nord Åse",
            ]
        },
        encoding="ISO-8859-1",
    )
    path.with_suffix(".cpg").unlink(missing_ok=True)
    return path


def shapefile_field_collision(root: Path) -> Path:
    """Field names that collide once truncated to shapefile's 10 characters.

    `porosity_average` and `porosity_avg_2` both become `porosity_a`. The
    export planner has to detect this *before* writing (`11` §4.2) — a
    geologist will not notice until a partner asks about the missing column.
    """
    n = 5
    return _write(
        root / "hostile" / "shapefile_field_collision" / "picks.gpkg",
        _points(n),
        {
            "porosity_average": [0.1] * n,
            "porosity_avg_2": [0.2] * n,
            "permeability_md": [1.0] * n,
        },
        driver="GPKG",
    )


def mixed_geometry(root: Path) -> Path:
    """One layer holding points and lines.

    Legal in GeoPackage, impossible in shapefile — an export has to split into
    separate files per type and say so (`11` §4.1).
    """
    geometry = np.asarray(
        [
            shapely.Point(1_500_000, 10_400_000),
            shapely.LineString([(1_510_000, 10_410_000), (1_520_000, 10_420_000)]),
            shapely.Point(1_530_000, 10_430_000),
        ],
        dtype=object,
    )
    return _write(
        root / "hostile" / "mixed_geometry.gpkg",
        geometry,
        {"label": ["a", "b", "c"]},
        driver="GPKG",
    )


def self_intersecting_polygons(root: Path) -> Path:
    """A bowtie polygon. Valid WKB, invalid geometry.

    Area and overlay results involving it are meaningless. The reader reports
    it rather than repairing it: silently fixing a self-intersection changes
    the boundary the geologist digitised (`09-editing.md` §4).
    """
    bowtie = shapely.Polygon(
        [
            (1_500_000, 10_400_000),
            (1_520_000, 10_420_000),
            (1_500_000, 10_420_000),
            (1_520_000, 10_400_000),
            (1_500_000, 10_400_000),
        ]
    )
    good = shapely.Polygon(
        [
            (1_540_000, 10_400_000),
            (1_560_000, 10_400_000),
            (1_560_000, 10_420_000),
            (1_540_000, 10_420_000),
            (1_540_000, 10_400_000),
        ]
    )
    return _write(
        root / "hostile" / "self_intersecting_polygons.gpkg",
        np.asarray([bowtie, good], dtype=object),
        {"name": ["bowtie", "clean"]},
        driver="GPKG",
    )


def coords_swapped(root: Path) -> Path:
    """A CSV with latitude in the longitude column.

    Midland is near (-102, 32). Swapped, it plots at (32, -102) — in the
    South Atlantic, and entirely plausible-looking until someone adds a
    basemap.
    """
    path = root / "hostile" / "coords_swapped.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = ["lon,lat,porosity"]
    for i in range(10):
        lat = 31.5 + i * 0.05
        lon = -102.5 + i * 0.05
        rows.append(f"{lat:.4f},{lon:.4f},{8.0 + i * 0.3:.2f}")  # deliberately reversed
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def path_traversal_name(root: Path) -> Path:
    """A layer carrying path-traversal strings where a *name* would arrive.

    `03-auth-security.md` §9 calls a layer named `../../etc/passwd` a real
    shapefile you may receive. It cannot be built as an actual OGR layer name
    — GDAL refuses ("The layer name may not contain special characters"),
    which is a small mercy but not a control we own.

    The surface we do own is a name reaching a filesystem path on export or
    download, and names arrive as attribute values and as user-supplied
    dataset titles far more often than as layer names. So the hostile strings
    live in a `name` column here, and `sanitize_name` is tested against them
    directly in test_readers.py.
    """
    hostile = [
        "../../etc/passwd",
        "..\\..\\windows\\system32\\config\\sam",
        "/absolute/path",
        "CON",
        "  ...  ",
    ]
    return _write(
        root / "hostile" / "path_traversal_names.gpkg",
        _points(len(hostile)),
        {"name": hostile},
        driver="GPKG",
    )


def empty_geometries(root: Path) -> Path:
    """A layer where most features carry no geometry.

    `11` §6: forty dropped null geometries should be *said*, not silently
    produce a layer forty features short of its source.
    """
    geometry = np.asarray(
        [shapely.Point(1_500_000, 10_400_000), shapely.Point(), None, shapely.Point()],
        dtype=object,
    )
    return _write(
        root / "hostile" / "empty_geometries.gpkg",
        geometry,
        {"id": [1, 2, 3, 4]},
        driver="GPKG",
    )


BUILDERS = {
    "valid_points": valid_points,
    "valid_geopackage": valid_geopackage,
    "shapefile_no_prj": shapefile_no_prj,
    "shapefile_latin1_attrs": shapefile_latin1_attrs,
    "shapefile_field_collision": shapefile_field_collision,
    "mixed_geometry": mixed_geometry,
    "self_intersecting_polygons": self_intersecting_polygons,
    "coords_swapped": coords_swapped,
    "path_traversal_name": path_traversal_name,
    "empty_geometries": empty_geometries,
}


def build_all(root: Path) -> dict[str, Path]:
    """Build every fixture under `root` and return name -> path."""
    return {name: builder(root) for name, builder in BUILDERS.items()}


if __name__ == "__main__":
    import sys

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("build/fixtures")
    for name, path in build_all(target).items():
        sys.stdout.write(f"{name:32s} {path}\n")
