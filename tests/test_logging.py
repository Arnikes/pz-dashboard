"""Real process handlers and HTTP responses exercise the shared logging contract."""

import http.client
import io
import json
import logging
import re
import socket
import sys
import threading
from collections import deque
from pathlib import Path
from unittest.mock import Mock

import pytest

import app
import auth
import config
import i18n
import logging_utils
import ops
from test_dashboard_update import startup as startup
from test_auth import auth_server as auth_server, request, sign_in


@pytest.fixture
def process_log(monkeypatch, capsys):
    logger = logging.getLogger("pz")
    previous = logger.handlers[:], logger.level, logger.propagate, logger.disabled
    logger.handlers = []
    logger.propagate = True
    logger.disabled = False

    class CapturedLog:
        output = ""

        def getvalue(self):
            self.output += capsys.readouterr().err
            return self.output

    stream = CapturedLog()
    monkeypatch.setitem(config.CFG, "log_level", "INFO")
    try:
        yield stream
    finally:
        for handler in logger.handlers:
            if handler not in previous[0]:
                handler.close()
        logger.handlers, logger.level, logger.propagate, logger.disabled = previous


@pytest.mark.parametrize(
    "configured,expected",
    [("debug", 10), (" INFO ", 20), ("warn", 30), ("WARNING", 30), ("ERROR", 40), ("CRITICAL", 50)],
)
def test_real_handler_filters_levels_and_formats_each_record(
    process_log, monkeypatch, configured, expected
):
    monkeypatch.setitem(config.CFG, "log_level", configured)
    logging_utils.configure()
    logger = logging_utils.get_logger("test")
    for level in (10, 20, 30, 40, 50):
        logger.log(level, "message-%s", level)
    lines = process_log.getvalue().splitlines()
    assert len(lines) == len([level for level in (10, 20, 30, 40, 50) if level >= expected])
    for line in lines:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2} [A-Z]+ pz.test message-\d+", line)
    assert f"message-{expected}" in lines[0]


def test_configuration_is_idempotent_and_leaves_other_handlers_alone(process_log):
    root = logging.getLogger()
    library = logging.getLogger("pz.telegram.mtproxy.transport")
    root_state = root.handlers[:], root.level
    library_state = library.handlers[:], library.level, library.propagate
    logger = logging_utils.configure("INFO")
    initial = logger.handlers[:]
    logging_utils.configure("DEBUG")
    assert logger.handlers == initial and len(initial) == 1
    logging_utils.get_logger("test").info("once")
    assert process_log.getvalue().count("once") == 1
    assert (root.handlers, root.level) == root_state
    assert (library.handlers, library.level, library.propagate) == library_state


def test_existing_root_handler_cannot_duplicate_or_expose_raw_diagnostics(process_log):
    root_output = io.StringIO()
    root_handler = logging.StreamHandler(root_output)
    root = logging.getLogger()
    root.addHandler(root_handler)
    try:
        logger = logging_utils.configure("DEBUG")
        assert logger.propagate is False and root_handler in root.handlers
        try:
            raise ValueError("exception-secret")
        except ValueError:
            logging_utils.get_logger("test").error("safe\ninjected", exc_info=True)
        assert root_output.getvalue() == ""
        output = process_log.getvalue()
        assert len(output.splitlines()) == 1 and "safe\\x0ainjected" in output
        assert "exception-secret" not in output and "Traceback" not in output
    finally:
        root.removeHandler(root_handler)
        root_handler.close()


def test_process_handler_threshold_also_applies_to_explicit_child_levels(process_log):
    logging_utils.configure("ERROR")
    logger = logging_utils.get_logger("explicit-child")
    previous = logger.level
    try:
        logger.setLevel(logging.DEBUG)
        logger.debug("hidden debug")
        logger.warning("hidden warning")
        logger.error("visible error")
        output = process_log.getvalue()
        assert len(output.splitlines()) == 1 and "visible error" in output
        assert "hidden" not in output
        logging_utils.configure("CRITICAL")
        logger.error("hidden after reconfigure")
        logger.critical("visible critical")
        output = process_log.getvalue()
        assert len(output.splitlines()) == 2 and "visible critical" in output
        assert "hidden" not in output
    finally:
        logger.setLevel(previous)


