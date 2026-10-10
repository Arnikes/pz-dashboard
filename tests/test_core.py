"""Юнит-тесты ядра пульта: парсеры docker, RCON-протокол (с фейковым сервером),
настройки, история онлайна, SSE-поток. Запуск: pytest -q tests (или из корня проекта)."""

import http.client
import io
import json
import struct
import sys
import tarfile
import threading
import time
import urllib.error
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest

DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD_DIR))

import configeditor  # noqa: E402
import workshop  # noqa: E402
import config  # noqa: E402
import dockerlib  # noqa: E402
import ops  # noqa: E402
import rcon  # noqa: E402


# ───────────────────────── парсеры ─────────────────────────


@pytest.mark.parametrize(
    "raw,names",
    [
        (
            "Players connected (6):\r\n-Alice\r\n--Bob\r\n-Katya-V\r\n"
            "-Дмитрий\r\n-players_online\r\n-none\r\n",
            ["Alice", "-Bob", "Katya-V", "Дмитрий", "players_online", "none"],
        ),
        ("Players connected (0):\n", []),
        ("Players connected (2):\nAlice\nKatya-V\n", ["Alice", "Katya-V"]),
    ],
)
def test_fetch_players_strips_only_rcon_list_marker(monkeypatch, raw, names):
    commands = []
    samples = []

    def fake_rcon(command):
        commands.append(command)
        return raw

    monkeypatch.setattr(ops, "rcon", fake_rcon)
    monkeypatch.setattr(ops, "record_players_sample", samples.append)
    assert ops.fetch_players() == {"names": names, "raw": raw, "count": len(names)}
    assert commands == ["players"]
    assert samples == [len(names)]


def test_parse_bytes():
    assert dockerlib.parse_bytes("123MiB") == 123 * 1024 * 1024
    assert dockerlib.parse_bytes("1.9GiB") == int(1.9 * 1024**3)
    assert dockerlib.parse_bytes("1.20kB") == 1200
    assert dockerlib.parse_bytes("42B") == 42
    assert dockerlib.parse_bytes("мусор") == 0


def test_command_output_includes_stderr_when_requested():
    script = "import os; os.write(1, b'normal\\n'); os.write(2, b'error\\n')"
    command = [sys.executable, "-c", script]
    assert dockerlib.sh(command) == (0, "normal", "error")
    assert dockerlib.sh(command, merge_stderr=True) == (0, "normal\nerror", "")


def test_log_readers_request_both_streams(monkeypatch):
    calls = []

    def fake_sh(args, timeout=120, merge_stderr=False):
        calls.append((args, merge_stderr))
        return 0, "normal\nerror" if merge_stderr else "normal", ""

    monkeypatch.setattr(dockerlib, "sh", fake_sh)
    assert dockerlib.container_logs("server") == ("normal\nerror", None)
    since, until = "2026-10-01T12:00:00Z", "2026-10-01T12:01:00Z"
    assert dockerlib.container_logs("server", 10000, since, until) == ("normal\nerror", None)
    scoped = calls[-1][0]
    assert scoped[scoped.index("--since") + 1] == since
    assert scoped[scoped.index("--until") + 1] == until
    assert scoped[scoped.index("--tail") + 1] == "10000"
    assert scoped[-1] == "server"
    assert all(merge for _, merge in calls)


def test_command_output_decodes_utf8_and_replaces_invalid_bytes():
    script = "import os; os.write(1, bytes.fromhex('d09fd180d0b8d0b2d0b5d182ff'))"
    assert dockerlib.sh([sys.executable, "-c", script]) == (0, "Привет�", "")


def test_fmt_size():
    assert ops.fmt_size(0) == "0 Б"
    assert ops.fmt_size(1024) == "1.0 КБ"
    assert ops.fmt_size(684000000).endswith("МБ")


def test_stats_parse(monkeypatch):
    monkeypatch.setattr(
        dockerlib,
        "sh",
        lambda args, timeout=120: (0, "12.34%|123MiB / 1.9GiB|6.29%|1.20kB / 3.40MB|17", ""),
    )
    s = dockerlib.container_stats("x")
    assert s["cpuPct"] == 12.34
    assert s["memUsed"] == 123 * 1024 * 1024
    assert s["memLimit"] == int(1.9 * 1024**3)
    assert s["pids"] == 17


def test_image_digests_parse(monkeypatch):
    """RepoDigests приходит как repo@sha256:... — хеш после @."""
    monkeypatch.setattr(
        dockerlib,
        "sh",
        lambda args, timeout=120: (
            0,
            "indifferentbroccoli/projectzomboid-server-docker@sha256:8e13816b92fdd\n",
            "",
        ),
    )
    assert dockerlib.image_digests("x") == "sha256:8e13816b92fdd"
    monkeypatch.setattr(dockerlib, "sh", lambda args, timeout=120: (0, "", ""))
    assert dockerlib.image_digests("x") is None


def test_effective_image(monkeypatch):
    """Образ для проверки обновлений берётся из контейнера, тег дописывается."""
    monkeypatch.setattr(
        ops,
        "container_state",
        lambda: {
            "status": "running",
            "running": True,
            "startedAt": None,
            "image": "indifferentbroccoli/projectzomboid-server-docker",
        },
    )
    assert ops._effective_image() == "indifferentbroccoli/projectzomboid-server-docker:latest"
    monkeypatch.setattr(ops, "container_state", lambda: None)
    assert ops._effective_image() == config.CFG["pz_image"]


def test_stats_history_throttle():
    ops._STATS.clear()
    ops.record_stats_sample({"cpuPct": 1.5, "memPct": 10})
    ops.record_stats_sample({"cpuPct": 2.5, "memPct": 11})  # троттлинг
    h = ops.get_stats_history()
    assert len(h) == 1 and h[0]["cpu"] == 1.5 and h[0]["mem"] == 10


def test_local_digest_cache(monkeypatch):
    """Локальный digest считается сам и кэшируется — docker дёргается один раз."""
    calls = {"n": 0}

    def fake_digests(img):
        calls["n"] += 1
        return "sha256:abc123"

    monkeypatch.setattr(ops.dockerlib, "image_digests", fake_digests)
    monkeypatch.setattr(
        ops,
        "container_state",
        lambda: {
            "status": "running",
            "running": True,
            "startedAt": None,
            "image": "indifferentbroccoli/projectzomboid-server-docker",
        },
    )
    ops._LOCAL_DIGEST["at"] = 0.0
    assert ops.local_digest_cached() == "sha256:abc123"
    assert ops.local_digest_cached() == "sha256:abc123"
    assert calls["n"] == 1
    assert ops._LOCAL_DIGEST["image"].endswith(":latest")


def test_list_mods_reads_server_ini(tmp_path, monkeypatch):
    config.CFG["data_dir"] = str(tmp_path)
    monkeypatch.setattr(ops, "_ws_titles", lambda ids: {})
    server_dir = tmp_path / "Server"
    server_dir.mkdir()
    (server_dir / "servertest.ini").write_text(
        "NightLength=60\n"
        "Mods=tsarslib;my mod;SoloMod\n"
        "WorkshopItems=111111111;222222222\n"
        "Map=Muldraugh, KY\n",
        encoding="utf-8",
    )
    assert ops.list_server_inis() == ["servertest.ini"]
    res = ops.list_mods("servertest.ini")
    assert res["ok"] and res["file"] == "servertest.ini"
    assert res["mods"] == ["tsarslib", "my mod", "SoloMod"]
    assert [w["workshopId"] for w in res["workshop"]] == ["111111111", "222222222"]
    assert res["paired"] is False and res["mappingSource"] == "metadata"
    assert res["workshop"][0]["url"].endswith("id=111111111")


def test_mods_paired(tmp_path, monkeypatch):
    """Равные количества без метаданных не дают соответствия по порядку."""
    config.CFG["data_dir"] = str(tmp_path)
    monkeypatch.setattr(ops, "_WS_TITLES", {"111": "Mod A", "222": "Mod B"})
    monkeypatch.setattr(ops, "_ws_titles", lambda ids: ops._WS_TITLES)
    server_dir = tmp_path / "Server"
    server_dir.mkdir()
    (server_dir / "srv.ini").write_text("Mods=modA;modB\nWorkshopItems=111;222\n", encoding="utf-8")
    res = ops.list_mods("srv.ini")
    assert res["paired"] is False and res["mappingSource"] == "metadata"
    assert res["pairs"] == []
    assert res["unbound"] == ["modA", "modB"]


def test_mods_disk_mapping(tmp_path, monkeypatch):
    """Один Workshop-элемент тянет несколько модов — маппинг из mod.info на диске."""
    config.CFG["data_dir"] = str(tmp_path)
    monkeypatch.setattr(ops, "_ws_titles", lambda ids: {})
    server_dir = tmp_path / "Server"
    server_dir.mkdir()
    (server_dir / "srv.ini").write_text(
        "Mods=libA;pluginB;localMod\nWorkshopItems=111\n", encoding="utf-8"
    )
    ws_dir = tmp_path / "steamapps" / "workshop" / "content" / "108600" / "111" / "mods"
    (ws_dir / "tsar" / "42").mkdir(parents=True)
    (ws_dir / "tsar" / "42" / "mod.info").write_text("name=Big Pack\nid=libA\n", encoding="utf-8")
    (ws_dir / "plug" / "42").mkdir(parents=True)
    (ws_dir / "plug" / "42" / "mod.info").write_text("id=pluginB\n", encoding="utf-8")
    res = ops.list_mods("srv.ini")
    assert res["mappingSource"] == "metadata"
    assert sorted(res["workshop"][0]["mods"]) == ["libA", "pluginB"]
    assert res["unbound"] == ["localMod"]
    assert res["paired"] is False


