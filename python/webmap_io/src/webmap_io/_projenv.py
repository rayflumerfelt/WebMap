"""Pin PROJ and GDAL data directories to the ones our wheels ship.

GDAL resolves its coordinate database through the ambient `PROJ_DATA` /
`PROJ_LIB` and `GDAL_DATA` environment variables, and a machine-wide value
set by an unrelated install wins over the data inside the rasterio wheel. A
PostgreSQL/PostGIS installation sets exactly these, and the failure is:

    PROJ: proj_create_from_database: ...postgis-3.6/proj/proj.db contains
    DATABASE.LAYOUT.VERSION.MINOR = 2 whereas a number >= 6 is expected.

That is a hard error on `CRS.from_epsg`, so a COG cannot be written at all —
but the more dangerous case is the near miss, where a *compatible-but-older*
proj.db resolves an EPSG code with a superseded datum shift and every grid
lands tens of metres from where it belongs, with nothing raised.

`02-data-model.md` §1 says never infer a CRS. Silently accepting whichever
PROJ database happens to be on PATH is the same class of mistake one level
down, so the version of PROJ we resolve against is pinned rather than
inherited.

pyproj is unaffected: it locates its own data directory internally and does
not consult these variables. This is a GDAL problem only, which is why it
lives in the package that owns rasterio.
"""

import importlib.util
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    import rasterio

#: Both spellings. PROJ >= 9 reads PROJ_DATA; earlier versions read PROJ_LIB,
#: and GDAL builds in the wild still set the old one.
_PROJ_VARS = ("PROJ_DATA", "PROJ_LIB")


def _bundled(name: str) -> Path | None:
    """Locate a data directory inside the rasterio wheel *without importing it*.

    Importing rasterio initialises GDAL, which reads these variables once and
    caches the result — so an override applied afterwards has no effect and
    the failure looks identical to not having applied it at all. `find_spec`
    resolves the module's location without executing it.
    """
    spec = importlib.util.find_spec("rasterio")
    if spec is None or spec.origin is None:
        return None
    candidate = Path(spec.origin).parent / name
    return candidate if candidate.is_dir() else None


def pin_proj_data() -> dict[str, str]:
    """Point GDAL at the bundled PROJ and GDAL data. Returns what changed.

    Called at `webmap_io` import time, which is before any submodule of this
    package imports rasterio — Python imports a package before its submodules,
    so the ordering holds without an import-order dance that a formatter would
    reshuffle.

    An ambient value that already points at our own bundled directory is left
    alone, so a deployment that configures this deliberately is not overridden.
    """
    changed: dict[str, str] = {}

    proj_data = _bundled("proj_data")
    if proj_data is not None:
        for var in _PROJ_VARS:
            current = os.environ.get(var)
            if current is None or Path(current) != proj_data:
                os.environ[var] = str(proj_data)
                changed[var] = str(proj_data)

    gdal_data = _bundled("gdal_data")
    if gdal_data is not None:
        current = os.environ.get("GDAL_DATA")
        if current is None or Path(current) != gdal_data:
            os.environ["GDAL_DATA"] = str(gdal_data)
            changed["GDAL_DATA"] = str(gdal_data)

    return changed


def gdal_env() -> "rasterio.Env":
    """A rasterio Env with the pinned data directories re-asserted.

    `pin_proj_data` only wins if it runs before anything imports rasterio,
    because GDAL reads these once at initialisation. Import order across a
    whole application is not something to rely on — a script that happens to
    `import rasterio` above `import webmap_io` would silently get the wrong
    PROJ database.

    Every rasterio call in this package runs inside this context, so the
    pinning holds regardless of who imported what first. Verified both ways:
    the Env override corrects an already-initialised bad GDAL.
    """
    import rasterio

    data = os.environ.get("PROJ_DATA")
    gdal = os.environ.get("GDAL_DATA")
    options: dict[str, str] = {}
    if data:
        options["PROJ_DATA"] = data
        options["PROJ_LIB"] = data
    if gdal:
        options["GDAL_DATA"] = gdal
    return rasterio.Env(**options)
