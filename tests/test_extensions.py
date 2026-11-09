"""Executable extension contracts through the host's real security and worker paths."""

import http.client
import io
import json
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import actions
import app
import i18n
import ops
import payloads
import runtime
from extensions import ActionSpec, ChannelSpec, ExtensionRegistry, Plugin
from plugin_example import build_plugin


@pytest.fixture
def extension_servers(authenticated_admin):
    servers = []

    def create(*plugins, registry=None):
        server = app.ThreadingHTTPServer(
            ("127.0.0.1", 0), app.Handler, plugins=plugins, registry=registry
        )
        server.auth, headers = authenticated_admin
        server.daemon_threads = True
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        servers.append((server, thread))
        return server, headers

    yield create
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(5)


def request(server, headers, method, path, data=None):
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        connection.request(method, path, json.dumps(data) if data is not None else None, headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), json.loads(response.read())
    finally:
        connection.close()


@pytest.fixture
def worker_state(monkeypatch):
    monkeypatch.setattr(ops, "_ACTIVE", {"op": None, "stages": []})
    monkeypatch.setattr(ops, "_OP_HISTORY", deque())
    monkeypatch.setattr(ops, "_OP_REQUEST_PENDING", set())
    monkeypatch.setattr(ops, "_OP_REQUEST_FENCED", set())
    monkeypatch.setattr(ops.dashboardupdate, "operation", lambda: None)
    monkeypatch.setattr(
        ops, "get_settings", lambda: {"autoUpdate": {"warnSeconds": 120, "intervalHours": 6}}
    )
    events = Mock()
    monkeypatch.setattr(ops, "log_event", events)
    return events


def wait_worker(name):
    for thread in threading.enumerate():
        if thread.name == f"op-{name}":
            thread.join(5)
            assert not thread.is_alive()


def test_external_module_action_and_polling_use_host_contracts(extension_servers, worker_state):
    completed = []
    server, headers = extension_servers(build_plugin(completed))
    epoch = ops.op_state()["snapshot"]["epoch"]
    body = {
        "op": "example-probe",
        "value": "opaque-value",
        "warnSeconds": 17,
        "requestId": "example-launch",
        "requestEpoch": epoch,
    }
    status, _, receipt = request(server, headers, "POST", "/api/action", body)
    assert status == 200 and receipt["started"] == "example-probe"
    assert isinstance(receipt["operationId"], str)
    wait_worker("example-probe")
    assert completed == [("opaque-value", 17)]
    status, _, resolution = request(
        server, headers, "POST", "/api/action", {**body, "op": "resolve-request"}
    )
    assert status == 200 and resolution["requestPending"] is False
    assert any(
        row["id"] == receipt["operationId"] for row in resolution["operationState"]["history"]
    )
    for locale, message in (("en", "Server stopped"), ("ru", "Сервер остановлен")):
        status, response_headers, data = request(
            server,
            {**headers, "X-PZ-Language": locale},
            "GET",
            "/api/plugins/example/status",
        )
        assert status == 200 and data == {"ok": True, "completed": 1, "message": message}
        assert response_headers["Content-Language"] == locale


def test_plugin_routes_and_actions_require_auth_and_local_post(extension_servers, worker_state):
    completed = []
    server, headers = extension_servers(build_plugin(completed))
    no_auth = {key: value for key, value in headers.items() if key != "Cookie"}
    assert request(server, no_auth, "GET", "/api/plugins/example/status")[0] == 401
    assert request(server, no_auth, "POST", "/api/action", {"op": "example-probe"})[0] == 401
    no_local_header = {key: value for key, value in headers.items() if key != "X-PZ-Request"}
    assert (
        request(server, no_local_header, "POST", "/api/action", {"op": "example-probe"})[0] == 403
    )
    assert (
        request(
            server,
            {**headers, "Origin": "https://external.invalid"},
            "POST",
            "/api/action",
            {"op": "example-probe"},
        )[0]
        == 403
    )
    assert completed == []


def test_plugin_registration_and_cache_are_per_server(extension_servers, worker_state):
    server, headers = extension_servers(build_plugin([]))
    other, other_headers = extension_servers()
    assert request(server, headers, "GET", "/api/plugins/example/status")[0] == 200
    assert request(other, other_headers, "GET", "/api/plugins/example/status")[0] == 404
    assert request(other, other_headers, "POST", "/api/action", {"op": "example-probe"})[0] == 400
    assert "example-status" in server.extensions.channels
    assert "example-status" not in other.extensions.channels