# ───────────────────────── фейковый RCON-сервер ─────────────────────────


def _pkt(rid, ptype, body: bytes) -> bytes:
    payload = struct.pack("<ii", rid, ptype) + body + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("соединение закрыто")
        buf += chunk
    return buf


def _read_pkt(sock):
    (size,) = struct.unpack("<i", _recv_exact(sock, 4))
    body = _recv_exact(sock, size)
    rid, ptype = struct.unpack("<ii", body[:8])
    return rid, ptype, body[8:-2].decode("utf-8", "replace")


class FakeRcon(threading.Thread):
    """Одноразовый Source RCON сервер: пустые 10-байтовые пакеты авторизации —
    регресс-тест на баг парсинга коротких пакетов."""

    daemon = True

    def __init__(self, password="secret", response="Players connected (0): \n", reject=False):
        super().__init__(daemon=True)
        self.password = password
        self.response = response
        self.reject = reject
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.port = srv.getsockname()[1]
        self._srv = srv

    def run(self):
        self._srv.settimeout(5)
        try:
            conn, _ = self._srv.accept()
        except TimeoutError:
            return
        conn.settimeout(5)
        try:
            rid, ptype, body = _read_pkt(conn)
            if self.reject or (ptype, body) != (3, self.password):
                conn.sendall(_pkt(-1, 2, b""))  # AUTH_RESPONSE с id=-1: отказ
            else:
                # пустой RESPONSE_VALUE + AUTH_RESPONSE — как отвечает PZ
                conn.sendall(_pkt(rid, 0, b""))
                conn.sendall(_pkt(rid, 2, b""))
                rid2, ptype2, cmd = _read_pkt(conn)
                assert ptype2 == 2, "ожидали EXECCOMMAND"
                conn.sendall(_pkt(rid2, 0, self.response.encode()))
        finally:
            conn.close()
            self._srv.close()


import socket  # noqa: E402  (после вспомогательных функций — читается сверху вниз)


def test_rcon_roundtrip():
    srv = FakeRcon()
    srv.start()
    out = rcon.RCON("127.0.0.1", srv.port, "secret", connect_timeout=3).run("players")
    assert "Players connected" in out


def test_rcon_wrong_password():
    srv = FakeRcon(password="right", reject=True)
    srv.start()
    with pytest.raises(rcon.RCONError):
        rcon.RCON("127.0.0.1", srv.port, "wrong", connect_timeout=3).run("players")


# ───────────────────────── настройки ─────────────────────────


def test_settings_validation(tmp_path):
    config.CFG["settings_file"] = str(tmp_path / "settings.json")
    config.CFG["events_file"] = str(tmp_path / "events.jsonl")
    assert ops.patch_settings({"watchdog": {"thresholdMin": 999}}) is None
    assert ops.get_settings()["watchdog"]["thresholdMin"] == 60
    assert ops.patch_settings({"autoUpdate": {"backupBeforeUpdate": False}}) is None
    assert ops.get_settings()["autoUpdate"]["backupBeforeUpdate"] is False
    assert (
        ops.patch_settings({"watchdog": {"enabled": "yes"}})
        == "watchdog.enabled должен быть true/false"
    )
    assert ops.patch_settings({"modsUpdate": {"warnSeconds": 99999}}) is None
    assert ops.get_settings()["modsUpdate"]["warnSeconds"] == 3600
    assert ops.patch_settings({"modsUpdate": {"warnSeconds": -5}}) is None
    assert ops.get_settings()["modsUpdate"]["warnSeconds"] == 0
    assert ops.get_settings()["modsUpdate"].get("intervalHours") == 6


# ───────────────────────── история онлайна ─────────────────────────


def test_players_history(tmp_path):
    config.CFG["dashboard_dir"] = str(tmp_path)
    ops._PH = None
    ops.record_players_sample(2)
    ops.record_players_sample(7)  # троттлинг: второй семпл в тот же интервал игнорируется
    assert len(ops._PH) == 1 and ops._PH[0]["count"] == 2

    # уводим семпл на 25 часов назад: новый добавится, старый выпадет из окна 24 ч
    old = (
        (datetime.now(timezone.utc) - timedelta(hours=25))
        .astimezone()
        .isoformat(timespec="seconds")
    )
    ops._PH[0]["ts"] = old
    ops.record_players_sample(4)
    assert len(ops._PH) == 1 and ops._PH[0]["count"] == 4
    assert ops.get_players_history()[-1]["count"] == 4


def test_history_persisted(tmp_path):
    config.CFG["dashboard_dir"] = str(tmp_path)
    ops._PH = None
    ops.record_players_sample(5)
    ops._PH = None  # имитация перезапуска: загрузка из файла
    assert ops.get_players_history()[0]["count"] == 5
    assert json.loads((tmp_path / "players-history.json").read_text())[0]["count"] == 5


# ───────────────────────── проверка обновлений модов ─────────────────────────


def test_fresh_lines_respects_duplicates():
    before = "a\nb\na"
    after = "a\nb\na\nb\nc\na"
    assert ops._fresh_lines(Counter(before.splitlines()), after) == ["b", "c", "a"]


def test_parse_mods_check_states():
    up = [
        "2026Z LOG : Mod f:1 st:1> CheckModsNeedUpdate: Checking...",
        "2026Z LOG : Mod f:1 st:2> CheckModsNeedUpdate: Mods updated",
    ]
    assert ops._parse_mods_check(up) == ("up-to-date", [])

    need = [
        "2026Z LOG : Mod f:1 st:1> CheckModsNeedUpdate: Checking...",
        "2026Z LOG : Mod f:1 st:2> CheckModsNeedUpdate: MOD killcount need update",
        "2026Z LOG : Mod f:1 st:3> CheckModsNeedUpdate: MOD etw need update",
    ]
    state, lines = ops._parse_mods_check(need)
    assert state == "needs-update"
    assert len(lines) == 2

    assert ops._parse_mods_check(["2026Z LOG : General f:1 st:1> что-то другое"]) == (None, [])


def test_mods_items_extraction():
    ws = {
        "2983905789": {
            "title": "Wandering Zombies",
            "url": "https://steamcommunity.com/sharedfiles/filedetails/?id=2983905789",
        }
    }
    lines = [
        "2026Z LOG : Mod f:1 st:1> CheckModsNeedUpdate: MOD wanderingzombies 2983905789 need update",
        "2026Z LOG : Mod f:1 st:2> CheckModsNeedUpdate: MOD unknownmod need update",
    ]
    items = ops._mods_items_from_lines(lines, ws)
    assert items[0]["workshopId"] == "2983905789"
    assert items[0]["title"] == "Wandering Zombies"
    assert "workshopId" not in items[1]
    assert items[1]["raw"].endswith("need update")


def test_check_mods_update_flow(monkeypatch):
    calls = {"n": 0}
    base = "\n".join(
        f"2026-09-08T12:0{i}.000000000Z LOG : General f:1 st:1> line{i}" for i in range(5)
    )
    result_line = (
        "2026-09-08T12:28:01.613439139Z LOG  : Mod          f:1 st:503,316,043> "
        "CheckModsNeedUpdate: Mods updated"
    )

    def fake_logs(name, tail=250):
        calls["n"] += 1
        return (base if calls["n"] == 1 else base + "\n" + result_line), None

    monkeypatch.setattr(dockerlib, "container_logs", fake_logs)
    monkeypatch.setattr(ops, "rcon", lambda cmd, quiet=False: "Checking started.")
    monkeypatch.setattr(ops, "docker_ok_cached", lambda ttl=60: True)
    monkeypatch.setattr(ops, "is_running", lambda: True)
    monkeypatch.setattr(ops, "_mods_registry", lambda: ({}, {}))
    res = ops.check_mods_update(source="manual", timeout=10)
    assert res["state"] == "up-to-date"
    assert res["error"] is None
    assert res["at"]


def test_check_mods_update_inconclusive(monkeypatch):
    base = "2026-09-08T12:00:00.000000000Z LOG : General f:1 st:1> hi"
    monkeypatch.setattr(dockerlib, "container_logs", lambda name, tail=250: (base, None))
    monkeypatch.setattr(ops, "rcon", lambda cmd, quiet=False: "Checking started.")
    monkeypatch.setattr(ops, "docker_ok_cached", lambda ttl=60: True)
    monkeypatch.setattr(ops, "is_running", lambda: True)
    res = ops.check_mods_update(source="auto", timeout=3)
    assert res["state"] == "inconclusive"
    assert res["error"]


# ────────── авторестарт модов: защита от двойного рестарта ──────────


def test_restarted_since(monkeypatch):
    monkeypatch.setattr(ops, "container_state", lambda: {"running": True, "startedAt": "S2"})
    assert ops._restarted_since("S1") == "restarted"
    assert ops._restarted_since("S2") is None
    assert ops._restarted_since(None) is None
    monkeypatch.setattr(ops, "container_state", lambda: {"running": False, "startedAt": "S2"})
    assert ops._restarted_since("S1") == "stopped"


