"""Fitting a variogram to a stored point layer. `05-geoprocessing.md` §6.3.

**Read-only and synchronous.** It creates nothing, and `05` §10 budgets the fit
at under 5 seconds for 500k points on a 20k subsample — inside `10` §6's
threshold for running inline. That matters more than it sounds: "what is the
range on this layer?" is a question asked while deciding whether to grid at
all, and an answer that arrives through a poll cycle is an answer nobody waits
for.

Kriging weights come entirely from the variogram, so kriging without one is
kriging with made-up parameters — and the made-up answer looks exactly as
plausible as the real one. This is the tool that makes the real one cheap to
get.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from webmap_core.logging import get_logger
from webmap_core.permissions import Principal
from webmap_core.services.datasets import resolve_feature_object

log = get_logger(__name__)

#: The seed used when the caller does not supply one. Fixed rather than
#: random: the subsample drives the fit, so an unseeded run gives a different
#: range every time it is asked, and a geologist comparing two answers cannot
#: tell whether the data changed or the dice did. Recorded in the response.
DEFAULT_SEED = 20260101

#: Below this a variogram is noise dressed as structure. Twenty points give
#: 190 pairs spread over every lag, and the fitted range from that is a number
#: with no information in it.
MIN_POINTS = 30


@dataclass(frozen=True)
class VariogramRequest:
    dataset_id: UUID
    value_column: str
    model: str | None = None
    detect_anisotropy: bool = True
    n_lags: int = 20
    subsample: int | None = None
    seed: int = DEFAULT_SEED
    where: str | None = None


async def fit_for_dataset(
    conn: AsyncConnection,
    principal: Principal,
    request: VariogramRequest,
    *,
    object_store: Any,
    bucket: str,
) -> dict[str, Any]:
    """Estimate and fit, returning everything needed to judge the fit.

    The numbers a caller needs are not just the model's parameters. The pair
    count and the residual say how much to trust them, and the
    nugget-to-sill ratio says whether there is spatial structure to exploit at
    all — a layer at 0.8 will krige to something very close to its own mean
    wherever control is sparse, and the fitted range is then a decoration.
    """
    import numpy as np

    from webmap_geo.control import read_control_points
    from webmap_geo.crs import frame_for
    from webmap_geo.exceptions import DegenerateInput
    from webmap_geo.variogram import estimate_experimental, fit, fit_auto

    parquet_key, _version = await resolve_feature_object(conn, principal, request.dataset_id)
    row = (
        await conn.execute(
            text("SELECT name, storage_srid FROM dataset WHERE id = :id"),
            {"id": request.dataset_id},
        )
    ).one()
    frame = frame_for(int(row.storage_srid))

    control = read_control_points(
        f"s3://{bucket}/{parquet_key}",
        request.value_column,
        frame,
        object_store,
        where=request.where,
    )
    if len(control) < MIN_POINTS:
        raise DegenerateInput(
            f"'{row.name}' has {len(control)} usable points for "
            f"'{request.value_column}' (minimum {MIN_POINTS}). A variogram from "
            f"fewer is noise with a curve through it — the fitted range would "
            f"look like a measurement and carry no information."
        )

    rng = np.random.default_rng(request.seed)
    fitted = fit_auto(
        control.coords,
        control.values,
        model=request.model,
        detect_anisotropy=request.detect_anisotropy,
        n_lags=request.n_lags,
        rng=rng,
    )

    # Re-estimated for the returned points rather than reusing the fit's own,
    # because `fit_auto` re-estimates internally when anisotropy is applied and
    # the caller should see the curve the reported model was fitted to.
    experimental = estimate_experimental(
        control.coords,
        control.values,
        n_lags=request.n_lags,
        anisotropy_ratio=fitted.anisotropy_ratio,
        anisotropy_angle=fitted.anisotropy_angle,
        rng=np.random.default_rng(request.seed),
    )
    if request.model is not None:
        # An explicitly requested model is honoured even when another fits
        # better — matching a partner's map is a legitimate reason to ask.
        fitted = fit(experimental, request.model)

    total = fitted.nugget + fitted.sill
    return {
        "dataset_id": str(request.dataset_id),
        "dataset_name": str(row.name),
        "value_column": request.value_column,
        "n_points": len(control),
        "model": fitted.model,
        "nugget": fitted.nugget,
        "sill": fitted.sill,
        "range": fitted.range_,
        "anisotropy_ratio": fitted.anisotropy_ratio,
        "anisotropy_angle": fitted.anisotropy_angle,
        "fit_residual": fitted.fit_residual,
        "n_pairs_used": fitted.n_pairs_used,
        "nugget_ratio": (fitted.nugget / total) if total > 0 else 0.0,
        "units": frame.units,
        "srid": int(row.storage_srid),
        "seed": request.seed,
        "lags": [
            {"distance": float(d), "semivariance": float(g), "n_pairs": int(n)}
            for d, g, n in zip(
                experimental.lags,
                experimental.gamma,
                experimental.counts,
                strict=True,
            )
        ],
        "warnings": _warnings(fitted, len(control), frame.units),
    }


def _warnings(fitted: Any, n_points: int, units: str) -> list[str]:
    """The three things that make a fitted variogram untrustworthy.

    Returned rather than raised: each of these produces a usable model that
    should be read with a caveat, not a failure. A caller that ignores them
    gets a grid; a caller that reads them gets a better one.
    """
    notes: list[str] = []
    total = fitted.nugget + fitted.sill

    if total > 0 and fitted.nugget / total > 0.6:
        notes.append(
            f"Nugget is {fitted.nugget / total:.0%} of the total variance. There is "
            f"little spatial structure here to exploit — a kriged surface will sit "
            f"close to the layer's mean wherever control is sparse, and the range "
            f"below is close to decoration."
        )
    if fitted.anisotropy_ratio > 1.0:
        notes.append(
            f"Anisotropy {fitted.anisotropy_ratio:.2f}:1 along "
            f"{fitted.anisotropy_angle:.0f}°. The range quoted is the major "
            f"axis; across strike it is roughly "
            f"{fitted.range_ / fitted.anisotropy_ratio:,.0f} {units}."
        )
    if fitted.model == "power":
        # A power variogram has no sill: semivariance rises without bound, which
        # is the signature of a regional trend rather than stationary structure.
        # Ordinary kriging assumes stationarity, so it will underestimate
        # uncertainty away from control and pull toward a local mean that is not
        # the right one. Worth saying out loud because the grid still looks fine.
        notes.append(
            "A power model fits best, which means semivariance keeps rising with "
            "distance rather than levelling off — the signature of a regional "
            "trend (a basin dipping across the area, say) rather than stationary "
            "structure. Ordinary kriging assumes stationarity and will pull "
            "toward a local mean away from control. Minimum curvature handles a "
            "trending surface better, and it is the fault-aware method as well."
        )
    if n_points < 100:
        notes.append(
            f"Fitted from {n_points} points. Below about a hundred the fit is "
            f"sensitive to which points happen to be there, so treat the range as "
            f"an order of magnitude rather than a measurement."
        )
    return notes


__all__ = ["DEFAULT_SEED", "MIN_POINTS", "VariogramRequest", "fit_for_dataset"]
