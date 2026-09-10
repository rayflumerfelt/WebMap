# syntax=docker/dockerfile:1.7
# ~2 GB. Accepted — see 01-architecture.md §4.1 and 06-rendering.md §2.

# --- shell assets ------------------------------------------------------------
# The render shell needs MapLibre and the overlay bundle beside it, because it
# is loaded from file:// and fetches nothing (03-auth-security.md §7). Built
# here rather than committed: they are build outputs, and a vendored copy of
# maplibre-gl.js would drift from the version the app renders with — which is
# exactly the divergence screenshotting the page is meant to prevent.
FROM node:22-slim AS shell
WORKDIR /build
RUN corepack enable
# `tsconfig.base.json` is not optional decoration: every package's tsconfig
# extends it, and without it `tsc` falls back to its ES5 defaults — `Map`,
# `Set` and `Object.fromEntries` all stop existing, and the failure reads as
# a hundred type errors in library code rather than as a missing file.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml tsconfig.base.json ./
COPY packages/ packages/
COPY apps/web/package.json apps/web/
COPY apps/render/shell/ apps/render/shell/
RUN pnpm install --frozen-lockfile --ignore-scripts
RUN pnpm --filter @webmap/style-model build  && cd packages/ui && npx vite build --config vite.overlay.config.ts
RUN cp node_modules/.pnpm/maplibre-gl@*/node_modules/maplibre-gl/dist/maplibre-gl.js        node_modules/.pnpm/maplibre-gl@*/node_modules/maplibre-gl/dist/maplibre-gl.css        apps/render/shell/

# --- service -----------------------------------------------------------------
# The tag must match the `playwright` version in `uv.lock` (1.62.0). The
# image ships the browsers that release expects at a versioned path, and a
# mismatch fails at launch with "Executable doesn't exist at
# /ms-playwright/chromium_headless_shell-<build>" — after a clean build and
# a healthy-looking start.
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

# Fonts for HTML overlays (legend, title block). Map labels use glyph PBFs
# served by the API, but overlay text uses system fonts — headless Linux ships
# with almost none, and missing fonts render as boxes with no error anywhere.
RUN apt-get update && apt-get install -y --no-install-recommends \
      fonts-inter fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Pinned to the version that wrote uv.lock — see api.Dockerfile.
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

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
    uv sync --frozen --no-dev --no-install-workspace --package webmap-render

COPY python/ python/
COPY apps/ apps/
# The built shell, from the stage above. A missing bundle is a build failure
# here rather than a render that comes back without a legend and says nothing
# about why.
COPY --from=shell /build/apps/render/shell/ apps/render/shell/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package webmap-render

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
EXPOSE 8002
CMD ["uv", "run", "--no-sync", "python", "-m", "webmap_render.main"]
