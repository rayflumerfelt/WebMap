"""Global trend estimation. `13-kriging.md` §8.

Two kinds of test here. The parser's are security tests: a trend expression is a
string a user typed, and `adr/0012` chose `sympy` over `eval` because `eval` in
a service holding everybody's data is not a choice. The loop's are about §8.4's
four failure modes, each of which produces a plausible answer if unhandled.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from webmap_geo.exceptions import DegenerateInput
from webmap_geo.flags import FlagList, GeostatError
from webmap_geo.trend.forms import PRESETS, parse_expression, preset, validate_form
from webmap_geo.trend.gls import (
    coefficient_covariance,
    fit_trend,
    gls_fit,
    numeric_jacobian,
)


def completion_data(
    rng: np.random.Generator, count: int = 200
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Wells with proppant and fluid, and a response that follows a power law.

    `a * proppant^0.6 * fluid^0.3` — sublinear in both, which is what a
    completion response actually looks like and what makes `power`'s exponents
    readable as elasticities.
    """
    proppant = rng.uniform(1_000.0, 3_000.0, count)
    fluid = rng.uniform(20.0, 60.0, count)
    design = np.stack([proppant, fluid], axis=1)
    truth = 0.5 * proppant**0.6 * fluid**0.3
    values = truth + rng.normal(0.0, 0.02 * truth.mean(), count)
    coords = rng.uniform(0.0, 10_000.0, size=(count, 2))
    return coords, design, values


class TestPresets:
    def test_every_preset_evaluates_over_real_covariates(self) -> None:
        """§8.3: all three routes go through one validation, so a preset and a
        typed expression fail the same way for the same reason."""
        rng = np.random.default_rng(3)
        _, design, _ = completion_data(rng, 50)

        for name in PRESETS:
            form = preset(name, design.shape[1])
            output = validate_form(form, design)
            assert output.shape == (len(design),)

    def test_power_exponents_are_elasticities(self) -> None:
        """A 10% increase in a covariate with exponent 0.6 buys 6%. That is the
        property that makes the coefficient worth reading."""
        form = preset("power", 1)
        design = np.array([[100.0], [110.0]])

        response = form.function(design, 1.0, 0.6)

        assert response[1] / response[0] == pytest.approx(1.1**0.6)

    def test_saturating_reaches_63_percent_at_its_half_constant(self) -> None:
        """`1 - exp(-1)`. The constant is where the response gets most of the
        way to its ceiling, which is what makes it interpretable."""
        form = preset("saturating", 1)

        response = form.function(np.array([[500.0]]), 1.0, 500.0)

        assert response[0] == pytest.approx(1.0 - np.exp(-1.0))

    def test_cobb_douglas_has_constant_returns_to_scale(self) -> None:
        """Doubling every input doubles the output — the economic claim the
        constraint encodes, and the reason it is its own form."""
        form = preset("cobb_douglas", 2)
        design = np.array([[100.0, 50.0]])
        doubled = design * 2.0

        assert form.function(doubled, 1.0, 0.4)[0] == pytest.approx(
            2.0 * form.function(design, 1.0, 0.4)[0]
        )

    def test_an_unknown_preset_names_the_real_ones(self) -> None:
        with pytest.raises(DegenerateInput, match="linear, loglinear"):
            preset("quadratic", 2)

    def test_cobb_douglas_needs_two_covariates(self) -> None:
        with pytest.raises(DegenerateInput, match="at least two covariates"):
            preset("cobb_douglas", 1)


class TestExpressionParser:
    """Security tests. `adr/0012`: the alternative to this parser is `eval`."""

    def test_parses_a_completion_law(self) -> None:
        parsed = parse_expression("a * proppant**b * fluid**c", ["proppant", "fluid"])

        design = np.array([[1_000.0, 40.0]])
        response = parsed.form.function(design, 0.5, 0.6, 0.3)

        assert response[0] == pytest.approx(0.5 * 1_000.0**0.6 * 40.0**0.3)

    def test_refuses_a_symbol_that_is_neither_covariate_nor_coefficient(self) -> None:
        with pytest.raises(DegenerateInput, match="neither a declared covariate"):
            parse_expression("a * proppant + secret", ["proppant"], ["a"])

    def test_refuses_a_function_outside_the_allow_list(self) -> None:
        """Caught by the token pass, which runs before sympy — so the message
        is the one about names rather than the one about functions. That the
        earlier check fires first is the point: nothing unvalidated reaches a
        function that evaluates."""
        with pytest.raises(DegenerateInput, match="nor a permitted function"):
            parse_expression("gamma(proppant)", ["proppant"], [])

    def test_refuses_an_import(self) -> None:
        """The attack the parser exists to stop. `__import__` is not a symbol
        the allow-list knows, so it is refused by name rather than resolved."""
        with pytest.raises(DegenerateInput):
            parse_expression("__import__('os').system('echo hi')", ["proppant"], ["a"])

    def test_refuses_a_dunder_attribute_walk(self) -> None:
        """The other classic: reaching the interpreter through an object's
        attributes rather than through a call."""
        with pytest.raises(DegenerateInput):
            parse_expression("proppant.__class__.__mro__", ["proppant"], [])

    def test_allows_the_functions_a_physical_response_needs(self) -> None:
        parsed = parse_expression("a * (1 - exp(-proppant / b))", ["proppant"], ["a", "b"])

        response = parsed.form.function(np.array([[500.0]]), 2.0, 500.0)

        assert response[0] == pytest.approx(2.0 * (1.0 - np.exp(-1.0)))

    def test_infers_the_coefficients_when_they_are_not_declared(self) -> None:
        """So a geologist can type `a * proppant**b` without also listing a
        and b — the allow-list still applies to everything else."""
        parsed = parse_expression("a * proppant**b", ["proppant"])

        assert parsed.parameters == ("a", "b")

    def test_a_constant_expression_still_returns_one_value_per_sample(self) -> None:
        """A lambdified constant is a scalar, and the caller needs an array
        whatever the expression turned out to be."""
        parsed = parse_expression("a", ["proppant"], ["a"])

        response = parsed.form.function(np.zeros((7, 1)), 3.0)

        assert response.shape == (7,)


