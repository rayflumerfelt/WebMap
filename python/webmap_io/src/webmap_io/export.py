"""Export, and what it costs. `11-file-io.md` §4.2, §7.

**Shapefile must be supported and must never influence the data model.**
Partners, vendors and regulators send it, shares are full of it, and every one
of §4.1's constraints is a way it loses data quietly. The job of this module is
to make the loss *loud* — before the file is written, not after the partner
asks why a column is called `porosity_a`.

The reporting is a pure function of the schema, which is the point: a
confirmation dialog, an MCP tool response and a `--dry-run` all need the same
answer, and a warning computed during the write is one nobody can be shown in
time to change their mind.

Two rules hold across every format here:

**Always a new object** (`03-auth-security.md` §8). Never a modification of a
source file, even where the source is writable. `CLAUDE.md` §3.4 is the same
rule from the other side.

**A shapefile is always zipped.** A bare `.shp` is useless without its sidecars
and users forward exactly what they are given, so handing over one file that
happens to be incomplete is handing over a support ticket.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Shapefile's field-name limit. Not negotiable — it is the DBF header.
SHAPEFILE_NAME_LIMIT = 10

#: Per-component size ceiling. `.shp` and `.dbf` each carry a 32-bit byte
#: offset, so 2 GB is where the format stops, not where it slows down.
SHAPEFILE_COMPONENT_LIMIT = 2 * 1024**3

#: What each format is called on the wire, and what it produces.
FORMATS = {
    "geojson": ".geojson",
    "gpkg": ".gpkg",
    "shapefile": ".zip",
    "csv": ".csv",
    "parquet": ".parquet",
}


@dataclass(frozen=True)
class ExportWarning:
    """One thing a format will lose, named before it happens."""

    code: str
    message: str
    affected: list[str]


@dataclass(frozen=True)
class ExportOptions:
    fmt: str
    #: Columns to include. Empty means all of them.
    columns: list[str] | None = None
    #: Reproject on the way out. `None` keeps the storage CRS, which is what a
    #: recipient loading it beside the rest of the project wants.
    target_srid: int | None = None


def plan_export(
    schema: list[dict[str, Any]],
    geometry_kind: str,
    fmt: str,
    *,
    feature_count: int | None = None,
) -> list[ExportWarning]:
    """What this export will lose, before it is written.

    Surfaced in the UI as a confirmation dialog and in the MCP tool's response.
    A geologist sending a file to a partner needs to know that
    `porosity_average` arrives as `porosity_a` — they will not notice until the
    partner asks, and by then the file has been forwarded twice.
    """
    if fmt not in FORMATS:
        raise ValueError(
            f"'{fmt}' is not an export format. Available: {', '.join(sorted(FORMATS))}."
        )
    if fmt == "shapefile":
        return _shapefile_warnings(schema, geometry_kind, feature_count)
    if fmt == "csv":
        return _csv_warnings(geometry_kind)
    # GeoPackage, GeoParquet and GeoJSON lose nothing worth warning about:
    # long field names, nulls, mixed geometry and a declared CRS all survive.
    return []


def _shapefile_warnings(
    schema: list[dict[str, Any]], geometry_kind: str, feature_count: int | None
) -> list[ExportWarning]:
    warnings: list[ExportWarning] = []
    names = [str(field["name"]) for field in schema]

    truncated = [name for name in names if len(name) > SHAPEFILE_NAME_LIMIT]
    if truncated:
        warnings.append(
            ExportWarning(
                "field_truncation",
                "Shapefile limits field names to 10 characters. These will be "
                "truncated: "
                + ", ".join(f"{name} -> {name[:SHAPEFILE_NAME_LIMIT]}" for name in truncated),
                truncated,
            )
        )

    seen: dict[str, list[str]] = {}
    for name in names:
        seen.setdefault(name[:SHAPEFILE_NAME_LIMIT].lower(), []).append(name)
    collisions = {key: value for key, value in seen.items() if len(value) > 1}
    if collisions:
        warnings.append(
            ExportWarning(
                "field_collision",
                "These fields collide after truncation and will be renamed with "
                "numeric suffixes: "
                + "; ".join(
                    f"{', '.join(value)} -> {key}" for key, value in collisions.items()
                ),
                [name for value in collisions.values() for name in value],
            )
        )

    nullable_numerics = [
        str(field["name"])
        for field in schema
        if str(field.get("type")) in {"number", "double", "integer"}
    ]
    if nullable_numerics:
        warnings.append(
            ExportWarning(
                "null_to_zero",
                "Shapefile has no null in a numeric field: blanks become 0, which "
                "reads as a measurement rather than a gap. Affected: "
                + ", ".join(nullable_numerics),
                nullable_numerics,
            )
        )

    if geometry_kind == "mixed":
        warnings.append(
            ExportWarning(
                "geometry_split",
                "Shapefile stores one geometry type per file. This layer will be "
                "exported as separate files per type, named for each.",
                [],
            )
        )

    if feature_count and feature_count > 1_000_000:
        warnings.append(
            ExportWarning(
                "size_risk",
                f"{feature_count:,} features may exceed shapefile's 2 GB per-component "
                f"limit, which fails during the write rather than before it. "
                f"GeoPackage has no such limit.",
                [],
            )
        )

    warnings.append(
        ExportWarning(
            "prefer_gpkg",
            "GeoPackage carries long field names, nulls, mixed geometry and the CRS, "
            "and most recipients read it. None of the above applies to it.",
            [],
        )
    )
    return warnings


def _csv_warnings(geometry_kind: str) -> list[ExportWarning]:
    if geometry_kind == "point":
        return [
            ExportWarning(
                "csv_geometry",
                "CSV carries the coordinates as X and Y columns and no CRS. The "
                "recipient has to be told which projection they are in.",
                [],
            )
        ]
    return [
        ExportWarning(
            "csv_geometry_wkt",
            "CSV has no geometry type: lines and polygons are written as WKT in a "
            "`geometry` column, which many spreadsheet tools will not read back.",
            [],
        )
    ]


def shapefile_field_names(names: list[str]) -> dict[str, str]:
    """The name each field actually gets in the DBF header.

    Deterministic, and the same function the writer and the warning use — a
    warning that predicted `porosity_a` while the writer produced `porosity_1`
    would be worse than no warning, because it would be believed.

    Suffixes are assigned in schema order, so a re-export of the same layer
    produces the same names. A recipient joining two exports on a field name
    depends on that far more than they know.
    """
    assigned: dict[str, str] = {}
    used: set[str] = set()
    for name in names:
        candidate = name[:SHAPEFILE_NAME_LIMIT]
        if candidate.lower() not in used:
            assigned[name] = candidate
            used.add(candidate.lower())
            continue
        for index in range(1, 100):
            suffix = str(index)
            trimmed = candidate[: SHAPEFILE_NAME_LIMIT - len(suffix)] + suffix
            if trimmed.lower() not in used:
                assigned[name] = trimmed
                used.add(trimmed.lower())
                break
        else:  # pragma: no cover - a hundred collisions on one prefix
            raise ValueError(
                f"Could not find a unique shapefile name for '{name}' — a hundred "
                f"fields share its first characters. Rename them, or export to "
                f"GeoPackage, which has no length limit."
            )
    return assigned


def zip_shapefile(directory: Path, archive: Path, *, stem: str) -> Path:
    """Zip a written shapefile and its sidecars.

    Every component, not a chosen list: a `.cpg` left behind turns attribute
    text into mojibake on a reader that trusts it, and which sidecars a driver
    wrote is the driver's business.
    """
    parts = sorted(path for path in directory.iterdir() if path.stem == stem)
    if not any(path.suffix.lower() == ".shp" for path in parts):
        raise FileNotFoundError(
            f"No .shp was written for '{stem}', so there is nothing to zip. The "
            f"export failed earlier than this."
        )
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in parts:
            bundle.write(path, arcname=path.name)
    return archive


__all__ = [
    "FORMATS",
    "SHAPEFILE_COMPONENT_LIMIT",
    "SHAPEFILE_NAME_LIMIT",
    "ExportOptions",
    "ExportWarning",
    "plan_export",
    "shapefile_field_names",
    "zip_shapefile",
]
