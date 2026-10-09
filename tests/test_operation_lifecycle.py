"""Lifecycle success means a working game server, with locks held until then."""

import itertools
import threading
from collections import deque
from copy import deepcopy
from unittest.mock import Mock

import pytest

import ops
import dockerlib
import i18n


@pytest.fixture
def lifecycle(monkeypatch):
    monkeypatch.setattr(ops, "_ACTIVE", {"op": None, "stages": []})
    monkeypatch.setattr(ops, "_OP_HISTORY", deque())
    monkeypatch.setattr(ops, "_OP_CANCEL", threading.Event())
    monkeypatch.setattr(ops.dashboardupdate, "operation", lambda: None)
    monkeypatch.setattr(ops, "log_event", Mock())
    monkeypatch.setattr(ops, "_begin_watchdog_grace", Mock())
    monkeypatch.setattr(ops, "is_running", lambda: True)
    monkeypatch.setattr(ops, "wait_until_running", lambda timeout: True)
    monkeypatch.setattr(ops, "graceful_stop", Mock(return_value="stopped"))
    monkeypatch.setattr(dockerlib, "container_start", Mock(return_value=(0, "", "")))


@pytest.mark.parametrize("result", ["ready", "timeout"])
@pytest.mark.parametrize(
    "operation",
    [
        "start",
        "restart",
        "resurrected",
        "apply-update",
        "apply-mods-update",
        "backup",
        "restore",
        "update-recovery",
    ],
)
def test_launch_keeps_lock_and_history_pending_until_game_readiness(
    lifecycle, monkeypatch, result, operation, tmp_path
):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def ready(timeout=600):
        ops._set_phase("Загрузка сервера", "Waiting for game")
        entered.set()
        assert release.wait(5)
        return result == "ready"

    monkeypatch.setattr(ops, "wait_until_ready", ready)
    # Mark completion after the worker's finally releases its lock.
    real_thread = threading.Thread

    def thread(*, target, **kwargs):
        def run():
            try:
                target()
            finally:
                finished.set()

        return real_thread(target=run, **kwargs)

    monkeypatch.setattr(ops.threading, "Thread", thread)
    settings = deepcopy(ops._DEFAULTS)
    settings["autoUpdate"]["backupBeforeUpdate"] = False
    monkeypatch.setattr(ops, "get_settings", lambda: settings)
    monkeypatch.setattr(ops, "_set_schedule", Mock())
    monkeypatch.setattr(ops, "rcon_warn_broadcast", Mock())
    monkeypatch.setattr(ops, "_effective_image", lambda: "test-image")
    monkeypatch.setattr(ops, "_installed_digest", lambda image: "old")
    monkeypatch.setattr(dockerlib, "image_pull", lambda image: (0, "", ""))
    monkeypatch.setattr(dockerlib, "image_digests", lambda image: "new")
    monkeypatch.setattr(dockerlib, "compose_version", lambda: True)
    monkeypatch.setattr(
        dockerlib,
        "compose_up",
        lambda cfg: (1 if operation == "update-recovery" else 0, "", "compose failed"),
    )
    monkeypatch.setattr(ops, "check_update", Mock())
    if operation in ("start", "restore"):
        monkeypatch.setattr(ops, "is_running", lambda: False)
    if operation == "resurrected":
        monkeypatch.setattr(ops, "graceful_stop", lambda hook: "resurrected")
    if operation == "backup":
        data = tmp_path / "data"
        data.mkdir()
        (data / "world.bin").write_bytes(b"world")
        monkeypatch.setattr(ops, "_backup_paths", lambda: (str(tmp_path / "backups"), str(data)))
        monkeypatch.setattr(
            ops,
            "_create_backup_archive",
            lambda *args: ("sample.tar", str(tmp_path / "sample.tar"), 5),
        )
        monkeypatch.setattr(ops, "_prune_backups", lambda keep: 0)
        monkeypatch.setattr(ops, "_journal_append", Mock())
    if operation == "restore":
        monkeypatch.setattr(ops, "container_state", lambda: {"running": False})
        monkeypatch.setattr(ops, "_restore_files", Mock())
    workers = {
        "start": ops._do_start,
        "restart": lambda: ops._do_restart(0),
        "resurrected": lambda: ops._do_restart(0),
        "apply-update": lambda: ops._do_apply_update(0),
        "update-recovery": lambda: ops._do_apply_update(0),
        "apply-mods-update": lambda: ops._do_apply_mods_update(0),
        "backup": lambda: ops._do_backup(True),
        "restore": lambda: ops._restore_prepared(tmp_path, "sample.tar"),
    }
    ops.start_op(operation, workers[operation])
    try:
        assert entered.wait(5)
        assert ops.op_busy()
        state = ops.op_state()
        assert state["history"] == []
        assert state["active"]["phase"] == "Загрузка сервера"
        assert state["active"]["stages"][-1] == "Загрузка сервера"
        with pytest.raises(ops.OpsError, match="другая операция"):
            ops.start_op("stop", Mock())
        state["active"]["stages"].append("Client mutation")
        assert "Client mutation" not in ops.op_state()["active"]["stages"]
    finally:
        release.set()
        assert finished.wait(5)
    assert not ops.op_busy()
    history = ops.op_state()["history"]
    assert len(history) == 1
    assert history[0]["ok"] == (result == "ready" and operation != "update-recovery")
    if result == "timeout" and operation != "update-recovery":
        assert "PZ/RCON не готов" in history[0]["message"]
    assert dockerlib.container_start.call_count == (
        0 if operation in ("apply-update", "resurrected") else 1
    )


