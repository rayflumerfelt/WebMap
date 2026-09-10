"""Response formatting. `04-mcp-server.md` §1, §4.

Responses are shaped for a reader with limited context: list responses compact
and paginated, detail responses full, never a 5 MB blob into a conversation.

**Dataset content is untrusted** (`03-auth-security.md` §9). Attribute values
and layer names come from shapefiles authored elsewhere, by people who did not
have this system in mind and occasionally by people who did. They are rendered
inside clearly-labelled data fields, escaped so they cannot break out of the
table they are in, and truncated so one long value cannot crowd out the
response.

This module is the reason the local server duplicating response formatting is
safe (`01-architecture.md` §2.2): there is no authorization logic here to
duplicate, only presentation.
"""

from typing import Any

#: `03-auth-security.md` §9: truncate long attribute values in list responses.
MAX_VALUE_CHARS = 200

#: Enough of a UUID to be unambiguous in a conversation, short enough not to
#: dominate a table. `04` §4.1's example response uses this shape.
ID_PREFIX = 8


def short_id(value: str) -> str:
    """`9f3a…c21b` — recognisable, and not mistaken for a full id."""
    text = str(value)
    if len(text) <= ID_PREFIX * 2 + 1:
        return text
    return f"{text[:ID_PREFIX]}…{text[-4:]}"


def clean(value: Any) -> str:
    """Render an untrusted value safely for a markdown table.

    Three things happen here, and each has a reason:

    - **Pipes and newlines are neutralised.** A value containing `|` would
      otherwise add a column, and a newline would end the row — so a crafted
      attribute could forge table structure around itself.
    - **Truncation at 200 characters**, per `03` §9. One long value must not
      crowd out the rest of the response.
    - **Nothing is interpreted.** The value is data. It is never treated as
      markdown, a link, or an instruction, however much it may resemble one.
    """
    if value is None:
        return "—"
    text = str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    if len(text) > MAX_VALUE_CHARS:
        return text[: MAX_VALUE_CHARS - 1] + "…"
    return text


def _count(value: Any) -> str:
    return f"{value:,}" if isinstance(value, int) else "—"


def _date(value: Any) -> str:
    return str(value)[:10] if value else "—"


def dataset_table(payload: dict[str, Any]) -> str:
    """The list/search response from `04-mcp-server.md` §4.1.

    Dense and scannable — a geologist refers to a layer by name, and Claude
    needs the id only to make the next call.

    **The id is shown in full, where `04` §4.1's example shows `9f3a…c21b`.**
    That example contradicts the tool it feeds: `webmap_describe_dataset`
    takes a UUID, and a truncated one cannot be expanded, so the documented
    flow — "find a dataset with webmap_search_datasets, inspect it with
    webmap_describe_dataset" — has no way to pass an id along. The gap had
    already been papered over: a test reached past the tools into the private
    HTTP helper to get an id, with a comment saying the table's was too short
    to use. Thirty-six characters a row is a small price for the column being
    an identifier rather than a decoration.
    """
    items = payload.get("items", [])
    total = payload.get("total", len(items))
    offset = payload.get("offset", 0)

    if not items:
        return (
            "**No datasets found.**\n\n"
            "This is what you can access, not what exists — a dataset owned by "
            "someone else and not shared with you does not appear. Try "
            "webmap_search_datasets with a broader term, or ask the owner to "
            "share it."
        )

    header = f"**{total:,} dataset{'s' if total != 1 else ''}**"
    if total > len(items):
        header += f" (showing {offset + 1}–{offset + len(items)})"

    lines = [
        header,
        "",
        "| Name | Kind | Features | Updated | ID |",
        "|---|---|---|---|---|",
    ]
    for item in items:
        lines.append(
            f"| {clean(item.get('name'))} "
            f"| {clean(item.get('kind'))} "
            f"| {_count(item.get('feature_count'))} "
            f"| {_date(item.get('updated_at') or item.get('synced_at'))} "
            f"| `{clean(item.get('id', ''))}` |"
        )

    if payload.get("has_more"):
        lines.append("")
        lines.append(f"More available — call again with offset={payload.get('next_offset')}.")
    return "\n".join(lines)