def test_do_restart_guard_aborts_on_manual_restart(monkeypatch):
    started = {"at": "S1"}
    flips = {"n": 0}

    def fake_broadcast(seconds, reason, abort_check=None):
        flips["n"] += 1
        started["at"] = f"S{flips['n'] + 1}"  # сервер перезапустили в момент отсчёта
        return True

    monkeypatch.setattr(ops, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(ops, "is_running", lambda: True)
    monkeypatch.setattr(
        ops, "container_state", lambda: {"running": True, "startedAt": started["at"]}
    )
    monkeypatch.setattr(ops, "rcon_warn_broadcast", fake_broadcast)
    monkeypatch.setattr(ops, "graceful_stop", lambda hook=None: "stopped")
    monkeypatch.setattr(dockerlib, "container_start", lambda name: (0, "", ""))
    monkeypatch.setattr(ops, "wait_until_running", lambda timeout=120: True)
    monkeypatch.setattr(ops, "wait_until_ready", lambda timeout=600: True)

    # без защиты рестарт доходит до конца, даже если сервер уже перезапустили
    assert ops._do_restart(600) != "aborted"
    assert flips["n"] == 1

    # с защитой: контейнер перезапущен вручную — авторестарт отменяется
    assert ops._do_restart(600, guard_restarted=True) == "aborted"
    assert flips["n"] == 2

    # с защитой: сервер остановлен вручную — тоже отменяемся
    monkeypatch.setattr(ops, "container_state", lambda: {"running": False, "startedAt": "S3"})
    assert ops._do_restart(600, guard_restarted=True) == "aborted"


@pytest.fixture
def mods_restart_env(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_ACTIVE",
        {
            "op": None,
            "phase": "",
            "message": "",
            "startedAt": None,
            "cancellable": False,
            "cancelRequested": False,
        },
    )
    monkeypatch.setattr(ops, "_OP_CANCEL", threading.Event())
    monkeypatch.setattr(ops, "_OP_HISTORY", deque())
    monkeypatch.setattr(ops, "is_running", lambda: True)
    monkeypatch.setattr(ops, "container_state", lambda: {"running": True, "startedAt": "S1"})
    monkeypatch.setattr(ops, "log_event", Mock())
    monkeypatch.setattr(ops, "rcon", Mock(return_value=""))
    stop = Mock(return_value="stopped")
    start = Mock(return_value=(0, "", ""))
    monkeypatch.setattr(ops, "graceful_stop", stop)
    monkeypatch.setattr(dockerlib, "container_start", start)
    monkeypatch.setattr(ops, "wait_until_running", Mock())
    monkeypatch.setattr(ops, "wait_until_ready", lambda timeout=600: True)
    threads = []
    original_thread = threading.Thread

    def record_thread(*args, **kwargs):
        thread = original_thread(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(ops.threading, "Thread", record_thread)
    yield stop, start, threads
    ops._OP_CANCEL.set()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()


@pytest.mark.parametrize("notify_fails", [False, True])
def test_cancel_mods_restart_wakes_countdown_and_preserves_server(
    mods_restart_env, monkeypatch, notify_fails
):
    stop, start, threads = mods_restart_env
    warned = threading.Event()
    messages = []

    def broadcast(command, quiet=False):
        messages.append(command)
        warned.set()
        if notify_fails and "cancelled" in command:
            raise rcon.RCONError("Нет связи")
        return ""

    monkeypatch.setattr(ops, "rcon", broadcast)
    ops.start_op(
        "mods-restart",
        lambda: ops._do_restart(600, "Обновление модов", guard_restarted=True, cancellable=True),
    )
    assert warned.wait(timeout=3)
    assert ops.op_state()["active"]["cancellable"]
    ops.cancel_mods_update()
    threads[0].join(timeout=3)
    assert not threads[0].is_alive(), "Отмена должна прервать ожидание десятисекундного шага"
    stop.assert_not_called()
    start.assert_not_called()
    assert any("cancelled" in message for message in messages)
    state = ops.op_state()
    assert state["active"] is None
    assert state["history"][0]["cancelled"] is True
    assert state["history"][0]["ok"] is True
    ops.log_event.assert_called_once_with("auto", "Автообновление модов отменено администратором")
    assert not ops._OP_CANCEL.is_set()
    # Отмена завершённой операции не должна влиять на следующий авторестарт.
    ops.start_op("mods-restart", lambda: ops._do_restart(0, cancellable=True))
    threads[-1].join(timeout=3)
    stop.assert_called_once()
    start.assert_called_once()
    assert not ops.op_state()["history"][0].get("cancelled")


@pytest.mark.parametrize("warn_seconds", [0, 10])
def test_cancel_mods_restart_before_stop(mods_restart_env, monkeypatch, warn_seconds):
    stop, start, threads = mods_restart_env
    ready = threading.Event()
    proceed = threading.Event()

    def worker():
        ready.set()
        assert proceed.wait(timeout=3)
        return ops._do_restart(warn_seconds, cancellable=True)

    # Отмена на последнем шаге предупреждения тоже должна предотвратить остановку.
    if warn_seconds:
        monkeypatch.setattr(
            ops, "rcon_warn_broadcast", lambda *args, **kwargs: ops.cancel_mods_update()
        )
    ops.start_op("mods-restart", worker)
    assert ready.wait(timeout=3)
    if not warn_seconds:
        ops.cancel_mods_update()
        ops.cancel_mods_update()  # повторный запрос безопасен
        assert ops.op_state()["active"]["cancelRequested"]
    proceed.set()
    threads[0].join(timeout=3)
    stop.assert_not_called()
    start.assert_not_called()
    assert ops.op_state()["history"][0]["cancelled"] is True


@pytest.mark.parametrize("operation", [None, "restart", "apply-mods-update", "backup"])
def test_cancel_mods_update_rejects_other_operations(mods_restart_env, operation):
    ops._ACTIVE.update(op=operation, cancellable=True)
    with pytest.raises(ops.OpsError, match="сейчас не выполняется"):
        ops.cancel_mods_update()
    assert not ops._OP_CANCEL.is_set()


def test_mods_restart_rejects_late_cancel_and_completes(mods_restart_env, monkeypatch):
    stop, start, threads = mods_restart_env

    def stopping(hook):
        assert ops.op_state()["active"]["cancellable"] is False
        with pytest.raises(ops.OpsError, match="уже останавливается"):
            ops.cancel_mods_update()
        return "stopped"

    stop.side_effect = stopping
    ops.start_op("mods-restart", lambda: ops._do_restart(0, cancellable=True))
    threads[0].join(timeout=3)
    stop.assert_called_once()
    start.assert_called_once()
    state = ops.op_state()
    assert state["active"] is None
    assert state["history"][0]["ok"] is True
    assert not state["history"][0].get("cancelled")


def test_scheduler_cancel_keeps_next_mods_check(mods_restart_env, monkeypatch):
    monkeypatch.setattr(configeditor, "auto_verify_running", Mock())
    stop, start, threads = mods_restart_env
    settings = {
        "autoUpdate": {"enabled": False},
        "modsUpdate": {
            "enabled": True,
            "restartOnUpdate": True,
            "intervalHours": 6,
            "warnSeconds": 600,
        },
        "nextModsCheck": 999,
    }
    monkeypatch.setattr(ops, "_SETTINGS", settings)
    monkeypatch.setattr(ops, "get_settings", lambda: settings.copy())
    monkeypatch.setattr(ops, "_save_settings", Mock())
    monkeypatch.setattr(ops, "_post_restart_rescan_tick", Mock())
    monkeypatch.setattr(ops, "_auto_backup_tick", Mock())
    check = Mock(return_value={"state": "needs-update"})
    monkeypatch.setattr(ops, "check_mods_update", check)
    monkeypatch.setattr(ops.time, "time", lambda: 1000)
    monkeypatch.setattr(ops.time, "sleep", Mock(side_effect=StopIteration))
    with pytest.raises(StopIteration):
        ops._scheduler_loop()
    ops.cancel_mods_update()
    threads[0].join(timeout=3)
    assert ops.op_state()["history"][0]["cancelled"] is True
    assert settings["nextModsCheck"] == 1000 + 6 * 3600
    ops._save_settings.assert_called_once()
    # Следующий тик не должен снова запускать только что отменённый рестарт.
    with pytest.raises(StopIteration):
        ops._scheduler_loop()
    check.assert_called_once_with(source="auto")
    assert len(threads) == 1
    stop.assert_not_called()
    start.assert_not_called()


def test_post_restart_rescan_resets_timer(monkeypatch, tmp_path):
    config.CFG["settings_file"] = str(tmp_path / "settings.json")
    config.CFG["events_file"] = str(tmp_path / "events.jsonl")
    ops._reset_post_restart_state()
    monkeypatch.setattr(ops, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(ops, "op_busy", lambda: False)
    monkeypatch.setattr(ops, "docker_ok_cached", lambda ttl=60: True)
    monkeypatch.setattr(ops, "container_state", lambda: {"running": True, "startedAt": "S1"})
    assert ops.patch_settings({"modsUpdate": {"enabled": True}}) is None

    # первый тик: фиксируем StartedAt, рескан не планируется
    ops._post_restart_rescan_tick(ops.get_settings())
    assert ops._POST_RESTART["startedAt"] == "S1"
    assert ops._POST_RESTART["dueAt"] is None

    # сервер перезапустился — рескан откладывается до загрузки
    monkeypatch.setattr(ops, "container_state", lambda: {"running": True, "startedAt": "S2"})
    ops._post_restart_rescan_tick(ops.get_settings())
    assert ops._POST_RESTART["dueAt"] is not None

    # время вышло, проверка: «актуально» — таймер сбрасывается на полный интервал
    ops._POST_RESTART["dueAt"] = time.time() - 1
    monkeypatch.setattr(ops, "is_running", lambda: True)
    monkeypatch.setattr(
        ops, "check_mods_update", lambda source="manual", timeout=45: {"state": "up-to-date"}
    )
    before = time.time()
    ops._post_restart_rescan_tick(ops.get_settings())
    assert ops._POST_RESTART["dueAt"] is None
    nxt = ops._SETTINGS["nextModsCheck"]
    assert 21600 - 10 <= nxt - before <= 21600 + 120

    # обновления ещё нужны — таймер не трогаем
    ops._POST_RESTART["dueAt"] = time.time() - 1
    ops._SETTINGS["nextModsCheck"] = 12345.0
    monkeypatch.setattr(
        ops, "check_mods_update", lambda source="manual", timeout=45: {"state": "needs-update"}
    )
    ops._post_restart_rescan_tick(ops.get_settings())
    assert ops._SETTINGS["nextModsCheck"] == 12345.0
    assert ops._POST_RESTART["dueAt"] is None

    # RCON ещё не поднялся — повтор; после лимита — отказ и сброс состояния
    def boom(source="manual", timeout=45):
        raise RuntimeError("RCON down")

    monkeypatch.setattr(ops, "check_mods_update", boom)
    ops._POST_RESTART["dueAt"] = time.time() - 1
    ops._post_restart_rescan_tick(ops.get_settings())
    assert ops._POST_RESTART["tries"] == 1
    assert ops._POST_RESTART["dueAt"] > time.time()
    ops._POST_RESTART["tries"] = ops._RESCAN_RETRIES
    ops._POST_RESTART["dueAt"] = time.time() - 1
    ops._post_restart_rescan_tick(ops.get_settings())
    assert ops._POST_RESTART["dueAt"] is None and ops._POST_RESTART["tries"] == 0


def test_post_restart_rescan_disabled_clears_state(monkeypatch):
    ops._POST_RESTART.update({"startedAt": "S1", "dueAt": 1.0, "tries": 2})
    monkeypatch.setattr(ops, "log_event", lambda *a, **k: None)
    ops._post_restart_rescan_tick({"modsUpdate": {"enabled": False}})
    assert ops._POST_RESTART == {"startedAt": None, "dueAt": None, "tries": 0}


# ─────────────── остановка / рестарт: restart policy ───────────────


def _run_state(**kwargs):
    st = {"status": "running", "running": True, "startedAt": "S1", "image": "img"}
    st.update(kwargs)
    return st


def test_graceful_stop_disables_restart_policy(monkeypatch):
    """Политика рестарта глушится до quit и возвращается после остановки."""
    calls = []
    st = _run_state()
    monkeypatch.setattr(ops, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(ops, "container_state", lambda: st)

    def fake_set(name, policy):
        calls.append(("set", policy))
        return True

    def fake_rcon(cmd, quiet=False):
        calls.append(("rcon", cmd))
        st.update({"status": "exited", "running": False})
        return "ok"

    monkeypatch.setattr(dockerlib, "get_restart_policy", lambda name: "unless-stopped")
    monkeypatch.setattr(dockerlib, "set_restart_policy", fake_set)
    monkeypatch.setattr(ops, "rcon", fake_rcon)
    assert ops.graceful_stop() == "stopped"
    assert calls == [("set", "no"), ("rcon", "quit"), ("set", "unless-stopped")]


def test_graceful_stop_resurrected_by_docker(monkeypatch):
    """Если policy заглушить не удалось и docker поднял контейнер заново
    (StartedAt сменился) — graceful_stop это распознаёт, не виснет."""
    st = _run_state()
    monkeypatch.setattr(ops, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(ops, "container_state", lambda: st)
    monkeypatch.setattr(dockerlib, "get_restart_policy", lambda name: "always")
    monkeypatch.setattr(dockerlib, "set_restart_policy", lambda name, policy: False)
    stops = []
    monkeypatch.setattr(dockerlib, "container_stop", lambda name, seconds=180: stops.append(name))

    def quit_and_resurrect(cmd, quiet=False):
        st.update({"status": "running", "running": True, "startedAt": "S2"})
        return "ok"

    monkeypatch.setattr(ops, "rcon", quit_and_resurrect)
    assert ops.graceful_stop() == "resurrected"
    assert stops == []


def test_graceful_stop_force_stop_when_quit_ignored(monkeypatch):
    """Без политики: quit не сработал → docker stop, политика не трогается."""
    calls = []
    st = _run_state()
    monkeypatch.setattr(ops, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(ops, "container_state", lambda: st)
    monkeypatch.setattr(dockerlib, "get_restart_policy", lambda name: "no")
    monkeypatch.setattr(
        dockerlib, "set_restart_policy", lambda name, policy: calls.append(("set", policy)) or True
    )

    def fail_quit(cmd, quiet=False):
        raise rcon.RCONError("нет ответа")

    monkeypatch.setattr(ops, "rcon", fail_quit)

    def force_stop(name, seconds=180):
        calls.append(("stop", name))
        st.update({"status": "exited", "running": False})
        return 0, name, ""

    monkeypatch.setattr(dockerlib, "container_stop", force_stop)
    monkeypatch.setattr(ops.time, "sleep", lambda s: None)
    assert ops.graceful_stop() == "stopped"
    assert calls == [("stop", "pzserver")]


# ─────────────────────── telegram-уведомления ───────────────────────


def test_telegram_settings_mask_and_patch(tmp_path):
    """Токен сохраняется на сервере, наружу уходит маской; пустое поле не затирает."""
    config.CFG["settings_file"] = str(tmp_path / "settings.json")
    config.CFG["events_file"] = str(tmp_path / "events.jsonl")
    ops._SETTINGS["telegram"].update({"enabled": False, "botToken": "", "chatId": ""})
    assert (
        ops.patch_settings(
            {
                "telegram": {
                    "enabled": True,
                    "botToken": "  123456:ABC-DEF1234  ",
                    "chatId": " -100123 ",
                }
            }
        )
        is None
    )
    st = ops.get_settings()["telegram"]
    assert st["enabled"] is True and st["chatId"] == "-100123"
    assert st["botToken"] == "" and st["botTokenMasked"] == "•••1234"
    assert "ABC-DEF1234" not in json.dumps(ops.get_settings())
    # пустое/маскированное значение не затирает сохранённый токен
    assert ops.patch_settings({"telegram": {"botToken": "", "chatId": "-100123"}}) is None
    assert ops.get_settings()["telegram"]["botTokenMasked"] == "•••1234"
    assert (
        ops.patch_settings({"telegram": {"enabled": "yes"}})
        == "telegram.enabled должен быть true/false"
    )
    assert ops.patch_settings({"telegram": {"groups": {"ops": False, "мусор": True}}}) is None
    assert ops.get_settings()["telegram"]["groups"] == {"ops": False}


def test_telegram_enqueue_filters(monkeypatch):
    """Группы подписки фильтруют события; сообщение без полного токена."""
    import notify

    sent = []
    monkeypatch.setattr(
        ops,
        "telegram_settings_raw",
        lambda: {
            "enabled": True,
            "botToken": "123456:SECRET",
            "chatId": "42",
            "groups": {"ops": True, "backup": False, "update": True, "problems": True},
        },
    )
    monkeypatch.setattr(notify._QUEUE, "put_nowait", lambda m: sent.append(m))
    notify.enqueue("restart", "Сервер перезапущен")  # ops → в очередь
    notify.enqueue("backup", "Бэкап создан")  # backup → выключен
    notify.enqueue("error", "Бэкап не удался")  # problems → в очередь
    notify.enqueue("console", "команда")  # без группы → мимо
    assert len(sent) == 2
    assert sent[0].startswith("🔄") and sent[0].endswith("Server restarted")
    assert sent[1].startswith("❌")
    # выключено → ничего
    monkeypatch.setattr(ops, "telegram_settings_raw", lambda: {"enabled": False})
    notify.enqueue("restart", "x")
    assert len(sent) == 2
    # формат: значок + имя сервера
    config.CFG["server_name"] = "TestPZ"
    assert notify._format("start", "up") == "▶️ [TestPZ] up"
    config.CFG["server_name"] = "Project Zomboid"


def test_telegram_send_and_test(monkeypatch):
    """sendMessage уходит с токеном из настроек; ответ API уважается; тест-кнопка."""
    import notify

    calls = []
    monkeypatch.setattr(
        ops,
        "telegram_settings_raw",
        lambda: {
            "enabled": True,
            "botToken": "123456:SECRET",
            "chatId": "42",
            "groups": {"ops": True, "backup": True, "update": True, "problems": True},
        },
    )

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"ok": True, "result": {}}).encode()

    def fake_urlopen(req, timeout=10):
        calls.append((req.full_url, json.loads(req.data.decode())))
        return FakeResp()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    ok, err = notify.send_message("привет")
    assert ok is True and err is None and len(calls) == 1
    url, body = calls[0]
    assert url == "https://api.telegram.org/bot123456:SECRET/sendMessage"
    assert body == {"chat_id": "42", "text": "привет"}
    ok, err = notify.test_message()
    assert ok is True and "connection test" in calls[-1][1]["text"]
    # без настроек — честная ошибка, без сетевого вызова
    monkeypatch.setattr(ops, "telegram_settings_raw", lambda: {})
    ok, err = notify.send_message("x")
    assert ok is False and err and len(calls) == 2


def test_telegram_send_uses_saved_token_not_mask(monkeypatch):
    """Регресс V20.1: notify читал get_settings(), который отдаёт токен маской
    и пустым botToken (защита от утечки в API) — отправка считала сохранённый
    токен незаданным, и «Проверить» всегда писала «не задан токен бота или chat id».
    Теперь отправка берёт telegram_settings_raw(): наружу маска, внутрь — токен."""
    import notify

    snap = json.loads(json.dumps(ops._SETTINGS))
    calls = []

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"ok": True}).encode()

    def fake_urlopen(req, timeout=10):
        calls.append(req.full_url)
        return FakeResp()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    try:
        ops._SETTINGS["telegram"].update(
            {
                "enabled": True,
                "botToken": "123456:REAL-TOKEN",
                "chatId": "-10042",
                "groups": {"ops": True, "backup": True, "update": True, "problems": True},
            }
        )
        # наружу токен по-прежнему не уходит
        assert ops.get_settings()["telegram"]["botToken"] == ""
        assert ops.get_settings()["telegram"]["botTokenMasked"] == "•••OKEN"
        # ...но отправка и проверка связи используют настоящий токен
        ok, err = notify.send_message("привет")
        assert ok is True and err is None
        assert calls == ["https://api.telegram.org/bot123456:REAL-TOKEN/sendMessage"]
        ok, err = notify.test_message()
        assert ok is True and len(calls) == 2
        # очередь уведомлений тоже видит настоящий токен: событие попадает в очередь
        queued = []
        monkeypatch.setattr(notify._QUEUE, "put_nowait", lambda m: queued.append(m))
        notify.enqueue("error", "сбой бэкапа")
        assert len(queued) == 1 and "сбой бэкапа" in queued[0]
    finally:
        ops._SETTINGS.clear()
        ops._SETTINGS.update(snap)


# ─────────────────────── watchdog: пробы RCON ───────────────────────


def _wd_reset():
    ops._WD.update(
        {
            "lastProbeAt": None,
            "lastResult": None,
            "lastError": None,
            "consecutiveFailures": 0,
            "alerted": False,
            "lastRestartAt": None,
        }
    )


def test_watchdog_skips_when_stopped_or_busy(monkeypatch):
    """Остановленный контейнер и занятый пульт — не зависание: счётчик сбрасывается."""
    _wd_reset()
    events = []
    monkeypatch.setattr(ops, "log_event", lambda *a, **k: events.append(a))
    monkeypatch.setattr(ops, "op_busy", lambda: False)
    monkeypatch.setattr(ops, "docker_ok_cached", lambda ttl=60: True)
    monkeypatch.setattr(
        ops,
        "container_state",
        lambda: {"status": "exited", "running": False, "startedAt": "S1", "image": "img"},
    )
    probes = []
    monkeypatch.setattr(ops.rconlib, "run_command", lambda *a, **k: probes.append(a))
    ops._watchdog_probe({"enabled": True, "thresholdMin": 1, "autoRestart": True})
    assert ops._WD["lastResult"] == "skipped"
    assert ops._WD["consecutiveFailures"] == 0 and not probes

    monkeypatch.setattr(ops, "op_busy", lambda: True)  # идёт операция
    monkeypatch.setattr(
        ops,
        "container_state",
        lambda: {"status": "running", "running": True, "startedAt": "S1", "image": "img"},
    )
    ops._watchdog_probe({"enabled": True, "thresholdMin": 1, "autoRestart": True})
    assert ops._WD["lastResult"] == "skipped" and not probes


def test_watchdog_alert_and_autorestart(monkeypatch):
    """Молчание дольше порога → событие и один авторестарт; кулдаун держит."""
    _wd_reset()
    events, restarts = [], []
    monkeypatch.setattr(ops, "log_event", lambda *a, **k: events.append(a))
    monkeypatch.setattr(ops, "op_busy", lambda: False)
    monkeypatch.setattr(ops, "docker_ok_cached", lambda ttl=60: True)
    monkeypatch.setattr(
        ops,
        "container_state",
        lambda: {"status": "running", "running": True, "startedAt": "S1", "image": "img"},
    )
    monkeypatch.setattr(ops, "is_running", lambda: True)
    monkeypatch.setattr(ops, "start_op", lambda op, fn: restarts.append(op))

    def dead_rcon(*a, **k):
        raise rcon.RCONError("таймаут")

    monkeypatch.setattr(ops.rconlib, "run_command", dead_rcon)
    wd = {"enabled": True, "thresholdMin": 1, "autoRestart": True}
    for _ in range(2):  # 2 × 30 с = 1 мин порога
        ops._watchdog_probe(wd)
    assert ops._WD["consecutiveFailures"] == 2 and restarts == ["restart"]
    # ещё десять проб — рестарт не повторяется (кулдаун и alerted)
    for _ in range(10):
        ops._watchdog_probe(wd)
    assert restarts == ["restart"]
    # успешная проба всё сбрасывает
    monkeypatch.setattr(ops.rconlib, "run_command", lambda *a, **k: "Дмитрий\n")
    ops._watchdog_probe(wd)
    assert ops._WD["lastResult"] == "ok" and ops._WD["consecutiveFailures"] == 0
    assert not ops._WD["alerted"]


# ─────────────────────── управление модами ───────────────────────

INI = """[General]
ServerName=test

[Mods]
Mods=tsarslib;commonpackage
WorkshopItems=2694464646;2804001857

[Other]
Key=1
"""


def test_ini_edits_preserve_unrelated_content():
    """Exercise the live lossless editor rather than the obsolete ops helper."""
    from configformats import edit_ini

    out = edit_ini(INI, {"WorkshopItems": "111"})
    assert "WorkshopItems=111\n" in out and "[Other]" in out
    out = edit_ini(INI, {"Mods": "a;b"})
    assert "Mods=a;b\n" in out and "Mods=" in out
    out = edit_ini(INI, {"ClientMods": "x"})
    assert out.rstrip().endswith("ClientMods=x")
    # многострочное значение (перенос с отступом) глотается целиком
    text = "Mods=aaa;\n  bbb;\n  ccc\nWorkshopItems=1\n"
    out = edit_ini(text, {"Mods": "one"})
    assert out == "Mods=one\nWorkshopItems=1\n"


def test_mods_toggle_blocked_without_mapping(tmp_path, monkeypatch):
    """Unknown metadata cannot activate a guessed ModID."""
    server_dir = tmp_path / "Server"
    server_dir.mkdir()
    (server_dir / "servertest.ini").write_text(INI, encoding="utf-8")
    monkeypatch.setitem(config.CFG, "data_dir", str(tmp_path))
    monkeypatch.setitem(config.CFG, "dashboard_dir", str(tmp_path / "panel"))
    monkeypatch.setattr(ops, "is_running", lambda: False)
    monkeypatch.setattr(ops, "op_busy", lambda: False)
    monkeypatch.setattr(workshop, "scan", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        configeditor,
        "context",
        lambda refresh=False: {
            "activeFile": "servertest.ini",
            "version": "42.15",
            "versionKnown": True,
            "owners": {},
            "generatesSettings": False,
        },
    )
    with pytest.raises(ops.OpsError):
        configeditor.legacy_toggle(
            {"file": "servertest.ini", "workshopId": "999999", "enable": True}
        )
    assert (server_dir / "servertest.ini").read_text(encoding="utf-8") == INI


# ─────────────────────── регресс: SSE-поток ───────────────────────


def test_sse_stream_serves_data(monkeypatch, authenticated_admin):
    """/api/stream должен реально слать кадры данных.

    Регресс: в обработчике было обращение к несуществующему self.STREAM_PLAN —
    поток падал сразу после retry-кадра, клиент бесконечно переподключался."""
    import app

    monkeypatch.setattr(app.Handler, "log_message", lambda *a, **k: None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    srv.auth, headers = authenticated_admin
    srv.daemon_threads = True
    th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    th.start()
    try:
        port = srv.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=6)
        conn.request("GET", "/api/stream", headers=headers)
        resp = conn.getresponse()
        assert resp.status == 200
        buf = b""
        deadline = ops.time.time() + 5
        while b"event: ops" not in buf and ops.time.time() < deadline:
            chunk = resp.read1(512)
            if not chunk:
                break  # соединение закрыто сервером — падение обработчика
            buf += chunk
        assert b"event: ops" in buf, "SSE закрылся до первого кадра данных"
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()


# ─────────────────────── регресс: бэкофф поиска модов ───────────────────────


def test_workshop_exec_backoff(monkeypatch):
    """Empty metadata scans are cached instead of repeatedly searching a container."""
    workshop.invalidate()
    calls = Mock(return_value={})
    monkeypatch.setattr(workshop, "local_files", lambda items: {})
    monkeypatch.setattr(workshop, "container_files", calls)
    assert workshop.scan(["123"], "42.15") == {"123": []}
    assert workshop.scan(["123"], "42.15") == {"123": []}
    calls.assert_called_once()


# ─────────────────────── регресс: бэкап с остановкой ───────────────────────


def test_backup_with_stop_aborts_on_resurrect(tmp_path, monkeypatch):
    """Если docker сам перезапустил контейнер посреди остановки — бэкап
    прерывается с ошибкой, а не снимает архив с полуживого мира."""
    config.CFG["backup_dir"] = str(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    (data / "world").mkdir()
    monkeypatch.setattr(ops, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(ops, "_set_phase", lambda *a, **k: None)
    monkeypatch.setattr(ops, "is_running", lambda: True)
    monkeypatch.setattr(ops, "rcon_warn_broadcast", lambda s, r: True)
    monkeypatch.setattr(ops, "graceful_stop", lambda hook=None: "resurrected")
    with pytest.raises(ops.OpsError):
        ops._do_backup(True)


# ─────────────────────── регресс: отложенная автопроверка ───────────────────────


def test_defer_next_check_saves(tmp_path):
    """defer_next_check пишет в основной словарь и сохраняет файл —
    раньше «Обновить сейчас» мутировало копию настроек, и откат автопроверки терялся."""
    config.CFG["settings_file"] = str(tmp_path / "settings.json")
    config.CFG["events_file"] = str(tmp_path / "events.jsonl")
    ops._SETTINGS["nextCheck"] = None
    snap = json.loads(json.dumps(ops._SETTINGS))
    try:
        ops.defer_next_check(6)
        now = ops.time.time()
        delta = ops._SETTINGS["nextCheck"] - now
        assert 5 * 3600 < delta <= 6 * 3600
        saved = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
        assert abs(saved["nextCheck"] - ops._SETTINGS["nextCheck"]) < 1
    finally:
        ops._SETTINGS.clear()
        ops._SETTINGS.update(snap)


# ─────────────────────── регресс: клампы settings.json ───────────────────────


def test_load_settings_clamps(tmp_path):
    """Значения из старого/ручного settings.json клампятся, как в patch_settings:
    интервал 0 иначе превратил бы планировщик в проверки каждые 20 с."""
    config.CFG["settings_file"] = str(tmp_path / "settings.json")
    config.CFG["events_file"] = str(tmp_path / "events.jsonl")
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "autoUpdate": {"intervalHours": 0, "warnSeconds": 99999},
                "modsUpdate": {"intervalHours": -5},
                "watchdog": {"thresholdMin": 1000},
                "backup": {"maxBackups": -1},
            }
        ),
        encoding="utf-8",
    )
    snap = json.loads(json.dumps(ops._SETTINGS))
    try:
        ops._load_settings()
        s = ops.get_settings()
        assert s["autoUpdate"]["intervalHours"] == 1
        assert s["autoUpdate"]["warnSeconds"] == 3600
        assert s["modsUpdate"]["intervalHours"] == 1
        assert s["watchdog"]["thresholdMin"] == 60
        assert s["backup"]["maxBackups"] == 0
    finally:
        ops._SETTINGS.clear()
        ops._SETTINGS.update(snap)


# ─────────────────────── регресс: маскирование токена ───────────────────────


def test_notify_error_masks_token(monkeypatch):
    """Ошибка отправки не должна уносить токен наружу (UI, события)."""
    import notify

    monkeypatch.setattr(
        ops,
        "telegram_settings_raw",
        lambda: {"enabled": True, "botToken": "123456:SECRET", "chatId": "42"},
    )

    def boom(req, timeout=10):
        raise OSError("connection failed for bot123456:SECRET (https url)")

    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    ok, err = notify.send_message("x")
    assert ok is False and err
    assert "123456:SECRET" not in err
    assert "•••" in err


# ─────────────── регресс V19: восстановление при resurrected ───────────────


def test_notify_error_surfaces_telegram_description(monkeypatch):
    """Регресс V20.3: HTTP-ошибка Telegram (400/401) показывалась безликим
    «HTTP Error 400: Bad Request», хотя в теле ответа Telegram пишет настоящую
    причину («chat not found» — бот не запущен/не добавлен в чат)."""
    import notify

    def boom(req, timeout=10):
        raise urllib.error.HTTPError(
            req.full_url,
            400,
            "Bad Request",
            {"Content-Type": "application/json"},
            io.BytesIO(
                json.dumps(
                    {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
                ).encode()
            ),
        )

    monkeypatch.setattr(
        ops,
        "telegram_settings_raw",
        lambda: {"enabled": True, "botToken": "123456:SECRET", "chatId": "42"},
    )
    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    ok, err = notify.send_message("x")
    assert ok is False
    assert "chat not found" in err
    assert "SECRET" not in err
    # «Проверить» добавляет к ошибке сохранённый chat id — видно, что лежит в настройках
    ok, err = notify.test_message()
    assert ok is False and "chat not found" in err and "chat id: 42" in err

    # тело не-json/пустое → хотя бы безликая ошибка, без падения
    def boom_raw(req, timeout=10):
        raise urllib.error.HTTPError(req.full_url, 500, "Internal Error", {}, io.BytesIO(b""))

    monkeypatch.setattr(notify.urllib.request, "urlopen", boom_raw)
    ok, err = notify.send_message("x")
    assert ok is False and err


def test_notify_fetch_recent_chats(monkeypatch):
    """Кнопка «Найти чаты бота»: getUpdates → уникальные чаты с настоящими id
    (в т.ч. супергруппы с -100, которые из ссылок копируют без префикса);
    ошибки Telegram уходят с описанием, токен маскируется."""
    import notify

    class FakeResp:
        def __init__(self, payload):
            self._p = payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(self._p).encode()

    updates = [
        {"message": {"chat": {"id": -1001234567890, "title": "Тест-группа", "type": "supergroup"}}},
        {
            "channel_post": {
                "chat": {"id": -1001234567890, "title": "Тест-группа", "type": "supergroup"}
            }
        },
        {"message": {"chat": {"id": 42, "first_name": "Иван", "type": "private"}}},
        {
            "my_chat_member": {
                "chat": {"id": -1009876543210, "title": "Вторая", "type": "supergroup"}
            }
        },
        {"callback_query": {"id": "x"}},  # без чата — пропускается
    ]
    seen = {}

    def fake_urlopen(req, timeout=10):
        seen["url"] = req.full_url
        return FakeResp({"ok": True, "result": updates})

    monkeypatch.setattr(
        ops,
        "telegram_settings_raw",
        lambda: {"enabled": True, "botToken": "123456:SECRET", "chatId": ""},
    )
    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    chats, err = notify.fetch_recent_chats()
    assert err is None
    assert seen["url"] == "https://api.telegram.org/bot123456:SECRET/getUpdates?limit=100"
    assert [c["id"] for c in chats] == ["-1001234567890", "-1009876543210", "42"]
    assert chats[0]["title"] == "Тест-группа" and chats[1]["title"] == "Вторая"
    assert chats[2]["title"] == "Иван" and chats[2]["type"] == "private"
    # нет токена — честная ошибка без сети
    monkeypatch.setattr(
        ops, "telegram_settings_raw", lambda: {"enabled": True, "botToken": "", "chatId": ""}
    )
    chats, err = notify.fetch_recent_chats()
    assert chats is None and "не задан токен" in err
    # ошибка Telegram с описанием и маской токена
    monkeypatch.setattr(
        ops,
        "telegram_settings_raw",
        lambda: {"enabled": True, "botToken": "123456:SECRET", "chatId": ""},
    )

    def boom(req, timeout=10):
        raise urllib.error.HTTPError(
            req.full_url,
            409,
            "Conflict",
            {},
            io.BytesIO(
                json.dumps(
                    {
                        "ok": False,
                        "error_code": 409,
                        "description": "Conflict: terminated by other getUpdates request",
                    }
                ).encode()
            ),
        )

    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    chats, err = notify.fetch_recent_chats()
    assert chats is None and "Conflict" in err and "SECRET" not in err


def test_telegram_chats_route_on_get(monkeypatch, authenticated_admin):
    """Регресс V20.4: маршрут /api/telegram-chats был объявлен в do_POST,
    а интерфейс дергает его GET'ом — «Нет такого маршрута». Теперь GET работает."""
    import app

    monkeypatch.setattr(app.Handler, "log_message", lambda *a, **k: None)
    monkeypatch.setattr(
        ops, "telegram_settings_raw", lambda: {"enabled": True, "botToken": "", "chatId": ""}
    )
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    srv.auth, headers = authenticated_admin
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    try:
        port = srv.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/telegram-chats", headers=headers)
        resp = conn.getresponse()
        assert resp.status == 200
        body = json.loads(resp.read().decode("utf-8"))
        assert body["ok"] is False and "токен" in body["error"]
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()


def test_restore_aborts_on_resurrect(tmp_path, monkeypatch):
    """Восстановление прерывается, если docker сам поднял контейнер посреди
    остановки. Регресс: _do_restore игнорировал результат graceful_stop —
    каталог данных очищался под живым сервером (потеря мира)."""
    config.CFG["backup_dir"] = str(tmp_path)
    data = tmp_path / "data"
    (data / "world").mkdir(parents=True)
    (data / "keep.txt").write_text("x", encoding="utf-8")
    config.CFG["data_dir"] = str(data)
    bak = tmp_path / "pz-backup-20260909-120000.tar.gz"
    with tarfile.open(bak, "w:gz") as archive:
        archive.add(data / "keep.txt", arcname="keep.txt")
    monkeypatch.setitem(config.CFG, "dashboard_dir", str(tmp_path / "dd"))
    wiped = []
    monkeypatch.setattr(ops, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(ops, "_set_phase", lambda *a, **k: None)
    monkeypatch.setattr(ops, "is_running", lambda: True)
    monkeypatch.setattr(ops, "rcon_warn_broadcast", lambda s, r: True)
    stop = Mock(return_value="resurrected")
    monkeypatch.setattr(ops, "graceful_stop", stop)
    original_rmtree = ops.shutil.rmtree

    def track_rmtree(path, **kwargs):
        wiped.append(str(path))
        return original_rmtree(path, **kwargs)

    monkeypatch.setattr(ops.shutil, "rmtree", track_rmtree)
    with pytest.raises(ops.OpsError, match="Docker сам перезапустил"):
        ops._do_restore(bak.name)
    stop.assert_called_once()
    assert not any(Path(path).is_relative_to(data) for path in wiped)
    assert (data / "keep.txt").exists(), "файлы мира не должны удаляться"


# ─────────────── регресс V19: спам rcon-error ───────────────


def test_rcon_error_events_throttled(monkeypatch):
    """RCON недоступен: событие пишется на смену состояния и раз в 5 минут,
    а не на каждый опрос (SSE players каждые 5 с заливал журнал и Telegram)."""
    events = []
    monkeypatch.setattr(ops, "log_event", lambda kind, text, **k: events.append(kind))

    def dead(*a, **k):
        raise rcon.RCONError("нет ответа")

    monkeypatch.setattr(ops.rconlib, "run_command", dead)
    ops._RCON_CACHE.update({"state": "ok", "error": None, "at": None})
    ops._RCON_LOG["at"] = 0.0
    try:
        for _ in range(12):  # минута опросов каждые 5 с
            with pytest.raises(rcon.RCONError):
                ops.rcon("players")
        assert len(events) == 1, "на постоянный сбой — одна запись, не 12"
        ops._RCON_LOG["at"] -= 301  # прошло 5 минут тишины
        with pytest.raises(rcon.RCONError):
            ops.rcon("players")
        assert len(events) == 2
    finally:
        ops._RCON_CACHE.update({"state": "unknown", "error": None, "at": None})
        ops._RCON_LOG["at"] = 0.0


# ─────────────── регресс V19: нечисловые значения в настройках ───────────────


def test_patch_settings_non_numeric_is_error(tmp_path):
    """POST /api/settings с нечисловым интервалом раньше ронял обработчик
    ValueError'ом (соединение рвалось) — теперь это валидационная ошибка."""
    config.CFG["settings_file"] = str(tmp_path / "settings.json")
    config.CFG["events_file"] = str(tmp_path / "events.jsonl")
    assert ops.patch_settings({"autoUpdate": {"intervalHours": "abc"}}) is not None
    assert ops.patch_settings({"modsUpdate": {"warnSeconds": [1]}}) is not None
    assert ops.patch_settings({"watchdog": {"thresholdMin": True}}) is not None
    assert ops.patch_settings({"backup": {"maxBackups": "много"}}) is not None
    # числовая строка по-прежнему допустима, настройки не изменились
    assert ops.patch_settings({"autoUpdate": {"intervalHours": "6"}}) is None
    assert ops.get_settings()["autoUpdate"]["intervalHours"] == 6


def test_load_settings_nextcheck_type_guard(tmp_path):
    """Строковый nextCheck в settings.json раньше ронял планировщик TypeError
    каждые 20 с (спам «Планировщик: …»). Нечисловые метки сбрасываются в None."""
    config.CFG["settings_file"] = str(tmp_path / "settings.json")
    config.CFG["events_file"] = str(tmp_path / "events.jsonl")
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "nextCheck": "скоро",
                "nextModsCheck": [1],
                "autoUpdate": {"enabled": True},
            }
        ),
        encoding="utf-8",
    )
    snap = json.loads(json.dumps(ops._SETTINGS))
    try:
        ops._load_settings()
        assert ops._SETTINGS["nextCheck"] is None
        assert ops._SETTINGS["nextModsCheck"] is None
        # валидное число сохраняется; true/false — не метка
        (tmp_path / "settings.json").write_text(
            json.dumps({"nextCheck": 123.5, "nextModsCheck": True}), encoding="utf-8"
        )
        ops._load_settings()
        assert ops._SETTINGS["nextCheck"] == 123.5
        assert ops._SETTINGS["nextModsCheck"] is None
    finally:
        ops._SETTINGS.clear()
        ops._SETTINGS.update(snap)


