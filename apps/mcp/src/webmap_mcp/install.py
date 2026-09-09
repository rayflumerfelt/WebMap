"""Register this MCP server with a local Claude client.

`adr/0008-local-stdio-mcp.md`: "Deployment gains a per-workstation step. The
MCP server has to be installed and registered in each user's Claude client
config. That is a packaging task the previous design did not have, and it
should be a single command."

This is that command:

    webmap-mcp-install --api-url https://webmap.corp --client claude-desktop

It writes the server into the client's config with the environment block that
fixes the API base URL and auth mode at install time — never switchable at
runtime, because a tool call that deletes a dataset does not care which
environment it landed in (`03-auth-security.md` §4.5).

Nothing here holds a credential. The config it writes names an executable and
a URL; the token still comes from the OS broker at call time.
"""

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The key this server occupies in a client's `mcpServers` map. Fixed, so a
#: re-install updates the existing entry rather than accumulating duplicates.
SERVER_KEY = "webmap"


@dataclass(frozen=True)
class ClientTarget:
    name: str
    path: Path
    description: str


def client_targets() -> dict[str, ClientTarget]:
    """Where each supported client keeps its MCP configuration.

    Paths differ by platform. Only the ones we can locate are offered — a
    command that writes a config file the client will never read is worse than
    one that says it cannot find it.
    """
    home = Path.home()
    targets: dict[str, ClientTarget] = {}

    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        targets["claude-desktop"] = ClientTarget(
            "claude-desktop",
            appdata / "Claude" / "claude_desktop_config.json",
            "Claude Desktop",
        )
    elif sys.platform == "darwin":
        targets["claude-desktop"] = ClientTarget(
            "claude-desktop",
            home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
            "Claude Desktop",
        )
    else:
        targets["claude-desktop"] = ClientTarget(
            "claude-desktop",
            home / ".config" / "Claude" / "claude_desktop_config.json",
            "Claude Desktop",
        )

    targets["claude-code"] = ClientTarget("claude-code", home / ".claude.json", "Claude Code")
    return targets


def server_entry(
    *,
    api_url: str,
    auth_mode: str,
    client_id: str | None,
    authority: str | None,
    dev_user: str | None,
    executable: str,
) -> dict[str, Any]:
    """The `mcpServers` entry, with configuration fixed at install time."""
    env = {"WEBMAP_MCP_API_BASE_URL": api_url, "WEBMAP_MCP_AUTH_MODE": auth_mode}
    if auth_mode == "broker":
        env["WEBMAP_MCP_CLIENT_ID"] = client_id or ""
        env["WEBMAP_MCP_AUTHORITY"] = authority or ""
    else:
        env["WEBMAP_MCP_DEV_USER"] = dev_user or "ada"
    return {"command": executable, "args": [], "env": env}


def install(
    target: ClientTarget,
    entry: dict[str, Any],
    *,
    dry_run: bool = False,
) -> str:
    """Merge the entry into the client's config, preserving everything else.

    Read-modify-write rather than overwrite: the file holds the user's other
    MCP servers and unrelated client settings, and replacing it would silently
    remove them. A malformed existing file is a hard stop for the same reason
    — rewriting it from scratch would discard whatever it contained.
    """
    existing: dict[str, Any] = {}
    if target.path.exists():
        try:
            existing = json.loads(target.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SystemExit(
                f"{target.path} is not valid JSON ({exc}). Refusing to rewrite "
                f"it — it holds your other MCP servers and client settings, and "
                f"replacing it would lose them. Fix or move the file and run "
                f"this again."
            ) from exc

    servers = existing.setdefault("mcpServers", {})
    replaced = SERVER_KEY in servers
    servers[SERVER_KEY] = entry

    rendered = json.dumps(existing, indent=2) + "\n"
    if not dry_run:
        target.path.parent.mkdir(parents=True, exist_ok=True)
        # Write beside the target and replace, so an interrupted write cannot
        # leave the user with a truncated config and no MCP servers at all.
        temporary = target.path.with_suffix(target.path.suffix + ".webmap-tmp")
        temporary.write_text(rendered, encoding="utf-8")
        temporary.replace(target.path)

    return "updated" if replaced else "added"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="webmap-mcp-install",
        description=(
            "Register the WebMap MCP server with a local Claude client. "
            "Configuration is fixed at install time and not switchable at "
            "runtime (03-auth-security.md §4.5)."
        ),
    )
    parser.add_argument(
        "--api-url",
        required=True,
        help="WebMap API base URL, e.g. https://webmap.corp. Must be https "
        "unless it is localhost.",
    )
    parser.add_argument(
        "--client",
        choices=sorted(client_targets()),
        default="claude-desktop",
        help="Which Claude client to register with.",
    )
    parser.add_argument(
        "--auth-mode",
        choices=("broker", "dev"),
        default="broker",
        help="broker uses the OS credential broker (production). dev asks the "
        "API for a token and only works when the API is itself in development "
        "mode.",
    )
    parser.add_argument("--client-id", help="Entra application (client) id. Broker only.")
    parser.add_argument(
        "--authority",
        help="Entra authority URL, e.g. https://login.microsoftonline.com/<tenant>. "
        "Broker only.",
    )
    parser.add_argument("--dev-user", default="ada", help="Roster key. Dev mode only.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without touching the config.",
    )
    args = parser.parse_args(argv)

    # Validate through the same settings object the server uses, so an install
    # cannot produce a configuration the server would refuse to start with.
    from webmap_mcp.settings import McpSettings

    try:
        McpSettings(
            api_base_url=args.api_url,
            auth_mode=args.auth_mode,
            client_id=args.client_id or "",
            authority=args.authority or "",
            dev_user=args.dev_user,
        )
    except ValueError as exc:
        # Pydantic's repr wraps each message in locations, types, and a docs
        # URL. On a command line that buries the sentence the user needs, so
        # only the messages are shown.
        parser.error("; ".join(_messages(exc)))

    executable = shutil.which("webmap-mcp")
    if executable is None:
        parser.error(
            "The 'webmap-mcp' executable is not on PATH. Install the server "
            "first — `uv tool install 'webmap-mcp[broker]'` — then run this "
            "again, so the config points at a command the client can launch."
        )

    target = client_targets()[args.client]
    entry = server_entry(
        api_url=args.api_url,
        auth_mode=args.auth_mode,
        client_id=args.client_id,
        authority=args.authority,
        dev_user=args.dev_user,
        executable=executable,
    )

    if args.dry_run:
        sys.stdout.write(
            f"Would write to {target.path}:\n"
            f"{json.dumps({'mcpServers': {SERVER_KEY: entry}}, indent=2)}\n"
        )
        return 0

    action = install(target, entry)
    sys.stdout.write(
        f"{action.capitalize()} '{SERVER_KEY}' in {target.description} "
        f"({target.path}).\n"
        f"  API:  {args.api_url}\n"
        f"  Auth: {args.auth_mode}\n"
        f"Restart {target.description} for it to pick up the change.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def _messages(exc: ValueError) -> list[str]:
    """The human-readable half of a pydantic ValidationError."""
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return [str(exc)]
    return [str(item.get("msg", "")).removeprefix("Value error, ") for item in errors()]
