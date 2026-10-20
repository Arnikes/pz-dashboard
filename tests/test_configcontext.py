"""Actual Docker mount aliases and launch profile ownership, without a daemon."""

import json
import io
import sys
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
import config  # noqa: E402
import configeditor as editor  # noqa: E402
import dockerlib  # noqa: E402

CONTAINER_STARTUP_VERSION = dockerlib.container_startup_version


@pytest.fixture
def docker_context(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "Server").mkdir(parents=True)
    monkeypatch.setitem(config.CFG, "data_dir", str(data))
    monkeypatch.setitem(config.CFG, "pz_container", "pz-test")
    monkeypatch.setitem(config.CFG, "dashboard_dir", str(tmp_path / "panel"))
    monkeypatch.setenv("HOSTNAME", "dashboard-test")
    monkeypatch.setenv("PZ_VERSION", "42.15.1")
    monkeypatch.delenv("PZ_CONFIG_FILE", raising=False)
    own = {"Mounts": [{"Source": "/vol/shared", "Destination": data.as_posix(), "RW": True}]}
    server = {
        "Config": {
            "Env": ["SERVER_NAME=world", "RCON_PASSWORD=do-not-expose"],
            "Cmd": ["start", "-cachedir=/project-config"],
            "Entrypoint": [],
        },
        "Mounts": [{"Source": "/vol/shared", "Destination": "/project-config", "RW": True}],
    }

    def inspect(args, **kwargs):
        assert args[:2] == ["docker", "inspect"]
        return 0, json.dumps([own if args[2] == "dashboard-test" else server]), ""

    monkeypatch.setattr(editor.dockerlib, "sh", inspect)
    monkeypatch.setattr(
        editor.dockerlib, "container_startup_version", lambda name, started_at: None
    )
    editor._CONTEXT.clear()
    yield own, server, data
    editor._CONTEXT.clear()


def test_mount_alias_and_runtime_cache_directory_are_confirmed(docker_context):
    ctx = editor.context()
    assert ctx["activeFile"] == "world.ini" and ctx["mountsKnown"] and ctx["dataWritable"]
    assert ctx["serverDataPath"] == "/project-config" and not ctx["dataDiagnostic"]
    assert ctx["owners"]["RCONPassword"] == "RCON_PASSWORD"
    assert "do-not-expose" not in json.dumps(ctx)


@pytest.mark.parametrize(
    "mismatch", ["volume", "cachedir", "server-shadow", "panel-shadow", "file-shadow"]
)
def test_different_mounts_and_shadowed_files_are_not_confirmed(docker_context, mismatch):
    own, server, data = docker_context
    if mismatch == "volume":
        server["Mounts"][0]["Source"] = "/vol/other"
    elif mismatch == "cachedir":
        server["Config"]["Cmd"] = ["-cachedir=/unmounted"]
    else:
        on_panel = mismatch == "panel-shadow"
        records = own["Mounts"] if on_panel else server["Mounts"]
        prefix = data.as_posix() if on_panel else "/project-config"
        suffix = "/Server/world.ini" if mismatch == "file-shadow" else "/Server"
        records.append({"Source": "/other", "Destination": prefix + suffix, "RW": True})
    ctx = editor.context()
    assert not ctx["mountsKnown"] and not ctx["dataWritable"] and ctx["dataDiagnostic"]


def test_panel_readonly_mount_is_readable_but_not_writable(docker_context):
    own, _, _ = docker_context
    own["Mounts"][0]["RW"] = False
    ctx = editor.context()
    assert ctx["mountsKnown"] and not ctx["dataWritable"] and ctx["dataDiagnostic"]


def test_disagreeing_profile_names_require_explicit_choice(docker_context, monkeypatch):
    _, server, _ = docker_context
    server["Config"]["Cmd"].append("-servername=other")
    assert editor.context()["activeFile"] is None
    monkeypatch.setenv("PZ_CONFIG_FILE", "world.ini")
    assert editor.context()["activeFile"] == "world.ini"


@pytest.mark.parametrize(
    "command_name,environment_name",
    [(None, "other"), ("other", None), ("first", "second"), (None, None)],
)
def test_explicit_profile_overrides_detected_or_missing_names(
    docker_context, monkeypatch, command_name, environment_name
):
    _, server, _ = docker_context
    server["Config"]["Env"] = [
        value for value in server["Config"]["Env"] if not value.startswith("SERVER_NAME=")
    ]
    if environment_name:
        server["Config"]["Env"].append(f"SERVER_NAME={environment_name}")
    if command_name:
        server["Config"]["Cmd"].append(f"-servername={command_name}")
    monkeypatch.setenv("PZ_CONFIG_FILE", "world.ini")

    assert editor.context()["activeFile"] == "world.ini"


