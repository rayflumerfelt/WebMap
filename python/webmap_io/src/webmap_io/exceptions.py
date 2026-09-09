"""I/O exceptions.

Every message here is one a geologist will read after a failed import, so it
names the fix. `11-file-io.md` §8 asserts on these messages, not just on the
failure — a bad message costs a support ticket every time.
"""


class WebMapIOError(Exception):
    """Base for reader, writer, and connector failures."""


class MissingCRS(WebMapIOError):
    """A source arrived with no coordinate reference system."""


class UnknownShare(WebMapIOError):
    """A share URI names a share that is not configured."""


class PathTraversal(WebMapIOError):
    """A resolved path escapes its configured share root."""


class UnsupportedFormat(WebMapIOError):
    """The file is not a format WebMap reads."""
