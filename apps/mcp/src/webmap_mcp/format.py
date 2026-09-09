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