def test_blank_explicit_profile_keeps_automatic_detection(docker_context, monkeypatch):
    monkeypatch.setenv("PZ_CONFIG_FILE", "")

    assert editor.context()["activeFile"] == "world.ini"


@pytest.mark.parametrize("version", ["42.15.1", "42.13", "41.78.16"])
def test_startup_header_parsing_ignores_mod_and_player_messages(version):
    header = (
        f"LOG  : General f:0, t:1765668999341, st:214952175> version={version} build demo=false"
    )
    assert dockerlib.parse_startup_version(header) == version
    assert dockerlib.parse_startup_version("2026-09-30T10:00:00.123Z " + header) == version
    for line in (
        f"LOG : General > player says version={version}",
        f"LOG : General > mod version={version}",
        f"LOG : Mod > version={version}",
        f"version={version}",
    ):
        assert dockerlib.parse_startup_version(line) is None


def test_streamed_version_probe_uses_current_launch_and_stops_reader(monkeypatch):
    # Docker still filters out headers belonging to an earlier launch.
    process = Mock(
        stdout=io.StringIO(
            "LOG : General > mod version=42.99.0\n"
            "LOG : General f:0, t:1> version=42.15.1 build\n"
            "LOG : General f:0, t:2> version=42.99.0 old launch\n"
        )
    )
    process.poll.return_value = None
    spawn = Mock(return_value=process)
    monkeypatch.setattr(dockerlib.subprocess, "Popen", spawn)
    assert dockerlib.container_startup_version("pz-test", "launch", timeout=1) == "42.15.1"
    assert spawn.call_args.args[0] == [
        "docker",
        "logs",
        "--tail",
        "100000",
        "--since",
        "launch",
        "--timestamps",
        "pz-test",
    ]
    assert spawn.call_args.kwargs["stderr"] == dockerlib.subprocess.STDOUT
    process.kill.assert_called_once()
    assert process.stdout.closed


def test_finite_tail_avoids_decoding_slow_history_before_current_launch(monkeypatch):
    release = threading.Event()
    header = (
        "2026-10-20T10:08:42.507752052Z LOG : General f:0 st:461,484,378> "
        "version=42.21.0 4a0e9546ec demo=false\n"
    )

    class SlowHistory(io.StringIO):
        def readline(self, size=-1):
            release.wait(timeout=2)
            return super().readline(size)

    full_scan = Mock(stdout=SlowHistory(header))
    full_scan.poll.return_value = None
    full_scan.kill.side_effect = release.set
    recent = Mock(stdout=io.StringIO(header))
    recent.poll.return_value = 0

    def spawn(args, **kwargs):
        # --since filters after old records have been decoded. Finite --tail
        # seeks backwards first, so those records never delay the first output.
        return recent if "--tail" in args else full_scan

    monkeypatch.setattr(dockerlib.subprocess, "Popen", spawn)
    assert (
        dockerlib.container_startup_version(
            "projectzomboid", "2026-10-20T10:08:17.319550897Z", timeout=0.3
        )
        == "42.21.0"
    )
    assert recent.stdout.closed
    full_scan.kill.assert_not_called()
    full_scan.stdout.close()


@pytest.mark.parametrize("empty_windows", [1, 2])
def test_version_probe_expands_window_then_falls_back_to_all_logs(monkeypatch, empty_windows):
    processes = [
        Mock(stdout=io.StringIO("recent activity without a startup header\n"))
        for _ in range(empty_windows)
    ]
    processes.append(Mock(stdout=io.StringIO("LOG : General > version=42.21.0 build\n")))
    for process in processes:
        process.poll.return_value = 0
    launched = []

    def launch(args, **kwargs):
        assert all(process.stdout.closed for process in launched)
        process = processes[len(launched)]
        launched.append(process)
        return process

    spawn = Mock(side_effect=launch)
    monkeypatch.setattr(dockerlib.subprocess, "Popen", spawn)

    assert dockerlib.container_startup_version("pz-test", "launch", timeout=2) == "42.21.0"
    tails = [
        call.args[0][call.args[0].index("--tail") + 1] if "--tail" in call.args[0] else None
        for call in spawn.call_args_list
    ]
    assert tails == ["100000", "1000000", None][: empty_windows + 1]
    for call in spawn.call_args_list:
        assert call.args[0][-4:] == ["--since", "launch", "--timestamps", "pz-test"]
        assert call.kwargs["stderr"] == dockerlib.subprocess.STDOUT
    assert all(process.stdout.closed for process in processes)


