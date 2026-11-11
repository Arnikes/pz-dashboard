"""Planned routes follow real workers, including skipped and nested branches."""

import json
import subprocess
import tarfile
import threading
from collections import deque
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

import actions
import config
import dashboardupdate
import dockerlib
import i18n
import operationprogress
import ops


@pytest.fixture
def workers(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "_ACTIVE", {"op": None, "stages": []})
    monkeypatch.setattr(ops, "_OP_HISTORY", deque())
    monkeypatch.setattr(ops, "_OP_GENERATION", 0)
    monkeypatch.setattr(ops, "_OP_CANCEL", threading.Event())
    monkeypatch.setattr(dashboardupdate, "operation", lambda: None)
    monkeypatch.setattr(ops, "log_event", Mock())
    monkeypatch.setattr(ops, "_begin_watchdog_grace", Mock())
    monkeypatch.setattr(ops, "rcon", Mock(return_value="Players connected (0):"))
    monkeypatch.setattr(ops, "rcon_warn_broadcast", Mock(return_value=True))
    monkeypatch.setattr(ops.time, "sleep", Mock())
    monkeypatch.setattr(ops, "is_running", lambda: True)
    monkeypatch.setattr(ops, "wait_until_running", lambda timeout: True)
    monkeypatch.setattr(dockerlib, "container_start", lambda name: (0, "", ""))
    server = {"status": "running", "running": True, "startedAt": "S1", "containerId": "game"}
    monkeypatch.setattr(ops, "container_state", lambda: server.copy())

    def stop(hook, *, expected_run=None):
        hook("stopped")
        server.update(status="exited", running=False)
        return "stopped"

    monkeypatch.setattr(ops, "graceful_stop", stop)
    settings = deepcopy(ops._DEFAULTS)
    monkeypatch.setattr(ops, "get_settings", lambda: settings)
    monkeypatch.setattr(ops, "_journal_append", Mock())
    monkeypatch.setattr(ops, "_prune_backups", Mock(return_value=0))
    monkeypatch.setattr(ops, "_prune_s3_backups", Mock(return_value=0))
    monkeypatch.setitem(config.CFG, "backup_dir", str(tmp_path / "backups"))
    monkeypatch.setitem(config.CFG, "dashboard_dir", str(tmp_path / "dashboard"))
    monkeypatch.setitem(config.CFG, "backup_s3_enabled", False)
    data = tmp_path / "data"
    data.mkdir()
    (data / "world.bin").write_bytes(b"world")
    monkeypatch.setitem(config.CFG, "data_dir", str(data))
    monkeypatch.setattr(ops, "_backup_paths", lambda: (config.CFG["backup_dir"], str(data)))

    def archive(bdir, ddir):
        name = "sample.tar.gz"
        dest = Path(bdir) / name
        ops._set_phase("Создание архива", name)
        with tarfile.open(dest, "w:gz") as target:
            target.add(data / "world.bin", arcname="world.bin")
        return name, str(dest), dest.stat().st_size

    monkeypatch.setattr(ops, "_create_backup_archive", archive)
    targets = []
    monkeypatch.setattr(
        ops.threading,
        "Thread",
        lambda *, target, **kwargs: Mock(start=lambda: targets.append(target)),
    )
    phases = []
    phase = ops._set_phase

    def observe(*args, **kwargs):
        phase(*args, **kwargs)
        phases.append((args[0], deepcopy(ops._ACTIVE.get("plan"))))

    monkeypatch.setattr(ops, "_set_phase", observe)
    return targets, phases, settings


def state(plan, key):
    return next(row["status"] for row in plan if row["id"] == key)


