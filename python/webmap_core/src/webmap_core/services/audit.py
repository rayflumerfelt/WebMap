"""The activity trail. `02-data-model.md` §3.12, `03-auth-security.md` §10.

Distinct from lineage: lineage says how a dataset was *made* and is durable
provenance; audit says what *happened* and who did it.

`actor_channel` is what makes "what did Claude do on my behalf" answerable —
and with several users, *whose* Claude. It comes from the request channel, not
from anything the caller asserts about itself.
"""

import json
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from webmap_core.logging import get_logger
from webmap_core.permissions import Principal

log = get_logger(__name__)


class AuditAction(StrEnum):
    """The events `03-auth-security.md` §10 requires.

    An enum rather than free strings so that a typo becomes an import error
    instead of an event nobody can find later. Tile reads are deliberately
    absent — too high volume, as §10 says.
    """

    LOGIN_SUCCEEDED = "auth.login.succeeded"
    LOGIN_FAILED = "auth.login.failed"
    TEAMS_CHANGED = "auth.teams.changed"

    DATASET_READ_MCP = "dataset.read.mcp"
    DATASET_CREATED = "dataset.created"
    DATASET_UPDATED = "dataset.updated"
    DATASET_DELETED = "dataset.deleted"

    PROJECT_CREATED = "project.created"
    PROJECT_UPDATED = "project.updated"
    PROJECT_DELETED = "project.deleted"

    # Autosave deliberately records nothing: the browser saves every few
    # seconds while a user pans, and burying §10's events under camera moves
    # would defeat the point of keeping them.
    SESSION_CREATED = "session.created"
    SESSION_UPDATED = "session.updated"
    SESSION_DELETED = "session.deleted"

    GRANT_CREATED = "grant.created"
    GRANT_REVOKED = "grant.revoked"
    OWNERSHIP_TRANSFERRED = "ownership.transferred"

    EXPORT_CREATED = "export.created"
    RENDER_CREATED = "render.created"
    JOB_SUBMITTED = "job.submitted"


async def record(
    conn: AsyncConnection,
    *,
    action: AuditAction,
    principal: Principal | None = None,
    actor_user_id: UUID | None = None,
    actor_channel: str | None = None,
    object_type: str | None = None,
    object_id: UUID | None = None,
    detail: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> None:
    """Write one audit event.

    `principal` is the normal path. `actor_user_id` and `actor_channel` exist
    for the events that happen before a principal is constructed — a failed
    login has no principal but is exactly the event worth recording.

    Deliberately not swallowing errors: an audit write that fails silently
    leaves a gap in the record that looks identical to nothing having
    happened. It participates in the caller's transaction, so an action whose
    audit cannot be written is not committed either.
    """
    user_id = actor_user_id if principal is None else principal.user_id
    channel = actor_channel if principal is None else principal.channel.value
    if channel is None:
        raise ValueError(
            "An audit event needs an actor_channel — it is what distinguishes "
            "an action a geologist took in the browser from one Claude took "
            "on their behalf (02-data-model.md §3.12)."
        )

    await conn.execute(
        text(
            """
            INSERT INTO audit_event (
                actor_user_id, actor_channel, action, object_type, object_id,
                detail, ip_address)
            VALUES (:user, :channel, :action, :object_type, :object_id,
                    :detail, :ip)
            """
        ),
        {
            "user": user_id,
            "channel": channel,
            "action": action.value,
            "object_type": object_type,
            "object_id": object_id,
            # Serialised here and cast in SQL rather than handed over as a
            # dict: asyncpg wants a string for jsonb, psycopg wants its own
            # Jsonb wrapper, and this is the one form both accept.
            "detail": json.dumps(detail) if detail is not None else None,
            "ip": ip_address,
        },
    )
    log.info(
        "audit",
        action=action.value,
        object_type=object_type,
        object_id=str(object_id) if object_id else None,
    )
