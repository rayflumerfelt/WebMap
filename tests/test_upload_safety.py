"""Archive extraction. `03-auth-security.md` §9, `11-file-io.md` §2.1.

Extraction is the point where an attacker-chosen filename becomes a
filesystem write, and shapefiles being multi-file means uploads arrive as
archives routinely. So every entry name is treated as hostile input.

No database and no network.
"""

import zipfile
from pathlib import Path

import pytest

from webmap_io.exceptions import PathTraversal, UnsupportedFormat
from webmap_io.upload import (
    MAX_COMPRESSION_RATIO,
    MAX_ENTRIES,
    prepare_upload,
    safe_extract,
)


def make_zip(path: Path, entries: dict[str, bytes]) -> Path:
    """Deflated, because real archives are.

    `ZipFile`'s default is ZIP_STORED, which would give every fixture a 1:1
    compression ratio and quietly make the zip-bomb test unable to fail.
    """
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


# --- traversal --------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        "../escaped.txt",
        "../../escaped.txt",
        "sub/../../escaped.txt",
        "..\\escaped.txt",
        "..\\..\\windows\\system32\\evil.dll",
        "/etc/passwd",
        "/absolute.txt",
    ],
)
def test_entries_that_escape_the_destination_are_refused(tmp_path: Path, entry: str) -> None:
    """Zip-slip, in the forms it actually arrives in.

    Backslash variants matter as much as forward ones: an archive written on
    Windows uses them, and `..\\..\\etc` reads as an ordinary filename to any
    check that only splits on `/`.
    """
    archive = make_zip(tmp_path / "hostile.zip", {entry: b"x"})

    with pytest.raises(PathTraversal):
        safe_extract(archive, tmp_path / "out")

    # Nothing was written outside the destination — the assertion that
    # actually matters, since a check that raises *after* writing is no check.
    assert not (tmp_path / "escaped.txt").exists()
    assert not (tmp_path.parent / "escaped.txt").exists()


def test_the_traversal_message_says_how_to_fix_the_archive(tmp_path: Path) -> None:
    """`CLAUDE.md` §8: name the next action.

    Whoever hits this is usually a geologist who zipped from the wrong
    directory, not an attacker.
    """
    archive = make_zip(tmp_path / "hostile.zip", {"../escaped.txt": b"x"})

    with pytest.raises(PathTraversal) as excinfo:
        safe_extract(archive, tmp_path / "out")

    message = str(excinfo.value)
    assert "'..' path segment" in message
    assert "Re-zip the files from inside their own folder" in message


def test_a_symlink_entry_is_refused(tmp_path: Path) -> None:
    """A link is a deferred traversal: the write that follows it looks
    innocent."""
    archive = tmp_path / "link.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.external_attr = (0xA1FF) << 16  # S_IFLNK | 0777
        zf.writestr(info, "/etc/passwd")

    with pytest.raises(PathTraversal, match="symbolic link"):
        safe_extract(archive, tmp_path / "out")


def test_ordinary_nested_paths_still_extract(tmp_path: Path) -> None:
    """The control. A guard that rejects real archives gets turned off.

    Shapefiles are routinely zipped inside a folder, so nested entries are
    the common case rather than the exception.
    """
    archive = make_zip(
        tmp_path / "ok.zip",
        {"data/points.shp": b"a", "data/points.shx": b"b", "data/points.dbf": b"c"},
    )

    extracted = safe_extract(archive, tmp_path / "out")

    assert len(extracted) == 3
    assert all(p.is_relative_to((tmp_path / "out").resolve()) for p in extracted)


# --- resource exhaustion ----------------------------------------------------


