"""Tests for managed Pi agent dir seeding (extensions / packages)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.inner import pi_settings
from omnigent.inner.pi_settings import prepare_managed_pi_agent_dir


def test_prepare_managed_pi_agent_dir_copies_settings_and_symlinks_npm(
    tmp_path: Path,
) -> None:
    """Gateway managed dir gets global settings metadata and npm install tree."""
    global_agent = tmp_path / "global-agent"
    global_agent.mkdir()
    (global_agent / "settings.json").write_text(
        json.dumps(
            {
                "extensions": ["/tmp/my-ext.ts"],
                "packages": ["npm:@foo/bar"],
            }
        ),
        encoding="utf-8",
    )
    npm_dir = global_agent / "npm" / "foo"
    npm_dir.mkdir(parents=True)

    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "models.json").write_text("{}", encoding="utf-8")

    prepare_managed_pi_agent_dir(
        managed,
        overlay={"retry": {"maxRetries": 5}},
        global_agent_dir=global_agent,
    )

    written = json.loads((managed / "settings.json").read_text(encoding="utf-8"))
    assert written["extensions"] == ["/tmp/my-ext.ts"]
    assert written["packages"] == ["npm:@foo/bar"]
    assert written["retry"] == {"maxRetries": 5}
    assert (managed / "npm").is_symlink()
    assert (managed / "npm").resolve() == (global_agent / "npm").resolve()


def test_prepare_managed_pi_agent_dir_empty_global_writes_overlay_only(
    tmp_path: Path,
) -> None:
    """Missing global settings still writes the Omnigent overlay."""
    global_agent = tmp_path / "empty-global"
    global_agent.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()

    prepare_managed_pi_agent_dir(
        managed,
        overlay={"retry": {"enabled": True}},
        global_agent_dir=global_agent,
    )

    written = json.loads((managed / "settings.json").read_text(encoding="utf-8"))
    assert written == {"retry": {"enabled": True}}


def test_prepare_managed_pi_agent_dir_deep_merges_nested_overlay(tmp_path: Path) -> None:
    """Nested settings (e.g. compaction) merge like Pi project overrides."""
    global_agent = tmp_path / "global-agent"
    global_agent.mkdir()
    (global_agent / "settings.json").write_text(
        json.dumps({"compaction": {"enabled": True, "reserveTokens": 16384}}),
        encoding="utf-8",
    )
    managed = tmp_path / "managed"
    managed.mkdir()

    prepare_managed_pi_agent_dir(
        managed,
        overlay={"compaction": {"reserveTokens": 8192}},
        global_agent_dir=global_agent,
    )

    written = json.loads((managed / "settings.json").read_text(encoding="utf-8"))
    assert written["compaction"] == {
        "enabled": True,
        "reserveTokens": 8192,
    }


@pytest.mark.parametrize("explicit_root", [False, True], ids=["default", "override"])
def test_user_setup_and_session_state_remain_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_root: bool
) -> None:
    global_agent = tmp_path / "global-agent"
    global_agent.mkdir()
    if explicit_root:
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(global_agent))
        monkeypatch.setattr(pi_settings, "DEFAULT_PI_AGENT_DIR", tmp_path / "unused")
    else:
        monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
        monkeypatch.setattr(pi_settings, "DEFAULT_PI_AGENT_DIR", global_agent)
    directories = ("skills", "extensions", "prompts", "themes", "npm", "git")
    files = ("mcp.json", "mcp-auth.json", "keybindings.json")
    isolated = (
        "auth.json",
        "models.json",
        "models-store.json",
        "AGENTS.md",
        "SYSTEM.md",
        "APPEND_SYSTEM.md",
    )
    for name in (*directories, "sessions"):
        (global_agent / name).mkdir()
    for name in (*files, *isolated):
        (global_agent / name).write_text("{}")
    managed = tmp_path / "managed"
    managed.mkdir()
    for name in ("models.json", "auth.json"):
        (managed / name).write_text("session-owned")

    for _ in range(2):
        prepare_managed_pi_agent_dir(managed)
        for name in (*directories, *files):
            assert (managed / name).resolve() == global_agent / name
        for name in ("models.json", "auth.json"):
            assert not (managed / name).is_symlink()
            assert (managed / name).read_text() == "session-owned"
        assert not any((managed / name).exists() for name in (*isolated[2:], "sessions"))


def test_relative_settings_keep_their_original_root(tmp_path: Path) -> None:
    global_agent = tmp_path / "global-agent"
    global_agent.mkdir()
    settings = {
        "extensions": [
            "./custom/demo.js",
            "!custom/disabled.js",
            "-extensions/off.js",
            "-builtin:mcp",
        ],
        "skills": ["../shared-skills", "/absolute/skills", "~/skills"],
        "packages": [
            "npm:@example/package",
            "./local-package",
            {"source": "../package", "skills": []},
        ],
    }
    settings_path = global_agent / "settings.json"
    settings_path.write_text(json.dumps(settings))
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "settings.json").symlink_to(settings_path)

    prepare_managed_pi_agent_dir(managed, global_agent_dir=global_agent)

    written = json.loads((managed / "settings.json").read_text())
    assert written["extensions"] == [
        str(global_agent / "custom/demo.js"),
        "!custom/disabled.js",
        "!" + str(global_agent / "custom/disabled.js"),
        "-extensions/off.js",
        "-" + str(global_agent / "extensions/off.js"),
        "-builtin:mcp",
    ]
    assert written["skills"] == [
        str(global_agent / "../shared-skills"),
        "/absolute/skills",
        "~/skills",
    ]
    assert written["packages"] == [
        "npm:@example/package",
        str(global_agent / "local-package"),
        {"source": str(global_agent / "../package"), "skills": []},
    ]
    assert json.loads(settings_path.read_text()) == settings
    assert not (managed / "settings.json").is_symlink()


@pytest.mark.parametrize("content", [b"{invalid", b"[]", b"\xff"])
def test_invalid_settings_do_not_hide_resources(tmp_path: Path, content: bytes) -> None:
    global_agent = tmp_path / "global-agent"
    global_agent.mkdir()
    (global_agent / "settings.json").write_bytes(content)
    (global_agent / "mcp.json").write_text("{malformed; Pi validates this}")
    managed = tmp_path / "managed"

    prepare_managed_pi_agent_dir(
        managed, global_agent_dir=global_agent, overlay={"retry": {"enabled": True}}
    )

    assert json.loads((managed / "settings.json").read_text()) == {"retry": {"enabled": True}}
    assert (managed / "mcp.json").is_symlink()


def test_unavailable_resource_and_existing_entry_do_not_block_other_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    global_agent = tmp_path / "global-agent"
    global_agent.mkdir()
    for name in ("mcp.json", "mcp-auth.json", "keybindings.json"):
        (global_agent / name).write_text("{}")
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "keybindings.json").symlink_to(tmp_path / "missing")
    original = Path.symlink_to
    original_read = Path.read_text

    def read(self: Path, *args: object, **kwargs: object) -> str:
        if self == global_agent / "settings.json":
            raise PermissionError("settings unavailable")
        return original_read(self, *args, **kwargs)

    def link(self: Path, target: Path, target_is_directory: bool = False) -> None:
        if self.name == "mcp-auth.json":
            raise PermissionError("resource unavailable")
        original(self, target, target_is_directory=target_is_directory)

    monkeypatch.setattr(Path, "symlink_to", link)
    monkeypatch.setattr(Path, "read_text", read)
    prepare_managed_pi_agent_dir(managed, global_agent_dir=global_agent)

    assert (managed / "mcp.json").is_symlink()
    assert not (managed / "mcp-auth.json").exists()
    assert (managed / "keybindings.json").readlink() == tmp_path / "missing"


def test_source_is_never_rewritten_in_place(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{}")
    prepare_managed_pi_agent_dir(
        tmp_path, global_agent_dir=tmp_path, overlay={"retry": {"enabled": True}}
    )
    assert settings_path.read_text() == "{}"
