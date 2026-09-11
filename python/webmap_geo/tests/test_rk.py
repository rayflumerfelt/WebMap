"""Regression kriging, end to end. `13-kriging.md` §10.0, §6.

The first estimator that composes the whole stack, so these are recovery tests:
build a field from a known trend plus a known correlated residual, and ask
whether RK gets both back. The properties that matter are the ones a user reads
off the result — the coefficients, the surface at the control points, and
whether the variance says where the map is guessing.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from webmap_geo.estimators.rk import regression_kriging
from webmap_geo.flags import FlagList
from webmap_geo.frame import AnalysisFrame
from webmap_geo.grid import GridDefinition
from webmap_geo.prep.transform import build_transform
from webmap_geo.prep.validate import samples_from
from webmap_geo.trend.forms import preset
from webmap_geo.variogram.model import FittedVariogram

FRAME = AnalysisFrame(srid=2277, units="usft")
EXTENT = 10_000.0

#: The residual's true structure.
TRUTH = FittedVariogram(model="exponential", nugget=0.02, sill=0.5, range_=3_000.0)

#: The true completion law: sublinear in proppant, as one is.
TRUE_SCALE = 0.5
TRUE_EXPONENT = 0.6


def correlated_field(rng: np.random.Generator, points: np.ndarray) -> np.ndarray:
    """A residual with exactly TRUTH's covariance."""
    separation = points[:, None, :] - points[None, :, :]
    distance = np.hypot(separation[..., 0], separation[..., 1])
    covariance = TRUTH.covariance(distance) + np.eye(len(points)) * 1e-8
    return np.linalg.cholesky(covariance) @ rng.standard_normal(len(points))


def scenario(count: int = 150, seed: int = 20260911) -> tuple[Any, Any, Any]:
    """Wells with a proppant covariate, a power-law response and a correlated
    residual — the shape of a real completion-response problem."""
    rng = np.random.default_rng(seed)
    points = rng.uniform(0.0, EXTENT, size=(count, 2))
    proppant = rng.uniform(1_000.0, 3_000.0, count)

    trend = TRUE_SCALE * proppant**TRUE_EXPONENT
    values = trend + correlated_field(rng, points)

    samples = samples_from(points[:, 0], points[:, 1], values, {"proppant": proppant})
    design = proppant[:, None]
    return samples, design, rng


def grid(nx: int = 20, ny: int = 20) -> GridDefinition:
    return GridDefinition(xmin=0.0, ymin=0.0, cell_size=EXTENT / nx, nx=nx, ny=ny, frame=FRAME)


def node_design(target: GridDefinition, proppant: float = 2_000.0) -> np.ndarray:
    """The scenario being mapped: one completion design, everywhere.

    Separate from the sample design because they are separate decisions — what
    happened, and what is being asked about.
    """
    return np.full((target.n_cells, 1), proppant)


class TestRecovery:
    def test_recovers_the_completion_law(self) -> None:
        """The coefficient a geologist reads off the run and argues with."""
        samples, design, rng = scenario()
        target = grid()

        result = regression_kriging(
            samples,
            design,
            node_design(target),
            target,
            preset("power", 1),
            rng=rng,
        )

        assert result.trend.theta[1] == pytest.approx(TRUE_EXPONENT, abs=0.15)

    def test_recovers_the_residual_structure(self) -> None:
        """Through REML, per `adr/0011`. A range recovered an order of
        magnitude short would mean the residual path had gone through the
        least-squares fitter."""
        samples, design, rng = scenario()
        target = grid()

        result = regression_kriging(
            samples, design, node_design(target), target, preset("power", 1), rng=rng
        )

        assert 0.4 * TRUTH.range_ < result.variogram.range_ < 2.5 * TRUTH.range_

    def test_the_surface_is_the_trend_plus_the_kriged_residual(self) -> None:
        """§10.0 step 3, asserted rather than assumed: the two surfaces come
        back separately so a reader can see which part of the map is the law and
        which is the geology."""
        samples, design, rng = scenario()
        target = grid()

        result = regression_kriging(
            samples, design, node_design(target), target, preset("power", 1), rng=rng
        )

        usable = np.isfinite(result.residual_surface)
        assert result.estimate[usable] == pytest.approx(
            (result.trend_surface + result.residual_surface)[usable]
        )

    def test_the_variance_is_lowest_near_control(self) -> None:
        samples, design, rng = scenario()
        target = grid()

        result = regression_kriging(
            samples, design, node_design(target), target, preset("power", 1), rng=rng
        )

        from scipy.spatial import cKDTree

        distance, _ = cKDTree(np.stack([samples.x, samples.y], axis=1)).query(
            target.cell_centres()
        )
        variance = result.variance.ravel()
        usable = np.isfinite(variance)

        # Quartiles of the actual distance distribution rather than absolute
        # thresholds: with 150 wells on a 10,000 ft square nothing is 1,200 ft
        # from control, and a fixed number makes this a test of the spacing.
        close, distant = np.percentile(distance[usable], [25, 75])
        near = variance[usable][distance[usable] <= close]
        far = variance[usable][distance[usable] >= distant]
        assert near.size and far.size
        assert float(near.mean()) < float(far.mean())


