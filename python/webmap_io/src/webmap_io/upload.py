"""Materialising an upload to disk, safely. `11-file-io.md` §2.1.

Shapefiles are multi-file, so uploads arrive as archives more often than not,
and an archive is a list of attacker-chosen paths. Everything in this module
exists because of that: extraction is the point where a filename becomes a
filesystem write.

`03-auth-security.md` §9 names path traversal as a real threat from real data,
not a hypothetical. It also names the mitigations as filename sanitization and
root confinement, which is what `safe_extract` does.
"""

import zipfile
from dataclasses import dataclass
from pathlib import Path

from webmap_io.exceptions import PathTraversal, UnsupportedFormat

#: Total uncompressed bytes an archive may expand to. A zip bomb is a few
#: kilobytes that becomes gigabytes; without a cap the first hostile upload
#: fills the disk and takes the API down with it.
MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024

#: Entry count cap. Separate from the size cap because a million empty files
#: costs no bytes and still exhausts inodes and the extraction loop.
MAX_ENTRIES = 10_000

#: Compression ratio above which an archive is refused outright. Ordinary
#: geospatial data compresses perhaps 10:1; 200:1 is not a shapefile.
MAX_COMPRESSION_RATIO = 200

#: The components a shapefile cannot be read without. `.prj` is deliberately
#: absent: a shapefile without one is *readable*, and refusing it here would
#: replace the specific CRS message from `11` §3 with a vague one about
#: missing parts.
SHAPEFILE_REQUIRED = (".shp", ".shx", ".dbf")

#: Extensions we can identify. Anything else is refused by name before it is
#: opened, so a hostile file never reaches a parser.
KNOWN_SUFFIXES = {
    ".shp": "shapefile",
    ".gpkg": "geopackage",
    ".geojson": "geojson",
    ".json": "geojson",
    ".csv": "csv",
    ".txt": "csv",
    ".xyz": "csv",
    ".kml": "kml",
    ".kmz": "kml",
    ".dxf": "dxf",
    ".tif": "raster",
    ".tiff": "raster",
    ".asc": "raster",
    ".grd": "surfer",
}


@dataclass(frozen=True)
class Upload:
    """A materialised upload, ready to read."""

    path: Path
    """The file a reader should open. For a shapefile, the `.shp`."""

    format: str
    root: Path
    """The directory holding it and any sidecars. Everything extracted stays
    inside this; nothing is written outside it."""

    warnings: list[str]


