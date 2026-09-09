"""Fetch the map-label fonts and build their MapLibre glyph ranges.

**MapLibre does not use system fonts.** `text-font` names a font *stack*, and
the renderer fetches signed-distance-field glyphs from the style's `glyphs`
URL. Missing glyphs are not an error — labels simply do not draw — so this has
to run before map labelling works at all.

    uv run python scripts/fetch_fonts.py          # download + build
    uv run python scripts/fetch_fonts.py --list   # show the roster

Ten families, chosen for variety rather than to fill a list: four humanist
sans that differ in texture, one neo-grotesque, one narrow, one condensed, two
serifs and a monospace. Every one is SIL OFL or Apache 2.0, which matters
because `00-overview.md` §7 puts this system on an internal network where a
webfont CDN is not reachable and redistribution has to be permitted.

Bold and italic are separate font *files* and separate stacks — MapLibre has
no `font-style` property. Oswald ships no italic at all, which is a fact the
formatting UI has to know rather than a gap to paper over.

Variable fonts are pinned to their weight before the glyphs are built. fontnik
renders a font's default instance and ignores variation axes, so a Bold stack
built from the variable file comes out identical to Regular — see `_instance`.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

#: Google Fonts' raw tree. HTTPS, and with a User-Agent, because the CDN
#: answers urllib's default with a 403 while serving the identical URL to
#: curl — the same trick `fetch_duckdb_extensions.py` needs.
BASE = "https://raw.githubusercontent.com/google/fonts/main"
USER_AGENT = "webmap-font-fetch/1.0"

FONT_DIR = Path(".fonts")
GLYPH_DIR = Path("infra/glyphs")
IMAGE = "webmap-glyphs"


@dataclass(frozen=True)
class Family:
    """One font family and the files that make its stacks.

    `paths` maps a MapLibre stack name to a path under the Google Fonts tree.
    A family with no italic simply has no italic entry — `Oswald` is the one
    here, and the styling UI disables the italic control when it is chosen
    rather than offering a checkbox that does nothing.
    """

    name: str
    licence: str
    note: str
    paths: dict[str, str] = field(default_factory=dict)

    @property
    def has_italic(self) -> bool:
        return any("Italic" in stack for stack in self.paths)


#: Weight axis values. A variable font carries every weight in one file, and
#: fontnik renders whatever the default instance is — so a Bold stack built
#: straight from `Roboto[wght].ttf` comes out byte-identical to Regular. It
#: did, and the two `0-255.pbf` files hashed the same. Variable sources are
#: pinned with fontTools before the glyphs are built.
WEIGHTS = {"Regular": 400, "Bold": 700, "Italic": 400}

#: The roster. `ofl/<dir>/<File>.ttf` is the Google Fonts layout; a filename
#: containing `[` is a variable font and gets instanced (see `_instance`).
FAMILIES: tuple[Family, ...] = (
    Family(
        "Noto Sans",
        "OFL 1.1",
        "Humanist sans; the broadest glyph coverage, and the safe default",
        {
            "Noto Sans Regular": "ofl/notosans/NotoSans[wdth,wght].ttf",
            "Noto Sans Bold": "ofl/notosans/NotoSans[wdth,wght].ttf",
            "Noto Sans Italic": "ofl/notosans/NotoSans-Italic[wdth,wght].ttf",
        },
    ),
    Family(
        "Open Sans",
        "OFL 1.1",
        "Neutral humanist; the most common web-map label face",
        {
            "Open Sans Regular": "ofl/opensans/OpenSans[wdth,wght].ttf",
            "Open Sans Bold": "ofl/opensans/OpenSans[wdth,wght].ttf",
            "Open Sans Italic": "ofl/opensans/OpenSans-Italic[wdth,wght].ttf",
        },
    ),
    Family(
        "Roboto",
        "Apache 2.0",
        "Neo-grotesque; tighter and more mechanical than the humanists",
        {
            "Roboto Regular": "ofl/roboto/Roboto[wdth,wght].ttf",
            "Roboto Bold": "ofl/roboto/Roboto[wdth,wght].ttf",
            "Roboto Italic": "ofl/roboto/Roboto-Italic[wdth,wght].ttf",
        },
    ),
    Family(
        "Source Sans 3",
        "OFL 1.1",
        "Humanist; unusually clean at small label sizes",
        {
            "Source Sans 3 Regular": "ofl/sourcesans3/SourceSans3[wght].ttf",
            "Source Sans 3 Bold": "ofl/sourcesans3/SourceSans3[wght].ttf",
            "Source Sans 3 Italic": "ofl/sourcesans3/SourceSans3-Italic[wght].ttf",
        },
    ),
    Family(
        "Lato",
        "OFL 1.1",
        "Warm semi-rounded sans; a softer texture than the rest",
        {
            "Lato Regular": "ofl/lato/Lato-Regular.ttf",
            "Lato Bold": "ofl/lato/Lato-Bold.ttf",
            "Lato Italic": "ofl/lato/Lato-Italic.ttf",
        },
    ),
    Family(
        "PT Sans",
        "OFL 1.1",
        "Slightly narrow; holds up in dense labelling",
        {
            "PT Sans Regular": "ofl/ptsans/PT_Sans-Web-Regular.ttf",
            "PT Sans Bold": "ofl/ptsans/PT_Sans-Web-Bold.ttf",
            "PT Sans Italic": "ofl/ptsans/PT_Sans-Web-Italic.ttf",
        },
    ),
    Family(
        "Oswald",
        "OFL 1.1",
        "Condensed; for contour labels and narrow polygons. No italic exists",
        {
            "Oswald Regular": "ofl/oswald/Oswald[wght].ttf",
            "Oswald Bold": "ofl/oswald/Oswald[wght].ttf",
        },
    ),
    Family(
        "Noto Serif",
        "OFL 1.1",
        "Serif; traditional for physical-feature names",
        {
            "Noto Serif Regular": "ofl/notoserif/NotoSerif[wdth,wght].ttf",
            "Noto Serif Bold": "ofl/notoserif/NotoSerif[wdth,wght].ttf",
            "Noto Serif Italic": "ofl/notoserif/NotoSerif-Italic[wdth,wght].ttf",
        },
    ),
    Family(
        "Playfair Display",
        "OFL 1.1",
        "High-contrast display serif; titles and map furniture",
        {
            "Playfair Display Regular": "ofl/playfairdisplay/PlayfairDisplay[wght].ttf",
            "Playfair Display Bold": "ofl/playfairdisplay/PlayfairDisplay[wght].ttf",
            "Playfair Display Italic": ("ofl/playfairdisplay/PlayfairDisplay-Italic[wght].ttf"),
        },
    ),
    Family(
        "Roboto Mono",
        "Apache 2.0",
        "Monospace; coordinates and grid references line up",
        {
            "Roboto Mono Regular": "ofl/robotomono/RobotoMono[wght].ttf",
            "Roboto Mono Bold": "ofl/robotomono/RobotoMono[wght].ttf",
            "Roboto Mono Italic": "ofl/robotomono/RobotoMono-Italic[wght].ttf",
        },
    ),
)


def _say(message: str, stream: TextIO | None = None) -> None:
    """Console output, the way `scripts/seed.py` does it (`CLAUDE.md` §7.5)."""
    (stream or sys.stdout).write(message + "\n")


def stacks() -> dict[str, str]:
    """Every stack name mapped to its source path."""
    return {stack: path for family in FAMILIES for stack, path in family.paths.items()}


def download(destination: Path) -> list[str]:
    """Fetch one file per stack, named after the stack.

    Named after the *stack* rather than the source file because a variable
    font supplies several stacks from one file, and the glyph builder derives
    the directory name from the filename it is given.
    """
    destination.mkdir(parents=True, exist_ok=True)
    failed: list[str] = []

    for stack, path in stacks().items():
        target = destination / f"{stack}.ttf"
        if target.exists():
            _say(f"  {stack:<28} cached")
            continue

        url = f"{BASE}/{path}"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read()
        except (urllib.error.URLError, OSError) as error:
            _say(f"  {stack:<28} FAILED ({error})", stream=sys.stderr)
            failed.append(stack)
            continue

        if "[" in path:
            payload = _instance(payload, stack)
        target.write_bytes(payload)
        _say(f"  {stack:<28} {len(payload) / 1024:.0f} KB")

    return failed


def _instance(payload: bytes, stack: str) -> bytes:
    """Pin a variable font to one weight, so Bold is actually bold.

    fontnik has no idea about variation axes: it renders the font's default
    instance, which for every Google variable font is Regular. Building a
    "Bold" stack from the variable file therefore produces glyphs identical to
    Regular — verified by hashing the output, which is how this was caught.

    `fontTools.varLib.instancer` bakes the axis into a static font. Any axis
    other than weight — width on the Noto faces, optical size elsewhere — is
    left at its default, which is what "Noto Sans Bold" should mean.
    """
    import io

    from fontTools import ttLib
    from fontTools.varLib import instancer

    style = stack.rsplit(" ", 1)[-1]
    weight = WEIGHTS.get(style, 400)

    font = ttLib.TTFont(io.BytesIO(payload))
    if "fvar" not in font:
        return payload

    axes = {a.axisTag for a in font["fvar"].axes}
    if "wght" not in axes:
        return payload

    # `updateFontNames` rewrites the name table to match the pinned instance.
    # Without it the instanced font still calls itself "Oswald Regular", and
    # that name is what fontnik writes into the PBF as the fontstack — so
    # `Oswald Bold/0-255.pbf` announced itself as Regular. Harmless for a
    # single-font stack, wrong for a fallback chain, and confusing either way.
    instanced = instancer.instantiateVariableFont(
        font, {"wght": weight}, inplace=True, updateFontNames=True
    )
    buffer = io.BytesIO()
    instanced.save(buffer)
    return buffer.getvalue()


def build(font_dir: Path, glyph_dir: Path) -> int:
    """Run the glyph builder in Docker.

    In a container because fontnik is a native module that does not build on
    Windows, and because pinning the toolchain in an image is what makes the
    output reproducible rather than dependent on whoever ran it.
    """
    glyph_dir.mkdir(parents=True, exist_ok=True)

    built = subprocess.run(
        [
            "docker",
            "build",
            "-f",
            "infra/docker/glyphs.Dockerfile",
            "-t",
            IMAGE,
            "infra/docker",
        ],
        check=False,
    )
    if built.returncode != 0:
        _say("Could not build the glyph image. Is Docker running?", stream=sys.stderr)
        return built.returncode

    return subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{font_dir.resolve()}:/fonts:ro",
            "-v",
            f"{glyph_dir.resolve()}:/glyphs",
            IMAGE,
            "/fonts",
            "/glyphs",
        ],
        check=False,
    ).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="Show the roster and exit")
    parser.add_argument("--font-dir", type=Path, default=FONT_DIR, help="Where downloads land")
    parser.add_argument(
        "--glyph-dir", type=Path, default=GLYPH_DIR, help="Where glyph ranges land"
    )
    args = parser.parse_args()

    if args.list:
        _say(f"{len(FAMILIES)} families, {len(stacks())} stacks\n")
        for family in FAMILIES:
            italic = "" if family.has_italic else "   (no italic)"
            _say(f"  {family.name:<20} {family.licence:<12} {family.note}{italic}")
        return 0

    _say(f"Downloading {len(stacks())} font files into {args.font_dir}/")
    failed = download(args.font_dir)
    if failed:
        _say(
            f"\n{len(failed)} download(s) failed: {', '.join(failed)}. "
            f"Fix the network or the path in FAMILIES and re-run; downloads "
            f"already fetched are cached.",
            stream=sys.stderr,
        )
        return 1

    _say("")
    return build(args.font_dir, args.glyph_dir)


if __name__ == "__main__":
    raise SystemExit(main())
