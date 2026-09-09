"""Variogram estimation and fitting. `05-geoprocessing.md` §6.3.

Kriging weights come entirely from the variogram, so kriging without variogram
analysis is kriging with made-up parameters — and the made-up answer looks
exactly as plausible as the real one.
"""

from webmap_geo.variogram.experimental import (
    DEFAULT_SUBSAMPLE,
    MIN_PAIRS_PER_LAG,
    ExperimentalVariogram,
    declustering_weights,
    estimate_experimental,
)
from webmap_geo.variogram.fit import (
    ANISOTROPY_AZIMUTHS,
    MIN_REPORTABLE_ANISOTROPY,
    detect_anisotropy_from,
    fit,
    fit_auto,
)
from webmap_geo.variogram.model import MODELS, FittedVariogram, anisotropy_transform

__all__ = [
    "ANISOTROPY_AZIMUTHS",
    "DEFAULT_SUBSAMPLE",
    "MIN_PAIRS_PER_LAG",
    "MIN_REPORTABLE_ANISOTROPY",
    "MODELS",
    "ExperimentalVariogram",
    "FittedVariogram",
    "anisotropy_transform",
    "declustering_weights",
    "detect_anisotropy_from",
    "estimate_experimental",
    "fit",
    "fit_auto",
]
