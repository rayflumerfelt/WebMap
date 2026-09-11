"""The anisotropy significance test. `13-kriging.md` §7.6.

The test that matters is the negative one: an isotropic field must not come back
with a confident azimuth. Four subsets of the same pairs always differ, so an
ellipse always exists — and before §7.6 the code accepted whatever came back,
which is how an artefact of the drilling pattern ends up quoted on a map as a
structural direction.
"""

from __future__ import annotations

import numpy as np
import pytest

from webmap_geo.flags import FlagList
from webmap_geo.variogram.aniso import (
    MEANINGFUL_RATIO,
    AnisotropyResult,
    detect_anisotropy,
)

#: Few permutations: each one refits four directional variograms, and the
#: properties under test are about the decision rather than the third decimal
#: of the p-value.
FAST = 19


def isotropic_field(rng: np.random.Generator, count: int = 300) -> tuple:
    """A field with the same structure in every direction."""
    points = rng.uniform(0.0, 10_000.0, size=(count, 2))
    # A smooth field built from a few random cosines: correlated at short range,
    # and correlated the same way whichever direction you walk.
    values = np.zeros(count)
    for _ in range(6):
        direction = rng.uniform(0, 2 * np.pi)
        wavelength = rng.uniform(3_000.0, 6_000.0)
        phase = rng.uniform(0, 2 * np.pi)
        projected = points[:, 0] * np.cos(direction) + points[:, 1] * np.sin(direction)
        values += np.cos(2 * np.pi * projected / wavelength + phase)
    return points, values


def anisotropic_field(rng: np.random.Generator, count: int = 300) -> tuple:
    """A field that varies slowly east-west and quickly north-south.

    What a channel system or a shoreline trend looks like: the same value
    persists for a long way along strike and changes quickly across it.
    """
    points = rng.uniform(0.0, 10_000.0, size=(count, 2))
    values = np.sin(2 * np.pi * points[:, 1] / 2_000.0) + 0.15 * rng.normal(size=count)
    return points, values


class TestIsotropicField:
    def test_does_not_claim_an_azimuth(self) -> None:
        """The failure this whole section exists to prevent: an ellipse fitted
        to four noisy ranges, quoted as a structural direction."""
        rng = np.random.default_rng(3)
        points, values = isotropic_field(rng)

        result = detect_anisotropy(points, values, rng, permutations=FAST)

        assert not result.use

    def test_flags_that_it_fell_back(self) -> None:
        """`INFO / ANISO_NOT_SIGNIFICANT`, with the ratio it saw — silence
        would leave a reader wondering whether anisotropy was even considered."""
        rng = np.random.default_rng(5)
        points, values = isotropic_field(rng)
        flags = FlagList()

        detect_anisotropy(points, values, rng, flags=flags, permutations=FAST)

        assert flags.has("ANISO_NOT_SIGNIFICANT")

    def test_reports_a_p_value_that_can_be_quoted(self) -> None:
        """§7.6: the p-value goes in the lineage record and on screen, because
        an azimuth without one looks like a measurement."""
        rng = np.random.default_rng(7)
        points, values = isotropic_field(rng)

        result = detect_anisotropy(points, values, rng, permutations=FAST)

        assert 0.0 < result.p_value <= 1.0


class TestAnisotropicField:
    def test_finds_the_direction_the_field_actually_varies_in(self) -> None:
        """Values change quickly with northing and slowly with easting, so the
        long range is east-west: azimuth 90."""
        rng = np.random.default_rng(11)
        points, values = anisotropic_field(rng)

        result = detect_anisotropy(points, values, rng, permutations=FAST)

        assert result.ratio > MEANINGFUL_RATIO
        assert result.azimuth == pytest.approx(90.0, abs=45.0)

    def test_keeps_the_per_direction_ranges_for_the_rose(self) -> None:
        rng = np.random.default_rng(13)
        points, values = anisotropic_field(rng)

        result = detect_anisotropy(points, values, rng, permutations=FAST)

        assert len(result.ranges) >= 2
        assert all(value > 0 for value in result.ranges.values())


class TestDecision:
    def test_a_significant_but_tiny_ratio_is_not_used(self) -> None:
        """A 1.2:1 ellipse changes a kriged surface less than the choice of
        model family does, so significance alone is not enough."""
        result = AnisotropyResult(
            ratio=1.2, azimuth=45.0, p_value=0.01, significant=True, ranges={}
        )

        assert not result.use

    def test_a_large_but_insignificant_ratio_is_not_used(self) -> None:
        result = AnisotropyResult(
            ratio=3.0, azimuth=45.0, p_value=0.4, significant=False, ranges={}
        )

        assert not result.use

    def test_both_together_are(self) -> None:
        result = AnisotropyResult(
            ratio=3.0, azimuth=45.0, p_value=0.01, significant=True, ranges={}
        )

        assert result.use


class TestReproducibility:
    def test_the_same_seed_gives_the_same_p_value(self) -> None:
        """§4.6 puts the seed in the lineage record so the run reproduces. A
        p-value that moved between runs would make that record a fiction."""
        points, values = isotropic_field(np.random.default_rng(17))

        first = detect_anisotropy(points, values, np.random.default_rng(2), permutations=FAST)
        second = detect_anisotropy(points, values, np.random.default_rng(2), permutations=FAST)

        assert first.p_value == second.p_value
        assert first.ratio == second.ratio
