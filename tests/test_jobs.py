"""Job lifecycle against a real Postgres. `10-jobs-async.md` §2, §7, §9, §10.

The `job` table carries no RLS policies — a job is an action rather than an
ownable artefact, and its *output* is what gets shared. That makes
`get_job`'s explicit `requested_by` check the only thing standing between one
person and another's job list, which is why it is tested here rather than
taken on trust.

`pytest.mark.integration` because these need the stack up; they skip with
instructions when it is not.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from webmap_core.db.session import principal_session
from webmap_core.exceptions import NotFound, PermissionDenied, QuotaExceeded
from webmap_core.jobs import ErrorKind
from webmap_core.models import JobState
from webmap_core.permissions import Principal
from webmap_core.services import jobs as service

pytestmark = pytest.mark.integration


class FakeRedis:
    """Just enough of the Redis surface the job service uses.

    The service touches two keys — the idempotency record and the cancel flag
    — and nothing here needs a real server. Expiry is not simulated: no test
    depends on a key ageing out, and a fake that silently expired things would
    make the dedupe tests flaky rather than fast.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value


PARAMS: dict[str, Any] = {
    "dataset_id": "6f1b0e2a-0000-4000-8000-000000000001",
    "method": "ordinary_kriging",
    "cell_size": 500.0,
}


async def _enqueue(
    engine: AsyncEngine,
    principal: Principal,
    *,
    redis: FakeRedis | None = None,
    kind: str = "interpolate",
    parameters: dict[str, Any] | None = None,
) -> service.Enqueued:
    async with principal_session(engine, principal) as conn:
        return await service.enqueue(
            conn,
            principal,
            kind=kind,
            parameters=PARAMS if parameters is None else parameters,
            redis=redis,
        )


# --- the row exists before anything can run it -------------------------------


async def test_enqueue_creates_a_queued_job_owned_by_the_requester(
    engine: AsyncEngine, principals: dict[str, object]
) -> None:
    owner = principals["owner"]
    assert isinstance(owner, Principal)

    enqueued = await _enqueue(engine, owner)

    async with principal_session(engine, owner) as conn:
        job = await service.get_job(conn, owner, enqueued.job_id)

    assert enqueued.was_created
    assert job["state"] == JobState.QUEUED
    assert job["requested_by"] == owner.user_id
    # The parameters round-trip as JSON, not as a string — a status poll that
    # has to json.loads the payload itself is a different contract.
    assert job["parameters"]["method"] == "ordinary_kriging"


# --- idempotency (§10) -------------------------------------------------------


async def test_an_identical_resubmission_returns_the_same_job(
    engine: AsyncEngine, principals: dict[str, object]
) -> None:
    """**The retry case.** Claude's tool call times out and it calls again;
    without this there are two workers gridding the same surface and two
    datasets registered, and the conversation cannot say which is which."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    redis = FakeRedis()

    first = await _enqueue(engine, owner, redis=redis)
    second = await _enqueue(engine, owner, redis=redis)

    assert second.job_id == first.job_id
    assert second.was_created is False, "the caller must be able to say which happened"


async def test_key_order_does_not_defeat_the_dedupe(
    engine: AsyncEngine, principals: dict[str, object]
) -> None:
    """The exact case dedupe exists for is a client retrying its own
    serialised request, so two payloads differing only in key order have to
    hash the same."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    redis = FakeRedis()

    first = await _enqueue(engine, owner, redis=redis, parameters=dict(PARAMS))
    reordered = {k: PARAMS[k] for k in reversed(list(PARAMS))}
    second = await _enqueue(engine, owner, redis=redis, parameters=reordered)

    assert second.job_id == first.job_id


async def test_two_people_gridding_the_same_thing_get_two_jobs(
    engine: AsyncEngine, principals: dict[str, object]
) -> None:
    """They are doing two pieces of work: they own different outputs and each
    expects their own job."""
    owner, teammate = principals["owner"], principals["teammate"]
    assert isinstance(owner, Principal) and isinstance(teammate, Principal)
    redis = FakeRedis()

    mine = await _enqueue(engine, owner, redis=redis)
    theirs = await _enqueue(engine, teammate, redis=redis)

    assert mine.job_id != theirs.job_id
    assert theirs.was_created


