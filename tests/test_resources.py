"""Resource regressions: shared collectors, bounded reads, and streamed exports."""

import io
import json
import subprocess
import threading
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app
import auth
import config
import dockerlib
import i18n
import ops
import payloads
import workshop


def test_stream_collects_once_for_concurrent_subscribers(monkeypatch):
    barrier = threading.Barrier(8)
    entered, release = threading.Event(), threading.Event()

    def collect(name, **_kwargs):
        entered.set()
        assert release.wait(5)
        return {"ok": True, "text": "Игроки"}

    provider = Mock(side_effect=collect)
    monkeypatch.setattr(payloads, "stream_payload", provider)
    cache = payloads.StreamCache()

    def subscribe():
        barrier.wait(timeout=5)
        return cache.frame("players", 5)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(subscribe) for _ in range(8)]
        try:
            assert entered.wait(5)
        finally:
            release.set()
        frames = [future.result(timeout=5) for future in futures]
    provider.assert_called_once_with("players", telemetry=cache)
    assert all(frame is frames[0] for frame in frames)
    assert "Игроки" in frames[0].decode("utf-8")


def test_slow_stream_does_not_block_other_channels(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def collect(name, **_kwargs):
        if name == "logs":
            entered.set()
            assert release.wait(5)
        return {"ok": True}

    monkeypatch.setattr(payloads, "stream_payload", collect)
    cache = payloads.StreamCache()
    with ThreadPoolExecutor(max_workers=2) as executor:
        logs = executor.submit(cache.frame, "logs", 5)
        try:
            assert entered.wait(5)
            operation = executor.submit(cache.frame, "ops", 1)
            assert b"event: ops" in operation.result(timeout=2)
        finally:
            release.set()
        assert b"event: logs" in logs.result(timeout=5)


def test_stream_ttl_starts_after_collection_and_shares_errors(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(payloads.time, "monotonic", lambda: clock[0])

    def collect(name, **_kwargs):
        clock[0] += 20
        raise OSError("offline")

    provider = Mock(side_effect=collect)
    monkeypatch.setattr(payloads, "stream_payload", provider)
    cache = payloads.StreamCache()
    first = cache.frame("logs", 5)
    assert json.loads(first.decode().split("data: ")[1]) == {"ok": False, "error": "offline"}
    clock[0] = 24.9
    assert cache.frame("logs", 5) is first
    assert provider.call_count == 1
    clock[0] = 25
    cache.frame("logs", 5)
    assert provider.call_count == 2
    # A new HTTP server must start with fresh data, rather than a process-global cache.
    payloads.StreamCache().frame("logs", 5)
    assert provider.call_count == 3


@pytest.mark.parametrize("language", ["en", "ru"])
def test_stream_localizes_once_per_snapshot_and_preserves_raw_data(monkeypatch, language):
    data = {"ok": False, "error": "Сервер остановлен", "text": "Сервер остановлен"}
    monkeypatch.setattr(payloads, "stream_payload", lambda _name, **_kwargs: data)
    present = Mock(wraps=i18n.present)
    monkeypatch.setattr(payloads.i18n, "present", present)
    token = i18n.LANGUAGE.set(language)
    try:
        cache = payloads.StreamCache()
        frames = [cache.frame("logs", 5) for _ in range(8)]
    finally:
        i18n.LANGUAGE.reset(token)
    assert sum(call.args == (data,) for call in present.call_args_list) == 1
    assert all(frame is frames[0] for frame in frames)
    result = json.loads(frames[0].decode().split("data: ")[1])
    assert result["error"] == ("Server stopped" if language == "en" else data["error"])
    assert result["text"] == data["text"]
    assert data["error"] == "Сервер остановлен"


@pytest.mark.parametrize("logs", [True, False])
@pytest.mark.parametrize("empty", [False, True])
def test_stream_skips_unsubscribed_logs_and_sleeps_until_due(monkeypatch, logs, empty):
    clock = [0.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: clock[0])
    if empty:
        monkeypatch.setattr(payloads, "STREAM_PLAN", ())
    collected = []

    def collect(name, **_kwargs):
        collected.append(name)
        clock[0] += 0.02
        return {"ok": True}

    monkeypatch.setattr(payloads, "stream_payload", collect)
    handler = object.__new__(app.Handler)
    handler.server = SimpleNamespace(auth=Mock())
    handler.server.auth.is_active.return_value = True
    handler.auth_session = "test-session"
    handler.connection = Mock()
    handler.wfile = io.BytesIO()
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    chunks = []
    handler._sse_write = chunks.append
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
        # Revocation is noticed on the next wake, before another collection.
        handler.server.auth.is_active.return_value = False

    monkeypatch.setattr(app.time, "sleep", sleep)
    handler._serve_stream(logs=logs)
    assert ("logs" in collected) is (logs and not empty)
    assert {name for name, _ in payloads.STREAM_PLAN} - {"logs"} <= set(collected)
    assert sleeps == pytest.approx([1.0 if empty else 1.02 - (0.2 if logs else 0.18)])
    assert chunks[-1] == b"event: auth-expired\ndata: {}\n\n"


def test_stream_collection_errors_are_localized_inside_cache(monkeypatch):
    monkeypatch.setattr(payloads, "stream_payload", Mock(side_effect=OSError("Сервер остановлен")))
    token = i18n.LANGUAGE.set("en")
    try:
        frame = payloads.StreamCache().frame("logs", 5)
    finally:
        i18n.LANGUAGE.reset(token)
    assert json.loads(frame.decode().split("data: ")[1])["error"] == "Server stopped"


@pytest.mark.parametrize("channel,collections", [("players", 1), ("mods", 2)])
def test_monitoring_collection_is_shared_across_languages_except_workshop(
    monkeypatch, channel, collections
):
    provider = Mock(return_value={"ok": False, "error": "Сервер остановлен", "names": ["Игрок"]})
    monkeypatch.setattr(payloads, "stream_payload", provider)
    cache = payloads.StreamCache()
    frames = {}
    for language in ("ru", "en", "ru", "en"):
        token = i18n.LANGUAGE.set(language)
        try:
            frames[language] = cache.frame(channel, 5).decode()
        finally:
            i18n.LANGUAGE.reset(token)
    assert provider.call_count == collections
    assert "Server stopped" in frames["en"]
    assert "Сервер остановлен" in frames["ru"]
    assert "Игрок" in frames["ru"] and "Игрок" in frames["en"]


def test_stream_reuses_container_inspection_and_stats_without_caching_controls(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(payloads.time, "monotonic", lambda: clock[0])
    state = Mock(
        return_value={"status": "running", "running": True, "startedAt": "", "image": "pz"}
    )
    stats = Mock(return_value={"cpuPct": 12, "memPct": 30})
    players = Mock(return_value={"names": ["Player"], "count": 1})
    monkeypatch.setattr(ops, "container_state", state)
    monkeypatch.setattr(dockerlib, "container_stats", stats)
    monkeypatch.setattr(ops, "fetch_players", players)
    monkeypatch.setattr(ops, "docker_ok_cached", lambda: True)
    monkeypatch.setattr(ops, "compose_ok_cached", lambda: True)
    monkeypatch.setattr(ops, "local_digest_cached", lambda **_kwargs: None)
    monkeypatch.setattr(ops, "list_backups", lambda: [])
    cache = payloads.StreamCache()
    for language in ("en", "ru"):
        token = i18n.LANGUAGE.set(language)
        try:
            for channel, interval in (("overview", 3), ("players", 5), ("stats", 5)):
                assert b'"ok":true' in cache.frame(channel, interval)
        finally:
            i18n.LANGUAGE.reset(token)
    assert state.call_count == stats.call_count == players.call_count == 1
    # A control check must see a stopped server even while the monitoring frame
    # from its previous running state is still within its three-second TTL.
    state.return_value = {**state.return_value, "running": False}
    assert not ops.is_running()
    assert state.call_count == 2
    clock[0] = 3
    frame = cache.frame("overview", 3)
    assert b'"running":false' in frame
    assert state.call_count == 3
    # Direct HTTP providers retain fresh reads and bypass the stream snapshot.
    assert payloads.stats_payload()["ok"] is False
    assert state.call_count == 4


def test_container_snapshot_caches_missing_state_and_tracks_target(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(payloads.time, "monotonic", lambda: clock[0])
    state = Mock(return_value=None)
    monkeypatch.setattr(ops, "container_state", state)
    cache = payloads.StreamCache()
    assert cache.container_state() is None
    assert cache.container_state() is None
    assert state.call_count == 1
    monkeypatch.setitem(config.CFG, "pz_container", "different-server")
    assert cache.container_state() is None
    assert state.call_count == 2
    clock[0] = 3
    cache.container_state()
    assert state.call_count == 3


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 65536])
@pytest.mark.parametrize("ending", ["", "\n", "\r\n"])
def test_reverse_reader_handles_utf8_and_line_boundaries(tmp_path, chunk_size, ending):
    path = tmp_path / "journal.jsonl"
    path.write_bytes(("первая\r\nвторая\n\nпоследняя" + ending).encode())
    assert [line.rstrip("\r") for line in ops._reverse_lines(path, chunk_size)] == [
        "последняя",
        "вторая",
        "первая",
    ]


def test_large_journals_read_only_tail_and_skip_broken_records(tmp_path, monkeypatch):
    path = tmp_path / "backups.jsonl"
    prefix = (json.dumps({"text": "old" * 300}) + "\n").encode() * 20000
    path.write_bytes(prefix + b'{"id":1}\ninvalid\n{"id":2}\n')
    monkeypatch.setitem(config.CFG, "dashboard_dir", str(tmp_path))
    monkeypatch.setitem(config.CFG, "events_file", str(path))
    monkeypatch.setattr(ops, "_EV_MEM", deque(maxlen=300))
    reads = []
    real_open = open

    class BoundedReader:
        def __init__(self, *args, **kwargs):
            self.source = real_open(*args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.source.close()

        def seek(self, *args):
            return self.source.seek(*args)

        def read(self, size):
            assert 0 < size <= 65536
            reads.append(size)
            return self.source.read(size)

    monkeypatch.setattr(ops, "open", BoundedReader, raising=False)
    assert ops.get_backup_journal(2) == [{"id": 2}, {"id": 1}]
    assert sum(reads) <= 65536
    reads.clear()
    assert ops.get_events(2) == [{"id": 2}, {"id": 1}]
    assert sum(reads) < len(prefix) // 50
    assert ops.get_backup_journal(0) == []


def test_metadata_caches_evict_old_and_expired_profiles(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(workshop.time, "time", lambda: clock[0])
    cache = OrderedDict()
    for key in range(100):
        workshop._store_cache(cache, key, {"mods": [key]}, ttl=120)
    assert list(cache) == list(range(92, 100))
    clock[0] += 120
    workshop._store_cache(cache, "new", {}, ttl=120)
    assert list(cache) == ["new"]


def test_overview_uses_one_inspection_and_updates_digest_on_image_change(monkeypatch):
    state = Mock(
        return_value={
            "status": "running",
            "running": True,
            "startedAt": "",
            "image": "repo/server:old",
        }
    )
    digest = Mock(side_effect=["sha256:old", "sha256:new"])
    monkeypatch.setattr(ops, "container_state", state)
    monkeypatch.setattr(dockerlib, "image_digests", digest)
    monkeypatch.setattr(ops, "_LOCAL_DIGEST", {"digest": None, "image": None, "at": 0})
    monkeypatch.setattr(ops, "docker_ok_cached", lambda: False)
    monkeypatch.setattr(ops, "list_backups", lambda: [])
    assert ops.overview()["update"]["local"] == "sha256:old"
    state.assert_called_once()
    state.return_value = {**state.return_value, "image": "repo/server:new"}
    assert ops.overview()["update"]["local"] == "sha256:new"
    assert state.call_count == 2
    assert digest.call_count == 2


@pytest.mark.parametrize(
    "failure", [None, OSError("missing"), subprocess.TimeoutExpired("docker", 120)]
)
def test_full_log_capture_writes_both_streams_directly_to_file(tmp_path, monkeypatch, failure):
    captured = b"normal\nerror\n"

    def run(args, *, stdout, stderr, timeout):
        assert args == ["docker", "logs", config.CFG["pz_container"]]
        assert stderr == subprocess.STDOUT and timeout == 120
        stdout.write(captured)
        if failure:
            raise failure
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(dockerlib.subprocess, "run", run)
    with (tmp_path / "logs").open("w+b") as target:
        assert ops.full_logs(target) is (failure is None)
        target.seek(0)
        assert target.read() == captured


@pytest.mark.parametrize("success", [True, False])
def test_full_log_http_export_preserves_bytes_and_reports_failure(monkeypatch, success):
    captured = ("  строка\nошибка\n" * 50000).encode()
    monkeypatch.setattr(ops, "docker_ok_cached", lambda: True)

    def collect(target):
        target.write(captured)
        return success

    monkeypatch.setattr(ops, "full_logs", collect)
    handler = object.__new__(app.Handler)
    handler.wfile = io.BytesIO()
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler._send_error_json = Mock()
    handler._send_full_logs()
    if success:
        assert handler.wfile.getvalue() == captured
        handler.send_header.assert_any_call("Content-Length", str(len(captured)))
        handler.send_response.assert_called_once_with(200)
    else:
        handler._send_error_json.assert_called_once_with(500, "Не удалось получить логи контейнера")
        assert handler.wfile.getvalue() == b""


def test_authenticated_stream_checks_revocation_and_expiry_without_decrypting(monkeypatch):
    manager = auth.Auth("admin", "long-test-password", auth.Fernet.generate_key())
    token = manager.sign_in("admin", "long-test-password", "test")
    session_id = manager.session(f"{auth.COOKIE_NAME}={token}")
    monkeypatch.setattr(manager._cipher, "decrypt", Mock(side_effect=AssertionError("decrypt")))
    assert manager.is_active(session_id)
    manager.sign_out(session_id)
    assert not manager.is_active(session_id)
    manager._sessions[session_id] = auth.time.time() - 1
    assert not manager.is_active(session_id)
    assert session_id not in manager._sessions
