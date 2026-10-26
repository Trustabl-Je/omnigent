"""UI test processes keep provider config and Playwright artifacts to themselves."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from tests.helpers import ui_configuration as configuration

pytest_plugins = ["pytester"]
_ROOT = Path(__file__).resolve().parents[2]
_USER_CONFIG = "user: real\n"

# Each worker installs its own mock provider, then waits until the other worker
# has too, so a shared config file would be overwritten or refuse the backup.
_OVERLAPPING_MOCK_CONFIG = """
import json, os, time
from pathlib import Path

from omnigent.config import global_config_path
from tests.helpers.ui_configuration import temp_omnigent_mock_config


def test_{name}():
    worker = os.environ["PYTEST_XDIST_WORKER"]
    records = Path(os.environ["ISOLATION_RECORDS"])
    seeded = global_config_path().read_text()
    with temp_omnigent_mock_config(f"http://mock-{{worker}}", "claude"):
        (records / f"{{worker}}.arrived").touch()
        deadline = time.monotonic() + 30
        while len(list(records.glob("*.arrived"))) < 2:
            assert time.monotonic() < deadline, "the two workers never overlapped"
            time.sleep(0.05)
        config = global_config_path().read_text()
    (records / f"{{worker}}.json").write_text(json.dumps(
        {{"path": str(global_config_path()), "seeded": seeded, "config": config}}
    ))
"""


def _configure(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run nested pytest with a synthetic user HOME and the UI config hooks."""
    home = pytester.path / "home"
    (home / ".omnigent").mkdir(parents=True)
    (home / ".omnigent" / "config.yaml").write_text(_USER_CONFIG)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join([str(_ROOT), os.environ.get("PYTHONPATH", "")])
    )
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("ISOLATION_RECORDS", str(pytester.path))
    for name in (
        "CI",
        "OMNIGENT_CONFIG_HOME",
        "OMNIGENT_UI_TESTS_CONFIG_HOME",
        "OMNIGENT_REPRO_SERVER_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    pytester.makeini("[pytest]\n")
    pytester.makeconftest("""
from tests.helpers import ui_timings
from tests.helpers.ui_configuration import pytest_configure, pytest_unconfigure


def pytest_addoption(parser):
    ui_timings.pytest_addoption(parser)
    parser.addoption("--ui-base-url")
""")
    return home


def test_parallel_workers_get_separate_config_homes(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _configure(pytester, monkeypatch)
    pytester.makepyfile(
        test_first=_OVERLAPPING_MOCK_CONFIG.format(name="first"),
        test_second=_OVERLAPPING_MOCK_CONFIG.format(name="second"),
    )
    result = pytester.runpytest_subprocess("-q", "-p", "xdist.plugin", "-n2", "--dist=loadfile")
    result.assert_outcomes(passed=2)
    records = {
        worker: json.loads((pytester.path / f"{worker}.json").read_text())
        for worker in ("gw0", "gw1")
    }
    for worker, record in records.items():
        assert record["seeded"] == _USER_CONFIG
        assert f"http://mock-{worker}" in record["config"]
        assert not Path(record["path"]).is_relative_to(home)
        assert not Path(record["path"]).parent.exists()
    assert records["gw0"]["path"] != records["gw1"]["path"]
    assert (home / ".omnigent" / "config.yaml").read_text() == _USER_CONFIG
    assert sorted(path.name for path in (home / ".omnigent").iterdir()) == ["config.yaml"]


@pytest.mark.parametrize("mode", ["default", "caller", "external", "repro"])
def test_serial_run_isolates_only_its_own_stack(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    home = _configure(pytester, monkeypatch)
    caller_home = pytester.path / "caller"
    args = ["-q"]
    if mode == "caller":
        caller_home.mkdir()
        monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(caller_home))
    elif mode == "external":
        monkeypatch.setenv("OMNIGENT_E2E_ALLOW_DEV_BASE_URL", "1")
        args.append("--ui-base-url=http://127.0.0.1:59999")
    elif mode == "repro":
        monkeypatch.setenv("OMNIGENT_REPRO_SERVER_URL", "http://127.0.0.1:59998")
    pytester.makepyfile("""
import json, os
from pathlib import Path

def test_record():
    Path(os.environ["ISOLATION_RECORDS"], "serial.json").write_text(
        json.dumps(os.environ.get("OMNIGENT_CONFIG_HOME"))
    )
""")
    result = pytester.runpytest_subprocess(*args)
    result.assert_outcomes(passed=1)
    config_home = json.loads((pytester.path / "serial.json").read_text())
    if mode == "default":
        assert not Path(config_home).is_relative_to(home)
        assert not Path(config_home).exists()
    elif mode == "caller":
        assert config_home == str(caller_home)
    else:
        assert config_home is None
    assert (home / ".omnigent" / "config.yaml").read_text() == _USER_CONFIG


@dataclass
class _ConfigStub:
    options: dict[str, Any] = field(default_factory=dict)
    stash: pytest.Stash = field(default_factory=pytest.Stash)

    def getoption(self, name: str, default: Any = None) -> Any:
        return self.options.get(name, default)


def test_worker_replaces_and_restores_an_inherited_suite_config_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    inherited = tmp_path / "controller"
    inherited.mkdir()
    (inherited / "config.yaml").write_text("controller copy\n")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(inherited))
    monkeypatch.setenv("OMNIGENT_UI_TESTS_CONFIG_HOME", str(inherited))
    monkeypatch.delenv("OMNIGENT_REPRO_SERVER_URL", raising=False)
    config = cast(pytest.Config, _ConfigStub())
    configuration._isolate_config_home(config)
    worker_home = Path(os.environ["OMNIGENT_CONFIG_HOME"])
    assert worker_home != inherited
    assert os.environ["OMNIGENT_UI_TESTS_CONFIG_HOME"] == str(worker_home)
    assert (worker_home / "config.yaml").read_text() == "controller copy\n"
    assert oct((worker_home / "config.yaml").stat().st_mode & 0o777) == "0o600"
    configuration.pytest_unconfigure(config)
    assert os.environ["OMNIGENT_CONFIG_HOME"] == str(inherited)
    assert os.environ["OMNIGENT_UI_TESTS_CONFIG_HOME"] == str(inherited)
    assert not worker_home.exists()
    assert (inherited / "config.yaml").read_text() == "controller copy\n"


def test_parallel_workers_keep_separate_playwright_output(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(pytester, monkeypatch)
    output = pytester.path / "test-results"
    output.mkdir()
    (output / "stale.txt").write_text("from an earlier run")
    artifact = """
import os
from pathlib import Path

def test_{name}(pytestconfig):
    worker = os.environ["PYTEST_XDIST_WORKER"]
    output = Path(pytestconfig.getoption("--output"))
    assert output.name == worker
    output.mkdir(parents=True, exist_ok=True)
    (output / "{name}.txt").write_text(worker)
"""
    pytester.makepyfile(
        test_first=artifact.format(name="first"), test_second=artifact.format(name="second")
    )
    result = pytester.runpytest_subprocess(
        "-q",
        "-p",
        "pytest_playwright.pytest_playwright",
        "-p",
        "xdist.plugin",
        "-n2",
        "--dist=loadfile",
        f"--output={output}",
    )
    result.assert_outcomes(passed=2)
    written = {path.name: path.parent.name for path in output.glob("gw*/*.txt")}
    assert sorted(written) == ["first.txt", "second.txt"]
    assert sorted(written.values()) == ["gw0", "gw1"]
    assert not (output / "stale.txt").exists()