def test_custom_sse_channel_runs_through_host_stream_and_session_revocation(monkeypatch):
    registry = runtime.build_registry((build_plugin([]),))
    cache = payloads.StreamCache(registry=registry)
    real_frame = cache.frame

    def frame(name, interval):
        if name == "example-status":
            return real_frame(name, interval)
        return f"event: {name}\ndata: {{}}\n\n".encode()

    cache.frame = frame
    auth = Mock()
    auth.is_active.return_value = True
    handler = object.__new__(app.Handler)
    handler.server = SimpleNamespace(extensions=registry, stream_cache=cache, auth=auth)
    handler.auth_session = "valid-session"
    handler.connection = Mock()
    handler.wfile = io.BytesIO()
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    chunks = []
    handler._sse_write = chunks.append
    monkeypatch.setattr(app.time, "sleep", lambda _: setattr(auth.is_active, "return_value", False))
    token = i18n.LANGUAGE.set("en")
    try:
        handler._serve_stream()
    finally:
        i18n.LANGUAGE.reset(token)
    custom = next(chunk for chunk in chunks if chunk.startswith(b"event: example-status\n"))
    assert json.loads(custom.decode().split("data: ")[1])["message"] == "Server stopped"
    assert chunks[-1] == b"event: auth-expired\ndata: {}\n\n"


def test_external_module_channel_reaches_authenticated_http_sse(extension_servers):
    registry = ExtensionRegistry(plugins=(build_plugin([]),))
    server, headers = extension_servers(registry=registry)
    no_auth = {key: value for key, value in headers.items() if key != "Cookie"}
    assert request(server, no_auth, "GET", "/api/stream")[0] == 401
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        connection.request("GET", "/api/stream", headers={**headers, "X-PZ-Language": "en"})
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/event-stream; charset=utf-8"
        lines = [response.readline().decode("utf-8") for _ in range(5)]
        assert "event: example-status\n" in lines
        data_line = next(line for line in lines if line.startswith("data: "))
        assert json.loads(data_line.removeprefix("data: ")) == {
            "ok": True,
            "completed": 0,
            "message": "Server stopped",
        }
    finally:
        # End this one stream promptly without revoking the session shared by tests.
        server.auth = SimpleNamespace(is_active=lambda _: False)
        connection.close()


def test_custom_collector_is_shared_across_locales_but_not_servers():
    collect = Mock(return_value={"ok": False, "error": "Сервер остановлен", "value": "raw"})
    registry = runtime.build_registry(
        (Plugin("example", channels=(ChannelSpec("probe", collect),)),)
    )
    cache = payloads.StreamCache(registry=registry)
    frames = {}
    for locale in ("en", "ru", "en"):
        token = i18n.LANGUAGE.set(locale)
        try:
            frame = cache.frame("probe", 30)
        finally:
            i18n.LANGUAGE.reset(token)
        if locale in frames:
            assert frame is frames[locale]
        frames[locale] = frame
    collect.assert_called_once_with()
    assert json.loads(frames["en"].decode().split("data: ")[1])["error"] == "Server stopped"
    assert json.loads(frames["ru"].decode().split("data: ")[1])["error"] == "Сервер остановлен"
    assert collect.return_value["error"] == "Сервер остановлен"
    payloads.StreamCache(registry=registry).frame("probe", 30)
    assert collect.call_count == 2


def test_custom_collector_concurrent_subscribers_share_one_snapshot():
    barrier = threading.Barrier(6)
    collect = Mock(return_value={"ok": True, "value": 1})
    registry = runtime.build_registry(
        (Plugin("example", channels=(ChannelSpec("probe", collect),)),)
    )
    cache = payloads.StreamCache(registry=registry)

    def read():
        barrier.wait(5)
        return cache.frame("probe", 5)

    with ThreadPoolExecutor(max_workers=6) as pool:
        frames = list(pool.map(lambda _: read(), range(6)))
    collect.assert_called_once_with()
    assert all(frame is frames[0] for frame in frames)


