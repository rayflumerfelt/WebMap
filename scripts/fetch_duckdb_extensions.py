"""Populate a host-side cache of DuckDB extensions.

For networks where the Docker build cannot reach `extensions.duckdb.org`.
Downloads on the host — which usually *can* reach it, since a captive portal
authenticates the machine rather than the container — and lays the files out
exactly as DuckDB expects, so `infra/compose.offline.yaml` can mount them.

    uv run python scripts/fetch_duckdb_extensions.py
    docker compose -f infra/compose.yaml -f infra/compose.offline.yaml up -d

The layout DuckDB looks in is `<root>/v<version>/<platform>/<name>.duckdb_extension`,
and the version must match the DuckDB the image runs — a mismatch surfaces as
"Extension not found" naming a directory that does exist, which is a confusing
way to learn about a version bump.
"""

from __future__ import annotations

import argparse
import gzip
import platform
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import TextIO

import duckdb

#: What `webmap_geo.dataplane` loads. Kept in step with REQUIRED_EXTENSIONS
#: there; a name here that is not needed only wastes a download.
EXTENSIONS = ("spatial", "httpfs")

#: HTTPS on purpose. DuckDB itself requests these over plain HTTP, which is
#: exactly what a captive portal intercepts — that interception is why this
#: script exists.
BASE_URL = "https://extensions.duckdb.org"

#: See `fetch` — the default urllib agent is refused.
USER_AGENT = "webmap-extension-fetch/1.0"


def _say(message: str, stream: TextIO | None = None) -> None:
    """Progress to the console, the way `scripts/seed.py` does it.

    `print` is banned repo-wide (`CLAUDE.md` §7.5) and a script is not an
    exemption — structlog is for the services, and a one-shot CLI emitting
    JSON log lines would be worse to read than what it replaces.
    """
    (stream or sys.stdout).write(message + "\n")


def container_platform() -> str:
    """The DuckDB platform string for the *image*, not for this machine.

    The cache is mounted into a Linux container, so a developer on macOS or
    Windows must still fetch `linux_amd64`. Defaulting to the host's own
    platform would download files the container cannot load and produce an
    error that says nothing about why.
    """
    return "linux_amd64"


def fetch(destination: Path, version: str, target: str) -> int:
    directory = destination / f"v{version}" / target
    directory.mkdir(parents=True, exist_ok=True)

    written = 0
    for name in EXTENSIONS:
        path = directory / f"{name}.duckdb_extension"
        if path.exists():
            _say(f"  {name}: already cached")
            continue

        url = f"{BASE_URL}/v{version}/{target}/{name}.duckdb_extension.gz"
        # An explicit User-Agent, because the CDN answers urllib's default with
        # 403 while serving the identical URL to curl. Debugging that as a
        # network-policy problem costs an hour; it is a header.
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                payload = gzip.decompress(response.read())
        except (urllib.error.URLError, OSError) as error:
            _say(f"  {name}: FAILED ({error})", stream=sys.stderr)
            continue

        path.write_bytes(payload)
        written += 1
        _say(f"  {name}: {len(payload):,} bytes")

    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        default=duckdb.__version__,
        help="DuckDB version the image runs (default: this machine's duckdb)",
    )
    parser.add_argument(
        "--platform",
        default=container_platform(),
        help="DuckDB platform string for the container (default: linux_amd64)",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=Path(".duckdb-extensions"),
        help="Cache directory (default: .duckdb-extensions, matching compose.offline.yaml)",
    )
    args = parser.parse_args()

    _say(
        f"Fetching DuckDB {args.version} extensions for {args.platform} "
        f"into {args.dest}/ (host is {platform.system()} {platform.machine()})"
    )
    fetch(args.dest, args.version, args.platform)

    directory = args.dest / f"v{args.version}" / args.platform
    missing = [n for n in EXTENSIONS if not (directory / f"{n}.duckdb_extension").exists()]
    if missing:
        _say(
            f"\nMissing: {', '.join(missing)}. This host cannot reach "
            f"{BASE_URL} either — fetch the files on a machine that can and "
            f"copy {directory} across.",
            stream=sys.stderr,
        )
        return 1

    _say(
        "\nReady. Start the stack with the offline overlay:\n"
        "  docker compose -f infra/compose.yaml -f infra/compose.offline.yaml up -d"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
