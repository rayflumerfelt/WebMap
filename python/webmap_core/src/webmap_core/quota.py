"""Resource quotas. `10-jobs-async.md` §7.

Business units share infrastructure. One geologist kriging 500k points should
not starve another team.
"""

from dataclasses import dataclass

from webmap_core.exceptions import QuotaExceeded


@dataclass(frozen=True)
class QuotaPolicy:
    max_concurrent_jobs_per_user: int = 3
    max_concurrent_jobs_per_team: int = 8
    max_queued_jobs_per_user: int = 20
    max_grid_cells: int = 16_000_000
    max_interpolation_points: int = 2_000_000
    daily_compute_seconds_per_user: int = 14_400  # 4 hours


DEFAULT_POLICY = QuotaPolicy()


@dataclass(frozen=True)
class Usage:
    """A principal's current consumption, as measured by the caller."""

    running_jobs: int
    queued_jobs: int
    team_running_jobs: int
    compute_seconds_today: int
    oldest_running_job_age_seconds: int | None = None


def check_concurrency(usage: Usage, policy: QuotaPolicy = DEFAULT_POLICY) -> None:
    """Raise QuotaExceeded naming the limit and what to do about it.

    'Quota exceeded' alone is useless. Naming the limit, the current value,
    and how long the blocking job has been running lets someone decide
    whether to wait or cancel — which is the only useful next action.
    """
    if usage.running_jobs >= policy.max_concurrent_jobs_per_user:
        age = (
            f"; the oldest started {usage.oldest_running_job_age_seconds // 60} minutes ago"
            if usage.oldest_running_job_age_seconds is not None
            else ""
        )
        raise QuotaExceeded(
            f"You have {usage.running_jobs} jobs running (limit "
            f"{policy.max_concurrent_jobs_per_user}){age}. Wait for one to "
            f"finish, or cancel it with webmap_cancel_job."
        )

    if usage.queued_jobs >= policy.max_queued_jobs_per_user:
        raise QuotaExceeded(
            f"You have {usage.queued_jobs} jobs queued (limit "
            f"{policy.max_queued_jobs_per_user}). The queue is not lost — it "
            f"will drain — but nothing further will be accepted until it does."
        )

    if usage.team_running_jobs >= policy.max_concurrent_jobs_per_team:
        raise QuotaExceeded(
            f"Your team has {usage.team_running_jobs} jobs running (limit "
            f"{policy.max_concurrent_jobs_per_team}). This is a shared limit — "
            f"someone else on the team is using the workers."
        )

    if usage.compute_seconds_today >= policy.daily_compute_seconds_per_user:
        used_hours = usage.compute_seconds_today / 3600
        limit_hours = policy.daily_compute_seconds_per_user / 3600
        raise QuotaExceeded(
            f"You have used {used_hours:.1f} hours of compute today (limit "
            f"{limit_hours:.0f}). This resets at midnight UTC."
        )


def check_grid_size(
    nx: int, ny: int, cell_size: float, unit: str, policy: QuotaPolicy = DEFAULT_POLICY
) -> None:
    """Reject an oversized grid, naming a cell size that would fit.

    `CLAUDE.md` §8: name the limit and the offending value, so the caller
    knows what to change and to what.
    """
    cells = nx * ny
    if cells <= policy.max_grid_cells:
        return

    # Cells scale with 1/cell_size^2, so the smallest cell size that fits is
    # the current one scaled by sqrt(ratio). Round up to something a geologist
    # would actually type.
    scale = (cells / policy.max_grid_cells) ** 0.5
    suggested = _round_up_nicely(cell_size * scale)
    ok_cells = int(nx / scale) * int(ny / scale)

    raise QuotaExceeded(
        f"Requested {cells:,} grid cells (limit {policy.max_grid_cells:,}). A "
        f"{cell_size:g} {unit} cell size over this extent gives {cells:,}; "
        f"{suggested:g} {unit} gives about {ok_cells:,}."
    )


def check_point_count(count: int, policy: QuotaPolicy = DEFAULT_POLICY) -> None:
    if count > policy.max_interpolation_points:
        raise QuotaExceeded(
            f"Dataset has {count:,} control points (limit "
            f"{policy.max_interpolation_points:,} for interpolation). Filter to "
            f"an area of interest, or aggregate to one value per location "
            f"first — duplicated points at the same location are the usual "
            f"cause of a count this size."
        )


def _round_up_nicely(value: float) -> float:
    """Round up to 1, 2, 2.5, or 5 x 10^n.

    A suggestion of '263.4 ft' is not a suggestion a geologist will accept.
    Same reasoning as `pretty_breaks` in `08-styling-palettes.md` §4.
    """
    from math import ceil, floor, log10

    if value <= 0:
        return 1.0
    magnitude = float(10 ** floor(log10(value)))
    for step in (1.0, 2.0, 2.5, 5.0, 10.0):
        if value <= step * magnitude:
            return step * magnitude
    return float(ceil(value))
