"""Vector readers against the fixtures. `11-file-io.md` §3 and §8.

`12-roadmap.md` Phase 1: *"All hostile fixtures from 11 §8 fail with
actionable messages."* So these assert on the message, not merely on the
exception — `11` §8 is explicit that a bad message here costs a support ticket
every time, and an assertion that only checks the type would pass for
`raise MissingCRS("no")`.

No database and no network. The fixtures are generated per session into a tmp
directory by `tests/fixtures/build.py`, so there are no binaries in the
repository and every byte is accounted for.
"""

from pathlib import Path

import numpy as np
import pytest
import shapely

from tests.fixtures.build import build_all
from webmap_io.exceptions import MissingCRS, UnsupportedFormat
from webmap_io.read import (
    attribute_schema,
    read_vector,
    read_xyz,
    sanitize_name,
)

TEXAS_CENTRAL = 2277


@pytest.fixture(scope="session")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return build_all(tmp_path_factory.mktemp("fixtures"))


# --- valid ------------------------------------------------------------------


def test_a_shapefile_with_a_prj_reads_with_the_right_crs(
    fixtures: dict[str, Path],
) -> None:
    """The Phase 1 criterion: correct CRS and bbox from a well-formed upload."""
    result = read_vector(fixtures["valid_points"])

    assert result.srid == TEXAS_CENTRAL
    assert result.geometry_kind == "point"
    assert result.feature_count == 50
    assert set(result.attributes) == {"well_name", "porosity"}
    assert result.warnings == []


def test_a_geopackage_carries_its_own_crs(fixtures: dict[str, Path]) -> None:
    """No sidecar to lose, which is why `11` §1 prefers it to shapefile."""
    result = read_vector(fixtures["valid_geopackage"])

    assert result.srid == TEXAS_CENTRAL
    assert result.geometry_kind == "linestring"
    assert result.feature_count == 3


def test_attribute_schema_is_inferred_with_nullability(
    fixtures: dict[str, Path],
) -> None:
    schema = {f["name"]: f for f in attribute_schema(read_vector(fixtures["valid_points"]))}

    assert schema["porosity"]["type"] == "number"
    assert schema["well_name"]["type"] == "string"


# --- hostile ----------------------------------------------------------------


def test_a_shapefile_without_a_prj_fails_with_the_documented_message(
    fixtures: dict[str, Path],
) -> None:
    """The exact message `11-file-io.md` §3 specifies.

    The commonest real failure — a shapefile zipped without its sidecar — and
    the one where guessing would be most tempting and most damaging.
    """
    with pytest.raises(MissingCRS) as excinfo:
        read_vector(fixtures["shapefile_no_prj"])

    message = str(excinfo.value)
    assert "no coordinate reference system" in message
    assert ".prj" in message, "the message must name the missing file"
    assert "specify the CRS explicitly" in message, "it must offer a way forward"


def test_a_missing_crs_can_be_supplied_explicitly(fixtures: dict[str, Path]) -> None:
    """The escape the error message offers has to actually exist.

    An error that suggests an action the software does not support is worse
    than one that says nothing.
    """
    result = read_vector(fixtures["shapefile_no_prj"], srid_override=TEXAS_CENTRAL)

    assert result.srid == TEXAS_CENTRAL
    assert result.feature_count == 20


def test_self_intersecting_polygons_are_reported_not_repaired(
    fixtures: dict[str, Path],
) -> None:
    """Reported, because repairing changes the boundary someone digitised.

    A silent `buffer(0)` would alter area and overlay results, and the
    geologist who drew it is the one who should decide.
    """
    result = read_vector(fixtures["self_intersecting_polygons"])

    assert result.feature_count == 2, "the invalid feature is kept, not dropped"
    assert len(result.warnings) == 1
    warning = result.warnings[0]
    assert "invalid" in warning
    assert "not repaired" in warning
    assert "unreliable" in warning, "the consequence has to be stated"


def test_empty_geometries_are_dropped_and_counted(fixtures: dict[str, Path]) -> None:
    """`11` §6: a layer forty features short of its source must say so."""
    result = read_vector(fixtures["empty_geometries"])

    assert result.feature_count == 1
    assert len(result.warnings) == 1
    assert "Dropped 3 of 4" in result.warnings[0]
    assert "read normally" in result.warnings[0]


