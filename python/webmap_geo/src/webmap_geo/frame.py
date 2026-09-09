"""The planar frame a caller's coordinate arrays are already expressed in."""

from dataclasses import dataclass
from typing import Literal

LengthUnit = Literal["m", "ft", "usft"]


@dataclass(frozen=True)
class AnalysisFrame:
    """The planar frame the caller's coordinates are already expressed in.

    Metadata, not an instruction. Nothing in webmap_geo reads this to decide
    whether to reproject — arrays arrive in the analysis CRS or the caller has
    a bug. It is carried into diagnostics and lineage so a stored grid can say
    what frame produced it, and it appears in error messages so "range 4200"
    is never ambiguous about its units.

    Validation that the srid is projected rather than geographic happens in
    webmap_core.crs.CrsContext, at the boundary that prepares the arrays.
    """

    srid: int
    units: LengthUnit

    def describe(self) -> str:
        """Human-readable frame, for error messages and diagnostics."""
        return f"EPSG:{self.srid} ({self.units})"
