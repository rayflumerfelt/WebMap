# 11 — File I/O and Connectors

Package: `python/webmap_io`.

---

## 1. Format support matrix

| Format | Read | Write | Library | Notes |
|---|---|---|---|---|
| Shapefile (`.shp`) | ✓ | ✓ | pyogrio | Interchange only — see §4 |
| GeoJSON | ✓ | ✓ | pyogrio / orjson | Always WGS84 per RFC 7946 |
| GeoPackage (`.gpkg`) | ✓ | ✓ | pyogrio | Preferred over shapefile for everything |
| GeoParquet | ✓ | ✓ | pyarrow + geoarrow | Large layers |
| CSV / XYZ | ✓ | ✓ | pandas | Column mapping required |
| GeoTIFF / COG | ✓ | ✓ | rasterio | Internal grid format |
| ASCII Grid (`.asc`) | ✓ | ✓ | rasterio | Common Surfer export |
| Surfer Grid (`.grd`) | ✓ | ✗ | custom | Read-only; v6 binary and v7 |
| ZMAP+ (`.dat`) | ✓ | ✗ | custom | Legacy interchange, still circulates |
| KML / KMZ | ✓ | ✓ | pyogrio | Presentation exchange |
| DXF | ✓ | ✗ | pyogrio | Fault picks sometimes arrive this way |

**pyogrio, not fiona.** GDAL bindings with a vectorized read path — 5–10× faster on large
layers, returns arrays rather than per-feature dicts.

---

## 2. Connector abstraction

Data lives in three places. Build one abstraction rather than special-casing each.

```python
# python/webmap_io/connectors/base.py

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class SourceDescriptor:
    """What a connector reports about a source, before ingest."""
    uri: str
    name: str
    size_bytes: int | None
    modified_at: datetime | None
    checksum: str | None
    format_hint: str | None


class Connector(ABC):
    """A source of geospatial data.

    Connectors resolve and fetch. They do NOT parse — that is the readers'
    job. Keeping the two separate means a new format works across every
    connector, and a new connector works with every format.
    """

    @abstractmethod
    async def list(self, prefix: str | None = None) -> list[SourceDescriptor]: ...

    @abstractmethod
    async def describe(self, uri: str) -> SourceDescriptor: ...

    @abstractmethod
    async def fetch(self, uri: str, dest: Path) -> Path:
        """Materialise to local disk. Multi-file formats (shapefile) fetch
        all sidecars."""

    @abstractmethod
    async def has_changed(self, uri: str, known_checksum: str | None) -> bool: ...
```

### 2.1 Upload connector

Files posted through the API or dropped into the browser. Simplest case.

### 2.2 File share connector

```python
class FileShareConnector(Connector):
    """SMB/CIFS shares mounted read-only into the container.

    SECURITY NOTE. This is the awkward one in a multi-user deployment. The
    server holds credentials to the share, which collapses per-user
    authorization at the filesystem boundary — anything the service account
    can read, the service can read on anyone's behalf.

    MITIGATION (required):
      1. Service account is READ-ONLY. No write path to any share, ever.
      2. Shares are explicitly mapped to teams in configuration. A share
         not in the map is not reachable.
      3. Shares are an explicit allowlist in configuration. A share not in
         the map is unreachable, which is the whole access control.
      4. Path traversal is blocked — resolved paths must remain within the
         configured share root.

    Kerberos constrained delegation would preserve per-user identity
    end-to-end and is the theoretically correct answer. It is genuinely
    painful to operate. Deferred; revisit if share-sourced data grows
    beyond a few well-understood locations.
    """

    def __init__(self, share_map: dict[str, ShareConfig]):
        self._shares = share_map

    def _resolve(self, uri: str) -> Path:
        share, rel = parse_share_uri(uri)
        cfg = self._shares.get(share)
        if cfg is None:
            raise UnknownShare(
                f"Share '{share}' is not configured. Configured shares: "
                f"{', '.join(sorted(self._shares))}"
            )
        resolved = (cfg.root / rel).resolve()
        if not resolved.is_relative_to(cfg.root.resolve()):
            raise PathTraversal(f"Path escapes share root: {uri}")
        return resolved
```

### 2.3 PostGIS connector

Existing spatial databases. Register a table or a view as a dataset.

```python
class PostgisConnector(Connector):
    """Read-only access to existing PostGIS databases.

    Connections are per-configured-database with credentials from the secret
    manager. Registered datasets record the source table and are either
    replicated into feat.* on a schedule, or read live for small
    slow-changing reference layers.

    LIVE READ is only permitted for datasets under 50k features. Above that,
    replication is required — a live join across databases per tile request
    will not perform.
    """
```

### 2.4 Materialization

**Reading shapefiles off a share for every map render will be miserably slow.** Treat all
external sources as upstream and sync into PostGIS or COG.

