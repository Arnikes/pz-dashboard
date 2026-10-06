"""Controlled downtime must not produce outage alerts or restart loops."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

import dockerlib
import ops
import rcon
import settingsmodel


@pytest.fixture
def watchdog(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ops.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ops.time, "time", lambda: clock[0] + 10000)
    monkeypatch.setattr(ops, "_SETTINGS", deepcopy(settingsmodel.DEFAULTS))
    monkeypatch.setattr(ops, "op_busy", lambda: False)
    monkeypatch.setattr(ops, "is_running", lambda: True)
    monkeypatch.setattr(ops, "container_state", lambda: {"running": True})
    monkeypatch.setattr(ops, "docker_ok_cached", lambda ttl=60: True)
    monkeypatch.setattr(ops, "_set_phase", Mock())
    monkeypatch.setattr(ops, "log_event", Mock())
    monkeypatch.setattr(ops, "start_op", Mock())
    monkeypatch.setattr(ops, "_RCON_CACHE", {"state": "ok", "at": None, "error": None})
    monkeypatch.setattr(ops, "_RCON_LOG", {"at": 0.0})
    monkeypatch.setattr(ops.rconlib, "run_command", Mock(side_effect=rcon.RCONError("timeout")))
    return clock


def test_grace_discards_old_failures_and_restarts_threshold_after_expiry(watchdog):
    wd = {"thresholdMin": 1, "autoRestart": True}
    ops._WD.update(consecutiveFailures=9, alerted=True, lastError="old failure")
    ops._begin_watchdog_grace()
    assert ops.watchdog_state()["graceRemainingSec"] == 300
    assert ops._WD["consecutiveFailures"] == 0
    assert not ops._WD["alerted"] and ops._WD["lastError"] is None

    for offset in (0, 30, 299.5):
        watchdog[0] = 1000 + offset
        ops._watchdog_probe(wd)
        assert ops._WD["lastResult"] == "grace"
    ops.rconlib.run_command.assert_not_called()
    ops.log_event.assert_not_called()
    ops.start_op.assert_not_called()

    watchdog[0] = 1300
    assert ops.watchdog_state()["graceRemainingSec"] == 0
    assert ops.watchdog_state()["graceUntil"] is None
    ops._watchdog_probe(wd)
    assert ops._WD["consecutiveFailures"] == 1
    ops.log_event.assert_not_called()
    watchdog[0] += 30
    ops._watchdog_probe(wd)
    assert ops._WD["alerted"]
    ops.start_op.assert_called_once()
    assert ops.start_op.call_args.args[0] == "restart"


@pytest.mark.parametrize("minutes", [0, 1, 12, 60])
def test_configured_grace_and_repeated_launch(watchdog, minutes):
    ops._SETTINGS["watchdog"]["gracePeriodMin"] = minutes
    ops._begin_watchdog_grace()
    assert ops.watchdog_state()["graceRemainingSec"] == minutes * 60
    assert ops._watchdog_in_grace() is bool(minutes)
    watchdog[0] += 20
    ops._begin_watchdog_grace()
    assert ops.watchdog_state()["graceRemainingSec"] == minutes * 60
    # Wall clock corrections do not change the suppression deadline.
    ops.time.time = lambda: 1
    assert ops.watchdog_state()["graceRemainingSec"] == minutes * 60


@pytest.mark.parametrize("busy", [False, True])
def test_regular_rcon_polling_is_muted_during_grace_or_operation(watchdog, monkeypatch, busy):
    if busy:
        monkeypatch.setattr(ops, "op_busy", lambda: True)
    else:
        ops._begin_watchdog_grace()
    for _ in range(3):
        with pytest.raises(rcon.RCONError):
            ops.rcon("players")
    assert ops._RCON_CACHE["state"] == "error"
    ops.log_event.assert_not_called()
    assert ops._RCON_LOG["at"] == 0

    watchdog[0] += 300
    monkeypatch.setattr(ops, "op_busy", lambda: False)
    with pytest.raises(rcon.RCONError):
        ops.rcon("players")
    ops.log_event.assert_called_once_with("rcon-error", "RCON: timeout")


@pytest.mark.parametrize("minutes", [0, 5])
def test_inflight_probe_does_not_publish_failure_after_restart_begins(
    watchdog, monkeypatch, minutes
):
    ops._SETTINGS["watchdog"]["gracePeriodMin"] = minutes
    ops._WD["consecutiveFailures"] = 1

    def restart_during_probe(*args):
        ops._begin_watchdog_grace()
        raise rcon.RCONError("old connection closed")

    monkeypatch.setattr(ops.rconlib, "run_command", restart_during_probe)
    ops._watchdog_probe({"thresholdMin": 1, "autoRestart": True})
    assert ops._WD["consecutiveFailures"] == 0
    ops.log_event.assert_not_called()
    ops.start_op.assert_not_called()


@pytest.mark.parametrize("resurrected", [False, True])
def test_manual_restart_arms_grace_before_stop_and_refreshes_at_launch(
    watchdog, monkeypatch, resurrected
):
    def stop(hook=None):
        assert ops._watchdog_in_grace()
        watchdog[0] += 400  # A slow stop must not consume the launch grace period.
        return "resurrected" if resurrected else "stopped"

    def start(name):
        assert ops.watchdog_state()["graceRemainingSec"] == 300
        return 0, "", ""

    monkeypatch.setattr(ops, "graceful_stop", stop)
    monkeypatch.setattr(dockerlib, "container_start", Mock(side_effect=start))
    monkeypatch.setattr(ops, "wait_until_running", lambda timeout: True)
    ops._do_restart(0)
    assert ops.watchdog_state()["graceRemainingSec"] == 300
    assert dockerlib.container_start.call_count == (0 if resurrected else 1)


def test_update_arms_grace_for_compose_launch(watchdog, monkeypatch):
    ops._SETTINGS["autoUpdate"]["backupBeforeUpdate"] = False
    monkeypatch.setattr(ops, "_effective_image", lambda: "image")
    monkeypatch.setattr(ops, "_installed_digest", lambda image: "old")
    monkeypatch.setattr(dockerlib, "image_pull", lambda image: (0, "", ""))
    monkeypatch.setattr(dockerlib, "image_digests", lambda image: "new")
    monkeypatch.setattr(dockerlib, "compose_version", lambda: True)
    monkeypatch.setattr(ops, "wait_until_running", lambda timeout: True)
    monkeypatch.setattr(ops, "check_update", Mock())

    def stop(hook=None):
        assert ops._watchdog_in_grace()
        watchdog[0] += 400

    def compose(cfg):
        assert ops.watchdog_state()["graceRemainingSec"] == 300
        return 0, "", ""

    monkeypatch.setattr(ops, "graceful_stop", stop)
    monkeypatch.setattr(dockerlib, "compose_up", compose)
    ops._do_apply_update(0)
    assert ops.watchdog_state()["graceRemainingSec"] == 300


def test_update_without_restart_does_not_arm_grace(watchdog, monkeypatch):
    monkeypatch.setattr(ops, "_effective_image", lambda: "image")
    monkeypatch.setattr(ops, "_installed_digest", lambda image: "same")
    monkeypatch.setattr(dockerlib, "image_pull", lambda image: (0, "", ""))
    monkeypatch.setattr(dockerlib, "image_digests", lambda image: "same")
    monkeypatch.setattr(ops, "check_update", Mock())
    ops._do_apply_update(0)
    assert not ops._watchdog_in_grace()


def test_cancelled_restart_does_not_arm_grace(watchdog, monkeypatch):
    monkeypatch.setattr(ops, "_OP_CANCEL", Mock(is_set=Mock(return_value=True)))
    monkeypatch.setattr(ops, "_ACTIVE", {"cancellable": True})
    assert ops._do_restart(0, cancellable=True) == "cancelled"
    assert not ops._watchdog_in_grace()
