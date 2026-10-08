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
