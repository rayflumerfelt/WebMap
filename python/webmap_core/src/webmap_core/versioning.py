"""Schema versioning for the JSON documents in the control plane.

`user_preferences`, `style_template`, `map_session`, and `palette` all carry
`schema_version` (`02-data-model.md` §6). Migrating unversioned JSON blobs is
miserable; the small cost is paid here instead.

Rules:
  - Loaders dispatch on `schema_version` and upgrade in memory.
  - Writers always write the current version.
  - A background job rewrites old rows opportunistically.
  - Never delete an upgrade path. v1 -> v2 -> v3 chains are fine.
"""

from collections.abc import Callable
from typing import Any

Document = dict[str, Any]
Upgrade = Callable[[Document], Document]

_UPGRADES: dict[tuple[str, int], Upgrade] = {}


def upgrader(kind: str, from_version: int) -> Callable[[Upgrade], Upgrade]:
    """Register an in-memory upgrade from `from_version` to the next."""

    def deco(fn: Upgrade) -> Upgrade:
        existing = _UPGRADES.get((kind, from_version))
        if existing is not None and existing is not fn:
            raise ValueError(
                f"An upgrade for {kind} v{from_version} is already registered "
                f"({existing.__module__}.{existing.__qualname__}). Two upgrades "
                f"from one version means the chain is ambiguous — merge them."
            )
        _UPGRADES[(kind, from_version)] = fn
        return fn

    return deco


def upgrade(kind: str, doc: Document, target: int) -> Document:
    """Walk a document forward to `target`, one version at a time."""
    version = doc.get("schema_version", 1)
    if version > target:
        raise ValueError(
            f"{kind} document is v{version} but this build understands up to "
            f"v{target}. It was written by a newer WebMap — upgrade this "
            f"deployment rather than downgrading the document."
        )
    while version < target:
        fn = _UPGRADES.get((kind, version))
        if fn is None:
            raise ValueError(
                f"No upgrade path for {kind} v{version} -> v{version + 1}. "
                f"Register one with @upgrader('{kind}', {version}) — upgrade "
                f"paths are never deleted (02-data-model.md §6)."
            )
        doc = fn(doc)
        version += 1
        doc["schema_version"] = version
    return doc
