"""The file share connector. `11-file-io.md` §2.2.

**This is the awkward one in a multi-user deployment**, and the spec says so
rather than glossing it: the server holds the credentials to the share, which
collapses per-user authorization at the filesystem boundary. Anything the
service account can read, this service can read on anybody's behalf.

The four mitigations §2.2 requires are the substance of this module, and three
of the four are enforced here rather than documented:

1. **Read-only.** There is no write path in this class. Not a flag — an absence.
2. **Shares are mapped explicitly.** A share not in the configuration is not
   reachable, so adding a share is a deployment decision and never an accident
   of someone typing a path.
3. **Registration sets `owner_team_id` from that mapping**, so ordinary object
   permissions apply from ingest onward. The mapping carries the team; the
   ingest service does the setting.
4. **Path traversal is blocked** — a resolved path that escapes its root is
   refused, including by way of a symlink, which `Path.resolve()` follows and
   an unresolved comparison would not see.

Kerberos constrained delegation would preserve per-user identity end to end and
is the theoretically correct answer. It is genuinely painful to operate, and
§2.2 defers it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from webmap_io.connectors.base import (
    Connector,
    PathTraversal,
    SourceDescriptor,
    UnknownSource,
)

#: Extensions worth listing. A share holds spreadsheets, documents and years of
#: unrelated files; listing all of them turns a picker into a file browser and
#: buries the four files anybody wants.
READABLE_SUFFIXES = frozenset(
    {".shp", ".gpkg", ".geojson", ".json", ".csv", ".txt", ".xyz", ".las", ".grd", ".tif"}
)

#: Read in blocks rather than whole: a share holds multi-gigabyte rasters, and
#: checksumming one by loading it into memory is how a worker gets killed.
_CHUNK = 1024 * 1024

#: Sidecars that must travel with a shapefile. `.prj` is the one that matters —
#: without it the reader has no CRS and `03.1` forbids inferring one — but a
#: missing `.dbf` loses every attribute just as silently.
SHAPEFILE_SIDECARS = (".shx", ".dbf", ".prj", ".cpg", ".qpj", ".sbn", ".sbx")


@dataclass(frozen=True)
class ShareConfig:
    """One configured share.

    `team_id` is not decoration: it is what makes mitigation 3 work. A dataset
    registered from this share is owned by this team from the moment it exists,
    so the permission model applies to share-sourced data exactly as it does to
    uploaded data.
    """

    root: Path
    team_id: UUID
    description: str | None = None


def parse_share_uri(uri: str) -> tuple[str, str]:
    """`share://name/relative/path` to `(name, relative path)`.

    A scheme, rather than a bare path, because a bare path in the database is
    indistinguishable from a local one and there is no way to tell later which
    share root it was relative to.
    """
    prefix = "share://"
    if not uri.startswith(prefix):
        raise UnknownSource(
            f"'{uri}' is not a share URI. They look like "
            f"'share://picks/2026/wolfcamp.shp' — the first segment names a "
            f"configured share and the rest is a path within it."
        )
    remainder = uri[len(prefix) :]
    share, _, relative = remainder.partition("/")
    if not share:
        raise UnknownSource(f"'{uri}' names no share. Expected 'share://<name>/<path>'.")
    return share, relative


class FileShareConnector(Connector):
    """SMB/CIFS shares mounted read-only into the container."""

    kind = "fileshare"

    def __init__(self, shares: dict[str, ShareConfig]) -> None:
        self._shares = shares

    def team_for(self, uri: str) -> UUID:
        """The team that owns anything registered from this URI. §2.2's third
        mitigation, which the ingest path applies."""
        share, _ = parse_share_uri(uri)
        return self._config(share).team_id

    def _config(self, share: str) -> ShareConfig:
        config = self._shares.get(share)
        if config is None:
            raise UnknownSource(
                f"Share '{share}' is not configured, so nothing on it is "
                f"reachable. Configured shares: "
                f"{', '.join(sorted(self._shares)) or '(none)'}. Adding one is a "
                f"deployment change, not a per-user setting."
            )
        return config

    def _resolve(self, uri: str) -> Path:
        share, relative = parse_share_uri(uri)
        config = self._config(share)
        root = config.root.resolve()
        # Resolved on both sides. An unresolved comparison passes a symlink
        # pointing out of the share, which is the interesting half of the
        # attack rather than the `../` half.
        resolved = (root / relative).resolve()
        if not resolved.is_relative_to(root):
            raise PathTraversal(
                f"'{uri}' resolves outside share '{share}'. Shares are rooted "
                f"deliberately, and a path that leaves its root is refused "
                f"whether it got there by '..' or by a symlink."
            )
        return resolved

    async def list(self, prefix: str | None = None) -> list[SourceDescriptor]:
        """Readable files under a share, or under a path within one.

        Sorted, because an unsorted listing changes order between calls for no
        reason a user can see, and a picker that reshuffles is one people stop
        trusting.
        """
        if prefix is None:
            return [
                SourceDescriptor(uri=f"share://{name}/", name=name, format_hint="share")
                for name in sorted(self._shares)
            ]

        root = self._resolve(prefix)
        share, _ = parse_share_uri(prefix)
        if not root.is_dir():
            return [await self.describe(prefix)]

        found = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in READABLE_SUFFIXES:
                continue
            relative = path.relative_to(self._config(share).root.resolve())
            found.append(self._describe_path(f"share://{share}/{relative.as_posix()}", path))
        return found

    async def describe(self, uri: str) -> SourceDescriptor:
        path = self._resolve(uri)
        if not path.is_file():
            raise UnknownSource(
                f"'{uri}' is not a file on the share. It may have been moved or "
                f"renamed upstream; re-pick the source to repoint the dataset."
            )
        return self._describe_path(uri, path)

    def _describe_path(self, uri: str, path: Path) -> SourceDescriptor:
        stat = path.stat()
        return SourceDescriptor(
            uri=uri,
            name=path.name,
            size_bytes=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            checksum=checksum_of(path),
            format_hint=path.suffix.lower().lstrip("."),
        )

    async def fetch(self, uri: str, dest: Path) -> Path:
        """Copy to local disk, sidecars and all.

        Copied rather than read in place because §2.4 is emphatic: reading
        shapefiles off a share for every render is miserably slow, so a share is
        an upstream to sync from and never a live source.
        """
        source = self._resolve(uri)
        if not source.is_file():
            raise UnknownSource(f"'{uri}' is not a file on the share.")

        dest.mkdir(parents=True, exist_ok=True)
        target = dest / source.name
        target.write_bytes(source.read_bytes())

        if source.suffix.lower() == ".shp":
            for suffix in SHAPEFILE_SIDECARS:
                sidecar = source.with_suffix(suffix)
                if sidecar.is_file():
                    (dest / sidecar.name).write_bytes(sidecar.read_bytes())
        return target


def checksum_of(path: Path) -> str:
    """SHA-256 of a file, read in blocks.

    Content rather than mtime: a share restored from backup has new mtimes and
    identical bytes, and re-ingesting every dataset on a restore is both slow
    and a pile of new versions nobody asked for.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "READABLE_SUFFIXES",
    "SHAPEFILE_SIDECARS",
    "FileShareConnector",
    "ShareConfig",
    "checksum_of",
    "parse_share_uri",
]
