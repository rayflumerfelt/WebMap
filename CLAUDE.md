# CLAUDE.md

Conventions and guardrails for developing WebMap. Read this before writing code.

This file lives at the repository root. Claude Code reads it automatically; add
`packages/*/CLAUDE.md` for package-specific rules as they accumulate.

---

## 1. What this project is

Geospatial mapping for subsurface geologists, with an MCP server exposing it to Claude.
Specifications live in `docs/00-overview.md` through `docs/12-roadmap.md`. **Read the relevant
spec before implementing a feature.** They contain decisions with rationale; re-deriving them
wastes time and usually lands somewhere worse.

If a spec is wrong, say so and propose a change. Do not silently work around it.

---

## 2. Commands

```bash
make setup            # Install all deps, both languages
make dev              # Start the full stack via Docker Compose
make check            # Lint + typecheck + test. Run before every commit.
make test             # Tests only
make test-visual      # Visual regression (needs the render service)
make update-goldens   # Regenerate visual goldens — review the diff
make migrate          # Apply Alembic migrations
make migration m="…"  # Generate a new migration
make mcp-inspect      # Launch MCP Inspector against the local server
make eval             # Run MCP evaluations
```

Single test:

```bash
uv run pytest python/webmap_geo/tests/test_kriging.py::test_recovers_synthetic_field -x
pnpm --filter @webmap/style-model test -- compile.test.ts
```

**Always run `make check` before declaring work complete.** Not "it should pass" — run it.

---

## 3. Non-negotiable rules

Violating these produces bugs that are invisible in review and expensive in production.

### 3.1 Coordinate reference systems

- **Never** run interpolation, distance, area, or buffer operations in a geographic CRS.
  Variogram ranges in degrees are meaningless.
- **Never** infer a CRS. If a file has no CRS, fail with a message asking for one.
- All geoprocessing takes an explicit `CrsContext`. Direct `pyproj` use outside
  `webmap_core.crs` is a lint error.
- Reprojection happens at defined boundaries only, never mid-algorithm.

### 3.2 Identity

- **Never** create a database session without a `Principal`. Use `principal_session()`.
- **Never** write a job payload without a `JobContext`. An anonymous job is a security bug.
- **Never** add a service-account path from MCP to data.
- `set_config(..., true)` — transaction-local. Without the `true`, RLS context leaks across
  pooled connections.

### 3.3 Determinism

- All randomness takes an explicit `numpy.random.Generator`. No bare `np.random.*`, no
  module-level seeding.
- Iterative solvers record tolerance and iteration count. A job that hit the iteration cap is
  flagged, not silently returned.
- Every derived dataset gets a lineage record with `webmap_geo.__version__`.

### 3.4 Destructive operations

- **Never** write in place to a source file. Editing creates a versioned output.
- Deletes are soft for 30 days.
- MCP destructive tools require `confirm: true`.

### 3.5 Package boundaries

- `python/webmap_geo` imports no web framework, no database, no `webmap_core`. NumPy and
  Shapely in, NumPy and Shapely out.
- `packages/map` imports nothing from `apps/web`.
- `packages/ui` imports no MapLibre.
- `packages/style-model` imports no React and no MapLibre.

Enforced by lint. If you need to violate one, the design is wrong.

---

## 4. Python conventions

### 4.1 Tooling

```toml
# pyproject.toml
[tool.ruff]
line-length = 96
target-version = "py312"

[tool.ruff.lint]
select = [
    "E", "F", "W",      # pycodestyle, pyflakes
    "I",                # isort
    "N",                # pep8-naming
    "UP",               # pyupgrade
    "B",                # bugbear
    "A",                # builtin shadowing
    "C4",               # comprehensions
    "DTZ",              # naive datetimes — always tz-aware
    "T20",              # no print()
    "SIM",              # simplify
    "PTH",              # pathlib over os.path
    "NPY",              # numpy-specific
    "RUF",
]
ignore = ["E501"]       # line length handled by the formatter

[tool.ruff.lint.flake8-tidy-imports.banned-api]
"numpy.random.seed".msg = "Use an explicit numpy.random.Generator. See CLAUDE.md §3.3."
"numpy.random.rand".msg = "Use an explicit numpy.random.Generator. See CLAUDE.md §3.3."

[tool.mypy]
python_version = "3.12"
strict = true
warn_unreachable = true
disallow_any_explicit = false     # geospatial libs return Any too often

[[tool.mypy.overrides]]
module = ["triangle.*", "pyamg.*", "gstools.*", "skgstat.*"]
ignore_missing_imports = true

[tool.pytest.ini_options]
addopts = "-ra --strict-markers --strict-config"
markers = [
    "slow: takes over 10 seconds",
    "reference: compares against Surfer/ArcGIS reference output",
    "integration: needs a database",
]
```