def safe_extract(archive: Path, destination: Path) -> list[Path]:
    """Extract a zip into `destination`, refusing anything that escapes it.

    The checks, and why each is separate:

    - **Absolute paths and drive letters** are rejected outright. `zipfile`
      strips leading slashes on some platforms and not others, so relying on
      it is relying on the platform.
    - **`..` segments** are rejected by name, before resolution. A resolution
      check alone would be enough on its own, but a name check gives a
      message that says what was wrong with the archive.
    - **The resolved path must stay under the destination.** This is the one
      that actually holds; the two above exist so the error is legible.
    - **Symlink entries are refused.** A symlink pointing at `/etc` turns a
      later, perfectly innocent write into a traversal.
    - **Size and ratio caps** stop a zip bomb before it lands.
    """
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    extracted: list[Path] = []

    try:
        with zipfile.ZipFile(archive) as zf:
            entries = zf.infolist()
            if len(entries) > MAX_ENTRIES:
                raise PathTraversal(
                    f"Archive contains {len(entries):,} entries (limit "
                    f"{MAX_ENTRIES:,}). A shapefile has a handful; this is not "
                    f"one."
                )

            total = sum(e.file_size for e in entries)
            packed = sum(e.compress_size for e in entries) or 1
            if total > MAX_UNCOMPRESSED_BYTES:
                raise PathTraversal(
                    f"Archive expands to {total / 1e9:.1f} GB (limit "
                    f"{MAX_UNCOMPRESSED_BYTES / 1e9:.0f} GB). Upload the "
                    f"layer as GeoPackage or GeoParquet instead — both are "
                    f"far smaller for the same data."
                )
            if total / packed > MAX_COMPRESSION_RATIO:
                raise PathTraversal(
                    f"Archive compression ratio is {total / packed:.0f}:1, "
                    f"above the {MAX_COMPRESSION_RATIO}:1 limit. Geospatial "
                    f"data does not compress like that; this archive is "
                    f"either corrupt or crafted."
                )

            for entry in entries:
                if entry.is_dir():
                    continue
                target = _safe_target(entry.filename, root)
                if _is_symlink(entry):
                    raise PathTraversal(
                        f"Archive entry '{entry.filename}' is a symbolic link. "
                        f"Links are refused because they turn a later write "
                        f"into a write somewhere else entirely."
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(entry) as source, target.open("wb") as sink:
                    sink.write(source.read())
                extracted.append(target)
    except zipfile.BadZipFile as exc:
        raise UnsupportedFormat(
            f"{archive.name} is not a readable zip archive. If this is a bare "
            f"shapefile, upload the .shp, .shx, .dbf and .prj together in a "
            f"zip — a .shp on its own cannot be read."
        ) from exc

    if not extracted:
        raise UnsupportedFormat(f"{archive.name} contains no files.")
    return extracted


def _safe_target(name: str, root: Path) -> Path:
    """Resolve an archive entry name to a path inside `root`, or raise."""
    if not name or name.endswith("/"):
        raise PathTraversal(f"Archive contains an entry with no filename: {name!r}")

    # Normalise separators before inspecting: a zip written on Windows uses
    # backslashes, and `..\\..\\etc` reads as an ordinary filename otherwise.
    normalised = name.replace("\\", "/")

    if normalised.startswith("/") or (len(normalised) > 1 and normalised[1] == ":"):
        raise PathTraversal(
            f"Archive entry '{name}' is an absolute path. Entries must be "
            f"relative to the archive root."
        )
    if any(part == ".." for part in normalised.split("/")):
        raise PathTraversal(
            f"Archive entry '{name}' contains a '..' path segment, which would "
            f"write outside the upload directory. Re-zip the files from inside "
            f"their own folder."
        )

    target = (root / normalised).resolve()
    if not target.is_relative_to(root):
        raise PathTraversal(f"Archive entry '{name}' resolves outside the upload directory.")
    return target


def _is_symlink(entry: zipfile.ZipInfo) -> bool:
    """Unix mode lives in the top 16 bits of `external_attr`. 0xA000 is S_IFLNK."""
    return (entry.external_attr >> 16) & 0xF000 == 0xA000


def prepare_upload(source: Path, destination: Path) -> Upload:
    """Materialise an upload and identify what it is.

    A zip is extracted; anything else is used where it lies. Format detection
    is by extension *and* by what the archive actually contains — a `.shp`
    without its `.shx` is identified as an incomplete shapefile rather than
    handed to a parser that will fail obscurely.
    """
    is_archive = source.suffix.lower() == ".zip"
    files = safe_extract(source, destination) if is_archive else [source]

    warnings: list[str] = []
    candidates = [f for f in files if f.suffix.lower() in KNOWN_SUFFIXES]
    if not candidates:
        found = sorted({f.suffix.lower() or "(no extension)" for f in files})
        raise UnsupportedFormat(
            f"No readable spatial data found. Saw {', '.join(found)}; WebMap "
            f"reads {', '.join(sorted(set(KNOWN_SUFFIXES)))}."
        )

    # Shapefile first: it is the only multi-file format here, so a zip
    # containing both a .shp and a stray .csv is a shapefile upload.
    shapefiles = [f for f in candidates if f.suffix.lower() == ".shp"]
    if shapefiles:
        chosen = shapefiles[0]
        if len(shapefiles) > 1:
            warnings.append(
                f"The archive contains {len(shapefiles)} shapefiles; reading "
                f"'{chosen.name}'. Upload them separately to register each as "
                f"its own dataset."
            )
        _require_shapefile_parts(chosen, files)
        return Upload(path=chosen, format="shapefile", root=destination, warnings=warnings)

    chosen = candidates[0]
    if len(candidates) > 1:
        warnings.append(
            f"The upload contains {len(candidates)} readable files; reading '{chosen.name}'."
        )
    return Upload(
        path=chosen,
        format=KNOWN_SUFFIXES[chosen.suffix.lower()],
        root=destination,
        warnings=warnings,
    )


def _require_shapefile_parts(shp: Path, files: list[Path]) -> None:
    """A `.shp` alone is useless. Say which parts are missing, by name.

    `11-file-io.md` §4.1: "`.shp` alone is useless — always zip on export;
    require all parts on import." The message names the actual missing
    extensions because "invalid shapefile" sends someone to re-export rather
    than to re-zip.
    """
    present = {f.suffix.lower() for f in files if f.stem == shp.stem}
    missing = [ext for ext in SHAPEFILE_REQUIRED if ext not in present]
    if missing:
        raise UnsupportedFormat(
            f"The shapefile '{shp.name}' is missing {', '.join(missing)}. A "
            f"shapefile is several files and cannot be read without them — "
            f"zip the whole set together, including the .prj that carries the "
            f"coordinate system."
        )
