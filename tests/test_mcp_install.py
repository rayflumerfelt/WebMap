"""The per-workstation install. `adr/0008-local-stdio-mcp.md`.

Registering the server in each user's Claude client is the cost the local
design accepted, and the ADR says it "should be a single command". These tests
cover the two ways that command could do real damage — clobbering a config
that holds the user's other MCP servers, and writing a configuration the
server would then refuse to start with.

No database, no network, no client installed.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from webmap_mcp.install import SERVER_KEY, ClientTarget, install, main, server_entry


def target_for(tmp_path: Path) -> ClientTarget:
    return ClientTarget("test", tmp_path / "claude_config.json", "Test Client")


def entry(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "api_url": "https://webmap.corp",
        "auth_mode": "broker",
        "client_id": "00000000-0000-0000-0000-000000000000",
        "authority": "https://login.microsoftonline.com/tenant",
        "dev_user": None,
        "executable": "/usr/local/bin/webmap-mcp",
    }
    kwargs.update(overrides)
    return server_entry(**kwargs)


# --- what the entry contains ------------------------------------------------


def test_configuration_is_fixed_in_the_entry_not_left_to_runtime() -> None:
    """`03-auth-security.md` §4.5: the API base URL is fixed at install time.

    A tool call that deletes a dataset does not care which environment it
    landed in, and this is the one component sitting on a machine where both
    configurations are plausible.
    """
    written = entry()

    assert written["env"]["WEBMAP_MCP_API_BASE_URL"] == "https://webmap.corp"
    assert written["env"]["WEBMAP_MCP_AUTH_MODE"] == "broker"


def test_the_entry_holds_no_secret() -> None:
    """The config file is plain JSON in the user's profile.

    A client id and an authority URL are public identifiers. Anything that
    could authenticate on its own must not be here — the token comes from the
    OS broker at call time, which is the whole point of the design.
    """
    env = entry()["env"]

    joined = " ".join(f"{k}={v}" for k, v in env.items()).lower()
    for forbidden in ("secret", "password", "token=", "key="):
        assert forbidden not in joined


def test_dev_mode_does_not_write_broker_settings() -> None:
    """Leaving an empty client id behind would make the settings guard fire
    on a config that is otherwise valid for dev."""
    env = entry(auth_mode="dev", dev_user="grace", client_id=None, authority=None)["env"]

    assert env["WEBMAP_MCP_DEV_USER"] == "grace"
    assert "WEBMAP_MCP_CLIENT_ID" not in env


# --- not destroying the user's config ---------------------------------------


def test_installing_preserves_other_servers_and_settings(tmp_path: Path) -> None:
    """The config holds the user's other MCP servers. Read-modify-write.

    Overwriting the file is the obvious implementation and it silently removes
    every other server the user had configured.
    """
    target = target_for(tmp_path)
    target.path.write_text(
        json.dumps(
            {
                "mcpServers": {"other": {"command": "other-server"}},
                "someUnrelatedSetting": {"theme": "dark"},
            }
        ),
        encoding="utf-8",
    )

    action = install(target, entry())

    config = json.loads(target.path.read_text(encoding="utf-8"))
    assert action == "added"
    assert config["mcpServers"]["other"] == {"command": "other-server"}
    assert config["someUnrelatedSetting"] == {"theme": "dark"}
    assert config["mcpServers"][SERVER_KEY]["command"].endswith("webmap-mcp")


def test_reinstalling_updates_rather_than_duplicating(tmp_path: Path) -> None:
    """A fixed key, so upgrading an install does not accumulate entries."""
    target = target_for(tmp_path)

    first = install(target, entry(api_url="https://old.corp"))
    second = install(target, entry(api_url="https://new.corp"))

    config = json.loads(target.path.read_text(encoding="utf-8"))
    assert (first, second) == ("added", "updated")
    assert len(config["mcpServers"]) == 1
    assert (
        config["mcpServers"][SERVER_KEY]["env"]["WEBMAP_MCP_API_BASE_URL"] == "https://new.corp"
    )


def test_a_malformed_config_is_a_hard_stop(tmp_path: Path) -> None:
    """Refuse rather than rewrite.

    If the file cannot be parsed, its contents are unknown — and rewriting it
    from scratch discards whatever the user had. The message says what to do.
    """
    target = target_for(tmp_path)
    target.path.write_text("{not json at all", encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        install(target, entry())

    message = str(excinfo.value)
    assert "Refusing to rewrite" in message
    assert "would lose them" in message
    # And the original is untouched.
    assert target.path.read_text(encoding="utf-8") == "{not json at all"


def test_installing_creates_the_directory_when_absent(tmp_path: Path) -> None:
    """A first-ever install on a fresh machine has no config directory."""
    target = ClientTarget("test", tmp_path / "nested" / "deeper" / "config.json", "Test")

    install(target, entry())

    assert json.loads(target.path.read_text(encoding="utf-8"))["mcpServers"][SERVER_KEY]


def test_a_dry_run_writes_nothing(tmp_path: Path) -> None:
    target = target_for(tmp_path)

    install(target, entry(), dry_run=True)

    assert not target.path.exists()


# --- refusing a configuration the server would reject -----------------------


def test_install_refuses_plain_http_to_a_remote_host(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Validated through the same settings object the server uses.

    An install that produces a config the server refuses to start with turns a
    clear error at install time into a mysterious one on first use. Here it
    also matters on its own: the broker hands this process the user's bearer
    token, and plain HTTP to an internal hostname puts it on the wire.
    """
    with pytest.raises(SystemExit):
        main(
            [
                "--api-url",
                "http://webmap.corp",
                "--auth-mode",
                "broker",
                # Supplied, so the broker validator passes and the
                # transport check is what fires.
                "--client-id",
                "00000000-0000-0000-0000-000000000000",
                "--authority",
                "https://login.microsoftonline.com/tenant",
            ]
        )

    assert "clear text" in capsys.readouterr().err


def test_install_refuses_broker_mode_without_its_configuration(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A broker with no tenant cannot acquire anything."""
    with pytest.raises(SystemExit):
        main(["--api-url", "https://webmap.corp", "--auth-mode", "broker"])

    assert "WEBMAP_MCP_CLIENT_ID" in capsys.readouterr().err


def test_localhost_over_plain_http_is_allowed() -> None:
    """Development runs against localhost, where there is no wire to sniff.

    A rule that also blocked this would make the development path unusable and
    get relaxed rather than respected.
    """
    from webmap_mcp.settings import McpSettings

    settings = McpSettings(api_base_url="http://localhost:8000", auth_mode="dev")

    assert settings.api_base_url == "http://localhost:8000"