def test_plan_is_complete_before_thread_start_and_detached_from_snapshots(workers):
    targets, _, _ = workers
    receipt = actions.dispatch({"op": "restart", "warnSeconds": 30})
    plan = receipt["operationState"]["active"]["plan"]
    assert [row["id"] for row in plan] == ["prepare", "warn", "stop", "start", "ready"]
    assert [row["status"] for row in plan] == [
        "current",
        "pending",
        "pending",
        "pending",
        "pending",
    ]
    plan[-1]["label"] = "Client mutation"
    assert ops.op_state()["active"]["plan"][-1]["label"] == "Загрузка сервера"
    targets[0]()
    assert ops.op_state()["active"] is None
    assert ops.op_state()["history"][0]["ok"]
    completed = ops.op_state()
    completed["history"][0]["plan"][-1]["label"] = "Client mutation"
    assert ops.op_state()["history"][0]["plan"][-1]["label"] == "Загрузка сервера"


@pytest.mark.parametrize("stop_server", [False, True])
@pytest.mark.parametrize("s3", [False, True])
def test_real_backup_phases_preserve_planned_branch_order(workers, monkeypatch, stop_server, s3):
    targets, phases, _ = workers
    monkeypatch.setitem(config.CFG, "backup_s3_enabled", s3)
    monkeypatch.setattr(ops.backups3, "validate", Mock())

    def upload(path, cfg, phase, *, verification):
        for label in (
            "Выгрузка бэкапа в S3",
            "Проверка загруженного объекта S3",
            "Удаление локальной копии бэкапа",
        ):
            phase(label)
        return "s3://bucket/sample.tar.gz"

    monkeypatch.setattr(ops.backups3, "upload_verified", upload)
    actions.dispatch({"op": "backup", "stopServer": stop_server})
    initial = ops.op_state()["active"]["plan"]
    assert state(initial, "archive-verify") == "pending"
    targets[0]()
    assert ops._OP_HISTORY[0]["ok"]
    ids = [
        next(row["id"] for row in plan if row["status"] == "current")
        for label, plan in phases
        if label != "Готово"
    ]
    expected = (
        ["warn", "stop", "archive", "start", "ready", "archive-verify"]
        if stop_server
        else ["world-save", "write-wait", "archive", "archive-verify"]
    )
    if s3:
        expected += ["s3-upload", "s3-verify", "local-cleanup"]
    assert ids == expected
    assert all(row["status"] in {"completed", "skipped"} for row in ops._OP_HISTORY[0]["plan"])


def test_failed_backup_archive_stays_incomplete_during_real_recovery(workers, monkeypatch):
    targets, phases, _ = workers

    def fail_archive(*args):
        ops._set_phase("Создание архива")
        raise ops.OpsError("Archive failed")

    monkeypatch.setattr(ops, "_create_backup_archive", fail_archive)
    actions.dispatch({"op": "backup", "stopServer": True})
    targets[0]()
    assert not ops._OP_HISTORY[0]["ok"]
    assert ops._OP_HISTORY[0]["message"] == "Archive failed"
    start = next(plan for phase, plan in phases if phase == "Запуск сервера")
    ready = next(plan for phase, plan in phases if phase == "Загрузка сервера")
    assert state(start, "archive") == state(ready, "archive") == "skipped"
    assert state(start, "start") == state(ready, "ready") == "current"
    assert state(ready, "archive-verify") == "pending"