def test_a_zip_bomb_is_refused_by_compression_ratio(tmp_path: Path) -> None:
    """A few kilobytes that becomes gigabytes.

    Caught on ratio rather than only on size, so it is refused before it is
    written rather than partway through filling the disk.
    """
    archive = make_zip(tmp_path / "bomb.zip", {"big.txt": b"\0" * (5 * 1024 * 1024)})

    with pytest.raises(PathTraversal) as excinfo:
        safe_extract(archive, tmp_path / "out")

    message = str(excinfo.value)
    assert "compression ratio" in message
    assert str(MAX_COMPRESSION_RATIO) in message


def test_too_many_entries_is_refused(tmp_path: Path) -> None:
    """A million empty files costs no bytes and still exhausts the extractor."""
    archive = make_zip(
        tmp_path / "many.zip",
        {f"f{i}.txt": bytes(str(i), "ascii") for i in range(MAX_ENTRIES + 1)},
    )

    with pytest.raises(PathTraversal, match="entries"):
        safe_extract(archive, tmp_path / "out")


def test_a_corrupt_archive_says_what_a_shapefile_upload_needs(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "not.zip"
    archive.write_bytes(b"definitely not a zip")

    with pytest.raises(UnsupportedFormat) as excinfo:
        safe_extract(archive, tmp_path / "out")

    assert ".shp, .shx, .dbf and .prj together" in str(excinfo.value)


# --- format detection -------------------------------------------------------


def test_an_incomplete_shapefile_names_the_missing_parts(tmp_path: Path) -> None:
    """`11-file-io.md` §4.1: a `.shp` alone is useless.

    Naming the extensions matters: "invalid shapefile" sends someone to
    re-export from their GIS, when the fix is to re-zip what they already
    have.
    """
    archive = make_zip(tmp_path / "partial.zip", {"points.shp": b"a", "points.prj": b"b"})

    with pytest.raises(UnsupportedFormat) as excinfo:
        prepare_upload(archive, tmp_path / "out")

    message = str(excinfo.value)
    assert ".shx" in message and ".dbf" in message
    assert "zip the whole set together" in message


def test_a_missing_prj_is_not_treated_as_a_missing_part(tmp_path: Path) -> None:
    """A shapefile without a .prj is readable — it just has no CRS.

    Refusing it here would replace the specific CRS message from `11` §3 with
    a vague one about missing files, and the specific message is the one that
    tells the user what to do.
    """
    archive = make_zip(
        tmp_path / "noprj.zip",
        {"points.shp": b"a", "points.shx": b"b", "points.dbf": b"c"},
    )

    upload = prepare_upload(archive, tmp_path / "out")

    assert upload.format == "shapefile"
    assert upload.path.name == "points.shp"


def test_an_archive_with_nothing_readable_lists_what_is_supported(
    tmp_path: Path,
) -> None:
    archive = make_zip(tmp_path / "docs.zip", {"readme.md": b"hello", "notes.pdf": b"x"})

    with pytest.raises(UnsupportedFormat) as excinfo:
        prepare_upload(archive, tmp_path / "out")

    message = str(excinfo.value)
    assert ".md" in message, "say what was actually found"
    assert ".gpkg" in message, "and what would have worked"


def test_multiple_shapefiles_are_reported_rather_than_silently_dropped(
    tmp_path: Path,
) -> None:
    """Reading one of several without saying so loses data invisibly."""
    entries: dict[str, bytes] = {}
    for stem in ("a", "b"):
        entries |= {f"{stem}.shp": b"1", f"{stem}.shx": b"2", f"{stem}.dbf": b"3"}
    archive = make_zip(tmp_path / "two.zip", entries)

    upload = prepare_upload(archive, tmp_path / "out")

    assert len(upload.warnings) == 1
    assert "2 shapefiles" in upload.warnings[0]
    assert "separately" in upload.warnings[0]


def test_a_bare_file_needs_no_archive(tmp_path: Path) -> None:
    """GeoPackage is one file, so requiring a zip would be pointless
    ceremony."""
    source = tmp_path / "layer.gpkg"
    source.write_bytes(b"x")

    upload = prepare_upload(source, tmp_path / "out")

    assert upload.format == "geopackage"
    assert upload.path == source