# ─────────────── регресс V19: предупреждение не кратно 10 с ───────────────


def test_warn_broadcast_non_multiple_of_ten(monkeypatch):
    """Отсчёт 45 с раньше не отправлял ни одного сообщения (шаг 10 не попадал
    в пороги {30, 10, 60…}) — сервер останавливался молча."""
    sent = []
    monkeypatch.setattr(ops, "player_notification_language", lambda: "ru")
    monkeypatch.setattr(ops, "rcon", lambda cmd, quiet=False: sent.append(cmd) or "")
    monkeypatch.setattr(ops.time, "sleep", lambda s: None)
    assert ops.rcon_warn_broadcast(45, "Стоп") is True
    assert any("30 сек" in s for s in sent) and any("10 сек" in s for s in sent)
    # кратные значения не сломались
    sent.clear()
    ops.rcon_warn_broadcast(90, "Стоп")
    assert any("1 мин" in s for s in sent) and any("30 сек" in s for s in sent)
    # совсем короткий отсчёт тоже слышен
    sent.clear()
    ops.rcon_warn_broadcast(5, "Стоп")
    assert any("5 сек" in s for s in sent)


# ─────────────── регресс V19: кэш compose version ───────────────


def test_compose_version_cached(monkeypatch):
    """compose_ok_cached: docker compose version не гоняется на каждом
    SSE-кадре overview (каждые 3 с), а кэшируется как docker_ok_cached."""
    calls = {"n": 0}

    def fake():
        calls["n"] += 1
        return True

    monkeypatch.setattr(dockerlib, "compose_version", fake)
    ops._COMPOSE_OK.update({"ok": None, "at": 0.0})
    try:
        assert ops.compose_ok_cached() is True
        assert ops.compose_ok_cached() is True
        assert calls["n"] == 1
    finally:
        ops._COMPOSE_OK.update({"ok": None, "at": 0.0})


