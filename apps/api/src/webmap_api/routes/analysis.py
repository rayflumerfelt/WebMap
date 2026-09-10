"""Synchronous analysis. `05-geoprocessing.md` §6.3, `10-jobs-async.md` §6.

Everything here answers in under a few seconds and creates nothing, which is
what puts it on the request path rather than in the queue. `10` §6 sets the
threshold deliberately low — blocking an API worker for five seconds is
acceptable and thirty is not, because it starves other requests on the same
process — and a variogram fit is budgeted at under five (`05` §10).

The alternative was a job, and it would have been the wrong call for the
question this answers. "What is the range on this layer?" is asked while
deciding whether to grid at all, and an answer that arrives through a poll
cycle is an answer nobody waits for.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import Field

from webmap_api.dependencies import AppSettings, CurrentPrincipal, ScopedConn
from webmap_core.logging import get_logger
from webmap_core.models import WebMapModel
from webmap_core.services import variograms as service

log = get_logger(__name__)
router = APIRouter(prefix="/api/v1", tags=["analysis"])


class VariogramBody(WebMapModel):
    value_column: str = Field(min_length=1, max_length=128)
    model: str | None = Field(
        None,
        description=(
            "spherical, exponential, gaussian or matern. Omit to fit all of them "
            "and keep the best by weighted residual."
        ),
    )
    detect_anisotropy: bool = True
    n_lags: int = Field(20, ge=5, le=100)
    seed: int = service.DEFAULT_SEED
    where: str | None = None


class Lag(WebMapModel):
    distance: float
    semivariance: float
    n_pairs: int


class VariogramResponse(WebMapModel):
    """The fit, plus what says how much to trust it.

    The empirical points come back with the model, because a variogram plot
    without them is a curve with nothing to judge it against — and `07` §9.5
    sizes its points by `n_pairs` for the same reason: a tail built from nine
    pairs should not look like the rest of the curve.
    """

    dataset_id: UUID
    dataset_name: str
    value_column: str
    n_points: int

    model: str
    nugget: float
    sill: float
    range: float
    anisotropy_ratio: float
    anisotropy_angle: float

    fit_residual: float
    n_pairs_used: int
    nugget_ratio: float

    units: str
    srid: int
    seed: int
    lags: list[Lag]
    warnings: list[str]


@router.post("/datasets/{dataset_id}/variogram", response_model=VariogramResponse)
async def fit_variogram(
    dataset_id: UUID,
    body: VariogramBody,
    principal: CurrentPrincipal,
    conn: ScopedConn,
    settings: AppSettings,
) -> Any:
    """Fit a variogram to a stored point layer, without gridding anything.

    A POST rather than a GET despite creating nothing: it takes a body, it
    does real work, and a URL long enough to carry a `where` predicate is not
    something to put in a proxy log.
    """
    from webmap_geo.dataplane import ObjectStore

    store = ObjectStore(
        endpoint=settings.s3_endpoint.removeprefix("http://").removeprefix("https://"),
        access_key=settings.s3_access_key.get_secret_value(),
        secret_key=settings.s3_secret_key.get_secret_value(),
        region=settings.s3_region,
        use_ssl=settings.s3_use_ssl,
    )

    result = await service.fit_for_dataset(
        conn,
        principal,
        service.VariogramRequest(
            dataset_id=dataset_id,
            value_column=body.value_column,
            model=body.model,
            detect_anisotropy=body.detect_anisotropy,
            n_lags=body.n_lags,
            seed=body.seed,
            where=body.where,
        ),
        object_store=store,
        bucket=settings.s3_bucket,
    )

    log.info(
        "variogram_fitted",
        dataset_id=str(dataset_id),
        model=result["model"],
        range=result["range"],
        n_points=result["n_points"],
    )
    return result


VariogramLags = Annotated[list[Lag], Field()]

__all__ = ["router"]