def test_invalid_level_falls_back_without_echoing_untrusted_value(process_log):
    logging_utils.configure("invalid-secret\n\x1b[31m")
    logging_utils.get_logger("test").info("visible")
    output = process_log.getvalue()
    assert output.count("Invalid PZ_LOG_LEVEL; using INFO") == 1
    assert "INFO pz.test visible" in output
    assert "invalid-secret" not in output


def test_formatter_escapes_controls_and_drops_exception_payloads(process_log):
    logging_utils.configure("DEBUG")
    try:
        raise ValueError("exception-secret")
    except ValueError:
        logging_utils.get_logger("test").error(
            "value=%s", "a\r\nb\t\x00\x1b\x7f\u0085\u202e\u2028\u2029", exc_info=True
        )
    output = process_log.getvalue()
    assert len(output.splitlines()) == 1
    assert "value=a\\x0d\\x0ab\\x09\\x00\\x1b\\x7f\\x85\\u202e\\u2028\\u2029" in output
    assert "exception-secret" not in output and "Traceback" not in output


@pytest.fixture
def event_log(tmp_path, monkeypatch, process_log):
    monkeypatch.setitem(config.CFG, "events_file", str(tmp_path / "nested" / "events.jsonl"))
    monkeypatch.setitem(config.CFG, "dashboard_dir", str(tmp_path))
    monkeypatch.setattr(ops, "_EV_MEM", deque(maxlen=300))
    enqueue = Mock()
    monkeypatch.setattr(ops.notifylib, "enqueue", enqueue)
    return process_log, enqueue


@pytest.mark.parametrize(
    "kind,override,expected",
    [
        ("backup", None, "INFO"),
        ("warn", None, "WARNING"),
        ("error", None, "ERROR"),
        ("rcon-error", None, "ERROR"),
        ("invalid", None, "ERROR"),
        ("update-check", "error", "ERROR"),
        ("docker", "WARN", "WARNING"),
        ("backup", "CRITICAL", "CRITICAL"),
        ("console", "DEBUG", "DEBUG"),
    ],
)
def test_event_levels_persist_emit_and_forward_normalized_notification_level(
    event_log, kind, override, expected
):
    output, enqueue = event_log
    logging_utils.configure("DEBUG")
    ops.log_event(kind, "event text", "detail", True, level=override)
    record = ops.get_events()[0]
    normalized = "error" if kind == "invalid" else kind
    assert record["type"] == normalized and record["level"] == expected
    if kind == "console":
        assert record["text"] == "Команда консоли отправлена" and "detail" not in record
    else:
        assert record["text"] == "event text" and record["detail"] == "detail"
    assert json.loads(Path(config.CFG["events_file"]).read_text(encoding="utf-8")) == record
    enqueue.assert_called_once_with(normalized, record["text"], level=expected)
    assert len(output.getvalue().splitlines()) == 1
    assert f" {expected} pz.events [{normalized}] " in output.getvalue()


def test_process_threshold_does_not_filter_events_or_notifications(event_log):
    output, enqueue = event_log
    logging_utils.configure("CRITICAL")
    ops.log_event("error", "still persisted")
    assert output.getvalue() == ""
    assert ops.get_events()[0]["level"] == "ERROR"
    assert "still persisted" in Path(config.CFG["events_file"]).read_text(encoding="utf-8")
    enqueue.assert_called_once_with("error", "still persisted", level="ERROR")


def test_legacy_event_records_are_not_rewritten_or_normalized(event_log):
    path = Path(config.CFG["events_file"])
    path.parent.mkdir(parents=True)
    old = {"id": 1, "type": "warn", "text": "legacy"}
    raw = json.dumps(old).encode() + b"\n"
    path.write_bytes(raw)
    assert ops.get_events() == [old]
    assert path.read_bytes() == raw
    ops.log_event("backup", "new", notify=False)
    assert ops.get_events()[1] == old
    assert path.read_bytes().startswith(raw)


def test_persistence_failure_keeps_memory_and_reports_category_without_recursion(
    event_log, monkeypatch
):
    output, enqueue = event_log
    logging_utils.configure()
    append = Mock(side_effect=OSError("path-secret\ncredential"))
    monkeypatch.setattr(ops.fileio, "append_jsonl", append)
    ops.log_event("warn", "still available")
    assert len(ops.get_events()) == 1 and ops.get_events()[0]["text"] == "still available"
    enqueue.assert_called_once_with("warn", "still available", level="WARNING")
    append.assert_called_once()
    ops._journal_append({"name": "backup.tar.gz"})
    output = output.getvalue()
    assert "Event persistence unavailable (OSError)" in output
    assert "Backup journal persistence unavailable (OSError)" in output
    assert "path-secret" not in output and "credential" not in output


