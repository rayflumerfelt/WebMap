"""The flag registry. `13-kriging.md` §4.5, §16.

The point of a code over a sentence is that a caller can branch on it, so the
tests are about the properties that make branching safe: a code means one
severity everywhere, an unregistered code cannot escape, and an error stops the
run rather than being returned in a list somebody has to remember to read.
"""

from __future__ import annotations

import pytest

from webmap_geo.flags import (
    REGISTRY,
    FlagList,
    GeostatError,
    Severity,
    UnknownFlag,
    flag,
    raise_flag,
)


class TestRegistry:
    def test_severity_comes_from_the_registry_not_the_caller(self) -> None:
        """A code that is an error in one place and a warning in another makes
        a caller's branch on severity meaningless."""
        assert flag("HIGH_NUGGET", "...").severity is Severity.WARNING
        assert flag("INSUFFICIENT_DATA", "...").severity is Severity.ERROR

    def test_an_unregistered_code_is_refused_and_lists_the_real_ones(self) -> None:
        """A typo would otherwise reach a caller branching on it and be
        silently unhandled."""
        with pytest.raises(UnknownFlag, match="HIGH_NUGET"):
            flag("HIGH_NUGET", "a typo")

    def test_the_registry_covers_every_code_the_spec_lists(self) -> None:
        """§16 is the contract. A code in the document and not here is one a
        caller was told to expect and will never see."""
        expected = {
            "COORD_DEGREES",
            "INSUFFICIENT_DATA",
            "TREND_NONFINITE",
            "TOO_FEW_THRESHOLDS",
            "BLOCK_INDICATOR_UNSUPPORTED",
            "TREND_ABSORBED",
            "COVAR_CONFOUND",
            "COVAR_COLLINEAR",
            "COVAR_SPATIAL_PROXY",
            "COVAR_EXTRAPOLATION",
            "TREND_NOT_CONVERGED",
            "TREND_UNIDENTIFIED",
            "TREND_NONMONOTONE",
            "HIGH_NUGGET",
            "ORDER_RELATION_SEVERE",
            "TREND_ONLY_WINS",
            "RANDOM_SPLIT_USED",
            "SR_UNSTABLE",
            "TARGET_LENGTH_NORMALIZED",
            "EXTRAPOLATED_FRACTION",
            "THRESHOLD_DROPPED",
            "ANISO_NOT_SIGNIFICANT",
            "DUPLICATES_RESOLVED",
            "ROWS_DROPPED",
            "TREND_SUBSET_FIT",
            "TAIL_MODEL_FALLBACK",
            "SINGULAR_SYSTEM",
        }

        assert set(REGISTRY) == expected

    def test_every_spec_carries_a_docs_anchor(self) -> None:
        """A flag whose message cannot point at the rule it enforces is a flag
        nobody can act on."""
        assert all(spec.anchor for spec in REGISTRY.values())


class TestRaising:
    def test_an_error_stops_the_run(self) -> None:
        """A caller that has to check a list before trusting an array will one
        day not check."""
        with pytest.raises(GeostatError, match="not enough"):
            raise_flag("INSUFFICIENT_DATA", "not enough samples")

    def test_a_warning_cannot_be_raised(self) -> None:
        """A warning that stops the run is an error that was registered
        wrongly, and this is where that gets noticed."""
        with pytest.raises(ValueError, match="registered wrongly"):
            raise_flag("HIGH_NUGGET", "nugget is high")

    def test_the_raised_error_carries_its_flag(self) -> None:
        with pytest.raises(GeostatError) as caught:
            raise_flag("INSUFFICIENT_DATA", "not enough", count=4)

        assert caught.value.flag.code == "INSUFFICIENT_DATA"
        assert caught.value.flag.detail == {"count": 4}


class TestFlagList:
    def test_keeps_the_order_decisions_were_made_in(self) -> None:
        """A reader following a diagnostic works backwards through exactly
        that order."""
        flags = FlagList()
        flags.add("ROWS_DROPPED", "first")
        flags.add("HIGH_NUGGET", "second")

        assert [item.msg for item in flags] == ["first", "second"]

    def test_separates_warnings_from_information(self) -> None:
        flags = FlagList()
        flags.add("ROWS_DROPPED", "info")
        flags.add("HIGH_NUGGET", "warning")

        assert [item.code for item in flags.warnings] == ["HIGH_NUGGET"]
        assert flags.errors == []

    def test_serialises_for_the_lineage_record(self) -> None:
        """§4.6: there is no run manifest — `lineage.parameters` carries the
        flag list, so it has to survive a JSON round trip."""
        flags = FlagList()
        flags.add("HIGH_NUGGET", "nugget/sill is 0.8", ratio=0.8)

        import json

        payload = json.loads(json.dumps(flags.as_list()))
        assert payload == [
            {
                "severity": "WARNING",
                "code": "HIGH_NUGGET",
                "msg": "nugget/sill is 0.8",
                "detail": {"ratio": 0.8},
            }
        ]

    def test_carries_the_figure_key_for_codes_that_have_one(self) -> None:
        """§4.5's `figure` is how a flag says "there is a picture of this"."""
        flags = FlagList()
        entry = flags.add("TREND_ABSORBED", "the trend took the structure")

        assert entry.figure == "variance_budget"

    def test_asking_whether_a_code_fired_is_one_call(self) -> None:
        flags = FlagList()
        flags.add("ROWS_DROPPED", "some")

        assert flags.has("ROWS_DROPPED")
        assert not flags.has("HIGH_NUGGET")
