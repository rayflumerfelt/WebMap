"""Structured flags. `13-kriging.md` §4.5, §16.

**This replaces free-text warnings**, and the reason is in `CLAUDE.md` §8: for
an MCP tool the message *is* the interface. Free text cannot be branched on,
cannot be counted, and cannot be linked back to the parameter that caused it —
so "the variogram has a high nugget" is a sentence a person reads and nothing
a caller can act on. A code can gate a diagnostic panel, drive a retry with a
different parameter, and be counted across a hundred runs to find out which
guard actually fires.

The registry is **one registry for the whole of `webmap_geo`**. §16 is explicit
that the existing free-text warnings in `interpolate/dispatch.py` migrate into
it rather than living beside it, because two vocabularies for the same thing is
how a caller comes to handle half of them.

Three severities, and the distinction is about what the caller should do:

- `ERROR` — the result is not usable. Raised, not returned.
- `WARNING` — the result is usable and somebody has to look at it. A map with
  a high nugget is still a map; it is a map whose confidence is lower than the
  colours suggest.
- `INFO` — something was decided automatically that a reader would want to know.
  Duplicates merged, a threshold dropped, a system solved by least squares.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


@dataclass(frozen=True)
class Flag:
    """One thing worth telling the caller about. `13` §4.5."""

    severity: Severity
    #: Stable, from the registry below. Stable means: a caller may branch on it,
    #: so renaming one is a breaking change to every consumer.
    code: str
    #: What happened, why, and what now — `CLAUDE.md` §8.
    msg: str
    detail: dict[str, Any] | None = None
    #: Key into the result's figure-data dict, where a picture says it better.
    figure: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """For the lineage record and the job document."""
        payload: dict[str, Any] = {
            "severity": self.severity.value,
            "code": self.code,
            "msg": self.msg,
        }
        if self.detail:
            payload["detail"] = self.detail
        if self.figure:
            payload["figure"] = self.figure
        return payload


@dataclass(frozen=True)
class FlagSpec:
    """What a code means, independent of any particular occurrence."""

    code: str
    severity: Severity
    #: Where the rule is written down, so a message can point at it.
    anchor: str
    figure: str | None = None


def _spec(code: str, severity: Severity, anchor: str, figure: str | None = None) -> FlagSpec:
    return FlagSpec(code=code, severity=severity, anchor=anchor, figure=figure)


#: `13` §16, in its order. Severity lives here rather than at each call site so
#: that a code cannot be raised as a warning in one place and an error in
#: another — which is how a caller's branch on severity stops meaning anything.
REGISTRY: dict[str, FlagSpec] = {
    entry.code: entry
    for entry in (
        _spec("COORD_DEGREES", Severity.ERROR, "13 §5.1"),
        _spec("INSUFFICIENT_DATA", Severity.ERROR, "13 §5.1"),
        _spec("TREND_NONFINITE", Severity.ERROR, "13 §8.4"),
        _spec("TOO_FEW_THRESHOLDS", Severity.ERROR, "13 §10.1"),
        _spec("BLOCK_INDICATOR_UNSUPPORTED", Severity.ERROR, "13 §11.1"),
        _spec("TREND_ABSORBED", Severity.WARNING, "13 §12.4", figure="variance_budget"),
        _spec("COVAR_CONFOUND", Severity.WARNING, "13 §12.6"),
        _spec("COVAR_COLLINEAR", Severity.WARNING, "13 §5.4"),
        _spec("COVAR_SPATIAL_PROXY", Severity.WARNING, "13 §5.4"),
        _spec("COVAR_EXTRAPOLATION", Severity.WARNING, "13 §5.4", figure="covariate_spread"),
        _spec("TREND_NOT_CONVERGED", Severity.WARNING, "13 §8.4"),
        _spec("TREND_UNIDENTIFIED", Severity.WARNING, "13 §8.4"),
        _spec("TREND_NONMONOTONE", Severity.WARNING, "13 §8.4"),
        _spec("HIGH_NUGGET", Severity.WARNING, "13 §7.5"),
        _spec("ORDER_RELATION_SEVERE", Severity.WARNING, "13 §10.3"),
        _spec("TREND_ONLY_WINS", Severity.WARNING, "13 §13"),
        _spec("RANDOM_SPLIT_USED", Severity.WARNING, "13 §13"),
        _spec("SR_UNSTABLE", Severity.WARNING, "13 §9"),
        _spec("TARGET_LENGTH_NORMALIZED", Severity.WARNING, "13 §5.3"),
        _spec("EXTRAPOLATED_FRACTION", Severity.WARNING, "05 §6.5"),
        _spec("THRESHOLD_DROPPED", Severity.INFO, "13 §10.1"),
        _spec("ANISO_NOT_SIGNIFICANT", Severity.INFO, "13 §7.6"),
        _spec("DUPLICATES_RESOLVED", Severity.INFO, "13 §5.1"),
        _spec("ROWS_DROPPED", Severity.INFO, "13 §5.1"),
        _spec("TREND_SUBSET_FIT", Severity.INFO, "13 §8.2"),
        _spec("TAIL_MODEL_FALLBACK", Severity.INFO, "13 §10.4"),
        _spec("SINGULAR_SYSTEM", Severity.INFO, "13 §11.1"),
    )
}


class UnknownFlag(KeyError):
    """A code that is not in the registry.

    Raised rather than tolerated: an unregistered code is a typo that would
    otherwise reach a caller branching on it, and be silently unhandled.
    """


def flag(code: str, msg: str, **detail: Any) -> Flag:
    """Build a flag, taking its severity from the registry.

    The severity is *not* an argument. A code that is an error in one place and
    a warning in another makes a caller's branch on severity meaningless, and
    that has to be impossible rather than discouraged.
    """
    spec = REGISTRY.get(code)
    if spec is None:
        raise UnknownFlag(
            f"'{code}' is not in the flag registry. Add it to `13-kriging.md` "
            f"§16 and to REGISTRY in this module — a code callers can branch on "
            f"has to be written down before it is raised. Known codes: "
            f"{', '.join(sorted(REGISTRY))}."
        )
    return Flag(
        severity=spec.severity,
        code=code,
        msg=msg,
        detail=detail or None,
        figure=spec.figure,
    )


@dataclass
class FlagList:
    """Flags accumulated through a run.

    Mutable and ordered, because the order they were raised in is the order the
    run made its decisions in, and a reader following a diagnostic works
    backwards through exactly that.
    """

    items: list[Flag] = field(default_factory=list)

    def add(self, code: str, msg: str, **detail: Any) -> Flag:
        entry = flag(code, msg, **detail)
        self.items.append(entry)
        return entry

    def extend(self, flags: list[Flag]) -> None:
        self.items.extend(flags)

    def of(self, severity: Severity) -> list[Flag]:
        return [item for item in self.items if item.severity is severity]

    @property
    def errors(self) -> list[Flag]:
        return self.of(Severity.ERROR)

    @property
    def warnings(self) -> list[Flag]:
        return self.of(Severity.WARNING)

    def has(self, code: str) -> bool:
        return any(item.code == code for item in self.items)

    def as_list(self) -> list[dict[str, Any]]:
        """For `lineage.parameters`, which §4.6 says carries the flag list."""
        return [item.as_dict() for item in self.items]

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self) -> Any:
        return iter(self.items)


class GeostatError(Exception):
    """An ERROR-severity flag, raised.

    `13` §4.5 has errors meaning the result is not usable, and a result that is
    not usable should not be returned at all — a caller that has to check a list
    before trusting an array will one day not check.
    """

    def __init__(self, flag: Flag) -> None:
        super().__init__(flag.msg)
        self.flag = flag


def raise_flag(code: str, msg: str, **detail: Any) -> None:
    """Raise an error-severity flag. Refuses to raise a warning."""
    entry = flag(code, msg, **detail)
    if entry.severity is not Severity.ERROR:
        raise ValueError(
            f"'{code}' is a {entry.severity.value} and cannot be raised — a "
            f"warning that stops the run is an error that was registered wrongly. "
            f"Add it to a FlagList instead."
        )
    raise GeostatError(entry)


__all__ = [
    "REGISTRY",
    "Flag",
    "FlagList",
    "FlagSpec",
    "GeostatError",
    "Severity",
    "UnknownFlag",
    "flag",
    "raise_flag",
]