def test_version_probe_does_not_expand_after_docker_or_reader_error(monkeypatch):
    process = Mock(stdout=io.StringIO("Error: logs are unavailable\n"))
    process.poll.return_value = 1
    spawn = Mock(return_value=process)
    monkeypatch.setattr(dockerlib.subprocess, "Popen", spawn)
    assert dockerlib.container_startup_version("pz-test", "launch", timeout=1) is None
    assert spawn.call_count == 1 and process.stdout.closed

    class BrokenStream(io.StringIO):
        def readline(self, size=-1):
            raise OSError("log pipe failed")

    broken = Mock(stdout=BrokenStream())
    broken.poll.return_value = 0
    spawn.reset_mock()
    spawn.return_value = broken
    assert dockerlib.container_startup_version("pz-test", "launch", timeout=0.3) is None
    assert spawn.call_count == 1 and broken.stdout.closed


def test_version_probe_shares_output_budget_between_windows(monkeypatch):
    monkeypatch.setattr(dockerlib, "STARTUP_LOG_BYTES_LIMIT", 80)
    processes = [
        Mock(stdout=io.StringIO("x" * 60 + "\n")),
        Mock(stdout=io.StringIO("LOG : General > version=42.21.0 build\n")),
    ]
    for process in processes:
        process.poll.return_value = 0
    spawn = Mock(side_effect=processes)
    monkeypatch.setattr(dockerlib.subprocess, "Popen", spawn)
    assert dockerlib.container_startup_version("pz-test", "launch", timeout=2) is None
    assert spawn.call_count == 2 and all(process.stdout.closed for process in processes)


def test_version_probe_shares_deadline_between_windows(monkeypatch):
    clock = [0]
    monkeypatch.setattr(dockerlib.time, "monotonic", lambda: clock[0])

    def consume_window(args, deadline, output_limit):
        clock[0] += 8
        return None, 1, True

    attempt = Mock(side_effect=consume_window)
    monkeypatch.setattr(dockerlib, "_startup_version_attempt", attempt)
    assert dockerlib.container_startup_version("pz-test", "launch", timeout=15) is None
    assert attempt.call_count == 2
    assert [call.args[1] for call in attempt.call_args_list] == [15, 15]


def test_version_probe_times_out_blocked_reader_and_cleans_up(monkeypatch):
    release = threading.Event()

    class BlockedStream(io.StringIO):
        def readline(self, size=-1):
            release.wait(timeout=2)
            return super().readline(size)

    process = Mock(stdout=BlockedStream())
    process.poll.return_value = None
    process.kill.side_effect = release.set
    spawn = Mock(return_value=process)
    monkeypatch.setattr(dockerlib.subprocess, "Popen", spawn)

    assert dockerlib.container_startup_version("pz-test", "launch", timeout=0.2) is None
    assert spawn.call_count == 1 and process.stdout.closed
    process.kill.assert_called_once()


def test_streamed_probe_is_bounded_and_handles_missing_docker(monkeypatch):
    monkeypatch.setattr(dockerlib, "STARTUP_LOG_BYTES_LIMIT", 2_000_000)
    process = Mock(
        stdout=io.StringIO("unrelated output\n" * 140000 + "LOG : General > version=42.15.1\n")
    )
    process.poll.return_value = 0
    spawn = Mock(return_value=process)
    monkeypatch.setattr(dockerlib.subprocess, "Popen", spawn)
    assert dockerlib.container_startup_version("pz-test", "launch", timeout=2) is None
    assert process.stdout.closed
    process.kill.assert_not_called()
    spawn.reset_mock()
    assert dockerlib.container_startup_version("pz-test", "0001-01-01") is None
    spawn.assert_not_called()
    spawn.side_effect = FileNotFoundError()
    assert dockerlib.container_startup_version("pz-test", "launch") is None


def test_first_install_noise_does_not_hide_the_game_version(monkeypatch):
    noise = ("Steam installation: " + "x" * 20000 + "\n") * 200
    process = Mock(stdout=io.StringIO(noise + "LOG : General f:0, t:1> version=42.21.0 build\n"))
    process.poll.return_value = 0
    monkeypatch.setattr(dockerlib.subprocess, "Popen", Mock(return_value=process))
    assert dockerlib.container_startup_version("pz-test", "launch", timeout=2) == "42.21.0"
    assert process.stdout.closed


