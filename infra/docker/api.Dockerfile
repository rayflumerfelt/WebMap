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
COPY alembic.ini ./
COPY infra/migrations/ infra/migrations/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package webmap-api

EXPOSE 8000
# --no-sync: the environment is already built above. Without it `uv run`
# re-syncs at container start, which reinstates the dev group and makes the
# running image differ from the built one.
CMD ["uv", "run", "--no-sync", "uvicorn", "webmap_api.main:app", \
     "--host", "0.0.0.0", "--port", "8000"]
