"""Progress reporting with named phases. `10-jobs-async.md` §4.

Gridding jobs run for minutes, and **a frozen progress bar is
indistinguishable from a hung worker**. That is the whole reason this exists:
not to be precise, but to keep a bar moving at a roughly constant rate so
nobody kills a job that was working.

Two consequences follow from that goal, and they are the two things worth
knowing here:

- **Phases are weighted by their real duration**, not by their conceptual
  importance. Fitting a variogram is the interesting step and takes a tenth
  of the time; solving is dull and takes over half. Weighting by importance
  produces a bar that races to 60% and then sits there, which is exactly the
  appearance of a hang.
- **Writes are throttled.** A solver reporting every iteration would issue
  thousands of UPDATEs against the control plane for a bar nobody can read
  faster than about once a second.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from uuid import UUID

from webmap_core.logging import get_logger

log = get_logger(__name__)

#: Phase names and weights per job kind. The weights sum to 1.0 and are
#: measured proportions of a typical run, not guesses about significance.
PHASES: dict[str, list[tuple[str, float]]] = {
    "interpolate": [
        ("Loading control points", 0.05),
        ("Fitting variogram", 0.10),
        ("Validating fault network", 0.05),
        ("Building constrained mesh", 0.15),
        ("Solving", 0.55),
        ("Writing grid", 0.10),
    ],
    "contour": [
        ("Loading grid", 0.15),
        ("Choosing levels", 0.05),
        ("Tracing contours", 0.60),
        ("Writing features", 0.20),
    ],
}

#: Seconds between database writes. One second is about as fast as a progress
#: bar can usefully be read, and it bounds a solver's write rate no matter how
#: often it reports.
MIN_WRITE_INTERVAL = 1.0


class ProgressReporter:
    """Turns phase transitions into a fraction and a sentence.

    The sentence matters more than the number. "Fitting variogram (2,000 of
    18,000 points)" says the job is alive and where it is; 0.34 alone does
    not, and a bar that has been at 0.34 for a minute is unreadable either
    way.
    """

    def __init__(
        self,
        write: Callable[[float, str], Awaitable[None]],
        job_id: UUID,
        kind: str,
        *,
        min_interval: float = MIN_WRITE_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if kind not in PHASES:
            raise KeyError(
                f"No progress phases are defined for job kind '{kind}'. Add them "
                f"to webmap_worker.progress.PHASES — a job with no phases reports "
                f"nothing for its whole run, which reads as a hang."
            )
        self._write = write
        self._job_id = job_id
        self._phases = PHASES[kind]
        self._min_interval = min_interval
        self._clock = clock
        self._index = -1
        self._last_write = float("-inf")

    @property
    def phase_name(self) -> str:
        return self._phases[self._index][0] if self._index >= 0 else "Starting"

    def _fraction_before(self, index: int) -> float:
        return sum(weight for _, weight in self._phases[:index])

    async def phase(self, name: str) -> None:
        """Advance to a named phase. Always writes.

        Unthrottled because a phase change is the one progress event a person
        actually reads — "Solving" appearing is what tells them the setup
        finished — and there are six of them in a run, not six thousand.
        """
        index = next((i for i, (phase, _) in enumerate(self._phases) if phase == name), None)
        if index is None:
            raise KeyError(
                f"'{name}' is not a phase of this job kind. Defined phases: "
                f"{', '.join(phase for phase, _ in self._phases)}."
            )
        # A phase that runs out of order would move the bar backwards, which
        # looks like the job restarting.
        if index < self._index:
            raise ValueError(
                f"Phase '{name}' comes before '{self.phase_name}', which has "
                f"already started. Progress running backwards reads as a job "
                f"restarting from the beginning."
            )
        self._index = index
        await self._emit(self._fraction_before(index), name, force=True)

    async def within_phase(self, fraction: float, detail: str | None = None) -> None:
        """Sub-progress inside the current phase, from a solver's iteration count.

        Throttled: this is the call that would otherwise fire thousands of
        times. Clamped to the phase's own span so a solver that overshoots its
        estimate cannot push the bar into the next phase's territory.
        """
        if self._index < 0:
            raise RuntimeError(
                "within_phase was called before any phase started, so there is "
                "no span to report a fraction of. Call phase() first."
            )
        name, weight = self._phases[self._index]
        clamped = max(0.0, min(1.0, fraction))
        overall = self._fraction_before(self._index) + weight * clamped
        await self._emit(overall, f"{name} ({detail})" if detail else name)

    async def _emit(self, fraction: float, message: str, *, force: bool = False) -> None:
        now = self._clock()
        if not force and now - self._last_write < self._min_interval:
            return
        self._last_write = now
        await self._write(fraction, message)


def total_weight(kind: str) -> float:
    """Exposed so a test can assert the weights still sum to one.

    A phase list that sums to 0.9 leaves the bar stuck at 90% through the last
    step of every job of that kind, which is the most alarming place for it to
    stop.
    """
    return sum(weight for _, weight in PHASES[kind])


__all__ = ["MIN_WRITE_INTERVAL", "PHASES", "ProgressReporter", "total_weight"]
