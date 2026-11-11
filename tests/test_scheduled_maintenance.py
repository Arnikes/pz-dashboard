"""Periodic chat and restarts use persisted schedules and the operation gate."""

import json
import time
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

import config
import configeditor
import ops
import settingsmodel


NOW = 1_790_000_000
HOUR = 3600
CONTAINER_ID = "a" * 64


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setitem(config.CFG, "settings_file", str(path))
    monkeypatch.setitem(config.CFG, "rcon_password", "test-rcon")
    monkeypatch.setattr(ops, "_SETTINGS", deepcopy(settingsmodel.DEFAULTS))
    monkeypatch.setattr(ops, "_SETTINGS_VERSION", {"epoch": "test", "revision": 0})
    monkeypatch.setattr(ops.dashboardupdate, "operation", lambda: None)
    monkeypatch.setattr(ops, "log_event", Mock())
    clock = Mock(wraps=time)
    clock.time.return_value = NOW
    monkeypatch.setattr(ops, "time", clock)
    return path


def enable_chat():
    assert (
        ops.patch_settings(
            {
                "chatAnnouncements": {
                    "enabled": True,
                    "message": 'Привет, "survivors"!',
                    "intervalMinutes": 1,
                }
            }
        )
        is None
    )


def enable_restart():
    assert (
        ops.patch_settings(
            {
                "scheduledRestart": {"enabled": True, "intervalHours": 1},
                "autoUpdate": {"warnSeconds": 30},
            }
        )
        is None
    )