def test_console_commands_are_not_copied_into_any_event_sink(event_log):
    output, enqueue = event_log
    logging_utils.configure("DEBUG")
    command = 'adduser "test" "fictional-password"'
    ops.log_event("console", command, detail={"command": command})
    record = ops.get_events()[0]
    assert record["text"] == "Команда консоли отправлена" and "detail" not in record
    assert list(ops._EV_MEM) == [record]
    saved = Path(config.CFG["events_file"]).read_text(encoding="utf-8")
    assert json.loads(saved) == record
    enqueue.assert_called_once_with("console", record["text"], level="INFO")
    assert record["text"] in output.getvalue()
    assert "fictional-password" not in saved + output.getvalue() + repr(enqueue.call_args)


def test_console_command_reaches_transport_unchanged_without_persisting_arguments(
    event_log, monkeypatch
):
    command = 'adduser "fictional player" "fictional-password with spaces"'
    run = Mock(return_value="User created")
    monkeypatch.setattr(ops.rconlib, "run_command", run)
    monkeypatch.setattr(ops, "op_busy", lambda: False)
    monkeypatch.setattr(ops, "_RCON_CACHE", {"state": "ok", "error": None, "at": None})
    assert ops.console_command(command) == "User created"
    run.assert_called_once_with(
        config.CFG["rcon_host"], config.CFG["rcon_port"], config.CFG["rcon_password"], command
    )
    assert ops.get_events()[0]["text"] == "Команда консоли отправлена"
    assert "fictional-password" not in Path(config.CFG["events_file"]).read_text(encoding="utf-8")


@pytest.mark.parametrize("language", ["en", "ru"])
def test_legacy_console_arguments_are_hidden_from_events_http_without_rewriting_disk(
    event_log, auth_server, language
):
    path = Path(config.CFG["events_file"])
    path.parent.mkdir(parents=True)
    old = {
        "id": 7,
        "ts": "2026-01-01T00:00:00+00:00",
        "type": "console",
        "text": 'adduser "test" "legacy-fictional-password"',
        "detail": {"command": "legacy-fictional-password"},
        "level": "INFO",
    }
    raw = (json.dumps(old) + "\n").encode()
    path.write_bytes(raw)
    cookie = sign_in(auth_server)
    status, _, body = request(
        auth_server, "GET", "/api/events", cookie=cookie, headers={"X-PZ-Language": language}
    )
    assert status == 200 and len(body["items"]) == 1
    expected_text = (
        "Console command submitted" if language == "en" else "Команда консоли отправлена"
    )
    assert body["items"][0] == {
        **{key: value for key, value in old.items() if key not in ("text", "detail")},
        "text": expected_text,
    }
    assert "legacy-fictional-password" not in json.dumps(body) + json.dumps(list(ops._EV_MEM))
    assert path.read_bytes() == raw
    assert i18n.translate("Команда консоли отправлена", locale=language) == expected_text


@pytest.mark.parametrize("failure", ["closed", "write", "flush"])
def test_broken_process_stream_does_not_abort_persistence_or_notifications(
    event_log, monkeypatch, failure
):
    _, enqueue = event_log
    logging_utils.configure("DEBUG")
    if failure == "closed":
        stream = io.StringIO()
        stream.close()
    else:
        stream = Mock()
        getattr(stream, failure).side_effect = OSError("sink-secret\nraw-detail")
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stderr", stream)
        ops.log_event("backup", "business event")
    assert len(ops.get_events()) == 1 and ops.get_events()[0]["text"] == "business event"
    saved = json.loads(Path(config.CFG["events_file"]).read_text(encoding="utf-8"))
    assert saved == ops.get_events()[0]
    enqueue.assert_called_once_with("backup", "business event", level="INFO")


