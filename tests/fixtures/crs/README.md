# CRS definition fixtures

What `project.crs_wkt` actually returns, captured from `pyproj` — the exact
string the browser's cursor readout has to parse
(`apps/web/src/crs/analysisCrs.ts`).

Checked from **both sides**, which is the point:

- `python/webmap_geo/tests/test_crs_definition.py` asserts `crs_definition()`
  still produces this. If pyproj changes its WKT dialect, that fails.
- `apps/web/src/crs/analysisCrs.test.ts` asserts proj4 parses it and lands on
  the right coordinates. If proj4js stops handling the dialect, that fails.

Either failure alone would otherwise be invisible: the server would keep
serving a definition the browser silently could not use, and the status bar
would show nothing with no error anywhere. That already nearly happened —
`to_wkt()` defaults to WKT2 while an earlier fixture here was WKT1, so the
frontend test was passing against a format the server does not send.

Regenerate deliberately, never automatically:

```bash
uv run python -c "import pathlib; from webmap_geo.crs import crs_definition;   pathlib.Path('tests/fixtures/crs/epsg2277.wkt').write_text(crs_definition(2277))"
```
