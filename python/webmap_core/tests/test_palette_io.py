"""Palette import and export. `08-styling-palettes.md` §5.1.

Every fixture here is written the way the tool that produces it actually writes
it — Surfer's `ColorMap` header, GMT's `r/g/b` colours and B/F/N lines, QGIS's
two different property spellings — because those are the details a reader
written from the format's description gets wrong, and the failure is a palette
with plausible colours in the wrong places rather than an error.

No files on disk: the readers take text, which is what the upload endpoint
hands them.
"""

from __future__ import annotations

import pytest

from webmap_core.style.palette import InvalidPalette, Palette, colour_at
from webmap_core.style.palette_io import (
    read_clr,
    read_cpt,
    read_json,
    read_palette,
    read_qgis_xml,
    write_clr,
    write_cpt,
    write_json,
    write_palette,
)

SURFER = """ColorMap 2 1
0.000000 0 0 255 255
50.000000 255 255 255 255
100.000000 255 0 0 255
"""

GMT = """# cpt-city relief
# COLOR_MODEL = RGB
-8000 0/0/80 -4000 0/0/255
-4000 0/0/255 0 100/200/255
0 100/200/255 2000 20/120/20
B 0/0/0
F 255/255/255
N 128/128/128
"""

QGIS_OLD = """<!DOCTYPE qgis_style>
<qgis_style version="2">
  <colorramps>
    <colorramp type="gradient" name="Blues">
      <prop k="color1" v="247,251,255,255"/>
      <prop k="color2" v="8,48,107,255"/>
      <prop k="discrete" v="0"/>
      <prop k="stops" v="0.25;198,219,239,255:0.5;107,174,214,255:0.75;33,113,181,255"/>
    </colorramp>
  </colorramps>
</qgis_style>
"""

QGIS_NEW = """<qgis_style version="2">
  <colorramps>
    <colorramp type="gradient" name="Reds">
      <Option type="Map">
        <Option name="color1" type="QString" value="255,245,240,255"/>
        <Option name="color2" type="QString" value="103,0,13,255"/>
        <Option name="stops" type="QString" value="0.5;251,106,74,255"/>
      </Option>
    </colorramp>
  </colorramps>
</qgis_style>
"""


def parsed(text: str, fmt: str) -> Palette:
    return read_palette(text, fmt, name="Test", palette_id="test")


# --- Surfer .clr -----------------------------------------------------------------


def test_a_surfer_spec_is_read_and_rescaled_to_0_1() -> None:
    """Surfer positions run 0–100. Storing them unscaled would make every
    palette in the system need to know where it came from."""
    palette = parsed(SURFER, "clr")

    assert [stop["position"] for stop in palette["stops"]] == [0.0, 0.5, 1.0]
    assert [stop["color"] for stop in palette["stops"]] == ["#0000ff", "#ffffff", "#ff0000"]


def test_a_clr_without_the_colormap_header_is_accepted() -> None:
    """Surfer writes the header; cpt-city conversions often do not, and it
    carries nothing the reader needs."""
    palette = parsed("0 0 0 0\n100 255 255 255\n", "clr")
    assert len(palette["stops"]) == 2


def test_a_clr_line_with_too_few_values_names_the_line() -> None:
    with pytest.raises(InvalidPalette, match="Line 2"):
        parsed("0 0 0 0\n50 255\n100 255 255 255\n", "clr")


def test_an_empty_clr_says_what_the_format_is() -> None:
    with pytest.raises(InvalidPalette, match="one 'position red green blue' line"):
        parsed("ColorMap 2 1\n# nothing else\n", "clr")


# --- GMT .cpt --------------------------------------------------------------------


def test_a_cpt_collapses_the_boundary_each_slice_repeats() -> None:
    """A `.cpt` writes both ends of every slice, so consecutive slices name the
    same boundary twice. Kept, the palette has two stops at one position and
    `colour_at` divides by a zero interval."""
    palette = parsed(GMT, "cpt")

    positions = [stop["position"] for stop in palette["stops"]]
    assert positions == sorted(positions)
    assert len(positions) == len(set(positions)), "a shared boundary was kept twice"
    assert len(palette["stops"]) == 4