# ─────────────── регресс V19: таймаут tar ───────────────


def test_backup_tar_timeout_readable(tmp_path, monkeypatch):
    """Зависший tar даёт понятную OpsError, а не «Внутренняя ошибка:
    Command … timed out» из общего перехватчика."""
    import subprocess as sp

    config.CFG["backup_dir"] = str(tmp_path)
    data = tmp_path / "data"
    (data / "w").mkdir(parents=True)
    config.CFG["data_dir"] = str(data)
    monkeypatch.setattr(ops, "_set_phase", lambda *a, **k: None)
    monkeypatch.setattr(ops, "is_running", lambda: False)

    def boom(*a, **k):
        raise sp.TimeoutExpired(cmd="tar", timeout=2400)

    monkeypatch.setattr(ops.subprocess, "run", boom)
    with pytest.raises(ops.OpsError) as ei:
        ops._do_backup(False)
    assert "2400" in str(ei.value)


# ─────────────── регресс V19: stats-кадр при остановленном сервере ───────────────


def test_stats_payload_error_is_ok_false(monkeypatch):
    """Кадр stats без контейнера — ok:false + error (интерфейс показывает
    состояние ошибки), а не ok:true с фиктивными нулями."""
    import payloads

    monkeypatch.setattr(ops, "container_state", lambda: None)
    data = payloads.stats_payload()
    assert data["ok"] is False and "error" in data
    # живые данные не сломались
    monkeypatch.setattr(
        ops,
        "container_state",
        lambda: {"status": "running", "running": True, "startedAt": None, "image": "img"},
    )
    monkeypatch.setattr(
        dockerlib,
        "container_stats",
        lambda name: {
            "cpuPct": 1.0,
            "memUsed": 1,
            "memLimit": 2,
            "memPct": 1,
            "netIn": 1,
            "netOut": 1,
            "pids": 1,
        },
    )
    data = payloads.stats_payload()
    assert data["ok"] is True and data["cpuPct"] == 1.0


