# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.12-python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependency layer first, so a source edit does not reinstall the world.
# Every workspace member's manifest is needed for the lock to resolve.
COPY pyproject.toml uv.lock ./
COPY python/webmap_geo/pyproject.toml   python/webmap_geo/
COPY python/webmap_io/pyproject.toml    python/webmap_io/
COPY python/webmap_core/pyproject.toml  python/webmap_core/
COPY apps/api/pyproject.toml            apps/api/
COPY apps/worker/pyproject.toml         apps/worker/
COPY apps/render/pyproject.toml         apps/render/
COPY apps/mcp/pyproject.toml            apps/mcp/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-workspace --no-dev

COPY python/ python/
COPY apps/ apps/
COPY alembic.ini ./
COPY infra/migrations/ infra/migrations/
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev


CMD ["uv", "run", "--frozen", "arq", "webmap_worker.main.WorkerSettings"]