@pytest.mark.parametrize("cancel", [False, True])
def test_nested_real_backup_done_keeps_update_future_pending(workers, monkeypatch, cancel):
    targets, phases, _ = workers
    container = {"Id": "game", "Image": "old", "State": {"Running": True}}
    model = {"services": {config.CFG["pz_service"]: {"image": "game:latest"}}}
    monkeypatch.setattr(dockerlib, "compose_version", lambda: True)
    monkeypatch.setattr(dockerlib, "image_pull", lambda image: (0, "", ""))
    monkeypatch.setattr(ops.serverupdate, "prepare", lambda: (container, model))
    monkeypatch.setattr(ops.serverupdate, "image_id", lambda image: "new")
    inspections = iter([container, {**container, "Image": "new"}])
    monkeypatch.setattr(dashboardupdate, "inspect", lambda name: next(inspections))
    monkeypatch.setattr(ops.serverupdate, "recreate", lambda model, snapshot: (0, "", ""))
    monkeypatch.setattr(ops, "check_update", Mock())
    if cancel:
        archive = ops._create_backup_archive

        def cancel_then_archive(*args):
            ops.cancel_image_update(ops._ACTIVE["id"])
            return archive(*args)

        monkeypatch.setattr(ops, "_create_backup_archive", cancel_then_archive)
    actions.dispatch({"op": "apply-update", "warnSeconds": 30})
    targets[0]()
    assert ops._OP_HISTORY[0]["ok"]
    nested = next(plan for phase, plan in phases if phase == "Готово")
    assert state(nested, "backup") == ("skipped" if cancel else "current")
    assert (
        state(nested, "warn")
        == state(nested, "stop")
        == state(nested, "recreate")
        == ("skipped" if cancel else "pending")
    )
    assert all(sum(row["status"] == "current" for row in plan) == 1 for _, plan in phases)
    assert all(len({row["id"] for row in plan}) == len(plan) for _, plan in phases)
    if cancel:
        assert all(
            state(plan, "cancel") == "current"
            for _, plan in phases
            if any(row["id"] == "cancel" for row in plan)
        )
        assert sum(row["id"] == "cancel" for row in ops._OP_HISTORY[0]["plan"]) == 1
        assert ops._OP_HISTORY[0]["cancelled"]


def test_docker_resurrection_skips_start_and_repeated_recovery_is_a_new_visit(workers, monkeypatch):
    targets, phases, _ = workers
    monkeypatch.setattr(ops, "graceful_stop", lambda hook: (hook("resurrected"), "resurrected")[1])
    actions.dispatch({"op": "restart", "warnSeconds": 0})
    targets[0]()
    ready = next(plan for phase, plan in phases if phase == "Загрузка сервера")
    assert state(ready, "start") == "skipped"
    assert state(ready, "ready") == "current"
    plan = operationprogress.build("restart", {"warnSeconds": 0})
    for phase in ("Остановка", "Запуск", "Загрузка сервера", "Запуск сервера"):
        operationprogress.advance(plan, phase)
    assert len({row["id"] for row in plan}) == len(plan)
    assert plan[-1]["label"] == "Запуск сервера"
    assert plan[-1]["status"] == "current"


def test_unknown_extension_keeps_raw_stages_without_inventing_a_route(workers):
    targets, _, _ = workers
    ops.start_op("extension-custom", lambda: ops._set_phase("Custom phase"))
    assert ops.op_state()["active"]["plan"] is None
    targets[0]()
    assert ops._OP_HISTORY[0]["ok"]


def test_real_countdown_cancellation_skips_interrupted_warning_and_future(workers, monkeypatch):
    targets, _, _ = workers
    requested = []

    def cancel(*args, **kwargs):
        ops.cancel_mods_update(ops._ACTIVE["id"])
        requested.append(ops.op_state()["active"]["plan"])

    monkeypatch.setattr(ops, "rcon_warn_broadcast", cancel)
    ops.start_op("mods-restart", lambda: ops._do_restart(30, cancellable=True))
    targets[0]()
    plan = requested[0]
    assert state(plan, "warn") == "skipped"
    assert state(plan, "stop") == state(plan, "start") == state(plan, "ready") == "skipped"
    assert state(plan, "cancel") == "current"
    finished = ops._OP_HISTORY[0]
    assert finished["cancelled"]
    assert len([row for row in finished["plan"] if row["id"] == "cancel"]) == 1
    assert state(finished["plan"], "cancel") == "completed"