### 4.2 Style

- Type hints everywhere. `strict = true` is on; do not add `# type: ignore` without a comment
  explaining why.
- Pydantic models for anything crossing a boundary (API, MCP, job payload). `extra="forbid"` —
  catch typos in Claude-supplied parameters loudly rather than ignoring them.
- Dataclasses for internal value objects. `frozen=True` by default.
- `pathlib.Path`, never `os.path`.
- Timezone-aware datetimes, always.
- Custom exceptions with actionable messages. Never raise bare `Exception`.

### 4.3 Comments

Explain **why**, not what. The code says what it does.

```python
# Good — explains a non-obvious decision
# Subsample before variogram estimation: all-pairs on 500k points is 1.25e11
# distances. Declustered weights prevent dense well pads from dominating.
sample = rng.choice(points, size=20_000, replace=False, p=decluster_weights)

# Bad — restates the code
# Choose 20000 random points
sample = rng.choice(points, size=20_000, replace=False, p=decluster_weights)
```

Comment the geological reasoning especially. A future maintainer will know Python and not know
why a nugget effect matters.

---

## 5. TypeScript conventions

```jsonc
// tsconfig.json
{
  "compilerOptions": {
    "strict": true,
    "noUncheckedIndexedAccess": true,     // array access returns T | undefined
    "exactOptionalPropertyTypes": true,
    "noImplicitOverride": true,
    "verbatimModuleSyntax": true,
    "moduleResolution": "bundler",
    "target": "ES2022"
  }
}
```

- No `any`. `unknown` plus narrowing.
- No default exports except React components required to have them.
- Discriminated unions over optional-field soup. `Symbology` in
  `docs/08-styling-palettes.md` is the model.
- Props interfaces exported from the component file.
- No `useEffect` for derived state — use `useMemo`.

### 5.1 Desktop-first CSS

This application targets workstations (`00-overview.md` §7.1). Write desktop as the base case.

- **No `max-width` media queries.** Base styles are the 1440 px layout; breakpoints only add
  capability at `min-width: 1920px` and above. A `max-width` query in a PR is a review
  rejection.
- No mobile navigation patterns — no hamburger, no bottom sheet, no drawer-over-content.
- Mantine control sizes are `xs` or `sm`. Never `md` or larger.
- Use the density tokens in `layout.css` (`--row-h`, `--control-h`, `--hit-slop`) rather than
  ad-hoc values, so density stays consistent across panels.
- Hover-revealed actions must also appear on `:focus-visible` and be tab-reachable.
- Right-click context menus must also bind `Shift+F10`.

---

## 6. Testing

### 6.1 What to test

| Code | Requirement |
|---|---|
| `webmap_geo` algorithms | Reference comparison + property tests. Highest bar in the repo. |
| Permission logic | Exhaustive: every visibility × grant × role combination |
| Style compilation | Shared TS/Python vectors, both must pass |
| File readers | Every `hostile/` fixture, asserting on the error message |
| MCP tools | Evaluations, plus schema validation |
| React components | Behaviour, not implementation |
| Rendering | Visual goldens |

### 6.2 Reference tests

```python
@pytest.mark.reference
def test_minimum_curvature_matches_surfer():
    """Briggs minimum curvature vs Surfer 25 reference grid.

    Tolerance 0.5% of value range. Exact agreement is not expected —
    boundary conditions and convergence criteria differ — but systematic
    divergence means a bug in the stencil.
    """
```

State the tolerance and *why it is what it is*. A tolerance without justification gets widened
whenever a test fails, which defeats the purpose.

### 6.3 Property tests

```python
from hypothesis import given, strategies as st

@given(commands=st.lists(edit_command_strategy(), min_size=1, max_size=20))
def test_undo_restores_state(commands):
    """undo(apply(s)) == s for any command sequence."""
```