def started_at(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def container_state(timestamp, *, running=True, container_id=CONTAINER_ID):
    return {
        "running": running,
        "status": "running" if running else "exited",
        "startedAt": started_at(timestamp),
        "containerId": container_id,
    }


def test_legacy_settings_keep_both_schedules_disabled(settings_file):
    settings_file.write_text('{"autoBackup": {"enabled": true}}', encoding="utf-8")
    ops._load_settings()
    saved = ops.get_settings()

    assert saved["autoBackup"]["enabled"]
    assert saved["chatAnnouncements"] == {"enabled": False, "message": "", "intervalMinutes": 30}
    assert saved["scheduledRestart"] == {"enabled": False, "intervalHours": 24}
    assert saved["nextChatAnnouncement"] is None
    assert saved["nextScheduledRestart"] is None


@pytest.mark.parametrize(
    "section, values",
    [
        ("chatAnnouncements", {"enabled": "true"}),
        ("chatAnnouncements", {"enabled": True, "message": " \u00a0 "}),
        ("chatAnnouncements", {"message": "multiline\nmessage"}),
        ("chatAnnouncements", {"message": "embedded\x00nul"}),
        ("chatAnnouncements", {"message": "embedded\ud800surrogate"}),
        ("chatAnnouncements", {"message": "a" * 201}),
        ("chatAnnouncements", {"intervalMinutes": True}),
        ("chatAnnouncements", {"intervalMinutes": 1.5}),
        ("chatAnnouncements", {"intervalMinutes": 1441}),
        ("scheduledRestart", {"enabled": 1}),
        ("scheduledRestart", {"intervalHours": "24"}),
        ("scheduledRestart", {"intervalHours": 0}),
        ("scheduledRestart", {"intervalHours": 169}),
    ],
)
def test_invalid_schedule_patch_leaves_persisted_settings_untouched(settings_file, section, values):
    assert ops.patch_settings({"autoBackup": {"enabled": True}}) is None
    before = ops.get_settings()
    content = settings_file.read_bytes()

    assert ops.patch_settings({section: values})
    assert ops.get_settings() == before
    assert settings_file.read_bytes() == content


def test_malformed_loaded_chat_message_cannot_be_sent(settings_file):
    settings_file.write_text(
        json.dumps(
            {
                "chatAnnouncements": {"enabled": True, "message": "unsafe\nmessage"},
                "nextChatAnnouncement": NOW,
                "nextScheduledRestart": "invalid",
            }
        ),
        encoding="utf-8",
    )
    ops._load_settings()
    saved = ops.get_settings()

    assert saved["chatAnnouncements"]["enabled"] is False
    assert saved["nextChatAnnouncement"] is None
    assert saved["nextScheduledRestart"] is None


def test_unknown_schedule_fields_do_not_poison_settings_serialization(settings_file, monkeypatch):
    patch = {
        "chatAnnouncements": {"message": "Keep this", "future": "\ud800"},
        "scheduledRestart": {"intervalHours": 6, "future": "\ud800"},
    }
    assert ops.patch_settings(patch) is None
    expected = ops.get_settings()
    persisted = json.loads(settings_file.read_text(encoding="utf-8"))
    for section, key in (("chatAnnouncements", "message"), ("scheduledRestart", "intervalHours")):
        assert persisted[section][key] == patch[section][key]
        assert "future" not in persisted[section]
        persisted[section]["future"] = "\ud800"

    settings_file.write_text(json.dumps(persisted), encoding="utf-8")
    monkeypatch.setattr(ops, "_SETTINGS", deepcopy(settingsmodel.DEFAULTS))
    ops._load_settings()

    for section in patch:
        assert ops.get_settings()[section] == expected[section]


def test_schedule_edits_persist_and_only_cadence_changes_reset_due_time(settings_file, monkeypatch):
    enable_chat()
    enable_restart()
    ops.time.time.return_value = NOW + 15

    assert ops.patch_settings({"chatAnnouncements": {"message": "Changed text"}}) is None
    assert ops.patch_settings({"scheduledRestart": {"enabled": True, "intervalHours": 1}}) is None
    settings = ops.get_settings()
    assert settings["nextChatAnnouncement"] == NOW + 60
    assert settings["nextScheduledRestart"] == NOW + HOUR

    assert ops.patch_settings({"chatAnnouncements": {"intervalMinutes": 10}}) is None
    assert ops.patch_settings({"scheduledRestart": {"intervalHours": 2}}) is None
    monkeypatch.setattr(ops, "_SETTINGS", deepcopy(settingsmodel.DEFAULTS))
    ops._load_settings()
    reloaded = ops.get_settings()
    assert reloaded["chatAnnouncements"]["message"] == "Changed text"
    assert reloaded["nextChatAnnouncement"] == NOW + 15 + 600
    assert reloaded["nextScheduledRestart"] == NOW + 15 + 2 * HOUR

    assert (
        ops.patch_settings(
            {"chatAnnouncements": {"enabled": False}, "scheduledRestart": {"enabled": False}}
        )
        is None
    )
    saved = json.loads(settings_file.read_text(encoding="utf-8"))
    assert saved["nextChatAnnouncement"] is None
    assert saved["nextScheduledRestart"] is None
    assert saved["chatAnnouncements"]["message"] == "Changed text"


@pytest.mark.parametrize("timed_out", [False, True])
def test_overdue_chat_sends_once_in_remote_mode(settings_file, monkeypatch, timed_out):
    enable_chat()
    monkeypatch.setattr(ops, "docker_ok_cached", lambda: False)
    inspect = Mock()
    monkeypatch.setattr(ops, "container_state", inspect)
    send = Mock(side_effect=ops.rconlib.RCONError("response timed out") if timed_out else None)
    monkeypatch.setattr(ops, "rcon", send)
    overdue = NOW + HOUR
    snapshot = ops.get_settings()

    ops._chat_announcement_tick(snapshot, NOW + 59)
    send.assert_not_called()
    ops._chat_announcement_tick(snapshot, overdue)
    ops._chat_announcement_tick(snapshot, overdue + 20)

    inspect.assert_not_called()
    send.assert_called_once_with("servermsg \"Привет, 'survivors'!\"", quiet=True)
    saved = json.loads(settings_file.read_text(encoding="utf-8"))
    assert saved["chatAnnouncements"]["message"] == 'Привет, "survivors"!'
    assert saved["nextChatAnnouncement"] == overdue + 60


def test_scheduler_prioritizes_a_due_restart_before_due_chat(settings_file, monkeypatch):
    enable_chat()
    enable_restart()
    ops.time.time.return_value = NOW + HOUR
    ops.time.sleep.side_effect = StopIteration
    monkeypatch.setattr(configeditor, "auto_verify_running", Mock())
    monkeypatch.setattr(ops, "_post_restart_rescan_tick", Mock())
    monkeypatch.setattr(ops, "docker_ok_cached", lambda: True)
    monkeypatch.setattr(ops, "container_state", lambda: container_state(NOW))
    busy = Mock(return_value=False)
    monkeypatch.setattr(ops, "op_busy", busy)

    def admit_restart(_operation, _worker):
        busy.return_value = True

    launch = Mock(side_effect=admit_restart)
    send = Mock()
    monkeypatch.setattr(ops, "start_op", launch)
    monkeypatch.setattr(ops, "rcon", send)

    with pytest.raises(StopIteration):
        ops._scheduler_loop()

    launch.assert_called_once()
    send.assert_not_called()
    assert ops.get_settings()["nextChatAnnouncement"] == NOW + 60


def test_disabled_chat_is_not_dispatched_from_a_stale_snapshot(settings_file, monkeypatch):
    enable_chat()
    stale = ops.get_settings()
    assert ops.patch_settings({"chatAnnouncements": {"enabled": False}}) is None
    send = Mock()
    monkeypatch.setattr(ops, "rcon", send)

    ops._chat_announcement_tick(stale, NOW + HOUR)

    send.assert_not_called()
    assert ops.get_settings()["nextChatAnnouncement"] is None


def test_due_schedules_wait_for_active_operations(settings_file, monkeypatch):
    enable_chat()
    enable_restart()
    monkeypatch.setattr(ops, "op_busy", lambda: True)
    monkeypatch.setattr(ops, "docker_ok_cached", lambda: True)
    monkeypatch.setattr(ops, "container_state", lambda: container_state(NOW))
    send = Mock()
    launch = Mock()
    monkeypatch.setattr(ops, "rcon", send)
    monkeypatch.setattr(ops, "start_op", launch)
    snapshot = ops.get_settings()

    ops._scheduled_restart_tick(snapshot, NOW + HOUR)
    ops._chat_announcement_tick(snapshot, NOW + HOUR)

    send.assert_not_called()
    launch.assert_not_called()
    assert ops.get_settings() == snapshot


@pytest.mark.parametrize("docker, running", [(False, True), (True, False)])
def test_scheduled_restart_cannot_launch_without_a_running_local_container(
    settings_file, monkeypatch, docker, running
):
    enable_restart()
    monkeypatch.setattr(ops, "docker_ok_cached", lambda: docker)
    monkeypatch.setattr(ops, "container_state", lambda: container_state(NOW, running=running))
    launch = Mock()
    monkeypatch.setattr(ops, "start_op", launch)

    ops._scheduled_restart_tick(ops.get_settings(), NOW + HOUR)

    launch.assert_not_called()
    assert ops.get_settings()["nextScheduledRestart"] == NOW + 2 * HOUR


def test_due_chat_skips_a_stopped_local_container(settings_file, monkeypatch):
    enable_chat()
    monkeypatch.setattr(ops, "docker_ok_cached", lambda: True)
    monkeypatch.setattr(ops, "container_state", lambda: {"running": False})
    send = Mock()
    monkeypatch.setattr(ops, "rcon", send)

    ops._chat_announcement_tick(ops.get_settings(), NOW + 60)

    send.assert_not_called()
    assert ops.get_settings()["nextChatAnnouncement"] == NOW + 120


def test_restart_due_time_respects_recent_container_start(settings_file, monkeypatch):
    enable_restart()
    recent_start = NOW + HOUR - 60
    monkeypatch.setattr(ops, "docker_ok_cached", lambda: True)
    monkeypatch.setattr(ops, "container_state", lambda: container_state(recent_start))
    launch = Mock()
    restart = Mock()
    monkeypatch.setattr(ops, "start_op", launch)
    monkeypatch.setattr(ops, "_do_restart", restart)

    ops._scheduled_restart_tick(ops.get_settings(), NOW + HOUR)
    launch.assert_not_called()
    snapshot = ops.get_settings()
    assert snapshot["nextScheduledRestart"] == recent_start + HOUR

    ops._scheduled_restart_tick(snapshot, recent_start + HOUR)
    ops._scheduled_restart_tick(snapshot, recent_start + HOUR + 20)
    launch.assert_called_once()
    action, run = launch.call_args.args
    assert action == "restart"
    run()
    assert restart.call_args.args[0] == 30
    expected = restart.call_args.kwargs["expected_run"]
    assert expected.started_at == started_at(recent_start)
    assert expected.container_id == CONTAINER_ID
    assert ops.get_settings()["nextScheduledRestart"] == recent_start + 2 * HOUR


def test_rejected_restart_admission_leaves_persisted_due_time(settings_file, monkeypatch):
    enable_restart()
    monkeypatch.setattr(ops, "docker_ok_cached", lambda: True)
    monkeypatch.setattr(ops, "container_state", lambda: container_state(NOW))
    launch = Mock(side_effect=[ops.OpsBusyError("busy"), "operation-id"])
    monkeypatch.setattr(ops, "start_op", launch)
    due = NOW + HOUR

    ops._scheduled_restart_tick(ops.get_settings(), due)
    assert ops.get_settings()["nextScheduledRestart"] == due
    assert json.loads(settings_file.read_text(encoding="utf-8"))["nextScheduledRestart"] == due

    ops._scheduled_restart_tick(ops.get_settings(), due + 20)
    assert launch.call_count == 2
    assert ops.get_settings()["nextScheduledRestart"] == due + 20 + HOUR


@pytest.mark.parametrize("restarted_during_warning", [False, True])
def test_guarded_restart_does_not_stop_a_new_container(
    settings_file, monkeypatch, restarted_during_warning
):
    prior = started_at(NOW - HOUR)
    replacement = started_at(NOW)
    container = container_state(NOW - HOUR if restarted_during_warning else NOW)
    monkeypatch.setattr(ops, "container_state", lambda: container)
    monkeypatch.setattr(ops, "_set_phase", Mock())
    monkeypatch.setattr(ops, "_require_ready", Mock())
    stop = Mock()
    monkeypatch.setattr(ops, "graceful_stop", stop)

    def warn(_seconds, _reason, *, abort_check):
        container["startedAt"] = replacement
        assert abort_check()

    warning = Mock(side_effect=warn)
    monkeypatch.setattr(ops, "rcon_warn_broadcast", warning)

    result = ops._do_restart(30, expected_run=ops._ContainerRun(CONTAINER_ID, prior))

    assert result == "aborted"
    assert warning.call_count == int(restarted_during_warning)
    stop.assert_not_called()


@pytest.mark.parametrize("replaced_before_quit", [False, True])
def test_guarded_stop_restores_policy_without_stopping_a_replacement(
    settings_file, monkeypatch, replaced_before_quit
):
    prior = started_at(NOW - HOUR)
    replacement = started_at(NOW)
    container = container_state(NOW - HOUR)
    monkeypatch.setattr(ops, "container_state", lambda: container)
    monkeypatch.setattr(ops.dockerlib, "get_restart_policy", lambda _container: "always")

    def set_policy(_container, policy):
        if replaced_before_quit and policy == "no":
            container["startedAt"] = replacement
        return True

    def wait_for_stop(_timeout, *, started_at):
        assert started_at == prior
        container["startedAt"] = replacement
        return "running"

    policy = Mock(side_effect=set_policy)
    monkeypatch.setattr(ops.dockerlib, "set_restart_policy", policy)
    monkeypatch.setattr(ops, "wait_until_stopped", wait_for_stop)
    force_stop = Mock()
    send = Mock()
    monkeypatch.setattr(ops.dockerlib, "container_stop", force_stop)
    monkeypatch.setattr(ops, "rcon", send)

    assert ops.graceful_stop(expected_run=ops._ContainerRun(CONTAINER_ID, prior)) == "aborted"

    assert send.call_count == int(not replaced_before_quit)
    force_stop.assert_not_called()
    assert [entry.args for entry in policy.call_args_list] == [
        (CONTAINER_ID, "no"),
        (CONTAINER_ID, "always"),
    ]


@pytest.mark.parametrize(
    "exited_id, exited_started_at, should_launch",
    [
        (CONTAINER_ID, NOW - HOUR, True),
        (CONTAINER_ID, NOW - 30, False),
        ("b" * 64, NOW - HOUR, False),
    ],
)
def test_scheduled_restart_launches_only_the_confirmed_stopped_run(
    settings_file, monkeypatch, exited_id, exited_started_at, should_launch
):
    container = container_state(NOW - HOUR)
    expected = ops._ContainerRun(CONTAINER_ID, container["startedAt"])
    monkeypatch.setattr(ops, "container_state", lambda: container)
    monkeypatch.setattr(ops, "_set_phase", Mock())
    monkeypatch.setattr(ops.dockerlib, "get_restart_policy", lambda _container: "no")

    def quit_game(command, *, quiet):
        assert command == "quit" and quiet
        container.update(container_state(exited_started_at, running=False, container_id=exited_id))

    monkeypatch.setattr(ops, "rcon", quit_game)
    launch = Mock(return_value=(0, "", ""))
    monkeypatch.setattr(ops, "_start_container", launch)
    monkeypatch.setattr(ops, "wait_until_running", Mock(return_value=True))
    monkeypatch.setattr(ops, "_require_ready", Mock())

    result = ops._do_restart(0, expected_run=expected)

    if should_launch:
        assert result is None
        launch.assert_called_once_with(CONTAINER_ID)
    else:
        assert result == "aborted"
        launch.assert_not_called()
