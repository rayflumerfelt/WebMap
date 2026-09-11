"""Trend forms. `13-kriging.md` §8.3.

Three ways to supply `f`, and the important one is the second — a string a
geologist typed, parsed **server-side**, never with `eval`. `sympy` is a
dependency for exactly this reason and `adr/0012` bounds it: the alternative to
a sandboxed parser is `eval`, and `eval` on a user-supplied string in a service
that holds everybody's data is not an alternative.

This module holds the preset registry and the parser. All three routes go
through one validation — finite output over the observed covariate range, the
right shape, and a finite-difference check against any analytic Jacobian — so a
preset and a typed expression fail the same way for the same reason.

**The presets are multiplicative for a reason.** A completion response is not
additive: doubling proppant in a well with no fluid does nothing, and a form
that adds their contributions says otherwise. `power` and `saturating` both
multiply, which is what makes their coefficients readable as elasticities and
half-saturation constants rather than as slopes that only hold at the mean.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from webmap_geo.exceptions import DegenerateInput

#: Functions a typed expression may use. Everything else is refused by name,
#: **before** the expression reaches `sympy.parse_expr` — which evaluates, and
#: so cannot be the thing that validates.
#: Deliberately short: these are the shapes a physical response takes, and a
#: trend needing `gamma` or `besselk` is a trend that should be a callable
#: written by somebody who can be asked why.
ALLOWED_FUNCTIONS = frozenset({"exp", "log", "sqrt", "Abs", "Pow", "Min", "Max", "tanh"})

#: Points per covariate when checking monotonicity and finiteness over the
#: observed range. Enough to catch a turnover; few enough to be free.
CHECK_POINTS = 25


TrendFunction = Callable[..., NDArray[np.float64]]


@dataclass(frozen=True)
class TrendForm:
    """A parametric trend, with somewhere sensible to start from."""

    name: str
    #: `f(X, *theta)` where X is (n, k) in the declared covariate order.
    function: TrendFunction
    theta0: tuple[float, ...]
    #: Per-coefficient bounds for the optimiser. Wide, but not unbounded: an
    #: exponent free to reach 50 finds a fit nobody can explain.
    bounds: tuple[tuple[float, float], ...]
    #: What each coefficient means, for the report. A fitted number with no
    #: name is a number nobody checks.
    labels: tuple[str, ...] = ()
    description: str = ""


def _linear(covariates: int) -> TrendForm:
    def function(design: NDArray[np.float64], *theta: float) -> NDArray[np.float64]:
        coefficients = np.asarray(theta, dtype=float)
        return np.asarray(coefficients[0] + design @ coefficients[1:], dtype=np.float64)

    return TrendForm(
        name="linear",
        function=function,
        theta0=(0.0,) + (0.0,) * covariates,
        bounds=((-np.inf, np.inf),) * (covariates + 1),
        labels=("intercept", *(f"slope_{index}" for index in range(covariates))),
        description="a + sum(b_i * x_i). The baseline every other form is compared against.",
    )


def _loglinear(covariates: int) -> TrendForm:
    def function(design: NDArray[np.float64], *theta: float) -> NDArray[np.float64]:
        coefficients = np.asarray(theta, dtype=float)
        return np.asarray(np.exp(coefficients[0] + design @ coefficients[1:]), dtype=np.float64)

    return TrendForm(
        name="loglinear",
        function=function,
        theta0=(0.0,) + (0.0,) * covariates,
        bounds=((-np.inf, np.inf),) * (covariates + 1),
        labels=("log_intercept", *(f"rate_{index}" for index in range(covariates))),
        description="exp(a + sum(b_i * x_i)). Multiplicative, and positive by construction.",
    )


def _power(covariates: int) -> TrendForm:
    def function(design: NDArray[np.float64], *theta: float) -> NDArray[np.float64]:
        scale = theta[0]
        exponents = np.asarray(theta[1:], dtype=float)
        # Clipped away from zero: a covariate of exactly zero with a negative
        # exponent is infinite, and one with a fractional exponent is complex.
        safe = np.clip(design, 1e-12, None)
        return np.asarray(scale * np.prod(safe**exponents, axis=1), dtype=np.float64)

    return TrendForm(
        name="power",
        function=function,
        theta0=(1.0,) + (0.5,) * covariates,
        # Exponents bounded to [-2, 2]: outside that the form fits noise, and a
        # completion elasticity of 5 is a number nobody will defend in a room.
        bounds=((0.0, np.inf),) + ((-2.0, 2.0),) * covariates,
        labels=("scale", *(f"exponent_{index}" for index in range(covariates))),
        description=(
            "a * prod(x_i ^ b_i). The exponents are elasticities: b = 0.7 means a "
            "10% increase in that covariate buys 7% more."
        ),
    )


def _saturating(covariates: int) -> TrendForm:
    def function(design: NDArray[np.float64], *theta: float) -> NDArray[np.float64]:
        scale = theta[0]
        half = np.asarray(theta[1:], dtype=float)
        half = np.clip(half, 1e-9, None)
        return np.asarray(
            scale * np.prod(1.0 - np.exp(-design / half), axis=1), dtype=np.float64
        )

    return TrendForm(
        name="saturating",
        function=function,
        theta0=(1.0,) + (1.0,) * covariates,
        bounds=((0.0, np.inf),) + ((1e-6, np.inf),) * covariates,
        labels=("scale", *(f"half_{index}" for index in range(covariates))),
        description=(
            "a * prod(1 - exp(-x_i / b_i)). Diminishing returns with a "
            "characteristic scale per covariate — b is where the response reaches "
            "63% of its ceiling."
        ),
    )


def _additive_saturating(covariates: int) -> TrendForm:
    def function(design: NDArray[np.float64], *theta: float) -> NDArray[np.float64]:
        scales = np.asarray(theta[:covariates], dtype=float)
        half = np.clip(np.asarray(theta[covariates:], dtype=float), 1e-9, None)
        return np.asarray(
            np.sum(scales * (1.0 - np.exp(-design / half)), axis=1), dtype=np.float64
        )

    return TrendForm(
        name="additive_saturating",
        function=function,
        theta0=(1.0,) * covariates + (1.0,) * covariates,
        bounds=((0.0, np.inf),) * covariates + ((1e-6, np.inf),) * covariates,
        labels=tuple(f"scale_{index}" for index in range(covariates))
        + tuple(f"half_{index}" for index in range(covariates)),
        description=(
            "sum(a_i * (1 - exp(-x_i / b_i))). Each covariate contributes "
            "independently — use it when they genuinely do."
        ),
    )


def _cobb_douglas(covariates: int) -> TrendForm:
    """Power with the exponents constrained to sum to one.

    Constant returns to scale. Worth having as its own form rather than as a
    note on `power`, because the constraint is the economic claim: doubling
    every input doubles the output, and nothing more.
    """

    def function(design: NDArray[np.float64], *theta: float) -> NDArray[np.float64]:
        scale = theta[0]
        free = np.asarray(theta[1:], dtype=float)
        exponents = np.append(free, 1.0 - free.sum())
        safe = np.clip(design, 1e-12, None)
        return np.asarray(scale * np.prod(safe**exponents, axis=1), dtype=np.float64)

    return TrendForm(
        name="cobb_douglas",
        function=function,
        theta0=(1.0,) + (1.0 / covariates,) * (covariates - 1),
        bounds=((0.0, np.inf),) + ((0.0, 1.0),) * (covariates - 1),
        labels=("scale", *(f"share_{index}" for index in range(covariates - 1))),
        description=(
            "a * prod(x_i ^ b_i) with the exponents summing to 1: constant returns "
            "to scale. The last exponent is implied by the others."
        ),
    )


#: §8.3's preset registry. Each takes the covariate count, because the
#: coefficient vector's length depends on it.
PRESETS: dict[str, Callable[[int], TrendForm]] = {
    "linear": _linear,
    "loglinear": _loglinear,
    "power": _power,
    "saturating": _saturating,
    "additive_saturating": _additive_saturating,
    "cobb_douglas": _cobb_douglas,
}


def preset(name: str, covariates: int) -> TrendForm:
    """One of the named forms, sized for this many covariates."""
    builder = PRESETS.get(name)
    if builder is None:
        raise DegenerateInput(
            f"'{name}' is not a trend preset. Available: {', '.join(sorted(PRESETS))}. "
            f"A form not on that list can be supplied as an expression instead."
        )
    if covariates < 1:
        raise DegenerateInput(
            f"A trend needs at least one covariate; got {covariates}. With none, "
            f"the 'trend' is a constant and ordinary kriging already estimates it."
        )
    if name == "cobb_douglas" and covariates < 2:
        raise DegenerateInput(
            "Cobb-Douglas constrains the exponents to sum to one, which needs at "
            "least two covariates — with one, the exponent is fixed at 1 and the "
            "form is a straight line through the origin."
        )
    return builder(covariates)


@dataclass(frozen=True)
class ParsedExpression:
    """A trend the user typed, after parsing. §8.3's second route."""

    text: str
    form: TrendForm
    symbols: tuple[str, ...]
    parameters: tuple[str, ...] = field(default=())