async def test_a_different_parameter_is_a_different_job(
    engine: AsyncEngine, principals: dict[str, object]
) -> None:
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    redis = FakeRedis()

    coarse = await _enqueue(engine, owner, redis=redis)
    fine = await _enqueue(engine, owner, redis=redis, parameters={**PARAMS, "cell_size": 250.0})

    assert fine.job_id != coarse.job_id


# --- quota (§7) --------------------------------------------------------------


async def test_the_concurrency_limit_names_the_limit_and_a_next_action(
    engine: AsyncEngine, principals: dict[str, object]
) -> None:
    """`CLAUDE.md` §8: a resource limit names the offending value and what to
    change. Refused at submission rather than queued indefinitely — a job
    sitting behind five of your own is indistinguishable from a stuck one, and
    queue depth is invisible from a conversation."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)

    for n in range(service.MAX_CONCURRENT_JOBS_PER_USER):
        await _enqueue(engine, owner, parameters={**PARAMS, "cell_size": 100.0 + n})

    with pytest.raises(QuotaExceeded) as excinfo:
        await _enqueue(engine, owner, parameters={**PARAMS, "cell_size": 999.0})

    message = str(excinfo.value)
    assert str(service.MAX_CONCURRENT_JOBS_PER_USER) in message
    assert "webmap_cancel_job" in message, "the message must name a next action"


async def test_a_finished_job_does_not_count_against_the_quota(
    engine: AsyncEngine, principals: dict[str, object]
) -> None:
    owner = principals["owner"]
    assert isinstance(owner, Principal)

    for n in range(service.MAX_CONCURRENT_JOBS_PER_USER):
        enqueued = await _enqueue(engine, owner, parameters={**PARAMS, "cell_size": 100.0 + n})
        async with principal_session(engine, owner) as conn:
            await service.mark_running(conn, enqueued.job_id)
            await service.mark_succeeded(conn, enqueued.job_id, {"dataset_id": str(uuid4())})

    # No exception: the three succeeded jobs are not occupying anything.
    await _enqueue(engine, owner, parameters={**PARAMS, "cell_size": 777.0})


async def test_one_persons_jobs_do_not_consume_anothers_quota(
    engine: AsyncEngine, principals: dict[str, object]
) -> None:
    owner, teammate = principals["owner"], principals["teammate"]
    assert isinstance(owner, Principal) and isinstance(teammate, Principal)

    for n in range(service.MAX_CONCURRENT_JOBS_PER_USER):
        await _enqueue(engine, owner, parameters={**PARAMS, "cell_size": 100.0 + n})

    await _enqueue(engine, teammate)  # not refused


# --- visibility --------------------------------------------------------------


async def test_someone_elses_job_is_not_readable(
    engine: AsyncEngine, principals: dict[str, object]
) -> None:
    """`job` has no RLS policies, so this application check is the only thing
    standing between one person and another's work. A job's parameters name
    the dataset and the method — that is what someone is working on."""
    owner, outsider = principals["owner"], principals["outsider"]
    assert isinstance(owner, Principal) and isinstance(outsider, Principal)

    enqueued = await _enqueue(engine, owner)

    async with principal_session(engine, outsider) as conn:
        with pytest.raises(PermissionDenied, match="belongs to someone else"):
            await service.get_job(conn, outsider, enqueued.job_id)


async def test_a_teammate_cannot_read_a_job_either(
    engine: AsyncEngine, principals: dict[str, object]
) -> None:
    """Team membership shares *artefacts*. A job is an action."""
    owner, teammate = principals["owner"], principals["teammate"]
    assert isinstance(owner, Principal) and isinstance(teammate, Principal)

    enqueued = await _enqueue(engine, owner)

    async with principal_session(engine, teammate) as conn:
        with pytest.raises(PermissionDenied):
            await service.get_job(conn, teammate, enqueued.job_id)


async def test_listing_shows_only_your_own_jobs(
    engine: AsyncEngine, principals: dict[str, object]
) -> None:
    owner, teammate = principals["owner"], principals["teammate"]
    assert isinstance(owner, Principal) and isinstance(teammate, Principal)

    mine = await _enqueue(engine, owner)
    await _enqueue(engine, teammate)

    async with principal_session(engine, owner) as conn:
        listed = await service.list_jobs(conn, owner)

    assert [job["id"] for job in listed] == [mine.job_id]


async def test_a_missing_job_says_rows_are_cleaned_up(
    engine: AsyncEngine, principals: dict[str, object]
) -> None:
    """Not "not found" — a poll for a job that finished last week should not
    read as though the work never happened."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)

    async with principal_session(engine, owner) as conn:
        with pytest.raises(NotFound, match="cleaned up"):
            await service.get_job(conn, owner, uuid4())