# ─────────────── регресс V19: скачивание исчезнувшего бэкапа ───────────────


def test_backup_download_missing_file_404(monkeypatch, authenticated_admin):
    """Файл удалён prune'ом между проверкой и чтением → честный 404 JSON,
    а не необработанное исключение и разрыв соединения."""
    import app

    monkeypatch.setattr(app.Handler, "log_message", lambda *a, **k: None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    srv.auth, headers = authenticated_admin
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    try:
        monkeypatch.setattr(ops, "backup_download_path", lambda name: "/несуществующий/путь.tar.gz")
        port = srv.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/backup/download?name=x.tar.gz", headers=headers)
        resp = conn.getresponse()
        assert resp.status == 404
        body = json.loads(resp.read().decode("utf-8"))
        assert body["ok"] is False
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()


# ─────────────────────── бэкапы по расписанию (V20) ───────────────────────


def _bk_fixture(tmp_path, monkeypatch):
    """Обвязка: временные каталоги данных/бэкапов/журнала, тихие события, сервер «стоит»."""
    config.CFG["backup_dir"] = str(tmp_path / "backups")
    config.CFG["data_dir"] = str(tmp_path / "data")
    config.CFG["dashboard_dir"] = str(tmp_path / "dd")
    config.CFG["settings_file"] = str(tmp_path / "dd" / "settings.json")
    config.CFG["events_file"] = str(tmp_path / "dd" / "events.jsonl")
    data = tmp_path / "data"
    (data / "Server").mkdir(parents=True)
    (data / "Maps" / "TestMap").mkdir(parents=True)
    (data / "Server" / "test.ini").write_text("Mods=\n", encoding="utf-8")
    (data / "Maps" / "TestMap" / "region.bin").write_bytes(b"x" * 2048)
    monkeypatch.setattr(ops, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(ops, "_set_phase", lambda *a, **k: None)
    monkeypatch.setattr(ops, "is_running", lambda: False)
    monkeypatch.setattr(ops, "container_state", lambda: {"running": False})
    return data


def _bk_snap():
    return json.loads(json.dumps(ops._SETTINGS))


def _bk_restore(snap):
    ops._SETTINGS.clear()
    ops._SETTINGS.update(snap)


def test_next_daily_run_boundaries():
    """Расписание суточное: до времени — сегодня, после — завтра; мусор → 03:00."""
    base = datetime(2026, 9, 10, 2, 0).timestamp()
    nxt = ops._next_daily_run("03:00", now=base)
    assert datetime.fromtimestamp(nxt).strftime("%d %H:%M") == "10 03:00"
    base2 = datetime(2026, 9, 10, 4, 0).timestamp()
    nxt2 = ops._next_daily_run("03:00", now=base2)
    assert datetime.fromtimestamp(nxt2).strftime("%d %H:%M") == "11 03:00"
    assert ops._next_daily_run("мусор", now=base) == nxt


def test_auto_backup_settings_validation(tmp_path):
    """patch_settings валидирует расписание и пересчитывает следующий запуск."""
    config.CFG["settings_file"] = str(tmp_path / "settings.json")
    snap = _bk_snap()
    try:
        assert ops.patch_settings({"autoBackup": {"time": "25:99"}}) is not None
        assert ops.patch_settings({"autoBackup": {"time": "в полдень"}}) is not None
        assert ops.patch_settings({"autoBackup": {"enabled": "да"}}) is not None
        assert ops.patch_settings({"autoBackup": {"enabled": True, "time": "3:05"}}) is None
        s = ops.get_settings()
        assert s["autoBackup"]["enabled"] is True
        assert s["autoBackup"]["time"] == "03:05"
        assert s["nextBackupRun"] > ops.time.time()
        saved = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
        assert saved["autoBackup"]["time"] == "03:05"
        assert ops.patch_settings({"autoBackup": {"enabled": False}}) is None
        assert ops.get_settings()["nextBackupRun"] is None
    finally:
        _bk_restore(snap)


def test_load_settings_bad_time_falls_back(tmp_path):
    """Мусор в settings.json (время 99:99, метка-строка) не роняет пульт."""
    config.CFG["settings_file"] = str(tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "autoBackup": {"enabled": True, "time": "99:99"},
                "nextBackupRun": "мусор",
            }
        ),
        encoding="utf-8",
    )
    snap = _bk_snap()
    try:
        ops._load_settings()
        assert ops._SETTINGS["autoBackup"]["time"] == "03:00"
        assert ops._SETTINGS["nextBackupRun"] is None
    finally:
        _bk_restore(snap)