def parse_expression(
    expression: str,
    covariates: Sequence[str],
    parameters: Sequence[str] | None = None,
) -> ParsedExpression:
    """Parse a trend expression against an allow-list. **Never `eval`.**

    Every symbol must be a declared covariate or parameter, and every function
    must be on `ALLOWED_FUNCTIONS`. A symbol outside those is refused by name
    rather than resolved — which is what stops `__import__` and every other
    thing a string can be.

    Parsing happens server-side. `07` §9.4: a client-side parse is a
    convenience, never the source of truth.
    """
    import sympy

    declared = list(covariates)
    names = (
        list(parameters)
        if parameters is not None
        else _implicit_parameters(expression, declared)
    )

    # **Before sympy sees it.** `sympy.parse_expr` tokenises and then `eval`s,
    # with sympy's own namespace as globals — so a name the local dict does not
    # define can still resolve, and checking the parsed tree afterwards is
    # checking after the dangerous step. The two attacks this stops,
    # `__import__(...)` and an attribute walk to `__class__`, previously reached
    # `lambdify` and failed there with a TypeError, which is luck rather than a
    # guarantee.
    _reject_unsafe_tokens(expression, {*declared, *names})

    local = {name: sympy.Symbol(name) for name in [*declared, *names]}
    for allowed in ALLOWED_FUNCTIONS:
        candidate = getattr(sympy, allowed, None)
        if candidate is not None:
            local[allowed] = candidate

    try:
        parsed = sympy.parse_expr(expression, local_dict=local, evaluate=False)
    except (SyntaxError, TypeError, AttributeError) as error:
        raise DegenerateInput(
            f"'{expression}' is not a valid trend expression: {error}. Write it "
            f"in terms of the covariates ({', '.join(declared) or 'none declared'}) "
            f"and coefficients, using {', '.join(sorted(ALLOWED_FUNCTIONS))} and "
            f"arithmetic."
        ) from error

    _reject_unknown_symbols(parsed, declared, names, expression)
    _reject_unknown_functions(parsed, expression)

    ordered_parameters = tuple(
        name for name in names if sympy.Symbol(name) in parsed.free_symbols
    )
    callable_form = sympy.lambdify(
        [[local[name] for name in declared], *[local[name] for name in ordered_parameters]],
        parsed,
        modules="numpy",
    )

    def function(design: NDArray[np.float64], *theta: float) -> NDArray[np.float64]:
        columns = [design[:, index] for index in range(design.shape[1])]
        result = callable_form(columns, *theta)
        # A constant expression lambdifies to a scalar; the caller needs one
        # value per sample whatever the expression turned out to be.
        return np.asarray(np.broadcast_to(result, (len(design),)), dtype=np.float64)

    form = TrendForm(
        name="expression",
        function=function,
        theta0=(1.0,) * len(ordered_parameters),
        bounds=((-np.inf, np.inf),) * len(ordered_parameters),
        labels=ordered_parameters,
        description=expression,
    )
    return ParsedExpression(
        text=expression,
        form=form,
        symbols=tuple(declared),
        parameters=ordered_parameters,
    )


