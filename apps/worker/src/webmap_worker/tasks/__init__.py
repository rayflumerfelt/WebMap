"""Worker tasks.

`health.ping` is the queue round-trip check. The geoprocessing tasks —
interpolate, contour, aggregate, ingest, sync, export — arrive in Phase 4
(`12-roadmap.md`).
"""

from webmap_worker.tasks.health import ping

__all__ = ["ping"]
