"""Real SDK/native Pi startup preserves user setup against a scripted provider.

Run serially: the scenarios share the session-scoped mock provider.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
import yaml

from omnigent.harnesses.pi_native.credentials import (
    pi_native_provider_launch,
    resolve_pi_native_provider,
)
from tests.e2e._harness_probes import cli_unavailable_reason
from tests.e2e.conftest import get_mock_requests
from tests.e2e.omnigent._pi_mock_gateway import write_pi_gateway_config
from tests.e2e.omnigent.conftest import configure_mock_llm, reset_mock_llm

_EXTENSION_PATH = (
    Path(__file__).resolve().parents[2] / "resources" / "pi_extensions" / "e2e_marker_extension.js"
)
_MARKER_NAME = "omnigent-pi-ext-marker"
_PROMPT = "say hi in 5 words"
_RUN_TIMEOUT_SEC = 180

_pytest_pi_unavailable = cli_unavailable_reason("pi")
pytestmark = pytest.mark.skipif(
    _pytest_pi_unavailable is not None,
    reason=(
        "pi managed-extensions e2e requires a runnable 'pi' CLI; "
        f"{_pytest_pi_unavailable}. Install/fix Pi to run this test."
    ),
)


def _seed_pi_extension_home(home: Path, agent_dir: Path, python: Path, transport: str) -> Path:
    """Seed auto-discovered resources, a local package and the shared echo MCP."""
    (agent_dir / "extensions").mkdir(parents=True)
    shutil.copyfile(_EXTENSION_PATH, agent_dir / "extensions" / "marker.js")
    disabled = agent_dir / "extensions" / "disabled.js"
    disabled.write_text(
        "module.exports = pi => pi.on('before_agent_start', event => ({"
        "systemPrompt: event.systemPrompt + '\\nDISABLED_EXTENSION_71AB'}));"
    )
    package = agent_dir / "local-package"
    (package / "extensions").mkdir(parents=True)
    (package / "extensions" / "prompt.js").write_text(
        "module.exports = pi => pi.on('before_agent_start', event => ({"
        "systemPrompt: event.systemPrompt + '\\nPI_PACKAGE_MARKER_18CF'}));"
    )
    skill = agent_dir / "skills" / "setup-probe" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: setup-probe\ndescription: PI_SKILL_MARKER_5E1B\n---\nProbe.\n")
    (agent_dir / "settings.json").write_text(
        json.dumps({"packages": ["./local-package"], "extensions": ["-" + str(disabled)]}),
        encoding="utf-8",
    )
    (agent_dir / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "setup": {
                        "command": str(python),
                        "args": [
                            str(
                                _EXTENSION_PATH.parents[2]
                                / "tools"
                                / "fixtures"
                                / "echo_stdio_mcp_server.py"
                            )
                        ],
                        **({"exposure": "direct"} if transport == "native" else {}),
                    }
                }
            }
        )
    )
    (agent_dir / "AGENTS.md").write_text("UNREQUESTED_GLOBAL_CONTEXT_71AB")
    return home / _MARKER_NAME


@pytest.mark.parametrize(
    "transport, explicit_root", [("sdk", False), ("sdk", True), ("native", True)]
)
def test_pi_gateway_run_loads_global_extensions(
    omnigent_repo_root: Path,
    omnigent_python: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
    explicit_root: bool,
) -> None:
    """Native can call user MCP tools; SDK retains its bridged-tool boundary."""
    model = f"mock-pi-ext-{uuid.uuid4().hex[:8]}"
    reset_mock_llm(mock_llm_server_url)
    replies = [{"text": "Hello from the mock model."}]
    if transport == "native":
        replies.append({"text": "Remembered the tool result."})
        replies.insert(
            0,
            {
                "tool_calls": [
                    {
                        "call_id": "setup_echo",
                        "name": "mcp__setup__echo",
                        "arguments": '{"text": "PI_MCP_PROBE_8A31"}',
                    }
                ]
            },
        )
    configure_mock_llm(
        mock_llm_server_url,
        replies,
        key=model,
    )

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    agent_dir = tmp_path / "custom-agent" if explicit_root else fake_home / ".pi" / "agent"
    marker_path = _seed_pi_extension_home(fake_home, agent_dir, omnigent_python, transport)
    assert not marker_path.exists()

    config_home = tmp_path / "omnigent-config"
    write_pi_gateway_config(config_home, mock_url=mock_llm_server_url, model=model)

    env = dict(mock_credentials_env)
    env["HOME"] = str(fake_home)
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    env["OMNIGENT_DATA_HOME"] = str(tmp_path / "data")
    env["PI_OFFLINE"] = "1"
    env.pop("PI_CODING_AGENT_DIR", None)
    if explicit_root:
        env["PI_CODING_AGENT_DIR"] = str(agent_dir)

    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"
    command = [
        str(omnigent_python),
        "-m",
        "omnigent",
        "run",
        str(yaml_path),
        "--model",
        model,
        "--harness",
        "pi",
        "-p",
        _PROMPT,
        "--no-log",
        "--no-session",
    ]
    if transport == "native":
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
        config = yaml.safe_load((config_home / "config.yaml").read_text())
        config["providers"]["mock-oai"]["openai"]["wire_api"] = "chat"
        provider = resolve_pi_native_provider(model=model, config_loader=lambda: config)
        assert provider is not None
        launch = pi_native_provider_launch(tmp_path / "managed", provider)
        env.update(launch.env)
        command = [
            shutil.which("pi"),
            "--mode",
            "json",
            "--print",
            "--approve",
            "--session",
            str(tmp_path / "session.jsonl"),
            *launch.args,
            _PROMPT,
        ]
    for turn in range(2 if transport == "native" else 1):
        if turn:
            marker_path.unlink()
            command[-1] = "Recall the previous tool result."
            env.update(pi_native_provider_launch(tmp_path / "managed", provider).env)
        result = subprocess.run(
            command,
            env=env,
            cwd=fake_home,
            capture_output=True,
            text=True,
            timeout=_RUN_TIMEOUT_SEC,
        )
        assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        assert marker_path.read_text(encoding="utf-8") == "loaded"
    requests = get_mock_requests(mock_llm_server_url, key=model)
    assert requests
    system = "\n".join(
        json.dumps(message["content"])
        for request in requests
        for message in request["messages"]
        if message["role"] in ("system", "developer")
    )
    assert "PI_PACKAGE_MARKER_18CF" in system
    assert "PI_SKILL_MARKER_5E1B" in system
    assert "UNREQUESTED_GLOBAL_CONTEXT_71AB" not in system
    assert "DISABLED_EXTENSION_71AB" not in system
    if transport == "native":
        assert len(requests) == 3
        assert any(
            "echo: PI_MCP_PROBE_8A31" in str(message.get("content", ""))
            for message in requests[-1]["messages"]
            if message["role"] == "tool"
        )
    else:
        names = {tool["function"]["name"] for request in requests for tool in request["tools"]}
        assert not (names & {"mcp__setup__echo", "codemode", "tool_search"}), (
            "SDK tools must remain restricted to the Omnigent bridge"
        )