class TestScenario:
    def test_a_different_completion_design_moves_the_whole_map(self) -> None:
        """The reason RK exists: the covariate is a *decision*, and asking what
        a bigger job would produce is the question the method answers."""
        samples, design, _ = scenario()
        target = grid()

        small = regression_kriging(
            samples,
            design,
            node_design(target, 1_200.0),
            target,
            preset("power", 1),
            rng=np.random.default_rng(1),
        )
        large = regression_kriging(
            samples,
            design,
            node_design(target, 2_800.0),
            target,
            preset("power", 1),
            rng=np.random.default_rng(1),
        )

        assert float(np.nanmean(large.estimate)) > float(np.nanmean(small.estimate))

    def test_the_residual_surface_does_not_move_with_the_scenario(self) -> None:
        """Because the residual is what the covariates did *not* explain. If it
        moved, the trend and the residual would not be separable and the whole
        decomposition would be decorative."""
        samples, design, _ = scenario()
        target = grid()

        small = regression_kriging(
            samples,
            design,
            node_design(target, 1_200.0),
            target,
            preset("power", 1),
            rng=np.random.default_rng(1),
        )
        large = regression_kriging(
            samples,
            design,
            node_design(target, 2_800.0),
            target,
            preset("power", 1),
            rng=np.random.default_rng(1),
        )

        usable = np.isfinite(small.residual_surface)
        assert small.residual_surface[usable] == pytest.approx(large.residual_surface[usable])


class TestVariance:
    def test_trend_uncertainty_is_off_by_default(self) -> None:
        """§11.2: it is a first-order approximation for a nonlinear `f`, and
        the whole point of reporting a variance is that somebody believes it."""
        samples, design, rng = scenario()
        target = grid()

        result = regression_kriging(
            samples, design, node_design(target), target, preset("power", 1), rng=rng
        )

        assert not result.trend_uncertainty_included

    def test_including_it_only_adds(self) -> None:
        """`g' cov(theta) g` is a quadratic form in a covariance matrix, so it
        cannot be negative — a variance that fell when uncertainty was added
        would mean the term had the wrong sign."""
        samples, design, _ = scenario()
        target = grid()

        without = regression_kriging(
            samples,
            design,
            node_design(target),
            target,
            preset("power", 1),
            rng=np.random.default_rng(3),
        )
        with_trend = regression_kriging(
            samples,
            design,
            node_design(target),
            target,
            preset("power", 1),
            rng=np.random.default_rng(3),
            include_trend_uncertainty=True,
        )

        usable = np.isfinite(without.variance)
        assert np.all(with_trend.variance[usable] >= without.variance[usable] - 1e-9)
        assert with_trend.trend_uncertainty_included


class TestFlags:
    def test_a_structureless_residual_is_flagged(self) -> None:
        """§7.5: above 60% nugget the map is close to the trend alone. That is a
        legitimate answer and not the one most people assume they are getting
        from a kriged map."""
        rng = np.random.default_rng(7)
        count = 120
        points = rng.uniform(0.0, EXTENT, size=(count, 2))
        proppant = rng.uniform(1_000.0, 3_000.0, count)
        # Pure noise for a residual: no spatial structure at all.
        values = TRUE_SCALE * proppant**TRUE_EXPONENT + rng.normal(0.0, 2.0, count)

        samples = samples_from(points[:, 0], points[:, 1], values, {"proppant": proppant})
        target = grid(10, 10)
        flags = FlagList()

        regression_kriging(
            samples,
            proppant[:, None],
            node_design(target),
            target,
            preset("power", 1),
            rng=rng,
            flags=flags,
        )

        assert flags.has("HIGH_NUGGET")

    def test_the_lineage_carries_what_it_takes_to_re_run(self) -> None:
        """§4.6: there is no run manifest — the lineage record is it."""
        samples, design, rng = scenario()
        target = grid()

        result = regression_kriging(
            samples, design, node_design(target), target, preset("power", 1), rng=rng
        )
        lineage = result.lineage()

        assert lineage["estimator"] == "rk"
        assert len(lineage["trend"]["theta"]) == 2
        assert lineage["variogram"]["range"] > 0
        assert "flags" in lineage


class TestTransform:
    def test_a_log_target_comes_back_in_its_own_units(self) -> None:
        """§6: the estimate is back-transformed as a *quantile* — the median —
        which is exact under any monotone transform. A mean would not be."""
        rng = np.random.default_rng(11)
        count = 150
        points = rng.uniform(0.0, EXTENT, size=(count, 2))
        proppant = rng.uniform(1_000.0, 3_000.0, count)
        values = np.exp(1.0 + 0.0005 * proppant + correlated_field(rng, points) * 0.2)

        samples = samples_from(points[:, 0], points[:, 1], values, {"proppant": proppant})
        target = grid(10, 10)

        result = regression_kriging(
            samples,
            proppant[:, None],
            node_design(target),
            target,
            preset("linear", 1),
            transform=build_transform("log", values),
            rng=rng,
        )

        finite = result.estimate[np.isfinite(result.estimate)]
        assert finite.size
        # Back in the target's own units: positive, and on the order of the data
        # rather than of its logarithm.
        assert float(finite.min()) > 0.0
        assert float(np.median(finite)) == pytest.approx(float(np.median(values)), rel=1.0)