def dataset_detail(detail: dict[str, Any]) -> str:
    """The full form. `04-mcp-server.md` §4.3.

    Includes everything needed for an accurate caption, because `04` §1's
    "metadata over pixels" principle means Claude writes captions from this
    rather than by looking at an image.
    """
    lines = [
        f"## {clean(detail.get('name'))}",
        "",
        f"- **ID**: `{detail.get('id')}`",
        f"- **Kind**: {clean(detail.get('kind'))}"
        + (f" ({clean(detail.get('geometry_kind'))})" if detail.get("geometry_kind") else ""),
        f"- **CRS**: EPSG:{detail.get('storage_srid')}",
        f"- **Owner**: {clean(detail.get('owner_name'))}"
        f" · visibility {clean(detail.get('visibility'))}",
    ]
    if detail.get("feature_count") is not None:
        lines.append(f"- **Features**: {_count(detail.get('feature_count'))}")
    if detail.get("grid_nx"):
        lines.append(
            f"- **Grid**: {detail.get('grid_nx')}×{detail.get('grid_ny')}"
            f" at {detail.get('grid_cell_size')} (analysis-CRS units)"
        )
    if detail.get("value_min") is not None:
        unit = f" {clean(detail.get('value_unit'))}" if detail.get("value_unit") else ""
        lines.append(
            f"- **Values**: {detail.get('value_min'):.4g} to "
            f"{detail.get('value_max'):.4g}{unit}"
        )
    if detail.get("bbox_4326"):
        west, south, east, north = detail["bbox_4326"]
        lines.append(
            f"- **Extent** (EPSG:4326): {west:.3f}, {south:.3f} to {east:.3f}, {north:.3f}"
        )
    if detail.get("data_vintage"):
        lines.append(f"- **Vintage**: {_date(detail.get('data_vintage'))}")
    if detail.get("description"):
        lines.append(f"- **Description**: {clean(detail.get('description'))}")

    schema = detail.get("attribute_schema") or []
    if schema:
        lines += ["", "### Attributes", "", "| Field | Type | Nullable |", "|---|---|---|"]
        for field in schema:
            lines.append(
                f"| {clean(field.get('name'))} | {clean(field.get('type'))} "
                f"| {'yes' if field.get('nullable') else 'no'} |"
            )

    lineage = detail.get("lineage")
    if lineage:
        lines += [
            "",
            "### Provenance",
            "",
            f"- **Operation**: {clean(lineage.get('operation'))}",
            f"- **webmap_geo**: {clean(lineage.get('webmap_geo_version'))}",
            f"- **Created**: {_date(lineage.get('created_at'))}",
        ]

    if detail.get("caption"):
        lines += ["", f"> {clean(detail.get('caption'))}"]

    return "\n".join(lines)


def search_results(payload: dict[str, Any], query: str) -> str:
    """Search output, with a next step when nothing matched.

    `04` §1: an error or an empty result tells Claude what to do next.
    "No datasets found" alone ends the conversation.
    """
    items = payload.get("items", [])
    if not items:
        return (
            f"**No datasets matched '{clean(query)}'.**\n\n"
            f"Search covers names and descriptions of datasets you can access. "
            f"Try a shorter or more general term — matching is fuzzy, so a "
            f"partial word usually works — or call webmap_list_datasets to see "
            f"everything available."
        )
    return dataset_table({"items": items, "total": len(items), "offset": 0})


