"""Preprocessing for geostatistics. `13-kriging.md` §5.

`CLAUDE.md` §6.1 puts `webmap_geo` algorithms at the highest bar in the repo, so
these are property tests and recovery tests rather than checks that a function
returns something. The properties are the ones the spec's own reasoning rests
on: declustering must actually remove the bias it claims to, a transform's
quantiles must survive a round trip exactly, and screening must fire on a
covariate that is really a spatial label.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from webmap_geo.flags import FlagList, GeostatError
from webmap_geo.prep.decluster import decluster, weighted_quantile
from webmap_geo.prep.screen import screen
from webmap_geo.prep.transform import build_transform
from webmap_geo.prep.validate import (
    default_dedup_tol,
    samples_from,
    validate,
)


def grid_samples(count: int = 100) -> tuple[Any, Any, Any]:
    """A regular grid of samples with a linear field on it."""
    side = int(np.sqrt(count))
    mesh_x, mesh_y = np.meshgrid(
        np.linspace(0.0, 10_000.0, side), np.linspace(0.0, 10_000.0, side)
    )
    x = mesh_x.ravel()
    y = mesh_y.ravel()
    return x, y, -9_000.0 - 0.01 * x + 0.005 * y


class TestValidate:
    def test_drops_a_row_with_no_target(self) -> None:
        x, y, values = grid_samples()
        values = values.copy()
        values[3] = np.nan
        flags = FlagList()

        cleaned = validate(samples_from(x, y, values), flags)

        assert cleaned.count == len(values) - 1
        assert flags.has("ROWS_DROPPED")

    def test_keeps_a_row_whose_unused_covariate_is_blank(self) -> None:
        """The difference between cleaning the data and throwing it away: a
        table with five columns of which the trend reads two should not lose a
        well because a third is blank."""
        x, y, values = grid_samples()
        used = np.ones_like(values)
        unused = np.ones_like(values)
        unused[5] = np.nan
        flags = FlagList()

        cleaned = validate(
            samples_from(x, y, values, {"used": used, "unused": unused}),
            flags,
            used_covariates=["used"],
        )

        assert cleaned.count == len(values)

    def test_drops_a_row_whose_used_covariate_is_blank(self) -> None:
        x, y, values = grid_samples()
        used = np.ones_like(values)
        used[5] = np.nan
        flags = FlagList()

        cleaned = validate(
            samples_from(x, y, values, {"used": used}), flags, used_covariates=["used"]
        )

        assert cleaned.count == len(values) - 1

    def test_averages_collocated_samples_and_says_so(self) -> None:
        """Two wells at one location is either a real pair of measurements or a
        data error, and which one it is changes the answer — so the merge is
        reported rather than silent."""
        x, y, values = grid_samples()
        x = np.append(x, x[0] + 0.001)
        y = np.append(y, y[0])
        values = np.append(values, values[0] + 100.0)
        flags = FlagList()

        cleaned = validate(samples_from(x, y, values), flags, dedup_tol=1.0)

        assert cleaned.count == len(values) - 1
        assert flags.has("DUPLICATES_RESOLVED")
        assert cleaned.values.max() == pytest.approx(values[0] + 50.0, abs=1.0)

    def test_the_error_policy_stops_rather_than_merging(self) -> None:
        x, y, values = grid_samples()
        x = np.append(x, x[0])
        y = np.append(y, y[0])
        values = np.append(values, values[0])

        with pytest.raises(GeostatError, match="duplicate policy is 'error'"):
            validate(samples_from(x, y, values), FlagList(), dedup_tol=1.0, duplicates="error")

    def test_refuses_too_few_samples_and_says_what_to_do_instead(self) -> None:
        """Not just "too few": a variogram fitted to ten points is a line
        through noise, and the message names the methods that do not need one."""
        x = np.linspace(0, 1000, 10)
        y = np.linspace(0, 1000, 10) + np.arange(10)
        values = np.arange(10, dtype=float)

        with pytest.raises(GeostatError, match="minimum curvature or IDW"):
            validate(samples_from(x, y, values), FlagList())

    def test_refuses_collinear_samples(self) -> None:
        """The variogram is defined along one direction and undefined across
        it — usually because X and Y were mapped to the same column."""
        x = np.linspace(0, 10_000, 50)
        y = 2.0 * x
        values = np.linspace(0, 1, 50)

        with pytest.raises(GeostatError, match="single line"):
            validate(samples_from(x, y, values), FlagList())

    def test_the_default_tolerance_survives_a_duplicate_pair(self) -> None:
        """The 1st percentile rather than the minimum: the minimum is often
        itself a duplicate pair, and a tolerance derived from it is zero."""
        x, y, _ = grid_samples()
        x = np.append(x, x[0])
        y = np.append(y, y[0])

        assert default_dedup_tol(x, y) > 0


class TestDecluster:
    def test_removes_the_bias_a_cluster_introduces(self) -> None:
        """The property the whole method exists for. Samples cluster in high
        values by construction — nobody drills the part that does not produce —
        so the naive mean overstates and the declustered one should not."""
        rng = np.random.default_rng(11)
        # A field that is high in one corner, sampled evenly...
        spread_x = rng.uniform(0, 10_000, 200)
        spread_y = rng.uniform(0, 10_000, 200)
        # ...and sampled again, heavily, in that corner.
        cluster_x = rng.uniform(8_000, 10_000, 300)
        cluster_y = rng.uniform(8_000, 10_000, 300)

        x = np.concatenate([spread_x, cluster_x])
        y = np.concatenate([spread_y, cluster_y])
        values = 0.001 * (x + y)

        samples = samples_from(x, y, values)
        truth = float(np.mean(0.001 * (spread_x + spread_y)))

        result = decluster(samples, rng)

        assert result.naive_mean > truth
        assert abs(result.declustered_mean - truth) < abs(result.naive_mean - truth)

    def test_weights_sum_to_the_sample_count(self) -> None:
        """§5.2 normalises to `n`, so a weighted mean is comparable with an
        unweighted one and a weighted least squares keeps its scale."""
        x, y, values = grid_samples()

        result = decluster(samples_from(x, y, values), np.random.default_rng(3))

        assert result.weights.sum() == pytest.approx(len(values))

    def test_even_sampling_gets_a_mean_that_did_not_move(self) -> None:
        """The property cell declustering actually has. Measured: 0.08 standard
        deviations on 400 evenly scattered samples — it does not invent a bias
        where there is no clustering.

        Not *equal weights*: the size that minimises the mean can be coarse
        enough to put a handful of cells over the field, so individual weights
        vary even here. An earlier version of this test asserted the weights and
        was asserting something the method does not promise.
        """
        rng = np.random.default_rng(5)
        x = rng.uniform(0, 10_000, 400)
        y = rng.uniform(0, 10_000, 400)
        values = rng.normal(100.0, 10.0, 400)

        result = decluster(samples_from(x, y, values), np.random.default_rng(5))

        shift = abs(result.naive_mean - result.declustered_mean) / float(values.std())
        assert shift < 0.2

    def test_a_clustered_patch_is_corrected_towards_the_truth(self) -> None:
        """The whole point, with a field whose true mean is known: a high corner
        covering a sixteenth of the area, oversampled three to two."""
        rng = np.random.default_rng(5)
        spread_x, spread_y = rng.uniform(0, 10_000, 200), rng.uniform(0, 10_000, 200)
        cluster_x, cluster_y = rng.uniform(8_000, 10_000, 300), rng.uniform(8_000, 10_000, 300)
        x = np.concatenate([spread_x, cluster_x])
        y = np.concatenate([spread_y, cluster_y])
        values = np.where((x > 7_500) & (y > 7_500), 200.0, 100.0) + rng.normal(0, 5, x.size)
        truth = 100.0 + 100.0 * 0.0625

        result = decluster(samples_from(x, y, values), np.random.default_rng(5))

        assert result.naive_mean > 150.0
        assert abs(result.declustered_mean - truth) < 10.0

    def test_the_answer_does_not_depend_on_where_the_grid_starts(self) -> None:
        """Which is why the origin is swept. A single fixed origin gives an
        answer that is a property of the grid rather than of the data."""
        rng = np.random.default_rng(13)
        x = np.concatenate([rng.uniform(0, 10_000, 100), rng.uniform(0, 1_000, 100)])
        y = np.concatenate([rng.uniform(0, 10_000, 100), rng.uniform(0, 1_000, 100)])
        values = 0.001 * (x + y)
        samples = samples_from(x, y, values)

        first = decluster(samples, np.random.default_rng(1))
        second = decluster(samples, np.random.default_rng(2))

        assert first.declustered_mean == pytest.approx(second.declustered_mean, rel=0.05)

    def test_an_explicit_cell_size_is_used_as_given(self) -> None:
        x, y, values = grid_samples()

        result = decluster(
            samples_from(x, y, values), np.random.default_rng(3), cell_size=2_000.0
        )

        assert result.cell_size == 2_000.0
        assert result.sweep == []

    def test_the_sweep_is_kept_for_the_diagnostic_panel(self) -> None:
        x, y, values = grid_samples()

        result = decluster(samples_from(x, y, values), np.random.default_rng(3))

        assert len(result.sweep) > 1
        assert all(size > 0 for size, _ in result.sweep)


class TestWeightedQuantile:
    def test_matches_the_unweighted_quantile_when_weights_are_equal(self) -> None:
        values = np.arange(100, dtype=float)
        weights = np.ones(100)

        weighted = weighted_quantile(values, weights, np.array([0.5]))

        assert weighted[0] == pytest.approx(np.median(values), abs=1.0)

    def test_a_down_weighted_cluster_moves_the_median(self) -> None:
        """Which is the point: thresholds placed on the naive distribution sit
        where the drilling was rather than where the values are."""
        values = np.concatenate([np.zeros(90), np.full(10, 100.0)])
        clustered = np.concatenate([np.full(90, 0.1), np.full(10, 9.0)])

        naive = weighted_quantile(values, np.ones(100), np.array([0.5]))[0]
        declustered = weighted_quantile(values, clustered, np.array([0.5]))[0]

        assert naive == pytest.approx(0.0)
        assert declustered > naive


class TestTransforms:
    @pytest.mark.parametrize("kind", ["none", "log", "boxcox", "nscore"])
    def test_a_quantile_survives_the_round_trip(self, kind: str) -> None:
        """The property that matters, and the reason `is_monotone` is always
        true: back-transforming a quantile is exact, whatever the transform did
        in between."""
        rng = np.random.default_rng(17)
        values = rng.lognormal(mean=3.0, sigma=0.8, size=500)

        transform = build_transform(kind, values)
        forward = transform.forward(values)
        back = transform.inverse(forward)

        assert back == pytest.approx(values, rel=1e-6)

    @pytest.mark.parametrize("kind", ["log", "boxcox", "nscore"])
    def test_order_is_preserved(self, kind: str) -> None:
        rng = np.random.default_rng(19)
        values = rng.lognormal(mean=2.0, sigma=1.0, size=200)

        transform = build_transform(kind, values)
        forward = transform.forward(values)

        assert np.all(np.argsort(forward) == np.argsort(values))

    def test_normal_score_produces_a_gaussian(self) -> None:
        """Required for SGS or any Gaussian comparison run, because the method
        assumes a Gaussian field and the field is not."""
        rng = np.random.default_rng(23)
        values = rng.lognormal(mean=3.0, sigma=1.2, size=2_000)

        scores = build_transform("nscore", values).forward(values)

        assert abs(float(np.mean(scores))) < 0.1
        assert abs(float(np.std(scores)) - 1.0) < 0.15

    def test_normal_score_gives_ties_the_same_score(self) -> None:
        """A kriging that treated two equal measurements as different is one
        nobody can explain."""
        values = np.array([1.0, 1.0, 2.0, 3.0, 3.0, 4.0] * 10)

        scores = build_transform("nscore", values).forward(values)

        assert scores[0] == pytest.approx(scores[1])

    def test_log_lifts_data_that_touches_zero(self) -> None:
        values = np.array([0.0, 1.0, 2.0, 3.0])

        transform = build_transform("log", values)

        assert np.all(np.isfinite(transform.forward(values)))

    def test_log_leaves_positive_data_alone(self) -> None:
        """An offset applied unnecessarily changes the shape of the
        distribution for no reason."""
        values = np.array([1.0, 2.0, 3.0])

        assert build_transform("log", values).parameters()["offset"] == 0.0

    def test_boxcox_lambda_is_fitted_and_bounded(self) -> None:
        rng = np.random.default_rng(29)
        values = rng.lognormal(mean=1.0, sigma=0.5, size=500)

        lam = build_transform("boxcox", values).parameters()["lambda"]

        assert -2.0 <= lam <= 2.0

    def test_an_unknown_transform_names_the_real_ones(self) -> None:
        from webmap_geo.exceptions import DegenerateInput

        with pytest.raises(DegenerateInput, match="none, log, boxcox, nscore"):
            build_transform("sqrt", np.array([1.0, 2.0]))

    def test_the_lineage_can_reproduce_the_transform(self) -> None:
        """§4.6: the lineage record is the manifest, so the parameters have to
        be there."""
        rng = np.random.default_rng(31)
        values = rng.lognormal(size=200)

        assert "lambda" in build_transform("boxcox", values).parameters()
        assert "table_size" in build_transform("nscore", values).parameters()


class TestScreen:
    def test_flags_two_covariates_that_carry_the_same_information(self) -> None:
        """Proppant and fluid intensity are usually chosen together. The map
        will be fine; the coefficients will change between runs."""
        rng = np.random.default_rng(37)
        x, y, values = grid_samples()
        proppant = rng.normal(2_000, 300, size=len(values))
        fluid = proppant * 1.4 + rng.normal(0, 1.0, size=len(values))
        flags = FlagList()

        screen(
            samples_from(x, y, values, {"proppant": proppant, "fluid": fluid}),
            flags,
        )

        assert flags.has("COVAR_COLLINEAR")

    def test_says_nothing_about_independent_covariates(self) -> None:
        rng = np.random.default_rng(41)
        x, y, values = grid_samples()
        flags = FlagList()

        screen(
            samples_from(
                x,
                y,
                values,
                {
                    "proppant": rng.normal(2_000, 300, size=len(values)),
                    "thickness": rng.normal(120, 20, size=len(values)),
                },
            ),
            flags,
        )

        assert not flags.has("COVAR_COLLINEAR")

    def test_flags_a_covariate_that_is_really_a_location(self) -> None:
        """Vintage increasing eastward as development moved. The trend absorbs
        the spatial signal the kriging was supposed to model: the fit improves
        and the prediction does not."""
        x, y, values = grid_samples()
        vintage = 2015.0 + x / 2_000.0
        flags = FlagList()

        screen(samples_from(x, y, values, {"vintage": vintage}), flags)

        assert flags.has("COVAR_SPATIAL_PROXY")

    def test_flags_a_scenario_outside_the_data(self) -> None:
        """The model answers, and it answers confidently — which is the thing
        most likely to be believed."""
        rng = np.random.default_rng(43)
        x, y, values = grid_samples()
        proppant = rng.uniform(1_500, 2_500, size=len(values))
        flags = FlagList()

        screen(
            samples_from(x, y, values, {"proppant": proppant}),
            flags,
            scenario={"proppant": np.full(100, 6_000.0)},
        )

        assert flags.has("COVAR_EXTRAPOLATION")

    def test_says_nothing_about_a_scenario_inside_the_data(self) -> None:
        rng = np.random.default_rng(47)
        x, y, values = grid_samples()
        proppant = rng.uniform(1_500, 2_500, size=len(values))
        flags = FlagList()

        screen(
            samples_from(x, y, values, {"proppant": proppant}),
            flags,
            scenario={"proppant": np.full(100, 2_000.0)},
        )

        assert not flags.has("COVAR_EXTRAPOLATION")

    def test_modifies_nothing(self) -> None:
        """§5.4: all three checks produce flags and none modify data. Every one
        of them is a judgement a geologist may legitimately overrule."""
        rng = np.random.default_rng(53)
        x, y, values = grid_samples()
        proppant = rng.normal(2_000, 300, size=len(values))
        samples = samples_from(x, y, values, {"proppant": proppant})
        before = samples.covariates["proppant"].copy()

        screen(samples, FlagList())

        assert np.array_equal(samples.covariates["proppant"], before)