def test_a_cpt_reads_slash_separated_channels() -> None:
    palette = parsed(GMT, "cpt")
    assert palette["stops"][0]["color"] == "#000050"


def test_background_foreground_and_nan_lines_are_not_stops() -> None:
    """They are clamp and nodata colours, not positions on the ramp. Folding
    them in would add a stop at each end the file never had."""
    palette = parsed(GMT, "cpt")
    assert "#ffffff" not in [stop["color"] for stop in palette["stops"]]


def test_a_hard_break_makes_the_palette_discrete() -> None:
    """A trailing `;` is GMT's own hard-break marker. Read as continuous, the
    import smooths away the boundaries the author drew."""
    text = "0 red 1 red ;\n1 blue 2 blue ;\n"
    palette = parsed(text, "cpt")
    assert palette["interpolation"] == "discrete"
    assert palette["isContinuous"] is False


def test_a_cpt_reads_three_numeric_tokens_as_a_colour() -> None:
    palette = parsed("0 255 0 0 1 0 0 255\n", "cpt")
    assert [stop["color"] for stop in palette["stops"]] == ["#ff0000", "#0000ff"]


def test_a_cpt_reads_named_colours() -> None:
    palette = parsed("0 red 1 blue\n", "cpt")
    assert [stop["color"] for stop in palette["stops"]] == ["#ff0000", "#0000ff"]


def test_an_unknown_colour_name_lists_the_ones_that_work() -> None:
    with pytest.raises(InvalidPalette, match="papayawhip"):
        parsed("0 papayawhip 1 blue\n", "cpt")


def test_a_cpt_of_only_special_lines_says_what_a_slice_is() -> None:
    with pytest.raises(InvalidPalette, match="z0 colour0 z1 colour1"):
        parsed("B 0/0/0\nF 255/255/255\nN 128/128/128\n", "cpt")


def test_cpt_z_values_are_rescaled_by_their_own_range() -> None:
    """A `.cpt` written over depths of -8,000 to 2,000 is a perfectly good ramp
    and its z-values mean nothing to WebMap. What matters is order and spacing,
    which is why the rescale is by range rather than by a constant."""
    palette = parsed(GMT, "cpt")

    assert palette["stops"][0]["position"] == 0.0
    assert palette["stops"][-1]["position"] == 1.0
    # -4000 sits 40% of the way from -8000 to 2000.
    assert palette["stops"][1]["position"] == pytest.approx(0.4)


# --- QGIS .xml -------------------------------------------------------------------


def test_a_qgis_ramp_includes_its_endpoints() -> None:
    """`color1` and `color2` live in their own properties rather than in
    `stops`, and a reader that only walks `stops` produces a ramp missing both
    ends."""
    palette = parsed(QGIS_OLD, "xml")

    assert len(palette["stops"]) == 5
    assert palette["stops"][0]["color"] == "#f7fbff"
    assert palette["stops"][-1]["color"] == "#08306b"


def test_the_newer_option_spelling_is_read_too() -> None:
    """QGIS has written properties two ways across versions. A reader that
    knows only one silently finds no endpoints on half the files people have."""
    palette = parsed(QGIS_NEW, "xml")

    assert len(palette["stops"]) == 3
    assert palette["stops"][1]["color"] == "#fb6a4a"


def test_a_qml_layer_style_is_refused_with_the_difference_named() -> None:
    with pytest.raises(InvalidPalette, match="colour-ramp export"):
        parsed("<qgis><renderer-v2 type='singleSymbol'/></qgis>", "xml")


def test_a_ramp_with_no_endpoints_says_which_ramps_can_be_imported() -> None:
    with pytest.raises(InvalidPalette, match="random"):
        parsed("<colorramp type='random'><prop k='count' v='10'/></colorramp>", "xml")