def render_summary(payload: dict[str, Any], *, master: bool = False) -> str:
    """The text block beside a rendered image. `04-mcp-server.md` §6.1.

    Everything here comes from the render's stored metadata, which came from
    the dataset registry — never from the pixels. That is what makes the tool
    description's instruction ("never state a value range you did not receive
    here") something Claude can actually follow: the numbers are present, so
    there is no reason to guess at them.
    """
    metadata = payload.get("metadata") or {}
    lines: list[str] = []

    render_id = clean(str(payload.get("id", "")))
    size = f"{payload.get('width', '?')}x{payload.get('height', '?')}"
    lines.append(f"**Render** `{short_id(render_id)}` · master {size}")
    lines.append("")

    layers = metadata.get("layers") or []
    if layers:
        described = ", ".join(
            f"{clean(layer.get('name'))} ({clean(layer.get('kind'))})" for layer in layers
        )
        lines.append(f"- **Layers**: {described}")

    value_range = metadata.get("value_range")
    if value_range and value_range.get("min") is not None:
        unit = clean(value_range.get("unit") or "")
        suffix = f" {unit}" if unit else ""
        lines.append(f"- **Values**: {value_range['min']:g} – {value_range['max']:g}{suffix}")

    if metadata.get("method"):
        lines.append(f"- **Method**: {clean(metadata['method'])}")
    if metadata.get("grid"):
        lines.append(f"- **Grid**: {clean(metadata['grid'])}")

    crs = metadata.get("crs") or {}
    if crs.get("srid"):
        name = clean(crs.get("name") or "")
        label = f"{name} (EPSG:{crs['srid']})" if name else f"EPSG:{crs['srid']}"
        lines.append(f"- **CRS**: {label}")

    if metadata.get("vintage"):
        lines.append(f"- **Vintage**: {clean(metadata['vintage'])}")

    extent = metadata.get("extent_4326")
    if extent:
        lines.append(
            f"- **Extent**: {extent[0]:.3f}, {extent[1]:.3f} to "
            f"{extent[2]:.3f}, {extent[3]:.3f} (WGS84)"
        )

    caption = payload.get("caption")
    if caption:
        lines.append("")
        lines.append(f"**Suggested caption**: {clean(caption)}")

    # `06-rendering.md` §5.1. A failed tile is a hole in the map, and a hole
    # looks exactly like sparse data — which is the reading a geologist will
    # reach for, because it is the one that looks like geology.
    failures = payload.get("failed_requests") or []
    if failures:
        lines.append("")
        lines.append(
            f"⚠️ {len(failures)} request(s) failed during rendering, so part of "
            f"this map may be incomplete. Do not describe the empty areas as "
            f"sparse data — re-render before drawing any conclusion from them."
        )

    if master:
        lines.append("")
        lines.append(
            f"Full-resolution image: `GET /api/v1/renders/{render_id}/image?size=master`"
        )

    return "\n".join(lines)


def session_summary(created: dict[str, Any]) -> str:
    """`04-mcp-server.md` §7.1: the tool's whole output is a link."""
    lines = [
        f"Session ready: **{clean(str(created.get('url', '')))}**",
        "",
        f"{_count(created.get('layer_count'))} layer(s) loaded.",
        "",
        "Changes save automatically. Ask me to re-render when you are done and "
        "I will pick up the current state.",
    ]
    return "\n".join(lines)


def session_detail(session: dict[str, Any]) -> str:
    """Session state, for picking up after the user has edited.

    This closes the loop `01-architecture.md` §5.2 describes: the session id is
    the shared vocabulary, so a map the user rearranged in the browser can be
    re-rendered without asking them what they changed.
    """
    name = f" — {clean(session['name'])}" if session.get("name") else ""
    lines = [f"**Session** `{clean(str(session.get('short_code', '')))}`{name}", ""]

    layers = session.get("layers") or []
    if layers:
        lines.append("| Layer | Kind | Visible | Opacity |")
        lines.append("|---|---|---|---|")
        for layer in layers:
            dataset = layer.get("dataset") or {}
            visible = "yes" if layer.get("visible") else "no"
            lines.append(
                f"| {clean(dataset.get('name'))} | {clean(dataset.get('kind'))} | "
                f"{visible} | {layer.get('opacity', 1):g} |"
            )
    else:
        lines.append("This session has no layers you can see.")

    hidden = session.get("hidden_layer_count") or 0
    if hidden:
        # Said plainly rather than left as a discrepancy in the count. The
        # reader would otherwise take what they can see to be the session, and
        # render a map missing a layer without knowing it.
        lines.append("")
        lines.append(
            f"{hidden} further layer(s) are in this session but not visible to "
            f"you. Anything rendered from it will omit them."
        )

    view = session.get("view") or {}
    if view.get("center"):
        lines.append("")
        lines.append(
            f"View: {view['center'][0]:.3f}, {view['center'][1]:.3f} at zoom {view.get('zoom')}"
        )
    elif view.get("bbox"):
        box = view["bbox"]
        lines.append("")
        lines.append(f"View: {box[0]:.3f}, {box[1]:.3f} to {box[2]:.3f}, {box[3]:.3f}")

    return "\n".join(lines)


# --- jobs --------------------------------------------------------------------


