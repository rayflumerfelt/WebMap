# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm

# uv pinned to the version that wrote uv.lock. The combined uv+python images
# lag the standalone binary, and an older uv cannot read a newer lock — with
# `--frozen` that surfaces as a build failure, but without it uv would quietly
# re-resolve and the image would not match the lock. Bump this with the lock.
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# rasterio's manylinux wheel links libexpat dynamically and does not vendor it;
# python:*-slim does not ship it. Missing, `import rasterio` fails with
# "libexpat.so.1: cannot open shared object file" at the first COG read or
# write — which in the worker is *after* a job has been accepted, solved, and
# is about to produce its only output.
#
# Installed in the API image too. Nothing there imports rasterio today, which
# is exactly why the gap went unnoticed: the first API path that reads a raster
# would 500 in production and pass every local test run outside Docker.
RUN apt-get update \
    && apt-get install --no-install-recommends -y libexpat1 \
    && rm -rf /var/lib/apt/lists/*


# `--package` is load-bearing, not an optimisation. The workspace root declares
# no runtime dependencies — members reach the environment through its dev group
# — so a plain `uv sync --no-dev` installs nothing at all. Naming the package
# pulls it and its workspace dependencies and leaves the other apps out.
#
# Dependency layer first, so a source edit does not reinstall the world. Every
# member's manifest is needed for the lock to resolve, even the ones not built
# into this image.
COPY pyproject.toml uv.lock ./
COPY python/webmap_geo/pyproject.toml   python/webmap_geo/
COPY python/webmap_io/pyproject.toml    python/webmap_io/
COPY python/webmap_core/pyproject.toml  python/webmap_core/
COPY apps/api/pyproject.toml            apps/api/
COPY apps/worker/pyproject.toml         apps/worker/
COPY apps/render/pyproject.toml         apps/render/
COPY apps/mcp/pyproject.toml            apps/mcp/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package webmap-api

COPY python/ python/
COPY apps/ apps/

# Map-label glyphs. MapLibre has no system-font fallback, so an image without
# these draws no labels — silently. Baked in rather than mounted for the same
# reason as the DuckDB extensions: 00-overview.md §7 puts this on an internal
# network, and a container that has to fetch something at runtime is already
# broken there.
COPY infra/glyphs/ /app/glyphs/
ENV WEBMAP_GLYPH_DIR=/app/glyphs
COPY alembic.ini ./
COPY infra/migrations/ infra/migrations/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package webmap-api

EXPOSE 8000

# DuckDB downloads `spatial` and `httpfs` on first use, which puts a public
# internet dependency in the request path — and 00-overview.md §7 puts this
# system on an internal network where that download cannot succeed. Baked in
# here so a running container never reaches out.
#
# Best-effort on purpose: a build behind a captive portal or an egress proxy
# cannot fetch them, and failing the build would leave a developer unable to
# build at all. What stops a container *without* them from serving traffic is
# `webmap_geo.dataplane.assert_extensions`, called at API and worker startup —
# so a bad image fails loudly at boot rather than 500ing on the first tile.
RUN uv run --no-sync python -c "\
import duckdb; c = duckdb.connect(); \
[c.execute(f'INSTALL {e}') for e in ('spatial', 'httpfs')]; \
print('duckdb extensions installed')" \
    || echo "WARNING: DuckDB extensions not baked in; this image will try to download them at runtime and assert_extensions will refuse to start if it cannot."

# --no-sync: the environment is already built above. Without it `uv run`
# re-syncs at container start, which reinstates the dev group and makes the
# running image differ from the built one.
CMD ["uv", "run", "--no-sync", "uvicorn", "webmap_api.main:app", \
     "--host", "0.0.0.0", "--port", "8000"]
