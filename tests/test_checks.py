"""The shared check command keeps all checks and diagnostics when parallelized."""

import subprocess
from pathlib import Path
from threading import Event, Thread
from unittest.mock import Mock

import pytest

from scripts import check


@pytest.fixture(autouse=True)
def static_scripts(tmp_path):
    """Use a small tree so command tests also exercise recursive script discovery."""
    root = tmp_path / "dashboard" / "static"
    (root / "nested").mkdir(parents=True)
    for name in ("app.js", "nested/worker.js", "ignored.css"):
        (root / name).write_text("", encoding="utf-8")


def pytest_calls(run):
    return [call for call in run.call_args_list if call.args[0][1:3] == ["-m", "pytest"]]


def fail_suite(command, **kwargs):
    if command[1:3] == ["-m", "pytest"] and kwargs.get("check"):
        raise subprocess.CalledProcessError(1, command)
    return subprocess.CompletedProcess(command, 0)


@pytest.mark.parametrize("cpus,expected", [(None, 1), (1, 1), (2, 2), (32, 4)])
def test_default_workers_respect_cpu_count_and_limit(monkeypatch, cpus, expected):
    monkeypatch.setattr(check.os, "cpu_count", lambda: cpus)
    assert check.parse_args([]).workers == expected


@pytest.mark.parametrize("value", ["-1", "auto", "1.5"])
def test_invalid_workers_fail_before_running_checks(value):
    with pytest.raises(SystemExit) as error:
        check.parse_args(["--workers", value])
    assert error.value.code == 2


@pytest.mark.parametrize("workers", [0, 2])
def test_full_checks_preserve_suite_and_failure_artifacts(monkeypatch, tmp_path, workers):
    monkeypatch.setattr(check, "ROOT", tmp_path)
    monkeypatch.setattr(check.sys, "argv", ["check.py", "--workers", str(workers)])
    monkeypatch.delenv("PYTEST_DEBUG_TEMPROOT", raising=False)
    run = Mock()
    monkeypatch.setattr(check.subprocess, "run", run)
    check.main()
    commands = [call.args[0] for call in run.call_args_list]
    assert commands[:4] == [
        [check.sys.executable, "-m", "pip", "check"],
        [check.sys.executable, "-m", "ruff", "check", "."],
        [check.sys.executable, "-m", "ruff", "format", "--check", "."],
        [check.sys.executable, "scripts/build_i18n.py", "--check"],
    ]
    assert [command for command in commands if command[:2] == ["node", "--check"]] == [
        ["node", "--check", "dashboard/static/app.js"],
        ["node", "--check", "dashboard/static/nested/worker.js"],
    ]
    pytest_command = commands[-1]
    cache_dir = pytest_command[pytest_command.index("-o") + 1].removeprefix("cache_dir=")
    assert Path(cache_dir).parent == tmp_path / ".tmp-pytest"
    assert not Path(cache_dir).exists()
    assert pytest_command == [
        check.sys.executable,
        "-m",
        "pytest",
        "-q",
        "tests",
        "-n",
        str(workers),
        "--dist=worksteal",
        "--durations=20",
        "--tracing=off",
        "--screenshot=only-on-failure",
        "-o",
        f"cache_dir={cache_dir}",
        f"--output=test-results/{Path(cache_dir).name}",
        f"--junitxml=test-results/{Path(cache_dir).name}/results.xml",
    ]
    assert all(call.kwargs == {"cwd": tmp_path, "check": True} for call in run.call_args_list)
    assert check.os.environ["PYTEST_DEBUG_TEMPROOT"] == str(tmp_path / ".tmp-pytest")


def test_failed_check_stops_with_its_exit_code(monkeypatch, tmp_path):
    monkeypatch.setattr(check, "ROOT", tmp_path)
    monkeypatch.setattr(check.sys, "argv", ["check.py"])
    run = Mock(side_effect=subprocess.CalledProcessError(7, ["check"]))
    monkeypatch.setattr(check.subprocess, "run", run)
    with pytest.raises(SystemExit) as error:
        check.main()
    assert error.value.code == 7
    assert run.call_count == 1