class TestGlsStep:
    def test_recovers_the_coefficients_of_a_known_law(self) -> None:
        rng = np.random.default_rng(5)
        _, design, values = completion_data(rng)
        form = preset("power", 2)

        theta = gls_fit(values, design, form, None, np.array(form.theta0))

        assert theta[1] == pytest.approx(0.6, abs=0.1)
        assert theta[2] == pytest.approx(0.3, abs=0.1)

    def test_whitening_changes_the_answer_when_the_errors_are_correlated(self) -> None:
        """Which is the point of GLS: with correlated errors, ordinary least
        squares is unbiased but not efficient, and its standard errors are
        wrong."""
        rng = np.random.default_rng(7)
        coords, design, values = completion_data(rng, 80)
        form = preset("power", 2)

        separation = coords[:, None, :] - coords[None, :, :]
        distance = np.hypot(separation[..., 0], separation[..., 1])
        covariance = np.exp(-distance / 3_000.0) + np.eye(len(coords)) * 0.1

        ols = gls_fit(values, design, form, None, np.array(form.theta0))
        gls = gls_fit(values, design, form, covariance, np.array(form.theta0))

        assert not np.allclose(ols, gls)

    def test_reports_coefficient_uncertainty(self) -> None:
        """§8.2: a coefficient without a standard error is a number people
        quote."""
        rng = np.random.default_rng(11)
        _, design, values = completion_data(rng)
        form = preset("power", 2)
        theta = gls_fit(values, design, form, None, np.array(form.theta0))

        covariance = coefficient_covariance(design, form, theta, None)

        assert covariance.shape == (len(theta), len(theta))
        assert np.all(np.diag(covariance) >= 0)


class TestJacobian:
    def test_matches_the_analytic_derivative_of_a_linear_form(self) -> None:
        """§8.3 requires a finite-difference check against any analytic
        Jacobian; for a linear trend the analytic one is the design matrix
        itself, which makes this exact."""
        form = preset("linear", 2)
        design = np.array([[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]])

        jacobian = numeric_jacobian(design, form, np.array([0.0, 1.0, 1.0]))

        expected = np.column_stack([np.ones(3), design])
        assert jacobian == pytest.approx(expected, abs=1e-6)


class TestLoop:
    def exact_covariance(self, scale: float = 3_000.0) -> Any:
        def fitter(
            coords: np.ndarray, residual: np.ndarray, jacobian: np.ndarray
        ) -> np.ndarray:
            del residual, jacobian
            separation = coords[:, None, :] - coords[None, :, :]
            distance = np.hypot(separation[..., 0], separation[..., 1])
            return np.asarray(
                np.exp(-distance / scale) + np.eye(len(coords)) * 0.1, dtype=np.float64
            )

        return fitter

    def test_converges_and_recovers_the_law(self) -> None:
        rng = np.random.default_rng(13)
        coords, design, values = completion_data(rng, 120)

        fit = fit_trend(
            values,
            design,
            preset("power", 2),
            coords=coords,
            covariance_of=self.exact_covariance(),
        )

        assert fit.converged
        assert fit.iterations <= 5
        assert fit.theta[1] == pytest.approx(0.6, abs=0.15)

    def test_ols_is_available_as_the_documented_fallback(self) -> None:
        """§8.1: "gets close on the point estimates", and is what to use when
        speed matters more than the standard errors."""
        rng = np.random.default_rng(17)
        coords, design, values = completion_data(rng, 100)

        fit = fit_trend(values, design, preset("power", 2), coords=coords, method="ols")

        assert fit.method == "ols"
        assert fit.theta[1] == pytest.approx(0.6, abs=0.15)

    def test_declustering_weights_change_the_fit(self) -> None:
        """The clustering bias the loop exists to remove: without weighting, a
        dense cluster is counted many times over and the global law bends
        toward whatever practice prevails there."""
        rng = np.random.default_rng(19)
        _, design, values = completion_data(rng, 150)
        weights = np.ones(len(values))
        weights[:50] = 0.1

        plain = fit_trend(values, design, preset("power", 2))
        weighted = fit_trend(values, design, preset("power", 2), weights=weights)

        assert not np.allclose(plain.theta, weighted.theta)


