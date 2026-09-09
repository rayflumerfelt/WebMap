"""The CRS definition the browser parses. `07-frontend.md` §5.2.

Half of a two-sided check. This asserts the server still produces the string in
`tests/fixtures/crs/epsg2277.wkt`; `apps/web/src/crs/analysisCrs.test.ts`
asserts proj4 can parse that same file and lands on the right coordinates.

Neither failure is visible on its own. A pyproj upgrade that changed the WKT
dialect would leave the server serving a definition the browser silently could
not use, and the status bar would show nothing with no error anywhere. That is
not hypothetical: `to_wkt()` defaults to WKT2, and an earlier version of the
frontend fixture was WKT1 — so the frontend test was passing against a format
the server does not send.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from webmap_geo.crs import crs_definition
from webmap_geo.exceptions import UnknownCrs

FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "crs" / "epsg2277.wkt"


def test_the_committed_fixture_is_what_the_server_produces() -> None:
    """If this fails, regenerate the fixture *and* check the frontend still
    parses it — the two are a pair, and updating one alone is the failure this
    test exists to prevent."""
    assert FIXTURE.is_file(), f"No CRS fixture at {FIXTURE}"

    assert crs_definition(2277) == FIXTURE.read_text(encoding="utf-8")


def test_the_definition_is_wkt2_and_names_its_authority() -> None:
    """The authority code is what lets anyone reading a session document check
    the definition against the EPSG registry rather than trusting it."""
    wkt = crs_definition(2277)

    assert wkt.startswith("PROJCRS[")
    assert 'ID["EPSG",2277]' in wkt


def test_the_definition_carries_the_axis_unit() -> None:
    """The single most consequential thing to get wrong: a cursor readout in
    metres against a well file in feet is off by 3.28x and looks plausible."""
    assert "US survey foot" in crs_definition(2277)


def test_a_geographic_crs_also_has_a_definition() -> None:
    """Not every project is projected at this layer — `validate_analysis_srid`
    is what refuses those — so this function must not be the thing that fails
    for one."""
    assert crs_definition(4326).startswith("GEOGCRS[")


def test_an_unknown_srid_is_refused_with_a_message_a_caller_can_act_on() -> None:
    """pyproj raises `CRSError`, which nothing maps — so a mistyped SRID from a
    caller became a 500 rather than a message naming the mistake. Same shape as
    the duplicate `MissingCRS` that made a .prj-less shapefile return 500."""
    with pytest.raises(UnknownCrs, match=r"epsg.io"):
        crs_definition(999_999)