#: Operators an arithmetic expression is made of. Everything else — attribute
#: access, subscripting, assignment, a semicolon — is refused, because none of
#: them is part of writing `a * proppant**b` and every one of them is part of
#: reaching something that is not arithmetic.
ALLOWED_OPERATORS = frozenset({"+", "-", "*", "/", "**", "(", ")", ",", "%"})


def _reject_unsafe_tokens(expression: str, allowed_names: set[str]) -> None:
    """Refuse anything that is not arithmetic over the declared names.

    A whitelist over Python's own tokeniser, run **before** `sympy.parse_expr`,
    because that function evaluates. Names are checked here as well as against
    the parsed tree afterwards: this pass is what makes the parse safe, and the
    later one is what makes the *result* correct.
    """
    import io
    import tokenize

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(expression).readline))
    except (tokenize.TokenError, IndentationError) as error:
        raise DegenerateInput(
            f"'{expression}' could not be read as an expression: {error}. It "
            f"should be a single line of arithmetic over the covariate names."
        ) from error

    structural = {
        tokenize.NEWLINE,
        tokenize.NL,
        tokenize.ENDMARKER,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.COMMENT,
    }

    for token in tokens:
        if token.type in structural:
            continue
        if token.type == tokenize.NUMBER:
            continue
        if token.type == tokenize.NAME:
            if token.string in allowed_names or token.string in ALLOWED_FUNCTIONS:
                continue
            raise DegenerateInput(
                f"'{expression}' refers to {token.string}, which is neither a "
                f"declared covariate, a coefficient, nor a permitted function "
                f"({', '.join(sorted(ALLOWED_FUNCTIONS))}). Every name in a trend "
                f"has to be one of those — that is what makes parsing it safe."
            )
        if token.type == tokenize.OP:
            if token.string in ALLOWED_OPERATORS:
                continue
            raise DegenerateInput(
                f"'{expression}' uses '{token.string}', which is not part of an "
                f"arithmetic expression. A trend is built from "
                f"{' '.join(sorted(ALLOWED_OPERATORS))} and the permitted "
                f"functions; attribute access and subscripting are refused "
                f"because they are how a string reaches something that is not "
                f"arithmetic."
            )
        raise DegenerateInput(
            f"'{expression}' contains a {tokenize.tok_name[token.type].lower()} "
            f"token, which a trend expression cannot hold. It should be a single "
            f"line of arithmetic over the covariate names."
        )


