"""MCP evaluations. `04-mcp-server.md` §10, `12-roadmap.md` Phase 3.

**What this proves, and what it does not.**

§10's purpose is stated plainly: "a tool description edit that degrades tool
selection is invisible without them." Testing *selection* needs a model in the
loop deciding which tool to call, and there is none here. What this harness
tests is the half that can be tested without one, and it is not the trivial
half:

- Every question's answer is **reachable** through the tool surface. Each
  `qa_pair` records the tool calls a correct agent would make, and this runs
  them against the live stack and asserts the answer's key fragments appear in
  what comes back. A response format that stops carrying the CRS, an
  attribute schema that stops listing types, a description that stops naming
  the fault-aware method — each breaks a question here.
- The questions themselves stay well-formed and honest: ten of them, each
  independent, each read-only, each needing more than one call except where
  the answer genuinely is one listing away.

What it cannot catch is Claude choosing `webmap_render_map` when it should
have chosen `webmap_interpolate`. That gap is real and is recorded against
the Phase 3 criterion rather than papered over.

`make eval` runs this. Needs the API and the seeded project.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest

pytestmark = pytest.mark.integration

EVALUATIONS = Path(__file__).parent / "evaluations.xml"

#: §10: "at least 10 evaluation questions".
MINIMUM_QUESTIONS = 10

#: A `$1` argument means "the first dataset id the previous call returned",
#: which is how a two-step question — find it, then describe it — is recorded
#: without hard-coding an id that a re-seed would change.
PREVIOUS_ID = "$1"

UUID_PATTERN = re.compile(r"`([0-9a-f-]{36})`")


def load() -> list[ElementTree.Element]:
    return list(ElementTree.parse(EVALUATIONS).getroot().findall("qa_pair"))


def identifier(pair: ElementTree.Element) -> str:
    return pair.get("id") or "unnamed"


@pytest.fixture(scope="module")
def tools() -> Any:
    """The MCP tools, pointed at the live stack as one dev user.

    The same rebinding `tests/test_mcp.py` uses: an installed server is
    configured once, for one workstation and one user, and this is how a test
    stands in for that.
    """
    import httpx

    url = "http://localhost:8000"
    try:
        httpx.get(f"{url}/health", timeout=3).raise_for_status()
    except Exception as exc:
        pytest.skip(
            f"No WebMap API at {url} ({type(exc).__name__}). Start it with: "
            f"docker compose -f infra/compose.yaml up -d"
        )

    import webmap_mcp.server as server
    from webmap_mcp.settings import McpSettings
    from webmap_mcp.tokens import build_token_source

    settings = McpSettings(api_base_url=url, auth_mode="dev", dev_user="ada")
    server.settings = settings
    server._tokens = build_token_source(settings)
    return server


async def run_plan(tools: Any, plan: ElementTree.Element) -> str:
    """Execute one question's recorded plan, returning everything it produced.

    The transcript is concatenated rather than reduced to the last response:
    an agent answering "which layer has the most points, and in what CRS"
    reads the count from the listing and the CRS from the detail, so the
    evidence is spread across calls and the assertion should see all of it.
    """
    transcript: list[str] = []
    last_id: str | None = None

    for step in plan:
        if step.tag == "inspect":
            transcript.append(_description_of(tools, step.get("tool", "")))
            continue

        name = step.get("tool", "")
        function = getattr(tools, name, None)
        if function is None:
            pytest.fail(
                f"The plan calls `{name}`, which the server does not expose. "
                f"Either the tool was renamed and this evaluation was not "
                f"updated, or the plan has a typo."
            )

        kwargs: dict[str, Any] = {}
        for argument in step.findall("arg"):
            key = argument.get("name", "")
            value = (argument.text or "").strip()
            if value == PREVIOUS_ID:
                if last_id is None:
                    pytest.fail(
                        f"`{name}` wants the previous call's dataset id, but no "
                        f"earlier call in this plan returned one."
                    )
                kwargs[key] = uuid.UUID(last_id)
            else:
                kwargs[key] = value

        response = str(await function(**kwargs))
        transcript.append(response)

        found = UUID_PATTERN.findall(response)
        if found:
            last_id = found[0]

    return "\n".join(transcript)


def _description_of(tools: Any, name: str) -> str:
    """A tool's own description, as the client sees it.

    Read off the registered function rather than the module source, so a
    description that fails to reach the registry — the failure that actually
    matters — is caught rather than passed over.
    """
    function = getattr(tools, name, None)
    if function is None:
        pytest.fail(f"No tool named `{name}` to inspect.")

    text = getattr(function, "__doc__", "") or ""
    # The parameter descriptions carry as much guidance as the docstring, and
    # for the fault question they carry the load.
    annotations = getattr(function, "__annotations__", {})
    return text + "\n" + "\n".join(str(value) for value in annotations.values())


# --- the questions themselves -------------------------------------------------


def test_there_are_at_least_ten_questions() -> None:
    """§10 sets the floor. Below it the suite stops covering the surface."""
    assert len(load()) >= MINIMUM_QUESTIONS


def test_every_question_is_complete() -> None:
    """A question with no expectation passes forever and tests nothing."""
    for pair in load():
        name = identifier(pair)
        assert pair.findtext("question", "").strip(), f"{name}: no question"
        assert pair.findtext("answer", "").strip(), f"{name}: no answer"
        assert pair.find("plan") is not None, f"{name}: no plan"
        assert pair.findall("expect"), f"{name}: nothing expected"


def test_question_ids_are_unique() -> None:
    """They name the failures, so two questions sharing one is a debugging
    dead end."""
    ids = [identifier(pair) for pair in load()]

    assert len(ids) == len(set(ids)), f"duplicate ids in {ids}"


def test_no_question_writes_anything() -> None:
    """§10: evaluations are read-only.

    One that gridded something would leave a dataset behind and change the
    answer to another question — and the questions are supposed to be
    independent of each other and of how many times the suite has run.
    """
    writing = {
        "webmap_interpolate",
        "webmap_contour",
        "webmap_render_map",
        "webmap_open_session",
        "webmap_cancel_job",
    }
    for pair in load():
        plan = pair.find("plan")
        assert plan is not None
        # `<call>` only. `<inspect>` reads a tool's description without
        # invoking it, which is exactly how the fault-method question can ask
        # about webmap_interpolate without gridding anything.
        called = {step.get("tool") for step in plan.findall("call")}
        offending = called & writing
        assert not offending, f"{identifier(pair)} calls {offending}, which writes"


# --- the answers are reachable ------------------------------------------------


@pytest.mark.parametrize("pair", load(), ids=identifier)
async def test_the_answer_is_reachable_through_the_tools(
    pair: ElementTree.Element, tools: Any
) -> None:
    """Run the plan; require the answer's fragments in what comes back.

    This is the regression that matters between model runs: a response that
    quietly stops carrying the CRS, or a description that stops naming the
    fault-aware method, makes the question unanswerable however well the model
    chooses its tools.
    """
    plan = pair.find("plan")
    assert plan is not None
    transcript = await run_plan(tools, plan)

    missing = [
        fragment.text.strip()
        for fragment in pair.findall("expect")
        if fragment.text and fragment.text.strip() not in transcript
    ]
    assert not missing, (
        f"{identifier(pair)}: the tools no longer surface {missing}.\n"
        f"Question: {(pair.findtext('question') or '').strip()}\n"
        f"Expected answer: {(pair.findtext('answer') or '').strip()}\n"
        f"--- transcript ---\n{transcript[:2000]}"
    )
