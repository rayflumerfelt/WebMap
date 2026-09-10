"""Applying a batch of feature edits to a versioned GeoParquet object.

`09-editing.md` §13, `adr/0005-single-editor-persistence.md`.

Feature objects are immutable and whole-object: an edit reads the current
version, applies the coalesced changes in memory, and writes
`features/ds_<hex>/v<N+1>.parquet`. Nothing is mutated, so a crash leaves an
orphan the retention job collects rather than a half-written layer.

Three decisions are made here rather than at the call site, because each is a
place where the forgiving behaviour is the wrong one:

**An unknown feature id is refused, not created.** Ids are assigned by the
writer, so a client-chosen id could collide with one this function would hand
out later — and an edit naming a feature that is not there means the client's
view is stale, which is what the version check exists to catch. The message
says so.

**A delete of every feature is refused.** `write_features` will not write an
empty object, and rightly: a zero-feature layer is indistinguishable from an
ingest that dropped everything. Deleting a layer is a different operation with
a different confirmation.

**`updated_at` is preserved for untouched features.** Stamping the whole layer
on every edit would make the column useless for exactly what it is for —
telling which features a session changed.

Geometry arrives in the dataset's **storage CRS**. This function never
reprojects: `adr/0003` puts that at defined boundaries, and the boundary for an
edit is where the API turns WGS84 from the browser into storage coordinates.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq
import shapely
from shapely.geometry.base import BaseGeometry

from webmap_io.exceptions import WebMapIOError
from webmap_io.parquet import WriteResult, write_features


class UnknownFeature(WebMapIOError):
    """An edit names a feature id the current version does not hold."""


class EmptyResult(WebMapIOError):
    """The edits would leave the layer with no features at all."""


@dataclass(frozen=True)
class FeatureChange:
    """One feature's new state, or its removal.

    `geometry` and `props` are independently optional so an attribute-only
    edit does not have to round-trip geometry it did not touch — which for a
    fault network is the difference between a few hundred bytes and a few
    megabytes on a rename.
    """

    feature_id: int
    #: `None` keeps the feature's current geometry. In the storage CRS.
    geometry: BaseGeometry | None = None
    #: `None` keeps the feature's current properties. Replaces them whole
    #: rather than merging: a merge cannot express clearing a field, and a
    #: field the user cleared coming back is worse than one they have to
    #: retype.
    props: Mapping[str, Any] | None = None
    deleted: bool = False

    def __post_init__(self) -> None:
        if self.deleted and (self.geometry is not None or self.props is not None):
            raise ValueError(
                f"Feature {self.feature_id} is marked deleted and also carries new "
                f"geometry or properties. A delete carries no new state; send two "
                f"changes if an edit and a delete were both intended."
            )


def apply_edits(
    source: Path,
    destination: Path,
    changes: Sequence[FeatureChange],
    *,
    srid: int,
) -> WriteResult:
    """Write the next version of a feature object with `changes` applied.

    `changes` are applied in order, so a batch that touches one feature twice
    ends with the last state — the coalescing §13 asks for, done here so a
    caller cannot get it wrong by de-duplicating on the way in.
    """
    if not changes:
        raise ValueError(
            "No changes to apply. Writing a new version identical to the "
            "current one would advance the version pointer and invalidate "
            "every cached tile for nothing."
        )

    table = pq.read_table(source, columns=["id", "geometry", "props", "updated_at"])
    ids: list[int] = [int(value) for value in table.column("id").to_pylist()]
    geometries: list[Any] = list(shapely.from_wkb(table.column("geometry").to_pylist()))
    props: list[dict[str, Any]] = [
        json.loads(value) if value else {} for value in table.column("props").to_pylist()
    ]
    updated: list[np.datetime64] = [
        np.datetime64(value, "us") for value in table.column("updated_at").to_pylist()
    ]

    position = {feature_id: index for index, feature_id in enumerate(ids)}
    now = np.datetime64("now", "us")
    removed: set[int] = set()

    for change in changes:
        index = position.get(change.feature_id)
        if index is None:
            raise UnknownFeature(
                f"Feature {change.feature_id} is not in version at {source.name}. "
                f"The edit was made against a version that has since been "
                f"replaced — reload the layer and reapply it. If this is a newly "
                f"drawn feature, it must be created rather than edited."
            )

        if change.deleted:
            removed.add(change.feature_id)
            continue

        # A feature edited after being deleted in the same batch comes back:
        # last state wins, and an undo of the delete inside one session is
        # exactly that sequence.
        removed.discard(change.feature_id)
        if change.geometry is not None:
            geometries[index] = change.geometry
        if change.props is not None:
            props[index] = dict(change.props)
        updated[index] = now

    keep = [index for index, feature_id in enumerate(ids) if feature_id not in removed]
    if not keep:
        raise EmptyResult(
            f"These edits delete all {len(ids)} features. A layer with no "
            f"features cannot be told from an import that dropped everything, "
            f"so it is not written — delete the dataset instead if that is "
            f"what was meant."
        )

    return write_features(
        destination,
        geometry=np.array([geometries[index] for index in keep], dtype=object),
        props=[props[index] for index in keep],
        srid=srid,
        ids=np.array([ids[index] for index in keep], dtype=np.int64),
        updated_at=np.array([updated[index] for index in keep], dtype="datetime64[us]"),
    )
