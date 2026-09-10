"""Visual regression. `06-rendering.md` §10.

Six cases, each protecting something a unit test cannot see. The harness and
the reasoning live in `conftest.py`; this file is the loop.

Run against a render service:

    docker compose -f infra/compose.yaml up -d render
    uv run pytest tests/visual/

With no goldens committed, every case **skips with instructions** rather than
passing. That is the point: a green suite that checked nothing is worse than a
skipped one that says so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.visual.conftest import (
    CASES,
    TOLERANCE,
    compare,
    golden_path,
    load_fixture,
    require_golden,
)

pytestmark = pytest.mark.visual


def render_case(render_url: str, case: str) -> bytes:
    """Render one fixture through the service's own HTTP interface.

    Through HTTP rather than by importing `webmap_render.service`: the browser
    pool, the request guards and the shell are the parts most likely to break,
    and calling the function directly skips all three.

    **The master image, not the preview.** `/render` returns both; the preview
    is downscaled to 1600 px for an MCP response (`adr/0006`), and comparing
    the downscaled one would let a change smaller than the resampling kernel
    through unnoticed.

    A render that could not fetch something still returns an image, with the
    failures listed. Those are raised here rather than compared: a map missing
    a layer differs from its golden everywhere, and "37% of pixels differ" is a
    much worse description of the problem than the failed URL.
    """
    import base64

    import httpx

    spec = load_fixture(case)
    response = httpx.post(f"{render_url}/render", json=spec, timeout=120)
    response.raise_for_status()
    body = response.json()

    if body.get("failed_requests"):
        raise AssertionError(
            f"{case}: the render could not fetch "
            f"{len(body['failed_requests'])} resource(s): "
            f"{body['failed_requests']}. The image would differ from its golden "
            f"everywhere, which describes the problem far worse than this does."
        )
    return base64.b64decode(body["image_base64"])


@pytest.mark.parametrize("case", sorted(CASES), ids=sorted(CASES))
def test_render_matches_golden(
    case: str, render_url: str, output_dir: Path, updating_goldens: bool
) -> None:
    """The rendered map is the golden, to within antialiasing.

    The tolerance absorbs jitter between Chromium builds and nothing larger: a
    label moved by one line on a 2560×1440 slide changes far more than 0.1% of
    the image. Raising it to make a case pass hides the class of change this
    exists to catch — `CLAUDE.md` §7.5.

    Under `--update-goldens` this writes rather than compares, and says so.
    Nothing about a *failure* can reach that path: the flag is read from the
    command line before any case runs.
    """
    if updating_goldens:
        image = render_case(render_url, case)
        target = golden_path(case)
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.is_file()
        target.write_bytes(image)
        pytest.skip(
            f"{'Replaced' if existed else 'Wrote'} {target}. Look at it before "
            f"committing — a golden nobody reviewed makes every later diff "
            f"meaningless."
        )

    golden = require_golden(case)
    changed = compare(render_case(render_url, case), golden, output_dir / f"{case}.png")

    assert changed < TOLERANCE, (
        f"{case}: {changed:.3%} of pixels differ (tolerance {TOLERANCE:.1%}). "
        f"{CASES[case]} "
        f"Review tests/visual/output/{case}.png against tests/visual/golden/{case}.png. "
        f"If the change is intended, regenerate with `make update-goldens` and put "
        f"the diff in the pull request."
    )


def test_every_case_has_a_fixture() -> None:
    """The one test here that needs no render service.

    A case listed with no fixture file skips at render time, which reads as
    "the service is down" rather than "somebody added a case and no data". This
    separates the two, and runs in every suite.
    """
    missing = []
    for case in sorted(CASES):
        try:
            load_fixture(case)
        except FileNotFoundError:
            missing.append(case)

    assert not missing, (
        f"These visual cases have no fixture: {', '.join(missing)}. Each needs "
        f"tests/visual/fixtures/<case>.json holding a render spec."
    )
