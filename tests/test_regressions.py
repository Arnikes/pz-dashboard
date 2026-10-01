"""Failure scenarios isolated from Docker and live server data."""

import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))

import config  # noqa: E402
import dockerlib  # noqa: E402
import ops  # noqa: E402


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    cfg = dict(config.CFG)
    for key in ("data_dir", "backup_dir", "dashboard_dir"):
        cfg[key] = str(tmp_path / key)
        Path(cfg[key]).mkdir()
    cfg["settings_file"] = str(tmp_path / "settings.json")
    cfg["events_file"] = str(tmp_path / "events.jsonl")
    monkeypatch.setattr(config, "CFG", cfg)
    monkeypatch.setattr(ops, "_SETTINGS", json.loads(json.dumps(ops._DEFAULTS)))
    monkeypatch.setattr(ops, "log_event", Mock())
    monkeypatch.setattr(ops, "_set_phase", Mock())
    monkeypatch.setattr(ops, "is_running", lambda: False)
    world = Path(cfg["data_dir"]) / "world.bin"
    world.write_bytes(b"original world")
    return world


@pytest.mark.parametrize("kind", ["broken", "empty", "traversal", "symlink"])
@pytest.mark.parametrize("operation", ["restore", "verify"])
def test_bad_archive_does_not_stop_or_wipe_server(sandbox, monkeypatch, kind, operation):
    archive = Path(config.CFG["backup_dir"]) / "bad.tar.gz"
    if kind == "broken":
        archive.write_bytes(b"not gzip")
    else:
        with tarfile.open(archive, "w:gz") as tar:
            if kind == "traversal":
                member = tarfile.TarInfo("../escaped.txt")
                member.size = 3
                tar.addfile(member, io.BytesIO(b"bad"))
            elif kind == "symlink":
                member = tarfile.TarInfo("escape")
                member.type = tarfile.SYMTYPE
                member.linkname = "../escaped"
                tar.addfile(member)
    monkeypatch.setattr(ops, "is_running", lambda: True)
    stop = Mock(return_value="stopped")
    monkeypatch.setattr(ops, "graceful_stop", stop)
    monkeypatch.setattr(ops, "rcon_warn_broadcast", Mock())
    with pytest.raises(ops.OpsError):
        (ops._do_restore if operation == "restore" else ops.verify_backup)(archive.name)
    stop.assert_not_called()
    assert sandbox.read_bytes() == b"original world"
    assert not list(Path(config.CFG["dashboard_dir"]).glob("restore-tmp-*"))
    assert not list(Path(config.CFG["dashboard_dir"]).glob("verify-tmp-*"))


@pytest.mark.parametrize("failure", ["exit", "timeout", "oserror"])
def test_failed_backup_restarts_server_and_removes_partial_archive(sandbox, monkeypatch, failure):
    monkeypatch.setattr(ops, "is_running", lambda: True)
    monkeypatch.setattr(ops, "rcon_warn_broadcast", Mock())
    monkeypatch.setattr(ops, "graceful_stop", Mock(return_value="stopped"))
    start = Mock(return_value=(0, "", ""))
    monkeypatch.setattr(dockerlib, "container_start", start)
    monkeypatch.setattr(ops, "wait_until_running", Mock(return_value=True))

    def fail(args, **kwargs):
        Path(args[2]).write_bytes(b"partial archive")
        if failure == "timeout":
            raise subprocess.TimeoutExpired("tar", 2400)
        if failure == "oserror":
            raise OSError("disk unavailable")
        return subprocess.CompletedProcess(args, 1, "", "disk full")

    monkeypatch.setattr(ops.subprocess, "run", fail)
    with pytest.raises(ops.OpsErrorReported):
        ops.run_backup_job("scheduled", True)
    start.assert_called_once_with(config.CFG["pz_container"])
    assert not list(Path(config.CFG["backup_dir"]).iterdir())
    assert ops.get_backup_journal()[0]["status"] == "error"


def test_backups_in_same_second_preserve_both_world_versions(sandbox, monkeypatch):
    monkeypatch.setattr(ops.time, "strftime", lambda fmt: "pz-backup-20260930-120000")
    first = ops.run_backup_job("manual", False)
    sandbox.write_bytes(b"second world")
    second = ops.run_backup_job("manual", False)
    assert first["name"] != second["name"]
    for result, expected in ((first, b"original world"), (second, b"second world")):
        with tarfile.open(Path(config.CFG["backup_dir"]) / result["name"]) as tar:
            assert tar.extractfile("./world.bin").read() == expected


def test_rotation_keeps_newest_backup_within_the_same_second(sandbox, monkeypatch):
    bdir = Path(config.CFG["backup_dir"])
    older = bdir / "older.tar.gz"
    newer = bdir / "newer.tar.gz"
    older.write_bytes(b"old")
    newer.write_bytes(b"new")
    base = 1_790_816_000_000_000_000
    os.utime(older, ns=(base, base))
    os.utime(newer, ns=(base + 100_000_000, base + 100_000_000))
    monkeypatch.setattr(ops.os, "listdir", lambda path: [older.name, newer.name])
    assert ops.list_backups()[0]["name"] == newer.name
    assert ops._prune_backups(1) == 1
    assert newer.exists() and not older.exists()


