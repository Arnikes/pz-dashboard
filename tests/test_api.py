"""API compatibility checks with isolated HTTP and no real server operations."""

import http.client
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))

import actions  # noqa: E402
import app  # noqa: E402
import ops  # noqa: E402
import payloads  # noqa: E402
import rcon  # noqa: E402


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(app.Handler, "log_message", lambda *args: None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, path, data=None):
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        try:
            connection.request(method, path, json.dumps(data) if data is not None else None)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    try:
        yield request
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def operation_env(monkeypatch):
    monkeypatch.setattr(
        ops, "get_settings", lambda: {"autoUpdate": {"warnSeconds": 120, "intervalHours": 6}}
    )
    monkeypatch.setattr(ops, "op_busy", lambda: False)
    start = Mock()
    defer = Mock()
    monkeypatch.setattr(ops, "start_op", start)
    monkeypatch.setattr(ops, "defer_next_check", defer)
    return start, defer


@pytest.mark.parametrize(
    "action,target,extra,args,kwargs",
    [
        ("start", "_do_start", {}, (), {}),
        ("stop", "_do_stop", {}, (120,), {}),
        ("restart", "_do_restart", {"warnSeconds": 30}, (30,), {}),
        ("apply-update", "_do_apply_update", {}, (120, "Обновление сервера"), {}),
        ("check-mods-update", "check_mods_update", {}, (), {"source": "manual"}),
        ("apply-mods-update", "_do_apply_mods_update", {}, (120,), {}),
        ("backup", "run_backup_job", {}, ("manual", False), {}),
        ("backup", "run_backup_job", {"stopServer": True}, ("manual", True), {}),
        ("verify-backup", "verify_backup", {"name": "world.tar.gz"}, ("world.tar.gz",), {}),
        ("restore", "_do_restore", {"name": "world.tar.gz"}, ("world.tar.gz",), {}),
    ],
)
def test_action_http_dispatch(api, monkeypatch, operation_env, action, target, extra, args, kwargs):
    worker = Mock()
    monkeypatch.setattr(ops, target, worker)
    start, defer = operation_env
    assert api("POST", "/api/action", {"op": action, **extra}) == (
        200,
        {"ok": True, "started": action},
    )
    worker.assert_not_called()
    start.assert_called_once()
    name, callback = start.call_args.args
    assert name == action
    callback()
    worker.assert_called_once_with(*args, **kwargs)
    if action == "apply-update":
        defer.assert_called_once_with(6)
    else:
        defer.assert_not_called()


@pytest.mark.parametrize("value,expected", [(-10, 0), (9000, 3600), ("42", 42), (None, 120)])
def test_warning_normalization(monkeypatch, operation_env, value, expected):
    worker = Mock()
    monkeypatch.setattr(ops, "_do_stop", worker)
    actions.dispatch({"op": "stop", "warnSeconds": value})
    operation_env[0].call_args.args[1]()
    worker.assert_called_once_with(expected)


def test_synchronous_update_check(api, monkeypatch, operation_env):
    check = Mock(return_value={"available": True})
    monkeypatch.setattr(ops, "check_update", check)
    assert api("POST", "/api/action", {"op": "check-update"}) == (
        200,
        {"ok": True, "check": {"available": True}},
    )
    check.assert_called_once_with(force_event=True)
    operation_env[0].assert_not_called()


def test_action_rejections(api, monkeypatch, operation_env):
    start, defer = operation_env
    monkeypatch.setattr(ops, "op_busy", lambda: True)
    assert api("POST", "/api/action", {"op": "unknown"}) == (
        400,
        {"ok": False, "error": "Неизвестная операция"},
    )
    for action in ("start", "apply-update", "check-update"):
        assert api("POST", "/api/action", {"op": action}) == (
            409,
            {"ok": False, "error": "Уже выполняется другая операция"},
        )
    start.assert_not_called()
    defer.assert_not_called()
    monkeypatch.setattr(ops, "op_busy", lambda: False)
    start.side_effect = ops.OpsError("занято")
    assert api("POST", "/api/action", {"op": "start"}) == (
        409,
        {"ok": False, "error": "занято"},
    )


def test_polling_and_streaming_payloads(api, monkeypatch):
    providers = {
        "overview": ("overview", {"running": True}, {"ok": True, "running": True}),
        "players": ("fetch_players", {"players": ["Alice"]}, {"ok": True, "players": ["Alice"]}),
        "stats": ("fetch_stats", {"error": "offline"}, {"ok": False, "error": "offline"}),
        "ops": ("op_state", {"op": None}, {"ok": True, "op": None}),
        "stats-history": ("get_stats_history", [{"cpu": 1}], {"ok": True, "points": [{"cpu": 1}]}),
        "players-history": ("get_players_history", [], {"ok": True, "points": []}),
    }
    monkeypatch.setattr(ops, "docker_ok_cached", lambda: False)
    for channel, (function, value, expected) in providers.items():
        monkeypatch.setattr(ops, function, Mock(return_value=value))
        path = next(path for path, name in payloads.GET_CHANNELS.items() if name == channel)
        assert api("GET", path) == (200, expected)
        assert payloads.stream_payload(channel) == expected


def test_parameterized_routes(api, monkeypatch):
    events = Mock(return_value=[])
    mods = Mock(return_value={"ok": True, "settings": {}})
    monkeypatch.setattr(ops, "get_events", events)
    monkeypatch.setattr(ops, "mods_config_state", mods)
    monkeypatch.setattr(ops, "list_mods", lambda filename: {"ok": True, "items": []})
    for query, limit in (("", 100), ("?limit=0", 1), ("?limit=999", 200), ("?limit=x", 100)):
        assert api("GET", "/api/events" + query) == (200, {"ok": True, "items": []})
        events.assert_called_with(limit)
    assert api("GET", "/api/mods?file=world.ini") == (200, {"ok": True, "settings": {}})
    mods.assert_called_once_with("world.ini")
    assert payloads.stream_payload("mods") == {"ok": True, "items": []}
    assert payloads.stream_payload("unknown")["ok"] is False


@pytest.mark.parametrize("docker,state", [(False, None), (True, {"running": True})])
def test_players_rcon_failure(monkeypatch, docker, state):
    monkeypatch.setattr(ops, "docker_ok_cached", lambda: docker)
    container = Mock(return_value=state)
    monkeypatch.setattr(ops, "container_state", container)
    monkeypatch.setattr(ops, "fetch_players", Mock(side_effect=rcon.RCONError("offline")))
    assert payloads.players_payload() == {"ok": False, "error": "offline"}
    assert container.call_count == int(docker)
