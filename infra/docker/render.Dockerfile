# syntax=docker/dockerfile:1.7
# ~2 GB. Accepted — see 01-architecture.md §4.1 and 06-rendering.md §2.
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

# Fonts for HTML overlays (legend, title block). Map labels use glyph PBFs
# served by the API, but overlay text uses system fonts — headless Linux ships
# with almost none, and missing fonts render as boxes with no error anywhere.
RUN apt-get update && apt-get install -y --no-install-recommends \
      fonts-inter fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app

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
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
EXPOSE 8002
CMD ["uv", "run", "--frozen", "python", "-m", "webmap_render.main"]