def test_real_restore_checks_archive_before_shutdown(workers, monkeypatch):
    targets, phases, _ = workers
    archive_dir = Path(config.CFG["backup_dir"])
    archive_dir.mkdir()
    name, _, _ = ops._create_backup_archive(str(archive_dir), config.CFG["data_dir"])
    phases.clear()

    @contextmanager
    def prepare(dest):
        yield dest

    monkeypatch.setattr(ops, "_prepare_restore_files", prepare)
    monkeypatch.setattr(ops, "_restore_files", Mock())
    monkeypatch.setattr(ops, "container_state", lambda: {"running": False})
    actions.dispatch({"op": "restore", "name": name})
    targets[0]()
    assert ops._OP_HISTORY[0]["ok"]
    assert [phase for phase, _ in phases] == [
        "Проверка архива перед восстановлением",
        "Подготовка файлов восстановления",
        "Предупреждение игроков",
        "Остановка сервера",
        "Восстановление файлов",
        "Запуск сервера",
        "Загрузка сервера",
        "Готово",
    ]
    for phase, plan in phases:
        if phase in {"Проверка архива перед восстановлением", "Подготовка файлов восстановления"}:
            assert state(plan, "warn") == state(plan, "stop") == "pending"


def test_real_mod_check_keeps_wait_visible_from_admission(workers, monkeypatch):
    targets, phases, _ = workers
    monkeypatch.setattr(ops, "docker_ok_cached", lambda: True)
    logs = iter([("before", ""), ("before\nCheckModsNeedUpdate: Mods updated", "")])
    monkeypatch.setattr(dockerlib, "container_logs", lambda *args: next(logs))
    monkeypatch.setattr(ops, "_parse_mods_check", lambda lines: ("up-to-date", []))
    monkeypatch.setattr(ops, "_mods_registry", lambda: [])
    actions.dispatch({"op": "check-mods-update"})
    assert state(ops._ACTIVE["plan"], "mods-wait") == "pending"
    targets[0]()
    assert ops._OP_HISTORY[0]["ok"]
    assert [phase for phase, _ in phases] == ["Проверка модов", "Ожидание", "Готово"]
    waiting = phases[1][1]
    assert state(waiting, "mods-check") == "completed"
    assert state(waiting, "mods-wait") == "current"


@pytest.mark.parametrize(
    "operation",
    [
        "start",
        "stop",
        "restart",
        "check-update",
        "check-dashboard-update",
        "check-mods-update",
        "apply-update",
        "apply-dashboard-update",
        "backup",
        "verify-backup",
        "restore",
        "apply-config",
        "prepare-workshop",
        "mods-restart",
        "apply-mods-update",
    ],
)
@pytest.mark.parametrize("enabled", [False, True])
def test_submission_preview_agrees_with_known_server_plan(operation, enabled):
    options = {
        "running": enabled,
        "warnSeconds": 30 if enabled else 0,
        "backupBeforeApply": enabled,
        "restart": enabled,
        "backupBeforeUpdate": enabled,
        "s3": False,
        "stopServer": enabled,
    }
    script = "const fs=require('fs'),vm=require('vm');vm.runInThisContext(fs.readFileSync('dashboard/static/operation-plan.js','utf8'));process.stdout.write(JSON.stringify(OperationPlan.preview(process.argv[1],JSON.parse(process.argv[2]))));"
    result = subprocess.run(
        ["node", "-e", script, operation, json.dumps(options)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    preview = json.loads(result.stdout)
    assert preview == operationprogress.build(operation, options)


def test_plan_labels_translate_but_ids_and_statuses_remain_stable():
    raw = {"plan": operationprogress.build("restart", {"warnSeconds": 30})}
    token = i18n.LANGUAGE.set("en")
    try:
        translated = i18n.present(raw)
    finally:
        i18n.LANGUAGE.reset(token)
    assert translated["plan"][1]["label"] == i18n.translate("Предупреждение игроков", locale="en")
    assert [(row["id"], row["status"]) for row in translated["plan"]] == [
        (row["id"], row["status"]) for row in raw["plan"]
    ]