def test_auto_backup_tick_fires_and_journals(tmp_path, monkeypatch):
    """Наступило время — планировщик запускает бэкап, архив и журнал создаются."""
    data = _bk_fixture(tmp_path, monkeypatch)
    snap = _bk_snap()
    fired = {}

    def fake_start_op(op, fn):
        fired["op"] = op
        fn()

    monkeypatch.setattr(ops, "start_op", fake_start_op)
    try:
        ops._SETTINGS["autoBackup"].update({"enabled": True, "time": "03:00", "stopServer": False})
        ops._SETTINGS["nextBackupRun"] = ops.time.time() - 60
        s = ops.get_settings()
        ops._auto_backup_tick(s, ops.time.time())
        assert fired["op"] == "backup"
        assert ops._SETTINGS["nextBackupRun"] > ops.time.time()
        items = ops.list_backups()
        assert len(items) == 1
        journal = ops.get_backup_journal()
        assert len(journal) == 1
        rec = journal[0]
        assert rec["trigger"] == "scheduled" and rec["status"] == "success"
        assert rec["name"] == items[0]["name"]
        assert rec["size"] > 0 and rec["path"].endswith(".tar.gz")
        assert (data / "Server" / "test.ini").exists()
    finally:
        _bk_restore(snap)


def test_auto_backup_tick_skips_when_busy(tmp_path, monkeypatch):
    """Пульт занят другой операцией — запуск откладывается, время не двигаем."""
    _bk_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(ops, "op_busy", lambda: True)
    called = {}
    monkeypatch.setattr(ops, "start_op", lambda *a, **k: called.setdefault("x", True))
    snap = _bk_snap()
    try:
        ops._SETTINGS["autoBackup"].update({"enabled": True, "time": "03:00"})
        ops._SETTINGS["nextBackupRun"] = ops.time.time() - 60
        before = ops._SETTINGS["nextBackupRun"]
        ops._auto_backup_tick(ops.get_settings(), ops.time.time())
        assert not called
        assert ops._SETTINGS["nextBackupRun"] == before
    finally:
        _bk_restore(snap)


