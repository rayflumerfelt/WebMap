"""Structured logging and OpenTelemetry wiring. `01-architecture.md` §7.

JSON to stdout, always. Every line carries `request_id` and `channel`
(`web` / `claude` / `worker`), plus `job_id` and `dataset_id` where they
apply — the critical trace is MCP tool call -> API -> worker -> render, which
crosses three services and is otherwise impossible to debug after the fact.

Provenance is *not* observability. Lineage records are first-class domain
data in the database (`02-data-model.md` §3.10), not log lines.
"""

import logging
from contextvars import ContextVar
from uuid import UUID

import structlog
from opentelemetry import trace
from structlog.typing import EventDict, WrappedLogger

# Set once per request or job and read by every log line below it. A
# ContextVar rather than a thread-local because the API is asyncio — a
# thread-local would leak across concurrent requests on the same thread.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
channel_var: ContextVar[str | None] = ContextVar("channel", default=None)
job_id_var: ContextVar[str | None] = ContextVar("job_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)


def _add_context(_logger: WrappedLogger, _method: str, event: EventDict) -> EventDict:
    """Attach the ambient request context to every event."""
    for key, var in (
        ("request_id", request_id_var),
        ("channel", channel_var),
        ("job_id", job_id_var),
        ("user_id", user_id_var),
    ):
        value = var.get()
        if value is not None:
            event.setdefault(key, value)
    return event


def _add_trace_context(_logger: WrappedLogger, _method: str, event: EventDict) -> EventDict:
    """Correlate logs with spans.

    Without trace_id on the log line, a trace tells you *that* the worker was
    slow and the logs tell you *why*, with no way to join the two.
    """
    span = trace.get_current_span()
    context = span.get_span_context()
    if context.is_valid:
        event.setdefault("trace_id", f"{context.trace_id:032x}")
        event.setdefault("span_id", f"{context.span_id:016x}")
    return event


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """Configure structlog and route stdlib logging through it.

    `json_output=False` gives the console renderer, which is for a developer
    reading a terminal. Never in a container — the log shipper expects JSON
    and a pretty-printed line is a parse failure per event.
    """
    shared: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        _add_context,
        _add_trace_context,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level]
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Everything third-party logs through stdlib. Send it to the same place,
    # or half the output is JSON and half is not.
    logging.basicConfig(format="%(message)s", level=level, force=True)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]


def bind_request(
    *,
    request_id: str,
    channel: str,
    user_id: UUID | None = None,
    job_id: UUID | None = None,
) -> None:
    """Bind the ambient context for a request or job.

    `channel` is what makes "what did Claude do on my behalf" answerable in
    the logs, mirroring `audit_event.actor_channel` (`02` §3.12).
    """
    request_id_var.set(request_id)
    channel_var.set(channel)
    user_id_var.set(str(user_id) if user_id else None)
    job_id_var.set(str(job_id) if job_id else None)