Use Hypothesis for anything with an algebraic property: undo/redo, style compile/decompile,
CRS round-trips, palette import/export.

### 6.4 What not to test

- Framework behaviour (that FastAPI parses JSON)
- Trivial getters
- Exact pixel values outside the visual harness

---

## 7. Working with AI agents on this codebase

Notes for both human and AI contributors.

### 7.1 Before writing code

1. Read the relevant spec in `docs/`.
2. Search for existing implementations. This codebase has deliberate abstractions —
   `Connector`, `CrsContext`, `Principal`, `Symbology`. Use them rather than adding parallel
   ones.
3. Check the package boundary rules. If your change needs a new cross-package import, stop and
   reconsider.

### 7.2 Scope discipline

- One logical change per commit.
- Do not refactor adjacent code while implementing a feature. Propose it separately.
- Do not add dependencies without saying why in the PR description. Every dependency in
  `webmap_geo` is load-bearing and reviewed.
- Do not "improve" specifications while implementing them. Flag the disagreement.

### 7.3 When blocked

Say so. A wrong guess in geoprocessing produces output that looks plausible and is wrong —
which is worse than no output, because someone will put it in a partner deck.

Specifically escalate rather than guessing on: CRS handling, variogram parameters, fault
semantics, permission edge cases, anything touching identity propagation.

### 7.4 Verification before completion

```
[ ] make check passes
[ ] New code has tests
[ ] Reference tests added for new algorithms
[ ] No new lint suppressions without a comment
[ ] Error messages name a next action
[ ] Migrations apply and roll back
[ ] Spec docs updated if behaviour changed
[ ] ADR written if a documented decision was contradicted
```

### 7.5 Do not

- Do not commit secrets, `.env` files, or real data. `tests/fixtures/` is synthetic only.
- Do not regenerate visual goldens to make a test pass. Investigate the diff.
- Do not widen a reference-test tolerance to make it pass.
- Do not add `# type: ignore` or `eslint-disable` without an explanatory comment.
- Do not use `print()` — use `structlog`.
- Do not catch broad exceptions to suppress errors.

---

## 8. Error message standard

Error messages are a user interface, and for MCP tools they are *the* interface. Claude reads
them and decides what to do next.

Every error answers three questions: what happened, why, and what now.

```python
# Good
raise MissingCRS(
    f"{path.name} has no coordinate reference system. Shapefiles store CRS "
    f"in a companion .prj file — check it was included in the upload. You "
    f"can also specify the CRS explicitly on import if you know it."
)

# Bad
raise ValueError("No CRS")
```

For permission errors, name the owner so the conversation can continue:

```python
raise PermissionDenied(
    f"You have viewer access to '{obj.name}' but editor is required. "
    f"Ask {owner_name} to grant edit access."
)
```

---

## 9. Git conventions

Conventional Commits, scoped by package:

```
feat(geo): fault-aware neighbourhood search for ordinary kriging
fix(render): wait for document.fonts.ready before screenshot
perf(io): bulk COPY instead of INSERT for feature loading
docs(mcp): clarify polling guidance in webmap_get_job description
```

Branches: `feat/fault-aware-kriging`, `fix/render-font-race`.

PR description states: what changed, why, how it was verified, and any spec updates.

---

## 10. Architecture Decision Records

`docs/adr/NNNN-title.md` whenever a decision contradicts or extends the specs.

```markdown
# 0007 — Cache Dijkstra frontiers by fault compartment

## Status
Accepted

## Context
Fault-aware kriging missed the 5-minute target at 100k points, running at
roughly 11 minutes. Profiling showed 78% of time in per-node Dijkstra.

## Decision
Process grid nodes in compartment-major order and reuse the search frontier
across nodes within the same compartment.

## Consequences
Runtime drops to ~4 minutes. Memory rises by ~200 MB for a 1000×1000 grid.
Node processing order is now significant — a future parallelisation must
partition by compartment, not by row.
```

---

