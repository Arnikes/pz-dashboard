"""Actual Docker mount aliases and launch profile ownership, without a daemon."""

import json
import io
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
import config  # noqa: E402
import configeditor as editor  # noqa: E402
import dockerlib  # noqa: E402


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
    # Streaming begins at the launch, so long tails cannot hide the actual header.
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
        "--since",
        "launch",
        "--timestamps",
        "pz-test",
    ]
    assert spawn.call_args.kwargs["stderr"] == dockerlib.subprocess.STDOUT
    process.kill.assert_called_once()
    assert process.stdout.closed


def test_streamed_probe_is_bounded_and_handles_missing_docker(monkeypatch):
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