@pytest.mark.parametrize("phase", ["prepare", "before_start", "worker"])
def test_extension_action_unexpected_failures_hide_secrets_and_release_admission(
    extension_servers, worker_state, caplog, phase
):
    secret = "token=private-plugin-value"
    completed = []

    def fail(*_args):
        raise RuntimeError(secret)

    def prepare(data, context):
        return fail if phase == "worker" else lambda: completed.append("done")

    plugin = Plugin(
        "failing",
        actions=(
            ActionSpec(
                "failing-probe",
                fail if phase == "prepare" else prepare,
                before_start=fail if phase == "before_start" else None,
            ),
        ),
    )
    server, headers = extension_servers(plugin, build_plugin(completed))
    body = {
        "op": "failing-probe",
        "requestId": f"failing-{phase}",
        "requestEpoch": ops.op_state()["snapshot"]["epoch"],
    }
    status, _, result = request(server, headers, "POST", "/api/action", body)
    if phase == "worker":
        assert status == 200
        wait_worker("failing-probe")
    else:
        assert status == 500 and result["ok"] is False
    assert not ops.op_busy()
    status, _, resolution = request(
        server, headers, "POST", "/api/action", {**body, "op": "resolve-request"}
    )
    assert status == 200 and resolution["requestPending"] is False
    evidence = json.dumps({"reply": result, "resolution": resolution}, ensure_ascii=False)
    assert secret not in evidence and secret not in caplog.text
    assert all(secret not in str(call) for call in worker_state.call_args_list)
    assert request(server, headers, "POST", "/api/action", {"op": "example-probe"})[0] == 200
    wait_worker("example-probe")
    assert completed == [("example", 120)]


def test_busy_and_fenced_plugin_actions_never_prepare_or_run(
    extension_servers, worker_state, monkeypatch
):
    prepare = Mock(return_value=Mock())
    server, headers = extension_servers(Plugin("example", actions=(ActionSpec("probe", prepare),)))
    body = {
        "op": "probe",
        "requestId": "late-plugin",
        "requestEpoch": ops.op_state()["snapshot"]["epoch"],
    }
    assert (
        request(server, headers, "POST", "/api/action", {**body, "op": "resolve-request"})[0] == 200
    )
    assert request(server, headers, "POST", "/api/action", body)[0] == 409
    prepare.assert_not_called()
    monkeypatch.setattr(ops, "op_busy", lambda: True)
    assert request(server, headers, "POST", "/api/action", {"op": "probe"})[0] == 409
    prepare.return_value.assert_not_called()


@pytest.mark.parametrize("phase", ["prepare", "before_start"])
@pytest.mark.parametrize("error_type", [actions.ActionError, ops.OpsError])
def test_expected_extension_errors_keep_the_authored_api_policy(
    extension_servers, worker_state, phase, error_type
):
    def reject(*_args):
        if error_type is actions.ActionError:
            raise actions.ActionError(400, "Сервер остановлен")
        raise ops.OpsError("Сервер остановлен")

    prepare = reject if phase == "prepare" else lambda *_: lambda: None
    plugin = Plugin(
        "example",
        actions=(
            ActionSpec("probe", prepare, before_start=reject if phase == "before_start" else None),
        ),
    )
    server, headers = extension_servers(plugin)
    status, _, data = request(
        server, {**headers, "X-PZ-Language": "en"}, "POST", "/api/action", {"op": "probe"}
    )
    assert status == (400 if error_type is actions.ActionError else 409)
    assert data == {"ok": False, "error": "Server stopped"}
    assert not ops.op_busy()


@pytest.mark.parametrize("phase", ["prepare", "before_start", "worker"])
@pytest.mark.parametrize("error_type", [actions.ActionError, ops.OpsError])
def test_malformed_expected_extension_diagnostics_are_safe_and_recover(
    extension_servers, worker_state, phase, error_type
):
    def reject(*_args):
        if error_type is actions.ActionError:
            raise actions.ActionError(400, "\ud800")
        raise ops.OpsError("\ud800")

    def prepare(*_args):
        return reject if phase == "worker" else lambda: None

    plugin = Plugin(
        "malformed",
        actions=(
            ActionSpec(
                "probe",
                reject if phase == "prepare" else prepare,
                before_start=reject if phase == "before_start" else None,
            ),
        ),
    )
    completed = []
    server, headers = extension_servers(plugin, build_plugin(completed))
    status, _, result = request(server, headers, "POST", "/api/action", {"op": "probe"})
    assert status == (200 if phase == "worker" else 500)
    if phase == "worker":
        wait_worker("probe")
        result = ops.op_state()
        assert result["history"][0]["ok"] is False
        # The same history must remain usable through both JSON and the SSE cache.
        assert request(server, headers, "GET", "/api/ops")[0] == 200
        assert b"event: ops\n" in payloads.StreamCache(registry=server.extensions).frame("ops", 1)
    else:
        assert result["ok"] is False
    json.dumps(result, ensure_ascii=False).encode("utf-8")
    assert not ops.op_busy()
    assert request(server, headers, "POST", "/api/action", {"op": "example-probe"})[0] == 200
    wait_worker("example-probe")
    assert completed == [("example", 120)]