# --- progress and terminal states --------------------------------------------


async def test_progress_carries_a_sentence_not_only_a_fraction(
    engine: AsyncEngine, principals: dict[str, object]
) -> None:
    """`10` §4. "Fitting variogram (2,000 of 18,000 points)" tells someone the
    job is alive and where it is; 0.34 alone does not."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    enqueued = await _enqueue(engine, owner)

    async with principal_session(engine, owner) as conn:
        await service.mark_running(conn, enqueued.job_id)
        await service.report_progress(
            conn, enqueued.job_id, 0.34, "Fitting variogram (2,000 of 18,000 points)"
        )
        job = await service.get_job(conn, owner, enqueued.job_id)

    assert job["progress"] == pytest.approx(0.34)
    assert "18,000 points" in job["progress_message"]


async def test_progress_on_a_queued_job_is_ignored(
    engine: AsyncEngine, principals: dict[str, object]
) -> None:
    """A job that has not started has made no progress. Guarding on state
    keeps a stale worker from moving the bar on a job it no longer owns."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    enqueued = await _enqueue(engine, owner)

    async with principal_session(engine, owner) as conn:
        await service.report_progress(conn, enqueued.job_id, 0.9, "nearly there")
        job = await service.get_job(conn, owner, enqueued.job_id)

    assert job["progress"] == 0.0


async def test_a_failure_records_its_kind_so_retry_policy_can_read_it(
    engine: AsyncEngine, principals: dict[str, object]
) -> None:
    """`10` §8: retrying an INPUT error just fails four times slower and
    buries the message that would have told the user what to fix."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    enqueued = await _enqueue(engine, owner)

    async with principal_session(engine, owner) as conn:
        await service.mark_running(conn, enqueued.job_id)
        await service.mark_failed(
            conn,
            enqueued.job_id,
            "Only 2 control points have finite values.",
            ErrorKind.INPUT,
        )
        job = await service.get_job(conn, owner, enqueued.job_id)

    assert job["state"] == JobState.FAILED
    assert job["error_kind"] == ErrorKind.INPUT
    assert job["finished_at"] is not None


# --- cancellation (§9) -------------------------------------------------------


async def test_cancelling_a_queued_job_stops_it_outright(
    engine: AsyncEngine, principals: dict[str, object]
) -> None:
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    redis = FakeRedis()
    enqueued = await _enqueue(engine, owner, redis=redis)

    async with principal_session(engine, owner) as conn:
        message = await service.request_cancel(conn, owner, enqueued.job_id, redis)
        job = await service.get_job(conn, owner, enqueued.job_id)

    assert job["state"] == JobState.CANCELLED
    assert "queued" in message


async def test_cancelling_a_running_job_says_it_is_cooperative(
    engine: AsyncEngine, principals: dict[str, object]
) -> None:
    """**The message is the feature.** A user who asks to cancel and sees the
    job still running for ten seconds concludes cancellation did not work.
    Saying it stops at the next checkpoint is what prevents that."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    redis = FakeRedis()
    enqueued = await _enqueue(engine, owner, redis=redis)

    async with principal_session(engine, owner) as conn:
        await service.mark_running(conn, enqueued.job_id)
        message = await service.request_cancel(conn, owner, enqueued.job_id, redis)
        job = await service.get_job(conn, owner, enqueued.job_id)

    assert job["state"] == JobState.RUNNING, "a running job is asked, not killed"
    assert "next checkpoint" in message
    assert "Nothing partial will be registered" in message
    assert await service.is_cancelled(redis, enqueued.job_id)


