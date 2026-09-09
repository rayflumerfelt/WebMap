# Cross-cutting tests

Package-level unit tests live beside their package (`python/*/tests`,
`apps/*/tests`). This tree holds the tests that span services.

| Directory | Contents | Phase |
|---|---|---|
| `fixtures/valid/` | Well-formed inputs, synthetic only (`CLAUDE.md` §7.5) | 1 |
| `fixtures/hostile/` | The eight cases from `11-file-io.md` §8 | 1 |
| `e2e/` | Playwright: session load, layer add, symbology edit, export | 2 |
| `visual/` | Render regression goldens (`06-rendering.md` §10) | 3 |
| `mcp_eval/` | MCP evaluations (`04-mcp-server.md` §10) | 3 |

The `hostile/` fixtures are not edge cases. Every one of them is something a
geologist will receive from a partner within the first month. Assert on the
*error message*, not just the failure — a bad message here costs a support
ticket every time.

Goldens under `visual/golden/` are committed; `visual/output/` is gitignored.
Regenerate goldens deliberately with `make update-goldens` and review the diff
in the PR. Never regenerate one to make a test pass (`CLAUDE.md` §7.5).