```python
async def sync_dataset(ctx: JobContext, dataset_id: UUID) -> SyncResult:
    """Refresh a dataset from its source.

    1. describe() the source; compare checksum against dataset.source_checksum
    2. Unchanged → mark synced_at, return early
    3. Changed → fetch, read, validate, load into a staging table
    4. Atomic swap staging → live
    5. Update bbox, feature_count, attribute_schema, checksum, synced_at

    The atomic swap matters: a geologist should never see a half-loaded
    layer, and a render mid-sync should complete against consistent data.
    """
```

The UI shows `synced_at` on every share- or database-sourced layer, so nobody misinterprets
stale data as current.

---

## 3. Reading

```python
# python/webmap_io/read.py

from dataclasses import dataclass
from pathlib import Path

import pyogrio


@dataclass(frozen=True)
class ReadResult:
    geometry: "GeoArray"
    attributes: "pyarrow.Table"
    srid: int
    geometry_kind: str
    warnings: list[str]


def read_vector(path: Path, layer: str | None = None) -> ReadResult:
    """Read any OGR-supported vector format.

    CRS RESOLUTION, in order:
      1. The file's declared CRS (.prj for shapefile, embedded elsewhere)
      2. A sidecar .prj if the format lacks embedded CRS
      3. FAIL — never guess.

    Guessing is how data ends up 300 km from where it belongs, and the error
    is invisible until someone overlays it on a basemap. If CRS cannot be
    determined, the ingest fails with a message asking the user to specify
    it explicitly.
    """
    info = pyogrio.read_info(path, layer=layer)
    if not info["crs"]:
        raise MissingCRS(
            f"{path.name} has no coordinate reference system. Shapefiles "
            f"store CRS in a companion .prj file — check it was included. "
            f"You can also specify the CRS explicitly on import if you know it."
        )
    ...
```

### 3.1 XYZ and CSV

```python
def read_xyz(
    path: Path,
    x_column: str, y_column: str, z_column: str,
    srid: int,
    delimiter: str | None = None,
) -> ReadResult:
    """Read scattered XYZ or tabular point data.

    Column mapping is REQUIRED, not sniffed. Files arrive with headers like
    'X,Y,Z', 'EAST,NORTH,TVDSS', 'lon,lat,porosity', or no header at all.
    Sniffing gets it wrong occasionally, and 'occasionally' means a map with
    latitude and longitude swapped that nobody notices until it is in a deck.

    The import UI proposes a mapping from the header; the user confirms it.
    """
```

The API exposes `POST /datasets/preview` returning the first 50 rows and a proposed mapping, so
the UI (and Claude) can confirm before committing.

---

## 4. Shapefile: support it, do not build on it

Shapefile must be supported — partners, vendors, and regulators send it, and shares are full
of it. It must never influence the internal data model.

### 4.1 Constraints that will bite

| Constraint | Consequence | Handling |
|---|---|---|
| Field names ≤ 10 chars | `porosity_avg` → `porosity_a` | Warn at schema-edit time (`09-editing.md` §7) |
| Field name collisions after truncation | Silent data loss | Detect, auto-suffix, report |
| No null support in numeric fields | Nulls become 0 | Warn; offer a sentinel value |
| Single geometry type per file | Mixed layers cannot export | Split into multiple files, named |
| 2 GB per component | Large layers fail | Check size, suggest GeoPackage |
| Multi-file | `.shp` alone is useless | Always zip on export; require all parts on import |
| Encoding ambiguity | Mojibake in attributes | Read `.cpg`; default UTF-8; expose an override |
| No CRS beyond `.prj` WKT | Ambiguous datums | Resolve strictly; fail rather than guess |
| Ring orientation | Some readers disagree | Normalise on read |

### 4.2 Loss reporting

```python
@dataclass(frozen=True)
class ExportWarning:
    code: str
    message: str
    affected: list[str]


def plan_shapefile_export(schema: list[AttributeField], geometry_kind: str) -> list[ExportWarning]:
    """Report what WILL be lost, before writing.

    Surfaced in the UI as a confirmation dialog and in the MCP export tool
    response. A geologist sending this to a partner needs to know that
    'porosity_average' arrives as 'porosity_a' — they will not notice until
    the partner asks about it, and by then the file has been forwarded twice.
    """
    warnings: list[ExportWarning] = []

    truncated = [f.name for f in schema if len(f.name) > 10]
    if truncated:
        warnings.append(ExportWarning(
            "field_truncation",
            "Shapefile limits field names to 10 characters. These will be "
            "truncated: " + ", ".join(f"{n} → {n[:10]}" for n in truncated),
            truncated,
        ))

    seen: dict[str, list[str]] = {}
    for f in schema:
        seen.setdefault(f.name[:10].lower(), []).append(f.name)
    collisions = {k: v for k, v in seen.items() if len(v) > 1}
    if collisions:
        warnings.append(ExportWarning(
            "field_collision",
            "These fields collide after truncation and will be renamed with "
            "numeric suffixes: "
            + "; ".join(f"{', '.join(v)} → {k}" for k, v in collisions.items()),
            [n for v in collisions.values() for n in v],
        ))

    if geometry_kind == "mixed":
        warnings.append(ExportWarning(
            "geometry_split",
            "Shapefile stores one geometry type per file. This layer will be "
            "exported as separate files per type.",
            [],
        ))
    return warnings
```

