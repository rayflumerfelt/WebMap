"""The DuckDB data plane. A library, not a service.

`adr/0002-duckdb-data-plane.md`: feature geometry, gridded values, tile
generation, and the aggregation catalog run in-process over GeoParquet and
COG on object storage. DuckDB is embedded here so there is no SQL path for a
geometry operation to escape through — which is what makes
`adr/0004-geoprocessing-owns-geometry.md` structural rather than aspirational.

Connections are cheap and short-lived: open, read immutable Parquet, close.
There is no shared mutable DuckDB state, so many concurrent readers across
many processes are fine (see the amendment to adr/0002).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import duckdb


@dataclass(frozen=True)
class ObjectStore:
    """Credentials for the S3-compatible store holding the data plane.

    Passed in by the caller rather than read from the environment. This
    package has no settings module and no framework imports; configuration is
    the orchestrator's job (`01-architecture.md` §3.1).
    """

    endpoint: str
    access_key: str
    secret_key: str
    region: str = "us-east-1"
    use_ssl: bool = False


#: The extensions the data plane cannot work without. `spatial` reads geometry;
#: `httpfs` reaches object storage.
REQUIRED_EXTENSIONS = ("spatial", "httpfs")


def _load(conn: duckdb.DuckDBPyConnection, extension: str) -> None:
    """Load an extension, preferring the copy already in the image.

    `INSTALL` reaches the network when the extension is not present locally,
    and that is a request-path dependency on the public internet. It is also
    where this failed in practice: a captive portal returned a 307 to a
    login splash for `http://extensions.duckdb.org`, and every tile request
    became a 500 whose message named neither the network nor the extension.

    `LOAD` is tried first so a properly built image never reaches out at all.
    The `INSTALL` fallback keeps a developer machine working; `assert_extensions`
    is what stops a container without them from serving traffic.
    """
    try:
        conn.execute(f"LOAD {extension}")
    except duckdb.Error:
        conn.execute(f"INSTALL {extension}")
        conn.execute(f"LOAD {extension}")


def assert_extensions() -> None:
    """Fail at startup if the DuckDB extensions are unavailable.

    The same reasoning as `assert_rls_enforced`: a deployment that cannot do
    its job should refuse to start, where the failure is obvious and names its
    cause, rather than serve a 500 on the first tile request that mentions an
    HTTP redirect. `00-overview.md` §7 puts this system on an internal network,
    so a container that has to download an extension is already broken — it
    just has not been asked for geometry yet.
    """
    conn = duckdb.connect(database=":memory:")
    try:
        for extension in REQUIRED_EXTENSIONS:
            try:
                _load(conn, extension)
            except duckdb.Error as error:
                raise RuntimeError(
                    f"DuckDB extension '{extension}' is neither present in this "
                    f"image nor downloadable ({error}). The data plane cannot "
                    f"read geometry or object storage without it. Rebuild the "
                    f"image on a network that can reach extensions.duckdb.org, "
                    f"or copy the extension into "
                    f"~/.duckdb/extensions/v<version>/<platform>/. Refusing to "
                    f"start."
                ) from error
    finally:
        conn.close()


@contextmanager
def connect(store: ObjectStore | None = None) -> Iterator[duckdb.DuckDBPyConnection]:
    """An in-memory DuckDB connection with the spatial extension loaded.

    `store` is omitted when reading local paths — the ingest writer and the
    tests both do that. Supplying it configures httpfs for `s3://` keys.
    """
    conn = duckdb.connect(database=":memory:")
    try:
        _load(conn, "spatial")
        if store is not None:
            _load(conn, "httpfs")
            # DuckDB's S3 settings are connection-scoped, so credentials never
            # outlive the connection and never reach a global.
            conn.execute(f"SET s3_endpoint = '{_escape(store.endpoint)}'")
            conn.execute(f"SET s3_access_key_id = '{_escape(store.access_key)}'")
            conn.execute(f"SET s3_secret_access_key = '{_escape(store.secret_key)}'")
            conn.execute(f"SET s3_region = '{_escape(store.region)}'")
            conn.execute(f"SET s3_use_ssl = {'true' if store.use_ssl else 'false'}")
            # MinIO and every other S3-compatible store that is not AWS.
            conn.execute("SET s3_url_style = 'path'")
        yield conn
    finally:
        conn.close()


def _escape(value: str) -> str:
    """Escape a value for a DuckDB SET statement.

    SET does not accept bind parameters, so these are interpolated. The values
    are deployment configuration rather than user input, but `03-auth-security.md`
    §9's rule is unconditional: never interpolate without escaping.
    """
    if "\x00" in value:
        raise ValueError("Object store configuration must not contain NUL bytes")
    return value.replace("'", "''")
