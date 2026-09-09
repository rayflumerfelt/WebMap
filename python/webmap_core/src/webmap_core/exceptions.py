"""Domain exceptions.

Every message answers three questions: what happened, why, and what now
(`CLAUDE.md` §8). For MCP tools the message *is* the interface — Claude reads
it and decides what to do next, so a dead-end message ends the conversation.
"""


class WebMapError(Exception):
    """Base for every error this system raises deliberately."""


class PermissionDenied(WebMapError):
    """The principal lacks the required permission on an object."""


class NotFound(WebMapError):
    """A referenced object does not exist, or is not visible to the principal."""


class MissingCRS(WebMapError):
    """A source arrived with no coordinate reference system. Never guess one."""


class InvalidToken(WebMapError):
    """A scoped tile or render token failed verification."""


class QuotaExceeded(WebMapError):
    """A per-user or per-team resource limit was reached."""


class LimitExceeded(WebMapError):
    """A request exceeds a hard resource bound."""


class VersionConflict(WebMapError):
    """An optimistic commit lost a race. Surfaces as HTTP 409.

    See `adr/0005-single-editor-persistence.md`: two editors contend on
    `dataset.version`, never on feature rows. The loser rebases; nothing is
    silently overwritten.
    """