def test_mixed_geometry_is_read_and_labelled_mixed(
    fixtures: dict[str, Path],
) -> None:
    """Legal in GeoPackage. The consequence lands at shapefile export, which
    stores one geometry type per file (`11` §4.1)."""
    result = read_vector(fixtures["mixed_geometry"])

    assert result.geometry_kind == "mixed"
    assert result.feature_count == 3


def test_undeclared_non_utf8_attributes_are_refused_not_guessed(
    fixtures: dict[str, Path],
) -> None:
    """A cp1252 shapefile with no .cpg. `11-file-io.md` §4.1.

    Before this was handled the call died with a bare `UnicodeDecodeError`
    from inside pyarrow — a stack trace, not an answer. The fixture existed to
    find exactly that, and did.

    Refusing rather than guessing is the same judgement the CRS rule makes:
    cp1252 and cp850 decode the same bytes to different letters, so a wrong
    guess yields plausible text that is silently incorrect, and nothing
    downstream can tell.
    """
    with pytest.raises(UnsupportedFormat) as excinfo:
        read_vector(fixtures["shapefile_latin1_attrs"])

    message = str(excinfo.value)
    assert "not valid UTF-8" in message
    assert ".cpg" in message, "name the file that would declare it"
    assert "cp1252" in message, "name the encoding it probably is"
    assert "does not guess" in message


def test_supplying_the_encoding_reads_the_accents_correctly(
    fixtures: dict[str, Path],
) -> None:
    """The escape the error offers has to work, and produce the right letters.

    Asserting the actual characters matters: a reader that fell back to
    `errors="replace"` would also "succeed", and the corruption would travel
    into every export from then on.
    """
    result = read_vector(fixtures["shapefile_latin1_attrs"], encoding="ISO-8859-1")
    operators = result.attributes["operator"]

    assert result.feature_count == 5
    assert "�" not in "".join(operators), f"replacement characters: {operators}"
    assert "Pétrole Générale" in operators
    assert "Müller Öl" in operators


def test_a_cpg_sidecar_is_honoured_without_being_asked(
    fixtures: dict[str, Path],
) -> None:
    """When the file declares its encoding, no override should be needed."""
    source = fixtures["shapefile_latin1_attrs"]
    source.with_suffix(".cpg").write_text("ISO-8859-1", encoding="ascii")
    try:
        result = read_vector(source)
        assert "Pétrole Générale" in result.attributes["operator"]
    finally:
        source.with_suffix(".cpg").unlink(missing_ok=True)


def test_field_names_that_collide_when_truncated_are_readable(
    fixtures: dict[str, Path],
) -> None:
    """Reading is fine; the collision is an *export* problem.

    Asserted here so the fixture's premise is pinned: both fields exist and
    both truncate to the same ten characters, which is what the export planner
    in `11` §4.2 has to detect before writing.
    """
    result = read_vector(fixtures["shapefile_field_collision"])

    assert {"porosity_average", "porosity_avg_2"} <= set(result.attributes)
    truncated = {name[:10] for name in ("porosity_average", "porosity_avg_2")}
    assert len(truncated) == 1, "the fixture must actually collide"


def test_swapped_coordinates_are_flagged(fixtures: dict[str, Path]) -> None:
    """Lat and lon the wrong way round in a CSV.

    `read_xyz` refuses to *guess* the mapping, but a mapping that is
    explicitly wrong still gets read. The range check catches the specific
    swap that puts West Texas in the South Atlantic — and says so rather than
    silently producing a layer nobody checks until it is on a slide.
    """
    result = read_xyz(
        fixtures["coords_swapped"],
        x_column="lon",
        y_column="lat",
        z_column="porosity",
        srid=4326,
    )

    assert len(result.warnings) == 1
    assert "wrong way round" in result.warnings[0]
    assert "Check the column mapping" in result.warnings[0]


