"""Geoprocessing worker. CPU-bound, memory-hungry, scaled independently.

Every task takes a `JobContext` and runs as the user who asked for it. A job
that resolves datasets without one is a security bug, not a style issue
(`03-auth-security.md` §5.1).
"""

__version__ = "0.1.0"
