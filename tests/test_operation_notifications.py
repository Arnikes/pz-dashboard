"""Completed runs need identities even when their timestamp and outcome match."""

import threading
from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest

import ops


@pytest.mark.parametrize("failure", [None, ops.OpsErrorReported, ops.OpsError, RuntimeError])
def test_same_second_results_have_distinct_stable_ids(monkeypatch, failure):
    monkeypatch.setattr(ops, "_ACTIVE", dict(ops._ACTIVE, op=None))
    monkeypatch.setattr(ops, "_OP_HISTORY", deque(maxlen=10))
    monkeypatch.setattr(ops, "_OP_CANCEL", threading.Event())
    monkeypatch.setattr(ops, "now_iso", lambda: "2026-10-08T12:00:00+00:00")
    monkeypatch.setattr(ops, "player_notification_language", lambda: "en")
    monkeypatch.setattr(ops, "log_event", Mock())
    monkeypatch.setattr(
        ops.threading, "Thread", lambda target, **kwargs: SimpleNamespace(start=target)
    )

    def run():
        if failure:
            raise failure("Disk unavailable")

    ops.start_op("backup", run)
    first = ops.op_state()["history"][0]
    ops.start_op("backup", run)
    second = ops.op_state()["history"][0]
    assert first["finishedAt"] == second["finishedAt"]
    assert first["message"] == second["message"]
    assert first["id"] != second["id"]
    assert UUID(first["id"]).version == UUID(second["id"]).version == 4
    assert ops.op_state()["history"] == [second, first]
    assert ops.op_state()["history"] == [second, first]
    assert ops.op_state()["active"] is None


@pytest.mark.parametrize("verdict", ["restarted", "stopped"])
def test_guarded_mod_restart_records_cancellation_and_actual_reason(monkeypatch, verdict):
    monkeypatch.setattr(ops, "_ACTIVE", dict(ops._ACTIVE, op=None))
    monkeypatch.setattr(ops, "_OP_HISTORY", deque(maxlen=10))
    monkeypatch.setattr(ops, "_OP_CANCEL", threading.Event())
    monkeypatch.setattr(ops.dashboardupdate, "operation", lambda: None)
    monkeypatch.setattr(ops, "log_event", Mock())
    monkeypatch.setattr(ops, "is_running", lambda: True)
    monkeypatch.setattr(ops, "container_state", lambda: {"startedAt": "before"})
    monkeypatch.setattr(ops, "_restarted_since", lambda started: verdict)
    ready = Mock()
    stop = Mock()
    monkeypatch.setattr(ops, "_require_ready", ready)
    monkeypatch.setattr(ops, "graceful_stop", stop)
    monkeypatch.setattr(
        ops.threading, "Thread", lambda target, **kwargs: SimpleNamespace(start=target)
    )
    ops.start_op("mods-restart", lambda: ops._do_restart(0, guard_restarted=True))
    result = ops.op_state()["history"][0]
    assert result["cancelled"] and result["ok"]
    assert ("перезапущен" if verdict == "restarted" else "остановлен") in result["message"]
    assert ready.call_count == (1 if verdict == "restarted" else 0)
    stop.assert_not_called()
