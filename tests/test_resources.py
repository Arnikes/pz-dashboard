"""Resource regressions: shared collectors, bounded reads, and streamed exports."""

import io
import json
import subprocess
import threading
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

import app
import auth
import config
import dockerlib
import ops
import payloads
import workshop


def test_stream_collects_once_for_concurrent_subscribers(monkeypatch):
    barrier = threading.Barrier(8)
    entered, release = threading.Event(), threading.Event()

    def collect(name):
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
    provider.assert_called_once_with("players")
    assert all(frame is frames[0] for frame in frames)
    assert "Игроки" in frames[0].decode("utf-8")


def test_slow_stream_does_not_block_other_channels(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def collect(name):
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

    def collect(name):
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