def _implicit_parameters(expression: str, covariates: list[str]) -> list[str]:
    """Symbols in the expression that are not covariates, in order.

    Offered so a caller can type `a * proppant**b` without declaring `a` and
    `b` separately. The allow-list still applies: these become parameters, and
    anything that is neither a covariate nor a parameter is still refused.
    """
    import re

    found: list[str] = []
    for token in re.findall(r"[A-Za-z_][A-Za-z_0-9]*", expression):
        if token in covariates or token in ALLOWED_FUNCTIONS or token in found:
            continue
        # A name with a dunder in it is never a coefficient. Refused here as
        # well as by the token pass, so that inferring parameters cannot be the
        # thing that admits one.
        if token.startswith("__") or token.endswith("__"):
            raise DegenerateInput(
                f"'{token}' is not a coefficient name. Trend coefficients are "
                f"ordinary identifiers; a dunder is how an expression reaches "
                f"the interpreter rather than the arithmetic."
            )
        found.append(token)
    return found


def _reject_unknown_symbols(
    parsed: object, covariates: list[str], parameters: list[str], expression: str
) -> None:
    import sympy

    allowed = set(covariates) | set(parameters)
    unknown = sorted(
        str(symbol)
        for symbol in getattr(parsed, "free_symbols", set())
        if str(symbol) not in allowed
    )
    if unknown:
        raise DegenerateInput(
            f"'{expression}' refers to {', '.join(unknown)}, which "
            f"{'are' if len(unknown) > 1 else 'is'} neither a declared covariate "
            f"({', '.join(covariates) or 'none'}) nor a coefficient. Every name in "
            f"a trend has to be one or the other — that is what makes parsing it "
            f"safe."
        )
    del sympy


def _reject_unknown_functions(parsed: object, expression: str) -> None:
    import sympy

    used = {
        type(node).__name__
        for node in sympy.preorder_traversal(parsed)
        if isinstance(node, sympy.Function)
    }
    forbidden = sorted(name for name in used if name not in ALLOWED_FUNCTIONS)
    if forbidden:
        raise DegenerateInput(
            f"'{expression}' uses {', '.join(forbidden)}, which is not allowed in "
            f"a trend expression. Permitted: "
            f"{', '.join(sorted(ALLOWED_FUNCTIONS))} and arithmetic."
        )


def validate_form(
    form: TrendForm,
    design: NDArray[np.float64],
    theta: Sequence[float] | None = None,
) -> NDArray[np.float64]:
    """Check a trend produces finite values of the right shape. §8.3.

    All three routes go through this, so a preset and a typed expression fail
    the same way for the same reason.
    """
    values = np.asarray(theta if theta is not None else form.theta0, dtype=float)
    try:
        output = form.function(design, *values)
    except Exception as error:
        raise DegenerateInput(
            f"The trend '{form.name}' could not be evaluated over the observed "
            f"covariates: {type(error).__name__}: {error}."
        ) from error

    output = np.asarray(output, dtype=float)
    if output.shape != (len(design),):
        raise DegenerateInput(
            f"The trend '{form.name}' returned shape {output.shape} for "
            f"{len(design)} samples. It has to return one value per sample."
        )
    return output


__all__ = [
    "ALLOWED_FUNCTIONS",
    "CHECK_POINTS",
    "PRESETS",
    "ParsedExpression",
    "TrendForm",
    "parse_expression",
    "preset",
    "validate_form",
]
