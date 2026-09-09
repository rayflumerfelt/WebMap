"""The job contract. `10-jobs-async.md` §2."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from webmap_core.models import JobState
from webmap_core.permissions import Channel, Principal


class ErrorKind(StrEnum):
    """Machine-readable failure class. Drives retry policy (`10` §8)."""

    TRANSIENT = "transient"  # retry automatically
    RESOURCE = "resource"  # OOM, timeout — retry once on a bigger worker
    INPUT = "input"  # bad data — never retry, tell the user
    PERMISSION = "permission"  # never retry
    INTERNAL = "internal"  # bug — never retry, alert


#: Retries per error kind. Retrying an INPUT error just fails four times
#: slower and buries the message that would have told the user what to fix.
RETRY_POLICY: dict[ErrorKind, int] = {
    ErrorKind.TRANSIENT: 3,
    ErrorKind.RESOURCE: 1,
    ErrorKind.INPUT: 0,
    ErrorKind.PERMISSION: 0,
    ErrorKind.INTERNAL: 0,
}


@dataclass(frozen=True)
class JobContext:
    """Every job payload embeds this.

    There is no such thing as an anonymous job in this system. The request is
    gone by the time the job runs, so the identity must travel in the payload
    — a job that resolves datasets without one is a security bug, not a style
    issue (`03-auth-security.md` §5.1).
    """

    job_id: UUID
    requested_by: UUID
    team_ids: frozenset[UUID]

    def principal(self) -> Principal:
        return Principal(
            user_id=self.requested_by, team_ids=self.team_ids, channel=Channel.WORKER
        )

    def to_payload(self) -> dict[str, Any]:
        """Serialise for the arq payload. frozenset is not JSON."""
        return {
            "job_id": str(self.job_id),
            "requested_by": str(self.requested_by),
            "team_ids": sorted(str(t) for t in self.team_ids),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "JobContext":
        return cls(
            job_id=UUID(payload["job_id"]),
            requested_by=UUID(payload["requested_by"]),
            team_ids=frozenset(UUID(t) for t in payload["team_ids"]),
        )


@dataclass(frozen=True)
class JobRecord:
    id: UUID
    kind: str
    state: JobState
    parameters: dict[str, Any]
    progress: float  # 0.0 .. 1.0
    progress_message: str | None
    result: dict[str, Any] | None
    error: str | None
    error_kind: ErrorKind | None
    requested_by: UUID
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @property
    def is_terminal(self) -> bool:
        return self.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)


class JobCancelled(Exception):
    """Raised inside a worker when the cancellation flag is observed.

    Cancellation is cooperative (`10` §9): long loops poll the flag at phase
    boundaries and inside the solver's iteration loop. Partial outputs are
    discarded — never leave a half-written COG registered as a dataset.
    """