def job_submitted(payload: dict[str, Any], *, what: str, detail: list[str]) -> str:
    """The response to a submission. `04-mcp-server.md` §5.1.

    **The last line is the load-bearing one.** Without "do not call again for
    the same input", an agent that polls, sees `queued`, and reasons that
    nothing is happening will resubmit — and while idempotency catches that
    within the hour, the response is what stops it being attempted at all.

    A submission that matched an existing job says so explicitly, because
    "queued" on a job someone else's retry created is otherwise
    indistinguishable from a fresh one.
    """
    job_id = clean(str(payload.get("job_id", "")))
    lines: list[str] = []

    if payload.get("already_running"):
        lines.append(f"Already running as job `{short_id(job_id)}` — not resubmitted.")
        lines.append("")
        lines.append(
            "An identical request from you is still in flight. Poll it with "
            "webmap_get_job rather than submitting again."
        )
        return "\n".join(lines)

    lines.append(f"{what} queued.")
    lines.append("")
    lines.append(f"- **Job**: `{short_id(job_id)}`")
    lines.extend(detail)
    lines.append("")
    lines.append(
        f"Poll with `webmap_get_job` using the full id `{job_id}`. Do not call "
        f"again for the same input."
    )
    return "\n".join(lines)


def job_status(job: dict[str, Any]) -> str:
    """`04-mcp-server.md` §8.1. Four shapes, because four states mean four
    different next actions."""
    job_id = clean(str(job.get("id", "")))
    state = str(job.get("state", "unknown"))
    kind = clean(job.get("kind"))

    if state == "running":
        return _running(job, job_id, kind)
    if state == "queued":
        return (
            f"**Job** `{short_id(job_id)}` — queued ({kind})\n\n"
            f"Waiting for a worker. Poll again in a few seconds; do not "
            f"resubmit."
        )
    if state == "succeeded":
        return _succeeded(job, job_id, kind)
    if state == "cancelled":
        return (
            f"**Job** `{short_id(job_id)}` — cancelled ({kind})\n\n"
            f"Nothing was produced. Cancelled jobs discard their partial "
            f"output, so no dataset was registered."
        )
    return _failed(job, job_id, kind)


def _running(job: dict[str, Any], job_id: str, kind: str) -> str:
    percent = round(float(job.get("progress") or 0.0) * 100)
    lines = [f"**Job** `{short_id(job_id)}` — running ({percent}%)"]

    if job.get("progress_message"):
        lines.append(clean(job["progress_message"]))

    remaining = job.get("estimated_remaining_seconds")
    if remaining is not None:
        lines.append(f"Estimated {remaining} s remaining.")

    lines.append("")
    poll = job.get("poll_after_seconds") or 3
    lines.append(f"Poll again in about {poll} s.")
    return "\n".join(lines)


def _succeeded(job: dict[str, Any], job_id: str, kind: str) -> str:
    result = job.get("result") or {}
    lines = [f"**Job** `{short_id(job_id)}` — succeeded ({kind})", ""]

    dataset_id = result.get("dataset_id")
    if dataset_id:
        lines.append(f"- **Output dataset**: `{clean(str(dataset_id))}`")
    if result.get("caption"):
        lines.append(f"- **Result**: {clean(result['caption'])}")
    if result.get("feature_count") is not None:
        lines.append(f"- **Features**: {_count(result['feature_count'])}")
    if result.get("interval"):
        lines.append(f"- **Contour interval**: {result['interval']:g}")

    # A filled run writes a second layer, and this is the only place its id is
    # ever shown. Left out, the polygons exist and nothing that reads this can
    # find them — which is indistinguishable from `fill` having been ignored.
    band_dataset_id = result.get("band_dataset_id")
    if band_dataset_id:
        lines.append(f"- **Filled bands**: `{clean(str(band_dataset_id))}`")
    if result.get("band_count") is not None:
        lines.append(f"- **Bands**: {_count(result['band_count'])}")

    grid = result.get("grid") or {}
    if grid.get("nx"):
        lines.append(
            f"- **Grid**: {grid['nx']}x{grid['ny']} at {grid.get('cell_size', 0):g} "
            f"(EPSG:{grid.get('srid')})"
        )

    lines.extend(_diagnostics(result.get("diagnostics") or {}))

    # **The warnings are not decoration.** A gridded surface looks identical
    # whether it came from 1,847 wells or six, and these are the only place
    # the difference is stated. Put last so they are the final thing read
    # before the dataset id is used.
    warnings = result.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("⚠️ **Read before using this surface:**")
        for warning in warnings:
            lines.append(f"- {clean(warning)}")

    return "\n".join(lines)