async def test_a_worker_sees_the_cancellation_flag(
    engine: AsyncEngine, principals: dict[str, object]
) -> None:
    """The flag is what the solver's iteration loop polls, and it has to be
    readable from a process that never saw the cancel request."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    redis = FakeRedis()
    enqueued = await _enqueue(engine, owner, redis=redis)

    assert not await service.is_cancelled(redis, enqueued.job_id)

    async with principal_session(engine, owner) as conn:
        await service.mark_running(conn, enqueued.job_id)
        await service.request_cancel(conn, owner, enqueued.job_id, redis)

    assert await service.is_cancelled(redis, enqueued.job_id)


async def test_cancelling_a_finished_job_says_so_rather_than_erroring(
    engine: AsyncEngine, principals: dict[str, object]
) -> None:
    """A race between a poll and a cancel is ordinary, not exceptional."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    enqueued = await _enqueue(engine, owner)

    async with principal_session(engine, owner) as conn:
        await service.mark_running(conn, enqueued.job_id)
        await service.mark_succeeded(conn, enqueued.job_id, {"dataset_id": str(uuid4())})
        message = await service.request_cancel(conn, owner, enqueued.job_id)

    assert "already finished" in message


async def test_cancelling_someone_elses_job_is_refused(
    engine: AsyncEngine, principals: dict[str, object]
) -> None:
    owner, outsider = principals["owner"], principals["outsider"]
    assert isinstance(owner, Principal) and isinstance(outsider, Principal)
    enqueued = await _enqueue(engine, owner)

    async with principal_session(engine, outsider) as conn:
        with pytest.raises(PermissionDenied):
            await service.request_cancel(conn, outsider, enqueued.job_id)


# --- identity travels with the payload (§2.1) --------------------------------


async def test_the_job_context_carries_the_requesters_teams(
    principals: dict[str, object],
) -> None:
    """`CLAUDE.md` §3.2: a job payload without a JobContext is a security bug.
    The teams matter as much as the user — a job that resolved datasets with
    an empty team set would silently lose access to every team-visible input
    and report the dataset as missing."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    job_id = uuid4()

    context = service.context_for(owner, job_id)
    round_tripped = type(context).from_payload(context.to_payload())

    assert round_tripped == context
    assert round_tripped.principal().team_ids == owner.team_ids


def test_the_idempotency_key_is_stable_across_runs(
    principals: dict[str, object],
) -> None:
    """Hashed rather than remembered, so two API processes agree without
    sharing anything but Redis."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)

    first = service.idempotency_key("interpolate", owner, PARAMS)
    second = service.idempotency_key("interpolate", owner, dict(PARAMS))

    assert first == second
    assert first != service.idempotency_key("contour", owner, PARAMS)


def test_uuid_parameters_do_not_break_the_key(principals: dict[str, object]) -> None:
    """Job parameters arrive off a Pydantic model, where an id is a UUID and
    not a string. `json.dumps` raises on one without a default."""
    owner = principals["owner"]
    assert isinstance(owner, Principal)
    dataset_id = uuid4()

    as_uuid = service.idempotency_key("interpolate", owner, {"dataset_id": dataset_id})
    as_text = service.idempotency_key("interpolate", owner, {"dataset_id": str(dataset_id)})

    assert as_uuid == as_text


def test_a_job_id_is_a_uuid_not_a_string(principals: dict[str, object]) -> None:
    owner = principals["owner"]
    assert isinstance(owner, Principal)

    context = service.context_for(owner, uuid4())

    assert isinstance(context.job_id, UUID)