**Recommend GeoPackage.** When a user exports to shapefile, offer GeoPackage as an alternative
in the same dialog, with a one-line reason. Most recipients can read it, and none of the above
applies.

---

## 5. Grid I/O

```python
# python/webmap_io/raster.py

import numpy as np
import rasterio
from rasterio.enums import Resampling


def write_cog(
    grid: np.ndarray,
    transform: rasterio.Affine,
    srid: int,
    path: Path,
    nodata: float = np.nan,
) -> None:
    """Write a Cloud-Optimized GeoTIFF.

    COG is the internal grid format because TiTiler serves it with dynamic
    colormap application — changing a palette becomes a URL parameter change
    rather than a regrid. That is what makes palette editing feel instant.

    Overviews are built at powers of 2 down to ~256 px, using average
    resampling. For a continuous surface, average is correct; nearest would
    make zoomed-out views noisy and misrepresent the surface.
    """
    profile = {
        "driver": "GTiff", "dtype": "float32", "nodata": nodata,
        "width": grid.shape[1], "height": grid.shape[0], "count": 1,
        "crs": rasterio.CRS.from_epsg(srid), "transform": transform,
        "tiled": True, "blockxsize": 512, "blockysize": 512,
        "compress": "deflate", "predictor": 3,   # floating-point predictor
        "BIGTIFF": "IF_SAFER",
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(grid.astype("float32"), 1)
        factors = [2 ** i for i in range(1, 8) if min(grid.shape) // 2 ** i >= 256]
        dst.build_overviews(factors, Resampling.average)
        dst.update_tags(ns="rio_overview", resampling="average")
```

### 5.1 Surfer grid reading

```python
def read_surfer_grd(path: Path) -> tuple[np.ndarray, rasterio.Affine, float]:
    """Read Surfer .grd — both v6 binary (DSBB) and v7 (SRBG).

    Read-only by design. We write COG; there is no reason to write .grd
    except interchange with Surfer, and a GeoTIFF import into Surfer works.

    Surfer's blanking value is 1.70141e38. Convert to NaN on read — leaving
    it produces grids whose statistics are dominated by the blank value, and
    the resulting colour ramp is a single flat colour.
    """
```

Geologists have decades of `.grd` files. Reading them is high-value and low-effort.

---

## 6. Ingest pipeline

```python
async def ingest(ctx: JobContext, source_uri: str, options: IngestOptions) -> UUID:
    """Register a source as a dataset.

    1. describe        connector metadata
    2. fetch           materialise locally
    3. detect          format from extension + magic bytes, not extension alone
    4. read            format-specific reader
    5. validate        CRS present, geometries valid, encoding sane
    6. normalise       ring orientation, drop empty geometries, coerce types
    7. load            create feat.ds_<hex>, bulk COPY
    8. index           GIST on geom and geom_4326, GIN on props
    9. register        dataset row, attribute_schema, bbox, feature_count
   10. caption         generate the one-line description for Claude
   11. audit           emit ingest event

    Steps 5-6 produce warnings attached to the dataset, visible in the UI and
    in webmap_describe_dataset. A layer with 40 dropped null geometries
    should say so rather than silently having 40 fewer features than the
    source file.
    """
```

### 6.1 Bulk loading

```python
async def bulk_load(conn, table: str, result: ReadResult, batch: int = 50_000) -> int:
    """COPY, not INSERT.

    500k features via INSERT is minutes. Via COPY with binary format it is
    seconds. Build indexes AFTER loading — maintaining a GIST index during
    a bulk load roughly triples the time.
    """
```

---

## 7. Export

```python
async def export_dataset(
    ctx: JobContext, dataset_id: UUID, fmt: str, options: ExportOptions
) -> ExportResult:
    """Export to a downloadable file.

    Always writes to a NEW object. Never modifies a source file, even when
    the dataset came from a writable location. See 03-auth-security.md §4.

    Shapefile exports are always zipped — a bare .shp is useless without its
    sidecars and users forward exactly what we give them.

    Returns a signed, short-TTL download URL plus the loss-report warnings
    from §4.2.
    """
```

---

## 8. Testing

```
tests/io/fixtures/
├── valid/
│   ├── points_nad83_texas_central.shp   (+ .shx .dbf .prj .cpg)
│   ├── faults_utm14n.gpkg
│   ├── grid_surfer_v7.grd
│   └── xyz_no_header.csv
└── hostile/
    ├── shapefile_no_prj/                # must fail with a clear message
    ├── shapefile_latin1_attrs/          # encoding handling
    ├── shapefile_field_collision/       # truncation collisions
    ├── mixed_geometry.gpkg              # split on shapefile export
    ├── self_intersecting_polygons.shp   # validation
    ├── coords_swapped.csv               # lat/lon reversed
    └── path_traversal_name.shp          # '../../etc/passwd' as layer name
```

The `hostile/` fixtures are not edge cases. Every one of them is something a geologist will
receive from a partner within the first month. Test them as first-class cases, and assert on
the *error message*, not just the failure — a bad message here costs a support ticket every
time.