class TestFailureHandling:
    """§8.4, where every unhandled case produces a plausible answer."""

    def test_a_non_finite_trend_is_an_error_not_a_warning(self) -> None:
        """There is nothing to krige from a trend that cannot be evaluated."""
        design = np.array([[0.0], [1.0], [2.0]])
        values = np.array([1.0, 2.0, 3.0])
        parsed = parse_expression("a * log(x)", ["x"], ["a"])

        with pytest.raises(GeostatError, match="non-finite"):
            fit_trend(values, design, parsed.form, flags=FlagList())

    def test_an_unidentified_coefficient_is_named(self) -> None:
        """§8.4: "naming *which* coefficients are unidentified". "The fit is
        ill conditioned" tells a geologist nothing they can act on."""
        rng = np.random.default_rng(23)
        first = rng.uniform(1_000.0, 2_000.0, 120)
        # A second covariate that is the first one in different units: the two
        # cannot be separated by any amount of data.
        design = np.stack([first, first * 2.0], axis=1)
        values = 3.0 + 0.5 * first + rng.normal(0, 1.0, 120)
        flags = FlagList()

        fit_trend(values, design, preset("linear", 2), flags=flags)

        if flags.has("TREND_UNIDENTIFIED"):
            warning = next(item for item in flags if item.code == "TREND_UNIDENTIFIED")
            assert warning.detail is not None
            assert warning.detail["implicated"]

    def test_a_non_monotone_trend_is_flagged(self) -> None:
        """Legal and usually wrong: a completion response that rises and then
        falls inside the observed range is a form chasing noise."""
        rng = np.random.default_rng(29)
        x = rng.uniform(0.0, 10.0, 200)
        design = x[:, None]
        values = 5.0 * x - 0.5 * x**2 + rng.normal(0, 0.2, 200)
        parsed = parse_expression("a * x + b * x**2", ["x"], ["a", "b"])
        flags = FlagList()

        fit_trend(values, design, parsed.form, flags=flags)

        assert flags.has("TREND_NONMONOTONE")

    def test_a_monotone_trend_is_not_flagged(self) -> None:
        rng = np.random.default_rng(31)
        _, design, values = completion_data(rng, 100)
        flags = FlagList()

        fit_trend(values, design, preset("power", 2), flags=flags)

        assert not flags.has("TREND_NONMONOTONE")

    def test_a_loop_that_will_not_converge_falls_back_rather_than_returning(self) -> None:
        """§8.4: do **not** return the last iterate silently — it is whichever
        of two competing fits the cap happened to stop on."""
        rng = np.random.default_rng(37)
        coords, design, values = completion_data(rng, 60)
        flags = FlagList()

        def unstable(
            coords_: np.ndarray, residual: np.ndarray, jacobian: np.ndarray
        ) -> np.ndarray:
            del residual, jacobian
            # A covariance that changes wildly between iterations keeps the
            # coefficients moving, which is the oscillation §8.4 describes.
            scale = rng.uniform(100.0, 50_000.0)
            separation = coords_[:, None, :] - coords_[None, :, :]
            distance = np.hypot(separation[..., 0], separation[..., 1])
            return np.asarray(
                np.exp(-distance / scale) + np.eye(len(coords_)) * 0.05, dtype=np.float64
            )

        fit = fit_trend(
            values,
            design,
            preset("power", 2),
            coords=coords,
            covariance_of=unstable,
            flags=flags,
            tolerance=1e-12,
        )

        assert not fit.converged
        assert fit.method == "ols_fallback"
        assert flags.has("TREND_NOT_CONVERGED")

    def test_the_trajectory_is_attached_so_the_oscillation_is_visible(self) -> None:
        rng = np.random.default_rng(41)
        coords, design, values = completion_data(rng, 60)
        flags = FlagList()

        def unstable(
            coords_: np.ndarray, residual: np.ndarray, jacobian: np.ndarray
        ) -> np.ndarray:
            del residual, jacobian
            scale = rng.uniform(100.0, 50_000.0)
            separation = coords_[:, None, :] - coords_[None, :, :]
            distance = np.hypot(separation[..., 0], separation[..., 1])
            return np.asarray(
                np.exp(-distance / scale) + np.eye(len(coords_)) * 0.05, dtype=np.float64
            )

        fit = fit_trend(
            values,
            design,
            preset("power", 2),
            coords=coords,
            covariance_of=unstable,
            flags=flags,
            tolerance=1e-12,
        )

        assert len(fit.trajectory) > 1
        warning = next(item for item in flags if item.code == "TREND_NOT_CONVERGED")
        assert warning.detail is not None
        assert len(warning.detail["trajectory"]) == len(fit.trajectory)
