# WebMap. The command surface documented in CLAUDE.md §2.
#
# Requires: Node 20+, pnpm, uv, Docker, GNU Make.
# On Windows, `make` is not present by default — install GNU Make (for example
# `winget install ezwinports.make`) or run the underlying commands directly;
# each recipe below is a single line for exactly that reason.

PNPM ?= pnpm
UV ?= uv
COMPOSE ?= docker compose -f infra/compose.yaml

.DEFAULT_GOAL := help
.PHONY: help setup dev down check lint typecheck test test-visual update-goldens \
        migrate migration seed mcp-inspect eval fmt clean render-shell

help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

setup:  ## Install all deps, both languages
	$(UV) sync
	$(PNPM) install

dev:  ## Start the full stack via Docker Compose
	$(COMPOSE) up --build

down:  ## Stop the stack
	$(COMPOSE) down

# CLAUDE.md §2: "Always run `make check` before declaring work complete.
# Not 'it should pass' — run it."
check: lint typecheck test  ## Lint + typecheck + test. Run before every commit.

lint:  ## Lint both languages, and the package boundary contracts
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run lint-imports
	$(PNPM) exec eslint .

typecheck:  ## Typecheck both languages
	$(UV) run mypy
	$(PNPM) exec tsc --build

test:  ## Tests only
	$(UV) run pytest -m "not slow"
	$(PNPM) exec vitest run

fmt:  ## Autoformat both languages
	$(UV) run ruff check --fix .
	$(UV) run ruff format .
	$(PNPM) exec eslint . --fix

test-visual:  ## Visual regression (needs the render service)
	$(COMPOSE) --profile render up -d render
	$(UV) run pytest tests/visual/

# CLAUDE.md §7.5: never regenerate goldens to make a test pass. Investigate
# the diff — `tests/visual/output/` holds the actual images.
update-goldens:  ## Regenerate visual goldens — review the diff
	$(UV) run pytest tests/visual/ --update-goldens

render-shell:  ## Build the render shell's MapLibre and overlay bundles
	$(PNPM) --filter @webmap/style-model build
	cd packages/ui && npx vite build --config vite.overlay.config.ts
	cp node_modules/.pnpm/maplibre-gl@*/node_modules/maplibre-gl/dist/maplibre-gl.js 	   node_modules/.pnpm/maplibre-gl@*/node_modules/maplibre-gl/dist/maplibre-gl.css 	   apps/render/shell/

migrate:  ## Apply Alembic migrations
	$(UV) run alembic upgrade head

migration:  ## Generate a new migration: make migration m="add x"
	$(UV) run alembic revision -m "$(m)"

seed:  ## Load synthetic Midland Basin data into the local stack
	$(UV) run python scripts/seed.py

mcp-inspect:  ## Launch MCP Inspector against the local server
	npx --yes @modelcontextprotocol/inspector $(UV) run webmap-mcp

eval:  ## Run MCP evaluations
	$(UV) run pytest tests/mcp_eval/

clean:  ## Remove build output and caches
	rm -rf .ruff_cache .mypy_cache .pytest_cache .import_linter_cache .turbo
	rm -rf packages/*/dist packages/*/*.tsbuildinfo apps/web/dist apps/web/dist-types
