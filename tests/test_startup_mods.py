"""Console startup checks mod freshness only on an available game server."""

from itertools import count
from unittest.mock import Mock

import pytest

import app
import dockerlib
import ops
import rcon
from test_dashboard_update import startup as startup


@pytest.mark.parametrize("state", ["up-to-date", "needs-update", "inconclusive"])
def test_startup_populates_mod_status_without_scheduled_updates(startup, monkeypatch, state):
    monkeypatch.setattr(ops, "docker_ok_cached", lambda: True)
    monkeypatch.setattr(ops, "is_running", lambda: True)
    monkeypatch.setattr(ops, "op_busy", lambda: False)
    monkeypatch.setattr(ops, "_LAST_MODS_CHECK", dict.fromkeys(ops._LAST_MODS_CHECK))
    settings = Mock(return_value={"modsUpdate": {"enabled": False}})
    monkeypatch.setattr(ops, "get_settings", settings)
    monkeypatch.setattr(ops, "_mods_registry", lambda: ({}, {}))
    ticks = count(0, 10)
    monkeypatch.setattr(ops.time, "time", lambda: next(ticks))
    result_line = {
        "up-to-date": "CheckModsNeedUpdate: Mods updated",
        "needs-update": "CheckModsNeedUpdate: 123456789 needs update",
        "inconclusive": "CheckModsNeedUpdate: Checking...",
    }[state]
    logs = Mock(side_effect=[("old log", None)] + [("old log\n" + result_line, None)] * 10)
    monkeypatch.setattr(dockerlib, "container_logs", logs)
    restart = Mock()
    monkeypatch.setattr(ops, "start_op", restart)
    app.main()
    ops.rcon.reset_mock()
    startup["pz-mods-startup-check"]()
    result = ops.mods_check_state()
    assert result["state"] == state
    assert result["source"] == "startup" and result["at"]
    assert bool(result["error"]) is (state == "inconclusive")
    assert bool(result["items"]) is (state == "needs-update")
    assert ops.rcon.call_args_list == [
        (("players",), {"quiet": True}),
        (("checkModsNeedUpdate",), {"quiet": True}),
    ]
    settings.assert_not_called()
    restart.assert_not_called()


@pytest.mark.parametrize("container", [None, {"running": False}, {"running": True}])
def test_startup_skips_missing_stopped_or_busy_game(startup, monkeypatch, container):
    monkeypatch.setattr(ops, "container_state", lambda: container)
    monkeypatch.setattr(ops, "op_busy", lambda: bool(container and container["running"]))
    check = Mock()
    monkeypatch.setattr(ops, "check_mods_update", check)
    app.main()
    ops.rcon.reset_mock()
    startup["pz-mods-startup-check"]()
    check.assert_not_called()
    ops.rcon.assert_not_called()


def test_startup_skips_mod_check_without_docker(startup, monkeypatch):
    monkeypatch.setattr(dockerlib, "docker_version", lambda: False)
    app.main()
    assert "pz-mods-startup-check" not in startup


def test_startup_skips_game_that_is_still_loading(startup, monkeypatch):
    monkeypatch.setattr(ops, "is_running", lambda: True)
    monkeypatch.setattr(ops, "op_busy", lambda: False)
    check = Mock()
    monkeypatch.setattr(ops, "check_mods_update", check)
    app.main()
    ops.rcon.side_effect = rcon.RCONError("RCON not ready")
    ops.log_event.reset_mock()
    startup["pz-mods-startup-check"]()
    check.assert_not_called()
    ops.log_event.assert_not_called()


def test_startup_mod_failure_does_not_prevent_image_check(startup, monkeypatch):
    monkeypatch.setattr(ops, "is_running", lambda: True)
    monkeypatch.setattr(ops, "op_busy", lambda: False)
    monkeypatch.setattr(
        ops, "check_mods_update", Mock(side_effect=ops.OpsError("logs unavailable"))
    )
    app.main()
    startup["pz-mods-startup-check"]()
    ops.log_event.assert_called_with("error", "logs unavailable")
    startup["pz-startup-check"]()
    ops.check_update.assert_called_once_with(force_event=True)