@pytest.mark.parametrize("diagnostic_status", [0, 2])
def test_diagnostic_replay_preserves_original_failure(monkeypatch, tmp_path, diagnostic_status):
    monkeypatch.setattr(check, "ROOT", tmp_path)
    monkeypatch.setattr(check.sys, "argv", ["check.py", "--workers", "2"])

    def execute(command, **kwargs):
        fail_suite(command, **kwargs)
        return subprocess.CompletedProcess(command, diagnostic_status)

    run = Mock(side_effect=execute)
    monkeypatch.setattr(check.subprocess, "run", run)
    with pytest.raises(SystemExit) as error:
        check.main()
    assert error.value.code == 1
    assert len(pytest_calls(run)) == 2
    replay = run.call_args.args[0]
    assert replay[:8] == [
        check.sys.executable,
        "-m",
        "pytest",
        "-q",
        "tests",
        "-n",
        "2",
        "--dist=worksteal",
    ]
    assert "--lf" in replay
    assert "--last-failed-no-failures=none" in replay
    assert "--tracing=on" in replay and "--screenshot=on" in replay
    primary = run.call_args_list[-2].args[0]
    cache_setting = primary[primary.index("-o") + 1]
    output = next(arg.removeprefix("--output=") for arg in primary if arg.startswith("--output="))
    assert replay[replay.index("-o") + 1] == cache_setting
    assert f"--output={output}/diagnostic" in replay
    assert f"--junitxml={output}/diagnostic/results.xml" in replay
    assert run.call_args.kwargs == {"cwd": tmp_path, "check": False}


def test_initial_tracing_keeps_failure_artifacts_without_replay(monkeypatch, tmp_path):
    monkeypatch.setattr(check, "ROOT", tmp_path)
    monkeypatch.setattr(check.sys, "argv", ["check.py", "--trace"])
    run = Mock(side_effect=fail_suite)
    monkeypatch.setattr(check.subprocess, "run", run)
    with pytest.raises(SystemExit) as error:
        check.main()
    assert error.value.code == 1
    assert len(pytest_calls(run)) == 1
    assert "--tracing=retain-on-failure" in run.call_args.args[0]


def test_diagnostic_start_error_preserves_original_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(check, "ROOT", tmp_path)
    monkeypatch.setattr(check.sys, "argv", ["check.py"])

    def execute(command, **kwargs):
        fail_suite(command, **kwargs)
        if command[1:3] == ["-m", "pytest"]:
            raise OSError("diagnostic unavailable")

    run = Mock(side_effect=execute)
    monkeypatch.setattr(check.subprocess, "run", run)
    with pytest.raises(SystemExit) as error:
        check.main()
    assert error.value.code == 1


def test_separate_checks_have_isolated_caches_and_artifacts(monkeypatch, tmp_path):
    monkeypatch.setattr(check, "ROOT", tmp_path)
    monkeypatch.setattr(check.sys, "argv", ["check.py"])
    run = Mock()
    monkeypatch.setattr(check.subprocess, "run", run)
    check.main()
    check.main()
    commands = [call.args[0] for call in pytest_calls(run)]
    caches = [command[command.index("-o") + 1] for command in commands]
    outputs = [next(arg for arg in command if arg.startswith("--output=")) for command in commands]
    assert caches[0] != caches[1]
    assert outputs[0] != outputs[1]
    assert all(not Path(cache.removeprefix("cache_dir=")).exists() for cache in caches)


def test_another_suite_waits_until_the_active_suite_releases_its_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(check, "ROOT", tmp_path)
    (tmp_path / ".tmp-pytest").mkdir()
    started, acquired = Event(), Event()

    def waiting_suite():
        started.set()
        with check.test_run_lock():
            acquired.set()

    with check.test_run_lock():
        thread = Thread(target=waiting_suite, daemon=True)
        thread.start()
        assert started.wait(timeout=3)
        assert not acquired.wait(timeout=0.1)
    assert acquired.wait(timeout=3)
    thread.join(timeout=3)
    assert not thread.is_alive()