## 11. Pre-commit

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.8.4
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format

  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.14.0
    hooks:
      - id: mypy
        additional_dependencies: [pydantic, numpy, types-shapely]

  - repo: local
    hooks:
      - id: eslint
        name: eslint
        entry: pnpm exec eslint --fix
        language: system
        files: \.(ts|tsx)$

      - id: tsc
        name: typecheck
        entry: pnpm exec tsc --build --noEmit
        language: system
        pass_filenames: false
        files: \.(ts|tsx)$

      - id: no-bare-random
        name: no bare numpy random
        entry: 'np\.random\.(seed|rand|randn|choice|permutation)\('
        language: pygrep
        files: \.py$
        exclude: ^tests/

      - id: no-max-width-query
        name: no max-width media queries (desktop-first)
        entry: '@media[^{]*max-width'
        language: pygrep
        files: \.(css|scss|ts|tsx)$

      - id: no-raw-db-session
        name: no direct engine.connect
        entry: 'engine\.(connect|begin)\('
        language: pygrep
        files: ^(apps|python)/.*\.py$
        exclude: ^(python/webmap_core/db/session\.py|tests/)

  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.21.2
    hooks:
      - id: gitleaks
```

The `local` pygrep hooks enforce §3.2, §3.3, and §5.1 mechanically. They are crude and they work.

---

## 12. CI

```yaml
# .github/workflows/ci.yml
name: CI
on: [push, pull_request]

jobs:
  python:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgis/postgis:16-3.4
        env: { POSTGRES_PASSWORD: postgres }
        options: >-
          --health-cmd pg_isready --health-interval 10s --health-retries 5
      redis:
        image: redis:7-alpine
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - run: uv sync --all-packages
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run mypy .
      - run: uv run pytest -m "not slow" --cov
      - run: uv run alembic upgrade head && uv run alembic downgrade base

  typescript:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: pnpm/action-setup@v4
      - run: pnpm install --frozen-lockfile
      - run: pnpm lint
      - run: pnpm typecheck
      - run: pnpm test

  style-parity:
    # The compilers must agree. This is the one job that catches drift
    # between the TypeScript and Python style compilers.
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pnpm --filter @webmap/style-model test
      - run: uv run pytest python/webmap_core/tests/test_style_vectors.py

  visual:
    runs-on: ubuntu-latest
    container: mcr.microsoft.com/playwright/python:v1.49.0-noble
    steps:
      - uses: actions/checkout@v4
      - run: uv sync
      - run: uv run pytest tests/visual/
      - uses: actions/upload-artifact@v4
        if: failure()
        with: { name: visual-diffs, path: tests/visual/output/ }

  mcp-eval:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: make eval
```

The `visual` job uploads diffs on failure. Reviewing an image is faster than reading a pixel
count.

---

## 13. Domain glossary

Terms that appear throughout and are not general software vocabulary.

| Term | Meaning |
|---|---|
| **Breakline** | Soft constraint. Value continuous across it, gradient discontinuous. Carries its own Z. |
| **Fault (hard constraint)** | No interpolation across it. Value discontinuous. |
| **Nugget** | Variogram intercept at zero lag. Measurement noise plus sub-sample-spacing variability. |
| **Sill** | Variogram plateau. Total variance. |
| **Range** | Lag distance at which the variogram reaches the sill. Beyond it, points are uncorrelated. |
| **Anisotropy** | Direction-dependent spatial correlation. Ratio + azimuth of the major axis. |
| **Declustering** | Weighting to correct for non-random sampling. Well control clusters by development history, not geology. |
| **Isopach** | Thickness map. |
| **Structure map** | Depth or elevation to a geological surface. |
| **Control point** | A well or measurement location with a known value. |
| **Throw** | Vertical displacement across a fault. |
| **Compartment** | A region bounded by faults, hydraulically or interpolation-isolated. |
| **TVDSS** | True vertical depth subsea. Negative below sea level in some conventions — check `project.depth_positive_down`. |
| **Blanking value** | Surfer's nodata sentinel, 1.70141e38. Convert to NaN on read. |

---

## 14. Quick reference

| Need | Location |
|---|---|
| Entity model, DDL | `docs/02-data-model.md` |
| Permission logic | `python/webmap_core/permissions.py` |
| CRS handling | `python/webmap_core/crs.py` |
| Interpolation | `python/webmap_geo/interpolate/` |
| Style compilation | `packages/style-model/` and `python/webmap_core/style/` |
| MCP tools | `apps/api/mcp/server.py` |
| Render service | `apps/render/service.py` |
| Map component | `packages/map/src/WebMap.tsx` |
| Test fixtures | `tests/fixtures/` |
| Visual goldens | `tests/visual/golden/` |