def test_backup_failure_journaled_and_reported(tmp_path, monkeypatch):
    """Сбой бэкапа: запись статуса error в журнал + событие с пометкой «по расписанию»."""
    config.CFG["backup_dir"] = str(tmp_path / "backups")
    config.CFG["data_dir"] = str(tmp_path / "empty-data")
    config.CFG["dashboard_dir"] = str(tmp_path / "dd")
    (tmp_path / "empty-data").mkdir()
    errors = []
    monkeypatch.setattr(ops, "log_event", lambda kind, text, *a, **k: errors.append((kind, text)))
    monkeypatch.setattr(ops, "_set_phase", lambda *a, **k: None)
    with pytest.raises(ops.OpsErrorReported):
        ops.run_backup_job("scheduled", False)
    journal = ops.get_backup_journal()
    assert len(journal) == 1
    assert journal[0]["status"] == "error" and journal[0]["trigger"] == "scheduled"
    assert journal[0]["error"]
    assert any(kind == "error" and "по расписанию" in text for kind, text in errors)


def test_verify_backup_ok_and_broken(tmp_path, monkeypatch):
    """Проверка архива: целостный проходит с подсчётом файлов, битый отклоняется."""
    _bk_fixture(tmp_path, monkeypatch)
    res = ops.run_backup_job("manual", False)
    v = ops.verify_backup(res["name"])
    assert v["files"] == 2
    assert v["totalSize"] > 0
    assert v["hasServerIni"] is True and v["hasMapData"] is True
    assert not list((tmp_path / "dd").glob("verify-tmp-*"))
    broken = tmp_path / "backups" / "broken.tar.gz"
    broken.write_bytes(b"not a gzip file at all")
    with pytest.raises(ops.OpsError):
        ops.verify_backup("broken.tar.gz")


def test_restore_roundtrip_returns_source(tmp_path, monkeypatch):
    """Контрольное восстановление: после порчи данных архив возвращает исходный мир."""
    data = _bk_fixture(tmp_path, monkeypatch)
    res = ops.run_backup_job("manual", False)
    (data / "Server" / "test.ini").write_text("ПОРЧА\n", encoding="utf-8")
    monkeypatch.setattr(dockerlib, "container_start", lambda name: (0, "", ""))
    monkeypatch.setattr(ops, "wait_until_running", lambda timeout=120: True)
    monkeypatch.setattr(ops, "wait_until_ready", lambda timeout=600: True)
    ops._do_restore(res["name"])
    assert (data / "Server" / "test.ini").read_text(encoding="utf-8") == "Mods=\n"
    assert (data / "Maps" / "TestMap" / "region.bin").read_bytes() == b"x" * 2048


def test_rotation_prunes_old_backups(tmp_path, monkeypatch):
    """Ротация: сверх лимита остаются только свежие копии."""
    _bk_fixture(tmp_path, monkeypatch)
    (tmp_path / "backups").mkdir()
    for i in range(9):
        (tmp_path / "backups" / f"pz-backup-2026090{i + 1}-030000.tar.gz").write_bytes(b"stub")
    assert len(ops.list_backups()) == 9
    assert ops._prune_backups(7) == 2
    assert len(ops.list_backups()) == 7


def test_backup_journal_roundtrip_file(tmp_path, monkeypatch):
    """Журнал — файл backups.jsonl: записи читаются новыми сверху с нужными полями."""
    _bk_fixture(tmp_path, monkeypatch)
    ops._journal_append(
        {
            "trigger": "manual",
            "name": "a.tar.gz",
            "size": 10,
            "path": "/backups/a.tar.gz",
            "status": "success",
            "duration": 1.0,
        }
    )
    ops._journal_append({"trigger": "scheduled", "status": "error", "error": "tar не удался"})
    path = tmp_path / "dd" / "backups.jsonl"
    assert path.exists()
    journal = ops.get_backup_journal(10)
    assert [r["trigger"] for r in journal] == ["scheduled", "manual"]
    assert journal[1]["name"] == "a.tar.gz" and journal[1]["size"] == 10
    assert journal[0]["status"] == "error"
    assert journal[0]["ts"]