def test_custom_telemetry_opt_in_does_not_cache_direct_polling(extension_servers, monkeypatch):
    states = []

    def collect(*, state_provider=None):
        states.append(state_provider)
        return {"ok": True, "state": state_provider() if state_provider else "live"}

    registry_plugin = Plugin(
        "example",
        channels=(
            ChannelSpec("probe", collect, route="/api/plugins/example/status", telemetry=True),
        ),
    )
    server, headers = extension_servers(registry_plugin)
    monkeypatch.setattr(ops, "container_state", lambda: {"running": True})
    cache = payloads.StreamCache(registry=server.extensions)
    cached = json.loads(cache.frame("probe", 5).decode().split("data: ")[1])
    assert cached == {"ok": True, "state": {"running": True}}
    assert states == [cache.container_state]
    assert request(server, headers, "GET", "/api/plugins/example/status")[2] == {
        "ok": True,
        "state": "live",
    }
    assert states == [cache.container_state, None]


@pytest.mark.parametrize("result", [None, "cancelled", "aborted", "handoff", "unexpected", {}])
def test_custom_worker_results_cannot_activate_host_handoff(
    extension_servers, worker_state, result
):
    plugin = Plugin("example", actions=(ActionSpec("probe", lambda *_: lambda: result),))
    server, headers = extension_servers(plugin)
    status, _, receipt = request(server, headers, "POST", "/api/action", {"op": "probe"})
    assert status == 200
    wait_worker("probe")
    entry = next(row for row in ops.op_state()["history"] if row["id"] == receipt["operationId"])
    allowed = result is None or (isinstance(result, str) and result in ("cancelled", "aborted"))
    assert entry["ok"] is allowed
    assert entry.get("cancelled", False) is (result in ("cancelled", "aborted"))
    assert not ops.op_busy()


def test_custom_worker_result_comparison_never_runs_inside_host(
    extension_servers, worker_state, caplog
):
    comparisons = []

    class UnsafeResult:
        def __eq__(self, other):
            comparisons.append(other)
            raise RuntimeError("token=private-comparison-value")

    plugin = Plugin("example", actions=(ActionSpec("probe", lambda *_: lambda: UnsafeResult()),))
    server, headers = extension_servers(plugin)
    assert request(server, headers, "POST", "/api/action", {"op": "probe"})[0] == 200
    wait_worker("probe")
    assert comparisons == []
    assert ops.op_state()["history"][0]["ok"] is False
    assert "private-comparison-value" not in caplog.text
    assert not ops.op_busy()


def test_action_context_settings_are_detached_from_persistent_state(
    extension_servers, worker_state, monkeypatch
):
    settings = {"autoUpdate": {"warnSeconds": 120, "intervalHours": 6}}
    monkeypatch.setattr(ops, "get_settings", lambda: settings)
    seen = []

    def prepare(data, context):
        with pytest.raises(TypeError):
            context.settings["new"] = True
        context.settings["autoUpdate"]["intervalHours"] = 99
        seen.append(context.warn_seconds)
        return lambda: None

    server, headers = extension_servers(Plugin("example", actions=(ActionSpec("probe", prepare),)))
    assert (
        request(server, headers, "POST", "/api/action", {"op": "probe", "warnSeconds": 99999})[0]
        == 200
    )
    wait_worker("probe")
    assert seen == [3600]
    assert settings["autoUpdate"]["intervalHours"] == 6