def test_readiness_rejects_empty_and_unrelated_rcon_responses(lifecycle, monkeypatch):
    replies = Mock(
        side_effect=[
            ops.rconlib.RCONError("booting"),
            "",
            "Unknown command",
            "Players connected (0):",
        ]
    )
    monkeypatch.setattr(ops, "rcon", replies)
    ticks = itertools.count()
    monkeypatch.setattr(ops.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(ops.time, "sleep", Mock())
    assert ops.wait_until_ready(20)
    assert replies.call_count == 4


@pytest.mark.parametrize("seconds", [5, 45, 300])
@pytest.mark.parametrize("rcon_fails", [False, True])
def test_countdown_honors_full_delay_even_when_warning_fails(
    lifecycle, monkeypatch, seconds, rcon_fails
):
    monkeypatch.setattr(ops.time, "time", lambda: 1000)
    sleep = Mock()
    monkeypatch.setattr(ops.time, "sleep", sleep)
    monkeypatch.setattr(
        ops, "rcon", Mock(side_effect=ops.rconlib.RCONError("offline") if rcon_fails else None)
    )
    ops._set_phase("Предупреждение игроков")
    assert ops.rcon_warn_broadcast(seconds, "Перезапуск сервера") == (not rcon_fails)
    assert sum(call.args[0] for call in sleep.call_args_list) == seconds
    assert ops._ACTIVE["countdownEndsAt"] == 1000 + seconds
    ops._set_phase("Остановка")
    assert ops._ACTIVE["countdownEndsAt"] is None


def test_operation_stages_translate_without_touching_server_data(lifecycle):
    raw = {
        "active": {"phase": "Загрузка сервера", "stages": ["Остановка", "Загрузка сервера"]},
        "names": ["Остановка"],
    }
    token = i18n.LANGUAGE.set("en")
    try:
        translated = i18n.present(raw)
    finally:
        i18n.LANGUAGE.reset(token)
    assert translated["active"]["stages"] == [
        i18n.translate("Остановка", locale="en"),
        "Loading server",
    ]
    assert translated["names"] == ["Остановка"]
    assert raw["active"]["stages"] == ["Остановка", "Загрузка сервера"]


def test_update_recovery_diagnostics_translate_as_a_complete_message(lifecycle):
    message = "docker compose up не удался: engine error. Сервер снова готов к работе, но обновление не подтверждено"
    assert (
        i18n.translate(message, locale="en")
        == "docker compose up failed: engine error. The server is ready again, but the update is unconfirmed"
    )


@pytest.mark.parametrize("operation", ["check-update", "check-dashboard-update"])
@pytest.mark.parametrize("available", [True, False, None])
def test_image_checks_hold_shared_lock_and_report_unknown_as_failure(
    lifecycle, monkeypatch, operation, available
):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def check(**kwargs):
        entered.set()
        assert release.wait(5)
        return {"available": available, "note": "Cannot compare"}

    target = ops if operation == "check-update" else ops.dashboardupdate
    monkeypatch.setattr(target, "check_update" if target is ops else "check", check)
    real_thread = threading.Thread

    def thread(*, target, **kwargs):
        def run():
            try:
                target()
            finally:
                finished.set()

        return real_thread(target=run, **kwargs)

    monkeypatch.setattr(ops.threading, "Thread", thread)
    worker = ops._do_check_update if operation == "check-update" else ops._do_check_dashboard_update
    ops.start_op(operation, worker)
    try:
        assert entered.wait(5)
        assert ops.op_busy()
        assert ops.op_state()["active"]["stages"][-1] != "Подготовка…"
        with pytest.raises(ops.OpsError):
            ops.start_op("restart", Mock())
        assert not ops.op_state()["history"]
    finally:
        release.set()
        assert finished.wait(5)
    assert not ops.op_busy()
    result = ops.op_state()["history"][0]
    assert result["ok"] == (available is not None)
    assert result["message"]


def test_nested_backup_completion_is_not_an_operation_stage(lifecycle):
    ops._set_phase("Скачивание нового образа")
    ops._set_phase("Создание архива")
    ops._set_phase("Готово", "Archive created")
    ops._set_phase("Предупреждение игроков")
    assert ops._ACTIVE["stages"] == [
        "Скачивание нового образа",
        "Создание архива",
        "Предупреждение игроков",
    ]


@pytest.mark.parametrize("state", ["up-to-date", "needs-update", "inconclusive"])
def test_mod_check_reports_unknown_result_as_failure(lifecycle, monkeypatch, state):
    result = {
        "state": state,
        "items": [{"workshopId": "111"}],
        "error": "No server result" if state == "inconclusive" else None,
    }
    monkeypatch.setattr(ops, "check_mods_update", Mock(return_value=result))
    if state == "inconclusive":
        with pytest.raises(ops.OpsError, match="No server result"):
            ops._do_check_mods_update()
    else:
        assert ops._do_check_mods_update() == result
        assert ops._ACTIVE["phase"] == "Готово"
    ops.check_mods_update.assert_called_once_with(source="manual")