def _diagnostics(diagnostics: dict[str, Any]) -> list[str]:
    """`05-geoprocessing.md` §6.5, condensed to the three numbers that change
    someone's mind about trusting a surface."""
    lines: list[str] = []

    control = diagnostics.get("n_control_points")
    if control is not None:
        lines.append(f"- **Control points**: {_count(control)}")

    output = diagnostics.get("output_range")
    source = diagnostics.get("input_range")
    if output and source:
        lines.append(
            f"- **Range**: {output[0]:g} to {output[1]:g} "
            f"(data: {source[0]:g} to {source[1]:g})"
        )

    fraction = diagnostics.get("extrapolated_fraction")
    if fraction is not None:
        radius = diagnostics.get("search_radius")
        within = f" of any control point within {radius:g}" if radius else ""
        lines.append(f"- **Extrapolated**: {fraction:.0%} of cells are out{within}")

    validation = diagnostics.get("cross_validation") or {}
    if validation.get("rmse") is not None:
        lines.append(f"- **Cross-validation RMSE**: {validation['rmse']:.4g}")

    return lines


def _failed(job: dict[str, Any], job_id: str, kind: str) -> str:
    """`04` §8.1: "both paths forward are named, and the consequence of the
    easy one is stated." The error text carries that; this adds only whether
    retrying is worth anything."""
    lines = [f"**Job** `{short_id(job_id)}` — failed ({kind})", ""]
    lines.append(clean(job.get("error") or "No error message was recorded."))

    error_kind = job.get("error_kind")
    if error_kind in ("input", "permission"):
        lines.append("")
        lines.append(
            "Resubmitting unchanged will fail the same way — this is about the "
            "request, not about the system being busy."
        )
    elif error_kind == "transient":
        lines.append("")
        lines.append("This looks temporary. Resubmitting is reasonable.")
    elif error_kind == "resource":
        lines.append("")
        lines.append(
            "The job exceeded a resource limit. A coarser cell size or a "
            "smaller extent would fit."
        )
    return "\n".join(lines)


def job_list(jobs: list[dict[str, Any]]) -> str:
    if not jobs:
        return "No jobs. Submitted analyses appear here while they run."

    lines = ["| Job | Kind | State | Progress | Started |", "|---|---|---|---|---|"]
    for job in jobs:
        percent = f"{round(float(job.get('progress') or 0.0) * 100)}%"
        lines.append(
            f"| `{short_id(str(job.get('id', '')))}` | {clean(job.get('kind'))} "
            f"| {clean(job.get('state'))} | {percent} | {_date(job.get('started_at'))} |"
        )
    return "\n".join(lines)


def variogram_summary(fit: dict[str, Any]) -> str:
    """The fitted model, in the order a geologist reads it.

    Range first, because it is the number that decides whether a layer can be
    gridded at all and the one people quote. Then the nugget as a *fraction*
    rather than a raw variance — 4,200 means nothing without the sill beside
    it, and 68% means "there is very little structure here" immediately.

    The empirical points are summarised rather than listed. Thirty lag rows in
    a conversation is a wall of numbers nobody reads; the count and the range
    they span say whether the curve is worth trusting, and the full array is on
    the API for anything that wants to draw it.
    """
    units = clean(fit.get("units", ""))
    lines = [
        f"**Variogram** — {clean(fit.get('dataset_name'))} · "
        f"`{clean(fit.get('value_column'))}`",
        "",
        f"- **Range**: {fit['range']:,.0f} {units}",
        f"- **Model**: {clean(fit.get('model'))}",
        f"- **Sill**: {fit['sill']:,.4g} · **Nugget**: {fit['nugget']:,.4g} "
        f"({fit['nugget_ratio']:.0%} of total variance)",
    ]

    if fit.get("anisotropy_ratio", 1.0) > 1.0:
        lines.append(
            f"- **Anisotropy**: {fit['anisotropy_ratio']:.2f}:1 along "
            f"{fit['anisotropy_angle']:.0f}° (major axis)"
        )
    else:
        lines.append("- **Anisotropy**: none detected — isotropic")

    lags = fit.get("lags") or []
    lines.append(
        f"- **Fitted from**: {_count(fit.get('n_points'))} points, "
        f"{_count(fit.get('n_pairs_used'))} pairs over {len(lags)} lags"
    )
    lines.append(f"- **Seed**: {fit.get('seed')} — the same request gives the same fit")

    warnings = fit.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("⚠️ **Read before gridding with this:**")
        for warning in warnings:
            lines.append(f"- {clean(warning)}")

    lines.append("")
    lines.append(
        'Pass these to `webmap_interpolate` with `method="ordinary_kriging"`, or '
        "let it fit its own — it runs the same code."
    )
    return "\n".join(lines)