@pytest.fixture
def http_log_server(process_log):
    logging_utils.configure("DEBUG")

    class Handler(app.Handler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/explode":
                raise RuntimeError("exception-secret\nrequest-secret")
            if path.startswith("/send-error/"):
                self.send_error(int(path.rsplit("/", 1)[1]), "diagnostic-secret")
            else:
                self._send_json({"ok": True}, int(path.strip("/") or 200))

    server = app.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    worker.start()
    try:
        yield server, process_log
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


@pytest.mark.parametrize(
    "status,level",
    [
        (200, "DEBUG"),
        (303, "DEBUG"),
        (400, "WARNING"),
        (404, "WARNING"),
        (500, "ERROR"),
        (503, "ERROR"),
    ],
)
@pytest.mark.parametrize("send_error", [False, True])
def test_http_emits_once_at_final_status_without_request_secrets(
    http_log_server, status, level, send_error
):
    server, output = http_log_server
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
    path = f"/send-error/{status}" if send_error else f"/{status}"
    try:
        connection.request(
            "GET",
            path + "?token=query-secret",
            headers={"Cookie": "cookie-secret", "Authorization": "Bearer authorization-secret"},
        )
        response = connection.getresponse()
        assert response.status == status
        response.read()
    finally:
        connection.close()
    lines = output.getvalue().splitlines()
    assert len(lines) == 1
    assert f" {level} pz.http HTTP GET {status}" in lines[0]
    assert all(
        secret not in lines[0]
        for secret in ("query-secret", "cookie-secret", "authorization-secret", "diagnostic-secret")
    )


@pytest.mark.parametrize(
    "raw,status",
    [
        (b"request-secret\r\n", 400),
        (b"GET /private?token=query-secret HTTP/9.9\r\n\r\n", 505),
        (
            b"METHOD-SECRET /private?token=query-secret HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
            501,
        ),
    ],
)
def test_malformed_http_diagnostics_do_not_log_raw_input(http_log_server, raw, status):
    server, output = http_log_server
    with socket.create_connection(server.server_address, timeout=5) as connection:
        connection.sendall(raw)
        connection.shutdown(socket.SHUT_WR)
        while connection.recv(65536):
            pass
    lines = output.getvalue().splitlines()
    assert len(lines) == 1
    assert f"HTTP UNKNOWN {status}" in lines[0]
    assert all(
        secret not in lines[0]
        for secret in ("request-secret", "query-secret", "METHOD-SECRET", "/private")
    )


def test_uncaught_http_error_uses_safe_logger_instead_of_traceback(http_log_server):
    server, output = http_log_server
    with socket.create_connection(server.server_address, timeout=5) as connection:
        connection.sendall(b"GET /explode?token=query-secret HTTP/1.1\r\nHost: localhost\r\n\r\n")
        assert connection.recv(65536) == b""
    lines = output.getvalue().splitlines()
    assert len(lines) == 1
    assert "ERROR pz.http Unhandled request failure (RuntimeError)" in lines[0]
    assert "secret" not in lines[0] and "Traceback" not in lines[0]


def test_invalid_authentication_uses_configured_logger_and_nonzero_exit(process_log, monkeypatch):
    monkeypatch.setattr(
        auth.Auth, "from_env", Mock(side_effect=auth.AuthError("auth-secret\ninjected"))
    )
    with pytest.raises(SystemExit) as error:
        app.main()
    assert error.value.code == 1
    output = process_log.getvalue()
    assert len(output.splitlines()) == 1
    assert "CRITICAL pz.startup Administrator authentication configuration is invalid" in output
    assert "auth-secret" not in output


@pytest.mark.parametrize("docker,compose", [(False, False), (True, False)])
def test_degraded_startup_events_have_warning_severity(
    startup, process_log, monkeypatch, docker, compose
):
    monkeypatch.setattr(app.dockerlib, "docker_version", lambda: docker)
    monkeypatch.setattr(app.dockerlib, "compose_version", lambda: compose)
    app.main()
    assert ops.log_event.call_args.kwargs["level"] == "WARNING"
    assert "INFO pz.startup PZ Dashboard listening on port" in process_log.getvalue()


def test_failed_update_check_preserves_category_with_error_severity(event_log, monkeypatch):
    monkeypatch.setattr(ops.dashboardupdate, "inspect", lambda *args: None)
    monkeypatch.setattr(ops.dockerlib, "registry_digests", Mock(side_effect=OSError("unavailable")))
    ops.check_update(force_event=True)
    assert ops.get_events()[0]["type"] == "update-check"
    assert ops.get_events()[0]["level"] == "ERROR"
