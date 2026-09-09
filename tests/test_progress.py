"""Progress reporting. `10-jobs-async.md` §4.

The reason this exists is stated in the spec and is worth repeating, because
every test here follows from it: **a frozen progress bar is indistinguishable
from a hung worker.** Someone watching one will kill a job that was working.

So the properties under test are about what a person sees — a bar that moves
at a roughly constant rate, never runs backwards, and does not stop at 90% —
rather than about arithmetic.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from webmap_worker.progress import (
    MIN_WRITE_INTERVAL,
    PHASES,
    ProgressReporter,
    total_weight,
)


class Recorder:
    """Captures what would have been written to the job row."""

    def __init__(self) -> None:
        self.writes: list[tuple[float, str]] = []

    async def __call__(self, fraction: float, message: str) -> None:
        self.writes.append((fraction, message))


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def reporter(
    kind: str = "interpolate", clock: FakeClock | None = None
) -> tuple[ProgressReporter, Recorder, FakeClock]:
    recorder, ticking = Recorder(), clock or FakeClock()
    return (
        ProgressReporter(recorder, uuid4(), kind, clock=ticking),
        recorder,
        ticking,
    )


# --- the bar reaches the end -------------------------------------------------


@pytest.mark.parametrize("kind", sorted(PHASES))
def test_the_phase_weights_sum_to_one(kind: str) -> None:
    """**A list summing to 0.9 leaves every job of that kind stuck at 90%
    through its last step** — which is the most alarming place for a bar to
    stop, because it is where a job that hung would also stop."""
    assert total_weight(kind) == pytest.approx(1.0)


async def test_progress_rises_monotonically_through_a_whole_run() -> None:
    """A bar that goes backwards reads as the job restarting from scratch."""
    report, recorder, clock = reporter()

    for phase, _ in PHASES["interpolate"]:
        clock.now += 10.0
        await report.phase(phase)

    fractions = [fraction for fraction, _ in recorder.writes]
    assert fractions == sorted(fractions)
    assert fractions[0] == pytest.approx(0.0)


async def test_the_last_phase_starts_below_one_and_leaves_room() -> None:
    """ "Writing grid" at 1.0 would show a finished bar over a job that is
    still uploading, and the upload is the part that can fail."""
    report, recorder, _ = reporter()

    await report.phase("Writing grid")

    fraction, _ = recorder.writes[-1]
    assert 0.85 < fraction < 1.0


# --- what the message says ---------------------------------------------------


async def test_a_phase_change_carries_its_name() -> None:
    """`10` §4: the sentence matters more than the number. "Solving" appearing
    is what tells someone the setup finished."""
    report, recorder, _ = reporter()

    await report.phase("Solving")

    _, message = recorder.writes[-1]
    assert message == "Solving"


async def test_sub_progress_carries_the_solvers_own_detail() -> None:
    """ "Fitting variogram (2,000 of 18,000 points)" says the job is alive and
    where it is; 0.34 alone does not."""
    report, recorder, clock = reporter()
    await report.phase("Fitting variogram")

    clock.now += MIN_WRITE_INTERVAL * 2
    await report.within_phase(0.5, "2,000 of 18,000 points")

    fraction, message = recorder.writes[-1]
    assert message == "Fitting variogram (2,000 of 18,000 points)"
    # Half of the 0.10 variogram phase, on top of the 0.05 before it.
    assert fraction == pytest.approx(0.10)


# --- throttling --------------------------------------------------------------


async def test_sub_progress_is_throttled() -> None:
    """**The call that would otherwise fire thousands of times.** A solver
    reporting every iteration would issue an UPDATE per iteration against the
    control plane, for a bar nobody can read faster than about once a second.
    """
    report, recorder, clock = reporter()
    await report.phase("Solving")
    before = len(recorder.writes)

    for i in range(1_000):
        clock.now += 0.001  # a millisecond per iteration
        await report.within_phase(i / 1_000)

    assert len(recorder.writes) - before <= 2, "the solver's reports were not throttled"


async def test_throttling_lets_progress_through_once_the_interval_passes() -> None:
    """Throttled, not suppressed — a long solve must still show movement."""
    report, recorder, clock = reporter()
    await report.phase("Solving")
    before = len(recorder.writes)

    for i in range(5):
        clock.now += MIN_WRITE_INTERVAL * 1.5
        await report.within_phase(i / 5)

    assert len(recorder.writes) - before == 5


async def test_a_phase_change_is_never_throttled() -> None:
    """There are six phase changes in a run, not six thousand, and each one is
    the event a person actually reads."""
    report, recorder, clock = reporter()
    clock.now = 100.0

    await report.phase("Loading control points")
    await report.phase("Fitting variogram")
    await report.phase("Solving")

    assert [message for _, message in recorder.writes] == [
        "Loading control points",
        "Fitting variogram",
        "Solving",
    ]


# --- refusals ----------------------------------------------------------------


async def test_a_phase_out_of_order_is_refused() -> None:
    """Progress running backwards reads as a job restarting from the
    beginning, which is worse than no progress at all."""
    report, _, _ = reporter()
    await report.phase("Solving")

    with pytest.raises(ValueError, match="restarting"):
        await report.phase("Fitting variogram")


async def test_an_unknown_phase_lists_the_real_ones() -> None:
    report, _, _ = reporter()

    with pytest.raises(KeyError, match="Solving"):
        await report.phase("Krigging")


async def test_sub_progress_before_any_phase_is_refused() -> None:
    """There is no span to report a fraction of, and guessing one would put
    the bar somewhere arbitrary."""
    report, _, _ = reporter()

    with pytest.raises(RuntimeError, match="Call phase\\(\\) first"):
        await report.within_phase(0.5)


async def test_a_solver_overshooting_its_estimate_cannot_pass_its_phase() -> None:
    """Iteration-count estimates are estimates. One that reports 1.4 must not
    push the bar into the next phase's territory and then appear to go
    backwards when that phase actually starts."""
    report, recorder, clock = reporter()
    await report.phase("Solving")
    solving_start = recorder.writes[-1][0]

    clock.now += MIN_WRITE_INTERVAL * 2
    await report.within_phase(1.4)
    overshot = recorder.writes[-1][0]

    await report.phase("Writing grid")
    next_phase = recorder.writes[-1][0]

    assert solving_start < overshot <= next_phase


def test_a_kind_with_no_phases_fails_at_construction() -> None:
    """Rather than reporting nothing for a whole run, which reads as a hang —
    the exact failure this module exists to prevent."""
    with pytest.raises(KeyError, match="aggregate"):
        ProgressReporter(Recorder(), uuid4(), "aggregate")
