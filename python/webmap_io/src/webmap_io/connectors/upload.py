"""The upload connector. `11-file-io.md` §2.1.

The simplest case, and it still earns a connector rather than a special case:
a dataset that came from an upload and one that came from a share are synced,
exported and re-read by the same code, and the only way to keep that true is
for the upload path to be a `Connector` like the others.

**An upload cannot change.** The object it was ingested from is immutable in
object storage, so `has_changed` is always `False` and a sync of an
upload-sourced dataset is a no-op that records the time. That is not a
degenerate case to apologise for — it is the answer, and returning it here
keeps every caller from having to ask which kind of source it holds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from webmap_io.connectors.base import Connector, SourceDescriptor, UnknownSource
from webmap_io.storage import get_file


def parse_object_uri(uri: str) -> tuple[str, str]:
    """`s3://bucket/key` to `(bucket, key)`."""
    prefix = "s3://"
    if not uri.startswith(prefix):
        raise UnknownSource(
            f"'{uri}' is not an object URI. Uploaded sources look like "
            f"'s3://webmap/uploads/<id>/picks.csv'."
        )
    bucket, _, key = uri[len(prefix) :].partition("/")
    if not bucket or not key:
        raise UnknownSource(f"'{uri}' names no key. Expected 's3://<bucket>/<key>'.")
    return bucket, key


class UploadConnector(Connector):
    """Files posted through the API and held in object storage."""

    kind = "upload"

    def __init__(self, store: Any) -> None:
        self._store = store

    async def list(self, prefix: str | None = None) -> list[SourceDescriptor]:
        """Uploads are not browsable.

        Deliberately empty rather than unimplemented: an upload belongs to the
        dataset it created, and listing the bucket would offer one user another
        user's file — the exact thing `dataset` rows and their permissions
        exist to mediate.
        """
        return []

    async def describe(self, uri: str) -> SourceDescriptor:
        # Parsed for its validation, not its bucket: describing an object that
        # is not addressable should fail here rather than at fetch time.
        _bucket, key = parse_object_uri(uri)
        return SourceDescriptor(
            uri=uri,
            name=key.rsplit("/", 1)[-1],
            format_hint=key.rsplit(".", 1)[-1].lower() if "." in key else None,
        )

    async def fetch(self, uri: str, dest: Path) -> Path:
        bucket, key = parse_object_uri(uri)
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / key.rsplit("/", 1)[-1]
        return get_file(self._store, bucket, key, target)

    async def has_changed(self, uri: str, known_checksum: str | None) -> bool:
        """Never. The object it names is immutable."""
        return False


__all__ = ["UploadConnector", "parse_object_uri"]
