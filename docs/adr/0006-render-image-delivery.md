# 0006 — Renders return image content blocks, sized for the conversation

## Status

Accepted — 2026-09-08

## Context

`04-mcp-server.md` §6.1 specified the `webmap_render_map` response as markdown
opening with:

```markdown
![Wolfcamp A Porosity](webmap://render/3f9c...)
```

A custom URI scheme inside a markdown image is inert. It renders as dead text, not
an image. MCP delivers images as image content blocks carrying base64 data, or as a
URL the client can actually resolve.

This is not a cosmetic defect. `00-overview.md` §8 defines Phase 1 success as a
geologist receiving "a correct, legible, correctly-projected image," and `04` §6.1
is the tool that produces it.

A second constraint compounds it. `00` §7 places the system on an internal network
and VPN only — so `claude.ai` cannot fetch `https://webmap.corp/...`. **The MCP
response body is the only path by which image bytes reach Claude.** That makes
response size a design parameter rather than an afterthought: `slide_full` is
2560×1440 at scale factor 2, which is commonly 1.5–4 MB of PNG, and base64 adds
roughly a third on top.

## Decision

Return a content-block list — an image block plus a text block carrying the
structured metadata — instead of markdown with a synthetic URI.

Separate what Claude sees from what goes into the deck:

1. **Inline a display derivative**, roughly 1024–1600 px on the longest edge. Enough
   for Claude to confirm the map rendered correctly and to show the user, and small
   enough to sit in a conversation that may hold a dozen of them.
2. **Persist the full-resolution master** in object storage against `render_id`,
   exactly as `02` §3.9 already describes.
3. **`webmap_get_render` gains a `size` parameter.** `size="master"` returns the
   full-resolution block for the case where Claude is assembling the deck itself —
   an opt-in cost, not one paid on every render.

Consequential detail: the `size` parameter on `webmap_render_map` governs the
**stored master**, not the inline preview. Its description says so, because
otherwise a caller asking for `thumbnail` to save tokens would be degrading the
artifact rather than the preview.

The `failed_requests` warning from `06-rendering.md` §5.1 goes in the text block,
where Claude will read it — a map with a hole in it needs to be distinguishable from
sparse data.

## Consequences

Renders display. That is the whole point, and it was not true before.

Claude sees a downsampled image, so it cannot verify fine detail — label collisions,
hairline contour weights — from the inline block. That is acceptable: `04` §1's
"metadata over pixels" principle already says Claude should write captions from the
structured metadata rather than by reading the image, and the visual regression
harness in `06` §10 is what actually guards rendering quality.

Two representations of every render now exist. The derivative is generated at render
time from the same page screenshot rather than stored separately — one screenshot,
two encodes — so this costs CPU, not another storage object.

If a future deployment does become reachable from `claude.ai`, the inline derivative
can be replaced by a URL and the size question disappears. That is the trigger to
revisit: public or tunnelled reachability, which `00` §7 currently forbids.
