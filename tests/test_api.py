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
import configeditor  # noqa: E402
import ops  # noqa: E402
import payloads  # noqa: E402
import rcon  # noqa: E402
from test_configeditor import env as editor_env  # noqa: E402, F401


@pytest.fixture
def api(monkeypatch, authenticated_admin):
    monkeypatch.setattr(app.Handler, "log_message", lambda *args: None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    server.auth, default_headers = authenticated_admin
    server.daemon_threads = True
    # The default 0.5s polling interval delays every fixture's shutdown.
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()

    def request(method, path, data=None, *, raw=None, headers=None):
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        try:
            body = raw if raw is not None else json.dumps(data) if data is not None else None
            connection.request(method, path, body, {**default_headers, **(headers or {})})
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


def test_backup_journal_paginates_all_saved_entries(api, tmp_path, monkeypatch):
    monkeypatch.setitem(app.config.CFG, "dashboard_dir", str(tmp_path))
    path = tmp_path / "backups.jsonl"
    path.write_text(
        "\n".join(json.dumps({"name": f"backup-{i}"}) for i in range(61)) + "\ninvalid\n",
        encoding="utf-8",
    )
    status, first = api("GET", "/api/backups/journal")
    assert status == 200 and len(first["items"]) == 25 and first["hasMore"]
    assert first["items"][0]["name"] == "backup-60"
    status, last = api("GET", "/api/backups/journal?offset=50&limit=25")
    assert status == 200 and len(last["items"]) == 11 and not last["hasMore"]
    assert last["items"][-1]["name"] == "backup-0"
    assert api("GET", "/api/backups/journal?offset=100")[1]["items"] == []
    assert api("GET", "/api/backups/journal?offset=invalid")[0] == 400
    journal = payloads.backup_journal_payload()
    assert journal == first


def test_operation_log_range_is_bounded_and_passed_to_docker(api, monkeypatch):
    logs = Mock(return_value=("2026-10-01T12:00:30Z ERROR test\n", None))
    monkeypatch.setattr(app.dockerlib, "container_logs", logs)
    since, until = "2026-10-01T12:00:00Z", "2026-10-01T12:01:00Z"
    status, result = api("GET", f"/api/logs?tail=50000&since={since}&until={until}")
    assert status == 200 and result["ok"]
    assert result["since"] == since and result["until"] == until
    logs.assert_called_once_with(app.config.CFG["pz_container"], 10000, since=since, until=until)
    assert not result["truncated"]


@pytest.mark.parametrize(
    "query",
    [
        "since=--follow",
        "since=2026-10-01T12:00:00",
        "until=invalid",
        "since=2026-10-01T12:01:00Z&until=2026-10-01T12:00:00Z",
    ],
)
def test_invalid_operation_log_range_never_reaches_docker(api, monkeypatch, query):
    logs = Mock()
    monkeypatch.setattr(app.dockerlib, "container_logs", logs)
    assert api("GET", "/api/logs?" + query)[0] == 400
    logs.assert_not_called()


def test_workshop_resolve_http_exposes_single_child_collection(api, monkeypatch):
    def steam(method, ids):
        if method == "GetCollectionDetails":
            return {
                "collectiondetails": [
                    {
                        "publishedfileid": ids[0],
                        "result": 1 if ids[0] == "1" else 9,
                        **({"children": [{"publishedfileid": "2"}]} if ids[0] == "1" else {}),
                    }
                ]
            }
        return {
            "publishedfiledetails": [
                {
                    "publishedfileid": ids[0],
                    "result": 1,
                    "consumer_app_id": 108600,
                    "title": "Title " + ids[0],
                }
            ]
        }

    monkeypatch.setattr(configeditor.workshop, "steam_call", steam)
    status, result = api("POST", "/api/workshop-resolve", {"input": "1"})
    assert status == 200 and result["ok"]
    assert result["source"] == {"kind": "collection", "workshopId": "1", "title": "Title 1"}
    assert result["items"] == [{"workshopId": "2", "title": "Title 2"}]


def test_config_http_revisions_and_secrets(api, editor_env):  # noqa: F811
    status, profiles = api("GET", "/api/server-configs")
    assert status == 200 and profiles["activeFile"] == "world.ini"
    status, current = api("GET", "/api/config-draft?file=world.ini")
    assert status == 200 and "topsecret" not in json.dumps(current)
    body = {
        "file": "world.ini",
        "draftRevision": current["draftRevision"],
        "ini": {"PublicName": "HTTP draft"},
    }
    status, draft = api("POST", "/api/config-draft", body)
    assert status == 200 and draft["changed"]
    assert api("POST", "/api/config-draft", body)[0] == 409
    status, result = api("POST", "/api/config-validate", {"file": "world.ini"})
    assert status == 200 and result["valid"]
    assert "HTTP draft" in result["diff"]["ini"] and "topsecret" not in json.dumps(result)


def test_config_http_running_and_boolean_errors(api, editor_env, monkeypatch):  # noqa: F811
    current = configeditor.draft("world.ini")
    monkeypatch.setattr(ops, "is_running", lambda: True)
    assert (
        api(
            "POST",
            "/api/action",
            {
                "op": "apply-config",
                "file": "world.ini",
                "draftRevision": current["draftRevision"],
                "restart": False,
            },
        )[0]
        == 409
    )
    assert (
        api(
            "POST",
            "/api/config-draft",
            {"file": "world.ini", "overwrite": "false"},
        )[0]
        == 400
    )


def test_removed_mod_toggle_route_cannot_write_files(api, editor_env, monkeypatch):  # noqa: F811
    original = configeditor.read_profile("world.ini")
    start = Mock()
    monkeypatch.setattr(ops, "start_op", start)
    status, _ = api(
        "POST", "/api/mods-config", {"file": "world.ini", "workshopId": "111", "enable": False}
    )
    assert status == 404
    start.assert_not_called()
    assert configeditor.read_profile("world.ini") == original


def test_config_http_snapshot_refresh_and_overwrite(api, editor_env, monkeypatch):  # noqa: F811
    data, _ = editor_env
    _, original = api("GET", "/api/config-draft?file=world.ini")
    path = data / "Server/world.ini"
    path.write_bytes(path.read_bytes().replace(b"Unknown=preserve", b"Unknown=external"))
    status, current = api("GET", "/api/config-draft?file=world.ini&refresh=1")
    assert status == 200 and "Unknown=external" in current["texts"]["ini"]
    status, saved = api(
        "POST",
        "/api/config-draft",
        {
            "file": "world.ini",
            "draftRevision": original["draftRevision"],
            "overwrite": True,
            "texts": original["texts"],
            "ini": {"PublicName": "Page snapshot"},
        },
    )
    assert status == 200 and "Unknown=preserve" in saved["texts"]["ini"]
    path.write_bytes(path.read_bytes().replace(b"Unknown=external", b"Unknown=later-external"))
    request = {"file": "world.ini", "draftRevision": saved["draftRevision"], "overwrite": True}
    status, preview = api("POST", "/api/config-validate", request)
    assert status == 200 and preview["valid"] and preview["conflictDiff"]
    jobs = []
    monkeypatch.setattr(ops, "start_op", lambda name, job: jobs.append(job))
    status, _ = api("POST", "/api/action", {"op": "apply-config", "restart": False, **request})
    assert status == 200 and len(jobs) == 1
    jobs[0]()
    assert "Unknown=preserve" in path.read_text(encoding="utf-8")
    assert "Page snapshot" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("operation,prepare", [("apply-config", False), ("prepare-workshop", True)])
@pytest.mark.parametrize("backup", [None, False, True])
def test_config_http_action_binds_profile_and_operation(
    api,
    editor_env,  # noqa: F811
    monkeypatch,
    operation,
    prepare,
    backup,
):  # noqa: F811
    draft = configeditor.draft("world.ini")
    start, run = Mock(), Mock()
    monkeypatch.setattr(ops, "start_op", start)
    monkeypatch.setattr(configeditor, "run", run)
    body = {
        "op": operation,
        "file": "world.ini",
        "draftRevision": draft["draftRevision"],
        "restart": False,
    }
    if backup is not None:
        body["backupBeforeApply"] = backup
    assert api("POST", "/api/action", body) == (200, {"ok": True, "operation": operation})
    run.assert_not_called()
    start.call_args.args[1]()
    run.assert_called_once_with(body, prepare)


@pytest.mark.parametrize("operation", ["apply-config", "prepare-workshop"])
@pytest.mark.parametrize("backup", ["false", "true", 0, 1, None, [], {}])
def test_config_http_rejects_invalid_backup_choice(api, editor_env, monkeypatch, operation, backup):  # noqa: F811
    current = configeditor.draft("world.ini")
    start = Mock()
    monkeypatch.setattr(ops, "start_op", start)
    status, result = api(
        "POST",
        "/api/action",
        {
            "op": operation,
            "file": "world.ini",
            "draftRevision": current["draftRevision"],
            "backupBeforeApply": backup,
        },
    )
    assert status == 400 and "backupBeforeApply" in result["error"]
    start.assert_not_called()


@pytest.mark.parametrize(
    "action,target,extra,args,kwargs",
    [
        ("start", "_do_start", {}, (), {}),
        ("stop", "_do_stop", {}, (120,), {}),
        ("restart", "_do_restart", {"warnSeconds": 30}, (30,), {}),
        ("check-update", "_do_check_update", {}, (), {}),
        ("check-dashboard-update", "_do_check_dashboard_update", {}, (), {}),
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


def test_cancel_mods_update_allowed_while_busy(api, monkeypatch, operation_env):
    monkeypatch.setattr(ops, "op_busy", lambda: True)
    cancel = Mock()
    monkeypatch.setattr(ops, "cancel_mods_update", cancel)
    assert api("POST", "/api/action", {"op": "cancel-mods-update"}) == (
        200,
        {"ok": True, "cancelRequested": True},
    )
    cancel.assert_called_once_with()
    operation_env[0].assert_not_called()
    cancel.side_effect = ops.OpsError("Остановка уже началась")
    assert api("POST", "/api/action", {"op": "cancel-mods-update"}) == (
        409,
        {"ok": False, "error": "Остановка уже началась"},
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
    monkeypatch.setattr(configeditor, "mod_response", mods)
    for query, limit in (("", 100), ("?limit=0", 1), ("?limit=999", 200), ("?limit=x", 100)):
        assert api("GET", "/api/events" + query) == (200, {"ok": True, "items": []})
        events.assert_called_with(limit)
    assert api("GET", "/api/mods?file=world.ini") == (200, {"ok": True, "settings": {}})
    mods.assert_called_once_with("world.ini", draft_mode=False, refresh=False)
    assert payloads.stream_payload("unknown")["ok"] is False


@pytest.mark.parametrize("docker,state", [(False, None), (True, {"running": True})])
def test_players_rcon_failure(monkeypatch, docker, state):
    monkeypatch.setattr(ops, "docker_ok_cached", lambda: docker)
    container = Mock(return_value=state)
    monkeypatch.setattr(ops, "container_state", container)
    monkeypatch.setattr(ops, "fetch_players", Mock(side_effect=rcon.RCONError("offline")))
    assert payloads.players_payload() == {"ok": False, "error": "offline"}
    assert container.call_count == int(docker)


@pytest.mark.parametrize("raw", ["[]", "null", "42", '"text"', "{broken", b"\xff"])
def test_invalid_json_is_rejected(api, monkeypatch, raw):
    patch = Mock()
    monkeypatch.setattr(ops, "patch_settings", patch)
    status, body = api("POST", "/api/settings", raw=raw)
    assert status == 400 and body["ok"] is False
    patch.assert_not_called()


@pytest.mark.parametrize("length", ["invalid", "-1", "65537"])
def test_invalid_content_length_is_rejected(api, length):
    status, body = api("POST", "/api/action", raw="", headers={"Content-Length": length})
    assert status == 400 and body["ok"] is False


@pytest.mark.parametrize("value", [[], {}, 12, True])
def test_invalid_action_and_command_types(api, operation_env, value):
    for path, key in (("/api/action", "op"), ("/api/rcon", "command")):
        status, body = api("POST", path, {key: value})
        assert status == 400 and body["ok"] is False
    operation_env[0].assert_not_called()


def test_logs_tail_parameter(api, monkeypatch):
    logs = Mock(return_value=("server log", None))
    monkeypatch.setattr(app.dockerlib, "container_logs", logs)
    for query, expected in (
        ("12", 12),
        ("0", 1),
        ("99999", 10000),
        ("bad", app.config.CFG["log_lines"]),
    ):
        assert api("GET", "/api/logs?tail=" + query) == (200, {"ok": True, "text": "server log"})
        assert logs.call_args.args[1] == expected


def test_settings_patch_is_atomic(monkeypatch):
    before = json.loads(json.dumps(ops._SETTINGS))
    monkeypatch.setattr(ops, "_SETTINGS", json.loads(json.dumps(before)))
    save = Mock()
    monkeypatch.setattr(ops, "_save_settings", save)
    patch = {
        "autoUpdate": {"enabled": not before["autoUpdate"]["enabled"], "intervalHours": "12"},
        "telegram": {"enabled": "invalid"},
    }
    original = json.loads(json.dumps(patch))
    assert ops.patch_settings(patch) is not None
    assert ops._SETTINGS == before
    assert patch == original
    save.assert_not_called()
    patch.pop("telegram")
    assert ops.patch_settings(patch) is None
    assert ops._SETTINGS["autoUpdate"]["enabled"] != before["autoUpdate"]["enabled"]
    assert ops._SETTINGS["autoUpdate"]["intervalHours"] == 12
    save.assert_called_once()


def test_settings_version_advances_only_for_accepted_patch(monkeypatch):
    monkeypatch.setattr(ops, "_SETTINGS", json.loads(json.dumps(ops._DEFAULTS)))
    monkeypatch.setattr(ops, "_SETTINGS_VERSION", {"epoch": "test-server", "revision": 2})
    monkeypatch.setattr(ops, "_save_settings", Mock())
    before = ops.get_settings()["version"]
    assert ops.patch_settings({"telegram": {"enabled": "invalid"}})
    assert ops.get_settings()["version"] == before
    assert ops.patch_settings({"telegram": {"chatId": "123"}}) is None
    assert before == {"epoch": "test-server", "revision": 2}
    assert ops.get_settings()["version"] == {"epoch": "test-server", "revision": 3}


def test_settings_storage_failure_is_reported_as_unavailable(api, monkeypatch):
    monkeypatch.setattr(ops, "_SETTINGS", json.loads(json.dumps(ops._DEFAULTS)))
    monkeypatch.setattr(ops, "_save_settings", Mock(side_effect=ops.OpsError("Нет места")))
    status, body = api("POST", "/api/settings", {"autoUpdate": {"enabled": True}})
    assert status == 503 and body == {"ok": False, "error": "Нет места"}
    assert ops._SETTINGS["autoUpdate"]["enabled"] is False


@pytest.mark.parametrize("value", ["false", 0, 1, None, [], {}])
def test_backup_rejects_non_boolean_stop_flag(api, operation_env, value):
    status, body = api("POST", "/api/action", {"op": "backup", "stopServer": value})
    assert status == 400 and body["ok"] is False
    operation_env[0].assert_not_called()
