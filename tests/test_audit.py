"""Audit regressions: failures must not report success or destroy existing data."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import config
import configformats
import configschema
import dockerlib
import fileio
import ops
import rcon


@pytest.fixture
def audit_env(tmp_path, monkeypatch):
    cfg = dict(config.CFG)
    for key in ("data_dir", "dashboard_dir", "backup_dir"):
        cfg[key] = str(tmp_path / key)
        Path(cfg[key]).mkdir()
    cfg["settings_file"] = str(tmp_path / "settings.json")
    cfg["events_file"] = str(tmp_path / "events.jsonl")
    monkeypatch.setattr(config, "CFG", cfg)
    monkeypatch.setattr(ops, "_SETTINGS", deepcopy(ops._DEFAULTS))
    monkeypatch.setattr(ops, "_SETTINGS_VERSION", {"epoch": "audit", "revision": 7})
    monkeypatch.setattr(ops, "_EV_MEM", deque(maxlen=300))
    monkeypatch.setattr(ops, "_set_phase", Mock())
    monkeypatch.setattr(ops, "is_running", lambda: False)
    monkeypatch.setattr(ops, "container_state", lambda: {"running": False})
    monkeypatch.setattr(dockerlib, "container_start", Mock(return_value=(0, "", "")))
    return Path(cfg["data_dir"])


@pytest.mark.parametrize("mode", ["start", "restart", "resurrected"])
def test_failed_launch_never_records_success(audit_env, monkeypatch, mode):
    monkeypatch.setattr(ops, "wait_until_running", Mock(return_value=False))
    monkeypatch.setattr(ops, "log_event", Mock())
    if mode == "start":
        action = ops._do_start
    else:
        monkeypatch.setattr(ops, "is_running", lambda: True)
        monkeypatch.setattr(
            ops,
            "graceful_stop",
            Mock(return_value="resurrected" if mode == "resurrected" else "stopped"),
        )

        def action():
            return ops._do_restart(0)

    with pytest.raises(ops.OpsError, match="не запустился"):
        action()
    ops.log_event.assert_not_called()


@pytest.mark.parametrize("fault", ["copy", "move-original", "publish"])
def test_restore_io_failure_preserves_original_world(audit_env, tmp_path, monkeypatch, fault):
    world = audit_env / "world.bin"
    world.write_bytes(b"original")
    (audit_env / "Server").mkdir()
    (audit_env / "Server" / "world.ini").write_bytes(b"original config")
    restored = tmp_path / "prepared"
    restored.mkdir()
    (restored / "world.bin").write_bytes(b"backup")
    (restored / "new.bin").write_bytes(b"new")
    copytree, replace = ops.shutil.copytree, ops.os.replace
    failed = False

    def failing_copy(source, target, **kwargs):
        if fault == "copy":
            Path(target, "partial.bin").write_bytes(b"partial")
            raise OSError("disk full")
        return copytree(source, target, **kwargs)

    def failing_replace(source, target):
        nonlocal failed
        publication = Path(source).parent.name.startswith(".pz-restore-")
        moving_original = Path(target).parent.name.startswith(".pz-previous-")
        if not failed and (
            (fault == "publish" and publication) or (fault == "move-original" and moving_original)
        ):
            failed = True
            raise OSError("write failed")
        return replace(source, target)

    monkeypatch.setattr(ops.shutil, "copytree", failing_copy)
    monkeypatch.setattr(ops.os, "replace", failing_replace)
    with pytest.raises((ops.OpsError, OSError)):
        ops._restore_prepared(str(restored), "test.tar.gz")
    assert world.read_bytes() == b"original"
    assert (audit_env / "Server" / "world.ini").read_bytes() == b"original config"
    assert {p.name for p in audit_env.iterdir()} == {"world.bin", "Server"}
    dockerlib.container_start.assert_not_called()


def test_restore_failed_rollback_retains_original_files(audit_env, tmp_path, monkeypatch):
    (audit_env / "world.bin").write_bytes(b"original")
    restored = tmp_path / "prepared"
    restored.mkdir()
    (restored / "world.bin").write_bytes(b"backup")
    replace = ops.os.replace

    def fail_publication_and_rollback(source, target):
        if Path(source).parent != audit_env:
            raise OSError("volume unavailable")
        return replace(source, target)

    monkeypatch.setattr(ops.os, "replace", fail_publication_and_rollback)
    with pytest.raises(ops.OpsError, match="Откат не завершён"):
        ops._restore_prepared(str(restored), "test.tar.gz")
    original = next(audit_env.glob(".pz-previous-*"))
    assert (original / "world.bin").read_bytes() == b"original"
    assert next(audit_env.glob(".pz-restore-*")).is_dir()
    dockerlib.container_start.assert_not_called()


def test_failed_settings_write_preserves_revision_memory_and_disk(audit_env, monkeypatch):
    ops._save_settings()
    settings = Path(config.CFG["settings_file"])
    before = settings.read_bytes()
    monkeypatch.setattr(ops.os, "replace", Mock(side_effect=OSError("disk full")))
    with pytest.raises(ops.OpsError, match="Не удалось сохранить"):
        ops.patch_settings({"autoUpdate": {"enabled": True}})
    assert ops._SETTINGS == ops._DEFAULTS
    assert ops._SETTINGS_VERSION["revision"] == 7
    assert settings.read_bytes() == before
    assert not list(settings.parent.glob(".settings-*"))


def test_startup_event_keeps_previous_history(audit_env):
    saved = {"ts": ops.now_iso(), "type": "backup", "text": "previous backup"}
    Path(config.CFG["events_file"]).write_text(json.dumps(saved) + "\n", encoding="utf-8")
    ops.log_event("docker", "Пульт запущен", notify=False)
    assert [entry["text"] for entry in ops.get_events()] == ["Пульт запущен", "previous backup"]


def test_malformed_player_history_does_not_break_collection(audit_env, monkeypatch):
    saved = {"ts": ops.now_iso(), "count": 2}
    malformed = [None, 1, "text", {}, {"ts": "bad", "count": 1}, {"ts": saved["ts"], "count": []}]
    Path(ops._ph_file()).write_text(json.dumps([*malformed, saved]), encoding="utf-8")
    monkeypatch.setattr(ops, "_PH", None)
    assert ops.get_players_history() == [saved]
    ops.record_players_sample(3)
    assert ops.get_players_history() == [saved]


def test_unknown_container_state_never_confirms_stop(audit_env, monkeypatch):
    monkeypatch.setattr(ops, "container_state", Mock(return_value=None))
    monkeypatch.setattr(ops.time, "sleep", Mock())
    assert ops.wait_until_stopped(timeout=10) == "running"


@pytest.mark.parametrize("state", [None, {"running": True}])
def test_restore_rechecks_stopped_container_before_writing(audit_env, tmp_path, monkeypatch, state):
    (audit_env / "world.bin").write_bytes(b"original")
    monkeypatch.setattr(ops, "container_state", Mock(return_value=state))
    replace = Mock()
    monkeypatch.setattr(ops, "_restore_files", replace)
    with pytest.raises(ops.OpsError, match="не подтверждена"):
        ops._restore_prepared(str(tmp_path), "test.tar.gz")
    replace.assert_not_called()
    dockerlib.container_start.assert_not_called()


def test_update_compares_container_image_even_if_tag_was_pulled(audit_env, monkeypatch):
    state = {"running": True, "image": "repo/server:latest", "imageId": "sha256:installed"}
    monkeypatch.setattr(ops, "container_state", Mock(return_value=state))
    digest = Mock(
        side_effect=lambda image: "sha256:old" if image == state["imageId"] else "sha256:new"
    )
    monkeypatch.setattr(dockerlib, "image_digests", digest)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return b'{"digest": "sha256:new"}'

    monkeypatch.setattr(ops.urllib.request, "urlopen", Mock(return_value=Response()))
    assert ops.check_update()["available"] is True
    digest.assert_called_once_with("sha256:installed")
    monkeypatch.setattr(dockerlib, "image_pull", Mock(return_value=(0, "", "")))
    monkeypatch.setattr(dockerlib, "compose_version", Mock(return_value=False))
    with pytest.raises(ops.OpsError, match="compose недоступен"):
        ops._do_apply_update(0)
    dockerlib.image_pull.assert_called_once_with("repo/server:latest")


def test_digest_cache_refreshes_for_same_tag_new_container(audit_env, monkeypatch):
    monkeypatch.setattr(ops, "_LOCAL_DIGEST", {"at": 0, "image": None, "imageId": None})
    digest = Mock(side_effect=["old", "new"])
    monkeypatch.setattr(dockerlib, "image_digests", digest)
    assert ops.local_digest_cached(image="repo:latest", image_id="id1") == "old"
    assert ops.local_digest_cached(image="repo:latest", image_id="id1") == "old"
    assert ops.local_digest_cached(image="repo:latest", image_id="id2") == "new"
    assert [call.args for call in digest.call_args_list] == [("id1",), ("id2",)]


def test_healthcheck_respects_configured_port():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/api/health"
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parents[1] / "dashboard" / "healthcheck.py")],
            env={**os.environ, "PORT": str(server.server_port)},
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr.decode()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("failures", [2, 5])
def test_windows_replace_retries_are_bounded(monkeypatch, failures):
    error = PermissionError("file temporarily busy")
    error.winerror = 32
    replace = Mock(side_effect=[error] * failures + [None])
    sleep = Mock()
    monkeypatch.setattr(fileio, "os", SimpleNamespace(name="nt", replace=replace))
    monkeypatch.setattr(fileio.time, "sleep", sleep)
    if failures == 5:
        with pytest.raises(PermissionError):
            fileio.replace("temporary", "target")
        assert replace.call_count == 5 and sleep.call_count == 4
    else:
        fileio.replace("temporary", "target")
        assert replace.call_count == 3 and sleep.call_count == 2
    assert all(call.args == ("temporary", "target") for call in replace.call_args_list)


def test_regular_permission_denial_is_not_retried(monkeypatch):
    replace = Mock(side_effect=PermissionError("read-only directory"))
    monkeypatch.setattr(fileio, "os", SimpleNamespace(name="nt", replace=replace))
    with pytest.raises(PermissionError):
        fileio.replace("temporary", "target")
    assert replace.call_count == 1


def test_oversized_lua_numbers_raise_validation_errors():
    value = 10**400
    with pytest.raises(configformats.FormatError):
        configformats.literal(value)
    with pytest.raises(configformats.FormatError):
        configformats.LuaTable(f"SandboxVars = {{ Count = {value} }}")
    errors = []
    configschema.check_field({"key": "Count", "type": "integer"}, value, errors, lua=True)
    assert errors == [{"key": "Count", "message": "Число вне допустимого диапазона"}]


@pytest.mark.parametrize("reply", ["delayed", "fragmented", "silent", "empty"])
def test_rcon_waits_for_response_and_rejects_silence(reply):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    errors = []

    def read_packet(connection):
        header = connection.recv(4)
        length = int.from_bytes(header, "little")
        remaining = length
        while remaining:
            chunk = connection.recv(remaining)
            if not chunk:
                raise AssertionError("unexpected EOF")
            remaining -= len(chunk)

    def serve():
        try:
            with server, server.accept()[0] as connection:
                connection.settimeout(2)
                read_packet(connection)
                connection.sendall(rcon._pack(1, 2, ""))
                read_packet(connection)
                if reply == "silent":
                    time.sleep(0.3)
                    return
                packet = rcon._pack(2, 0, "" if reply == "empty" else "Players connected (0):")
                if reply == "delayed":
                    time.sleep(0.12)
                if reply == "fragmented":
                    connection.sendall(packet[:6])
                    time.sleep(0.12)
                    packet = packet[6:]
                connection.sendall(packet)
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client = rcon.RCON("127.0.0.1", port, "secret", idle_timeout=0.03, total_timeout=0.25)
    try:
        if reply == "silent":
            with pytest.raises(rcon.RCONError, match="не ответил"):
                client.run("players")
        else:
            assert client.run("players") == ("" if reply == "empty" else "Players connected (0):")
    finally:
        thread.join(timeout=3)
    assert not thread.is_alive() and not errors