@pytest.mark.parametrize(
    "data", [[], None, 42, {"autoUpdate": {"intervalHours": 1e309}}, {"nextCheck": {}}]
)
def test_malformed_settings_do_not_break_startup(sandbox, data):
    Path(config.CFG["settings_file"]).write_text(json.dumps(data), encoding="utf-8")
    ops._load_settings()
    assert ops.get_settings()["autoUpdate"]["intervalHours"] == 6


def test_loaded_settings_validate_switches_and_telegram_types(sandbox):
    Path(config.CFG["settings_file"]).write_text(
        json.dumps(
            {
                "autoUpdate": {"enabled": "false"},
                "telegram": {"botToken": 123456789, "groups": ["ops"]},
                "nextCheck": float("nan"),
            }
        ),
        encoding="utf-8",
    )
    ops._load_settings()
    settings = ops.get_settings()
    assert settings["autoUpdate"]["enabled"] is False
    assert settings["telegram"]["botToken"] == ""
    assert settings["telegram"]["groups"] == ops._DEFAULTS["telegram"]["groups"]
    assert settings["nextCheck"] is None


@pytest.mark.parametrize("restart", ["exit", "timeout"])
def test_backup_restart_failure_is_reported(sandbox, monkeypatch, restart):
    monkeypatch.setattr(ops, "is_running", lambda: True)
    monkeypatch.setattr(ops, "rcon_warn_broadcast", Mock())
    monkeypatch.setattr(ops, "graceful_stop", Mock(return_value="stopped"))
    monkeypatch.setattr(
        dockerlib, "container_start", Mock(return_value=(1 if restart == "exit" else 0, "", "fail"))
    )
    monkeypatch.setattr(ops, "wait_until_running", Mock(return_value=False))
    with pytest.raises(ops.OpsErrorReported, match="после бэкапа"):
        ops.run_backup_job("manual", True)
    assert len(ops.list_backups()) == 1
    assert ops.get_backup_journal()[0]["status"] == "error"


def test_restore_start_timeout_is_reported(sandbox, monkeypatch):
    backup = ops.run_backup_job("manual", False)
    monkeypatch.setattr(dockerlib, "container_start", Mock(return_value=(0, "", "")))
    monkeypatch.setattr(ops, "wait_until_running", Mock(return_value=False))
    with pytest.raises(ops.OpsError, match="сервер не запустился"):
        ops._do_restore(backup["name"])
    assert sandbox.read_bytes() == b"original world"


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_nonfinite_settings_patch_is_rejected_atomically(sandbox, value):
    before = ops.get_settings()
    assert ops.patch_settings({"autoUpdate": {"enabled": True, "intervalHours": value}})
    assert ops.get_settings() == before


def test_scheduler_dispatches_image_update_through_operation_lock(sandbox, monkeypatch):
    ops._SETTINGS["autoUpdate"].update({"enabled": True, "warnSeconds": 30})
    ops._SETTINGS["nextCheck"] = 1
    monkeypatch.setattr(ops, "op_busy", lambda: False)
    monkeypatch.setattr(ops, "check_update", Mock())
    monkeypatch.setattr(ops, "update_state", lambda: {"available": True})
    update = Mock()
    start = Mock()
    monkeypatch.setattr(ops, "_do_apply_update", update)
    monkeypatch.setattr(ops, "start_op", start)
    monkeypatch.setattr(ops, "_post_restart_rescan_tick", Mock())
    monkeypatch.setattr(ops.time, "sleep", Mock(side_effect=StopIteration))
    with pytest.raises(StopIteration):
        ops._scheduler_loop()
    update.assert_not_called()
    start.assert_called_once()
    operation, callback = start.call_args.args
    assert operation == "apply-update"
    callback()
    update.assert_called_once_with(30, "Автообновление сервера")


def test_scheduler_does_not_update_if_another_operation_starts_during_check(sandbox, monkeypatch):
    ops._SETTINGS["autoUpdate"]["enabled"] = True
    ops._SETTINGS["nextCheck"] = 1
    active = dict(ops._ACTIVE, op=None)
    monkeypatch.setattr(ops, "_ACTIVE", active)

    def check(**kwargs):
        active["op"] = "restart"

    monkeypatch.setattr(ops, "check_update", check)
    monkeypatch.setattr(ops, "update_state", lambda: {"available": True})
    update = Mock()
    monkeypatch.setattr(ops, "_do_apply_update", update)
    monkeypatch.setattr(ops, "_post_restart_rescan_tick", Mock())
    monkeypatch.setattr(ops.time, "sleep", Mock(side_effect=StopIteration))
    with pytest.raises(StopIteration):
        ops._scheduler_loop()
    update.assert_not_called()
    assert active["op"] == "restart"
    assert any(call.args[0] == "error" for call in ops.log_event.call_args_list)