def test_observed_version_overrides_hint_and_cache_is_launch_scoped(docker_context, monkeypatch):
    _, server, _ = docker_context
    server.update(Id="container-a", Image="image-a", State={"StartedAt": "launch-a"})
    probe = Mock(side_effect=["42.20.1", "42.21.0", "41.78.16"])
    monkeypatch.setattr(editor.dockerlib, "container_startup_version", probe)
    ctx = editor.context()
    assert ctx["version"] == "42.20.1" and ctx["versionSource"] == "startup-log"
    assert ctx["versionKnown"] and "42.15.1" in ctx["versionDiagnostic"]
    assert editor.context(refresh=True)["version"] == "42.20.1"
    assert probe.call_count == 1
    server["State"]["StartedAt"] = "launch-b"
    assert editor.context(refresh=True)["version"] == "42.21.0"
    server["Image"] = "image-b"
    ctx = editor.context(refresh=True)
    assert ctx["version"] == "41.78.16" and not ctx["versionKnown"]
    assert probe.call_count == 3


def test_automatic_version_after_game_update_uses_current_launch_without_hint(
    docker_context, monkeypatch
):
    _, server, _ = docker_context
    monkeypatch.delenv("PZ_VERSION")
    monkeypatch.setattr(editor.dockerlib, "container_startup_version", CONTAINER_STARTUP_VERSION)
    server.update(
        Id="container-a", Image="image-a", State={"StartedAt": "2026-10-20T10:08:17.000Z"}
    )
    history = [
        "2026-10-12T21:38:00.000Z LOG : General > version=41.78.16 build\n",
        "2026-10-20T10:08:42.507Z LOG : General f:0 st:461,484,378> version=42.20.0 build\n",
    ]
    processes = []

    def spawn(args, **kwargs):
        assert args[:4] == ["docker", "logs", "--tail", "100000"]
        since = args[args.index("--since") + 1]
        assert since == server["State"]["StartedAt"]
        # Model Docker's --since filtering so old retained headers cannot win.
        stream = "".join(line for line in history if line.split()[0] >= since)
        process = Mock(stdout=io.StringIO(stream))
        process.poll.return_value = 0
        processes.append(process)
        return process

    probe = Mock(side_effect=spawn)
    monkeypatch.setattr(dockerlib.subprocess, "Popen", probe)
    ctx = editor.context()
    assert ctx["version"] == "42.20.0" and ctx["versionKnown"]
    assert ctx["versionSource"] == "startup-log" and ctx["versionDiagnostic"] is None
    assert editor.context(refresh=True)["version"] == "42.20.0" and probe.call_count == 1

    server["State"]["StartedAt"] = "2026-10-21T10:08:17.000Z"
    server["Image"] = "image-b"
    history.append("2026-10-21T10:08:42.507Z LOG : General > version=42.21.0 build\n")
    ctx = editor.context(refresh=True)
    assert ctx["version"] == "42.21.0" and ctx["versionKnown"]
    assert ctx["versionSource"] == "startup-log" and ctx["versionDiagnostic"] is None
    path = next((Path(config.CFG["dashboard_dir"]) / "game-metadata").glob("*-version.json"))
    cached = editor.load_json(path)
    assert cached == {
        "identity": ["pz-test", "container-a", "image-b", "2026-10-21T10:08:17.000Z"],
        "version": "42.21.0",
    }
    assert editor.context(refresh=True)["version"] == "42.21.0" and probe.call_count == 2
    assert all(process.stdout.closed for process in processes)


@pytest.mark.parametrize("candidate", [42, ["42.15"], "wrong"])
def test_invalid_version_cache_is_ignored(docker_context, monkeypatch, candidate):
    _, server, _ = docker_context
    server.update(Id="container-a", Image="image-a", State={"StartedAt": "launch-a"})
    monkeypatch.setattr(editor.dockerlib, "container_startup_version", lambda *args: "42.20.1")
    editor.context()
    path = next((Path(config.CFG["dashboard_dir"]) / "game-metadata").glob("*-version.json"))
    cached = editor.load_json(path)
    cached["version"] = candidate
    editor.save_json(path, cached)
    assert editor.context(refresh=True)["version"] == "42.20.1"