def test_malformed_xml_says_so() -> None:
    with pytest.raises(InvalidPalette, match="not valid XML"):
        parsed("<colorramp>", "xml")


# --- WebMap .json ----------------------------------------------------------------


def test_json_round_trips_through_write_and_read() -> None:
    original = parsed(SURFER, "clr")
    again = read_palette(write_json(original), "json", name="Test", palette_id="test")
    assert again == original


def test_a_truncated_json_palette_is_refused() -> None:
    with pytest.raises(InvalidPalette, match="'stops' array"):
        parsed('{"name": "half a file"}', "json")


# --- exports ----------------------------------------------------------------------


def test_a_clr_export_is_read_back_identically() -> None:
    """The strongest available check on the writer: the reader is the thing
    that would notice a column in the wrong order."""
    original = parsed(SURFER, "clr")
    again = read_palette(write_clr(original), "clr", name="Test", palette_id="test")

    for before, after in zip(original["stops"], again["stops"], strict=True):
        assert after["color"] == before["color"]
        assert after["position"] == pytest.approx(before["position"])


def test_a_cpt_export_is_read_back_identically() -> None:
    original = parsed(SURFER, "clr")
    again = read_palette(write_cpt(original), "cpt", name="Test", palette_id="test")

    assert [stop["color"] for stop in again["stops"]] == [
        stop["color"] for stop in original["stops"]
    ]


def test_a_cpt_export_writes_over_0_1_rather_than_a_data_range() -> None:
    """A `.cpt` carrying somebody's structure values would be wrong for every
    other map it is used on; `makecpt -T` is how a range gets attached."""
    text = write_cpt(parsed(SURFER, "clr"))
    first_slice = next(
        line for line in text.splitlines() if not line.startswith(("#", "B", "F", "N"))
    )
    assert first_slice.split()[0] == "0"


def test_qgis_is_read_but_not_written() -> None:
    """A QGIS style file carries more than a ramp, and producing a partial one
    would be worse than producing none."""
    with pytest.raises(InvalidPalette, match="read but not written"):
        write_palette(parsed(SURFER, "clr"), "xml")


# --- shared behaviour --------------------------------------------------------------


def test_an_unknown_format_lists_the_ones_that_work() -> None:
    with pytest.raises(InvalidPalette, match=r"\.cpt \(GMT\)"):
        parsed(SURFER, "sld")


def test_a_single_stop_palette_is_refused() -> None:
    with pytest.raises(InvalidPalette, match="at least two"):
        parsed("0 255 0 0\n", "clr")


def test_a_palette_whose_stops_all_sit_together_is_refused() -> None:
    """They collapse to one stop, so this trips the "at least two" guard rather
    than the zero-span one — which is the right order: "a ramp needs two stops"
    is a sentence someone can act on."""
    with pytest.raises(InvalidPalette, match="at least two"):
        parsed("50 255 0 0\n50 0 0 255\n50 0 255 0\n", "clr")


def test_an_imported_palette_samples_through_colour_at() -> None:
    """The point of normalising in the reader: one `colour_at` for every source
    format, with no branch on where the palette came from."""
    palette = parsed(SURFER, "clr")
    assert colour_at(palette, 0.0) == "#0000ff"
    assert colour_at(palette, 0.5) == "#ffffff"
    assert colour_at(palette, 1.0) == "#ff0000"


def test_a_channel_outside_0_255_is_refused() -> None:
    with pytest.raises(InvalidPalette, match="outside 0"):
        parsed("0 300 0 0\n100 0 0 0\n", "clr")


def test_read_clr_and_read_cpt_are_reachable_directly() -> None:
    """The dispatcher is a convenience; the readers are the interface, and a
    caller that already knows the format should not have to spell it twice."""
    assert read_clr(SURFER)["stops"]
    assert read_cpt(GMT)["stops"]
    assert read_qgis_xml(QGIS_OLD)["stops"]
    assert read_json('{"stops": [{"position": 0, "color": "#000000"}]}')["stops"]
