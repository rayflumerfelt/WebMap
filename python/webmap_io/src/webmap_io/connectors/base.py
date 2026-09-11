"""The connector abstraction. `11-file-io.md` §2.

Data lives in three places — uploaded, on a share, in somebody's PostGIS — and
the point of this abstraction is that none of them is special-cased anywhere
else. **Connectors resolve and fetch. They do not parse.** A new format then
works across every connector, and a new connector works with every format,
which is the whole return on the indirection.

The asymmetry worth naming: `fetch` is the only method that must handle a
*multi-file* format. A shapefile is five files and a caller that fetched only
the `.shp` gets a file that reads as empty rather than as broken
(`11` §4.1), so fetching sidecars is the connector's job and not the reader's.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class SourceDescriptor:
    """What a connector reports about a source, before ingest.

    Every field but `uri` and `name` is optional because every field but those
    two is something a source may genuinely not know. A share reports a size
    and an mtime; a database view reports neither. **`checksum` being `None` is
    not "unchanged"** — it means "cannot tell", and `has_changed` answers `True`
    for it rather than skipping a sync that was asked for.
    """

    uri: str
    name: str
    size_bytes: int | None = None
    modified_at: datetime | None = None
    checksum: str | None = None
    format_hint: str | None = None


class ConnectorError(Exception):
    """Base for every connector failure. Each carries a next action."""


class UnknownSource(ConnectorError):
    """The URI names something this connector cannot reach."""


class PathTraversal(ConnectorError):
    """A resolved path escaped its configured root."""


class Connector(ABC):
    """A source of geospatial data."""

    #: Matches `connector_kind_t` in the schema, and is written to
    #: `dataset.connector` so a sync can find its way back here.
    kind: str

    @abstractmethod
    async def list(self, prefix: str | None = None) -> list[SourceDescriptor]:
        """What is available, optionally under a prefix."""

    @abstractmethod
    async def describe(self, uri: str) -> SourceDescriptor:
        """One source's metadata, without fetching it."""

    @abstractmethod
    async def fetch(self, uri: str, dest: Path) -> Path:
        """Materialise to local disk, sidecars included.

        Returns the path to open — the `.shp` of a shapefile, not the
        directory it landed in.
        """

    async def has_changed(self, uri: str, known_checksum: str | None) -> bool:
        """Whether the source differs from what was last ingested.

        Defaulted rather than abstract, because every connector that can
        produce a checksum answers this the same way, and the two that cannot
        both want the same conservative answer: **re-read**. A sync that
        wrongly re-reads costs time; one that wrongly skips leaves a geologist
        looking at last month's picks believing they are current.
        """
        if known_checksum is None:
            return True
        descriptor = await self.describe(uri)
        if descriptor.checksum is None:
            return True
        return descriptor.checksum != known_checksum


__all__ = [
    "Connector",
    "ConnectorError",
    "PathTraversal",
    "SourceDescriptor",
    "UnknownSource",
]