def test_correctly_mapped_coordinates_are_not_flagged() -> None:
    """The control: the swap warning must not fire on good data."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "good.csv"
        path.write_text(
            "lon,lat,porosity\n" + "\n".join(f"-102.{i},31.{i},8.0" for i in range(5)),
            encoding="utf-8",
        )
        result = read_xyz(path, x_column="lon", y_column="lat", z_column="porosity", srid=4326)

    assert result.warnings == []


def test_xyz_refuses_to_guess_the_column_mapping(fixtures: dict[str, Path]) -> None:
    """A wrong column name lists the real ones rather than sniffing.

    Sniffing gets it wrong occasionally, and occasionally means a map with
    latitude and longitude swapped that nobody notices until it is in a deck.
    """
    with pytest.raises(UnsupportedFormat) as excinfo:
        read_xyz(
            fixtures["coords_swapped"],
            x_column="easting",
            y_column="northing",
            z_column="porosity",
            srid=TEXAS_CENTRAL,
        )

    message = str(excinfo.value)
    assert "no column named 'easting'" in message
    assert "lon, lat, porosity" in message, "it must list what is available"
    assert "required rather than guessed" in message


# --- name sanitization ------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "../../etc/passwd",
        r"..\..\windows\system32\config\sam",
        "/absolute/path",
        "....//....//etc",
        "name\x00truncated",
        "..",
        ".",
        "",
        "   ",
        "CON",
        "nul",
        "LPT1",
    ],
)
def test_sanitize_name_never_yields_a_traversable_path(hostile: str) -> None:
    """`03-auth-security.md` §9. A layer called `../../etc/passwd` is real.

    The assertion is on the *property* rather than on a specific output: the
    result must be a single, non-empty path segment that resolves inside its
    parent. Asserting an exact string would pin the implementation and miss
    the next encoding someone finds.
    """
    cleaned = sanitize_name(hostile)

    assert cleaned, "must never be empty — an empty filename is its own problem"
    assert "/" not in cleaned and "\\" not in cleaned
    assert "\x00" not in cleaned
    assert cleaned not in {".", ".."}
    assert cleaned.lower() not in {"con", "nul", "lpt1"}

    root = Path("/srv/exports").resolve()
    assert (root / cleaned).resolve().parent == root


def test_sanitize_name_keeps_ordinary_names_readable() -> None:
    """A sanitizer nobody can live with gets bypassed.

    Real dataset names must survive recognisably, or people will route around
    the control.
    """
    assert sanitize_name("Wolfcamp A Porosity") == "Wolfcamp A Porosity"
    assert sanitize_name("Midland Basin Faults v2.1") == "Midland Basin Faults v2.1"
    assert sanitize_name("well-picks_2026") == "well-picks_2026"


def test_hostile_names_survive_a_round_trip_as_data(
    fixtures: dict[str, Path],
) -> None:
    """Traversal strings are safe *as attribute values* — they only matter
    when used as a path.

    This is the distinction worth keeping: the reader must not mangle data,
    and the sanitizer must not be skipped at the boundary where a name becomes
    a filename.
    """
    result = read_vector(fixtures["path_traversal_name"])
    names = result.attributes["name"]

    assert "../../etc/passwd" in names, "values are data and must be preserved"
    assert all(sanitize_name(n) != n or "/" not in n for n in names)


# --- format handling --------------------------------------------------------


def test_a_missing_file_says_what_a_shapefile_needs(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedFormat) as excinfo:
        read_vector(tmp_path / "nothing.shp")

    assert ".shx and .dbf" in str(excinfo.value)


def test_an_unreadable_file_is_refused_with_the_supported_list(
    tmp_path: Path,
) -> None:
    path = tmp_path / "not-spatial.shp"
    path.write_bytes(b"this is not a shapefile")

    with pytest.raises(UnsupportedFormat) as excinfo:
        read_vector(path)

    assert "WebMap reads" in str(excinfo.value)


def test_polygon_ring_orientation_is_normalised(fixtures: dict[str, Path]) -> None:
    """Readers disagree about winding (`11` §4.1).

    Normalising on read means everything downstream can assume one
    convention instead of each component deciding for itself.
    """
    result = read_vector(fixtures["self_intersecting_polygons"])
    clean = next(
        g
        for g, name in zip(result.geometry, result.attributes["name"], strict=True)
        if name == "clean"
    )

    ring = np.asarray(shapely.get_coordinates(shapely.get_exterior_ring(clean)))
    # Shoelace: positive area means counter-clockwise, which is what
    # shapely.orient_polygons produces for exterior rings.
    area = float(
        np.sum(
            ring[:-1, 0] * ring[1:, 1] - ring[1:, 0] * ring[:-1, 1],
        )
    )
    assert area > 0, "exterior ring should be counter-clockwise after normalisation"
