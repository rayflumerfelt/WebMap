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

    Dense and scannable, ids present but not dominant — a geologist refers to
    a layer by name, and Claude needs the id only to make the next call.
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
            f"| `{short_id(item.get('id', ''))}` |"
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