@pytest.mark.parametrize("bad_result", ["raise", "unserializable", "unicode"])
def test_custom_collector_failures_are_safe_for_polling_and_sse_and_recover(
    extension_servers, monkeypatch, caplog, bad_result
):
    secret = "token=private-collector-value"

    class Sensitive:
        def __repr__(self):
            return secret

    failing = [True]

    def collect():
        if failing[0]:
            if bad_result == "raise":
                raise RuntimeError(secret)
            if bad_result == "unicode":
                return {"ok": True, "value": "\ud800"}
            return {"ok": True, "value": Sensitive()}
        return {"ok": True, "value": "recovered"}

    plugin = Plugin(
        "example", channels=(ChannelSpec("probe", collect, route="/api/plugins/example/status"),)
    )
    server, headers = extension_servers(plugin)
    status, _, result = request(server, headers, "GET", "/api/plugins/example/status")
    assert status == 200 and result["ok"] is False
    clock = [0.0]
    monkeypatch.setattr(payloads.time, "monotonic", lambda: clock[0])
    cache = payloads.StreamCache(registry=server.extensions)
    frame = cache.frame("probe", 5)
    assert json.loads(frame.decode().split("data: ")[1]) == result
    assert secret not in frame.decode() and secret not in caplog.text
    failing[0] = False
    assert request(server, headers, "GET", "/api/plugins/example/status")[2] == {
        "ok": True,
        "value": "recovered",
    }
    clock[0] = 6
    assert json.loads(cache.frame("probe", 5).decode().split("data: ")[1]) == {
        "ok": True,
        "value": "recovered",
    }


def test_registry_and_specs_cannot_be_mutated_through_input_aliases():
    channels = [ChannelSpec("probe", lambda: {"ok": True}, route="/api/plugins/example/probe")]
    plugin = Plugin("example", channels=channels)
    registry = runtime.build_registry([plugin])
    channels.clear()
    assert "probe" in registry.channels
    with pytest.raises(TypeError):
        registry.channels["other"] = channels
    with pytest.raises(TypeError):
        registry.poll_routes["/api/plugins/example/new"] = "probe"
    with pytest.raises(FrozenInstanceError):
        plugin.name = "changed"


@pytest.mark.parametrize("interval", [0, -1, True, float("inf"), float("nan"), "5"])
def test_invalid_channel_intervals_are_rejected(interval):
    with pytest.raises((TypeError, ValueError)):
        runtime.build_registry(
            (Plugin("example", channels=(ChannelSpec("probe", lambda: {}, interval=interval),)),)
        )


@pytest.mark.parametrize("name", ["cancel-update", "resolve-request", "apply-config", "start"])
def test_reserved_actions_cannot_be_replaced(name):
    with pytest.raises((TypeError, ValueError)):
        runtime.build_registry(
            (Plugin("example", actions=(ActionSpec(name, lambda *_: lambda: None),)),)
        )


@pytest.mark.parametrize(
    "route",
    [
        "/api/health",
        "/api/action",
        "/api/plugins/other/probe",
        "/api/plugins/example/../health",
        "/api/plugins/example/probe?x=1",
    ],
)
def test_plugin_poll_routes_cannot_shadow_host_or_escape_namespace(route):
    with pytest.raises((TypeError, ValueError)):
        runtime.build_registry(
            (Plugin("example", channels=(ChannelSpec("probe", lambda: {}, route=route),)),)
        )


def test_duplicate_registrations_and_sse_control_names_are_rejected():
    for plugins in (
        (Plugin("example"), Plugin("example")),
        (
            Plugin("one", actions=(ActionSpec("probe", lambda *_: lambda: None),)),
            Plugin("two", actions=(ActionSpec("probe", lambda *_: lambda: None),)),
        ),
        (
            Plugin(
                "example",
                channels=(ChannelSpec("probe", lambda: {}), ChannelSpec("probe", lambda: {})),
            ),
        ),
        (
            Plugin(
                "example",
                channels=(
                    ChannelSpec("first", lambda: {}, route="/api/plugins/example/probe"),
                    ChannelSpec("second", lambda: {}, route="/api/plugins/example/probe"),
                ),
            ),
        ),
        (Plugin("example", channels=(ChannelSpec("auth-expired", lambda: {}),)),),
    ):
        with pytest.raises((TypeError, ValueError)):
            runtime.build_registry(plugins)
