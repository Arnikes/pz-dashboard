"""Юнит-тесты ядра пульта: парсеры docker, RCON-протокол (с фейковым сервером),
настройки, история онлайна, SSE-поток. Запуск: pytest -q tests (или из корня проекта)."""
import http.client
import json
import struct
import sys
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD_DIR))

import config  # noqa: E402
import dockerlib  # noqa: E402
import ops  # noqa: E402
import rcon  # noqa: E402


# ───────────────────────── парсеры ─────────────────────────

def test_parse_bytes():
    assert dockerlib.parse_bytes("123MiB") == 123 * 1024 * 1024
    assert dockerlib.parse_bytes("1.9GiB") == int(1.9 * 1024 ** 3)
    assert dockerlib.parse_bytes("1.20kB") == 1200
    assert dockerlib.parse_bytes("42B") == 42
    assert dockerlib.parse_bytes("мусор") == 0


def test_fmt_size():
    assert ops.fmt_size(0) == "0 Б"
    assert ops.fmt_size(1024) == "1.0 КБ"
    assert ops.fmt_size(684000000).endswith("МБ")


def test_stats_parse(monkeypatch):
    monkeypatch.setattr(
        dockerlib, "sh",
        lambda args, timeout=120: (0, "12.34%|123MiB / 1.9GiB|6.29%|1.20kB / 3.40MB|17", ""),
    )
    s = dockerlib.container_stats("x")
    assert s["cpuPct"] == 12.34
    assert s["memUsed"] == 123 * 1024 * 1024
    assert s["memLimit"] == int(1.9 * 1024 ** 3)
    assert s["pids"] == 17


def test_image_digests_parse(monkeypatch):
    """RepoDigests приходит как repo@sha256:... — хеш после @."""
    monkeypatch.setattr(dockerlib, "sh", lambda args, timeout=120: (
        0, "indifferentbroccoli/projectzomboid-server-docker@sha256:8e13816b92fdd\n", ""))
    assert dockerlib.image_digests("x") == "sha256:8e13816b92fdd"
    monkeypatch.setattr(dockerlib, "sh", lambda args, timeout=120: (0, "", ""))
    assert dockerlib.image_digests("x") is None


def test_effective_image(monkeypatch):
    """Образ для проверки обновлений берётся из контейнера, тег дописывается."""
    monkeypatch.setattr(ops, "container_state", lambda: {
        "status": "running", "running": True, "startedAt": None,
        "image": "indifferentbroccoli/projectzomboid-server-docker",
    })
    assert ops._effective_image() == "indifferentbroccoli/projectzomboid-server-docker:latest"
    monkeypatch.setattr(ops, "container_state", lambda: None)
    assert ops._effective_image() == config.CFG["pz_image"]


def test_stats_history_throttle():
    ops._STATS.clear()
    ops.record_stats_sample({"cpuPct": 1.5, "memPct": 10})
    ops.record_stats_sample({"cpuPct": 2.5, "memPct": 11})   # троттлинг
    h = ops.get_stats_history()
    assert len(h) == 1 and h[0]["cpu"] == 1.5 and h[0]["mem"] == 10


def test_local_digest_cache(monkeypatch):
    """Локальный digest считается сам и кэшируется — docker дёргается один раз."""
    calls = {"n": 0}

    def fake_digests(img):
        calls["n"] += 1
        return "sha256:abc123"

    monkeypatch.setattr(ops.dockerlib, "image_digests", fake_digests)
    monkeypatch.setattr(ops, "container_state", lambda: {
        "status": "running", "running": True, "startedAt": None,
        "image": "indifferentbroccoli/projectzomboid-server-docker",
    })
    ops._LOCAL_DIGEST["at"] = 0.0
    assert ops.local_digest_cached() == "sha256:abc123"
    assert ops.local_digest_cached() == "sha256:abc123"
    assert calls["n"] == 1
    assert ops._LOCAL_DIGEST["image"].endswith(":latest")


def test_parse_mods_ini(tmp_path, monkeypatch):
    config.CFG["data_dir"] = str(tmp_path)
    monkeypatch.setattr(ops, "_ws_titles", lambda ids: {})
    server_dir = tmp_path / "Server"
    server_dir.mkdir()
    (server_dir / "servertest.ini").write_text(
        "NightLength=60\n"
        "Mods=tsarslib;my mod;SoloMod\n"
        "WorkshopItems=111111111;222222222\n"
        "Map=Muldraugh, KY\n",
        encoding="utf-8")
    assert ops.list_server_inis() == ["servertest.ini"]
    parsed = ops.parse_mods_ini("servertest.ini")
    assert parsed["mods"] == ["tsarslib", "my mod", "SoloMod"]
    assert parsed["items"] == ["111111111", "222222222"]
    res = ops.list_mods("servertest.ini")
    assert res["ok"] and res["file"] == "servertest.ini"
    assert res["mods"] == ["tsarslib", "my mod", "SoloMod"]
    assert [w["workshopId"] for w in res["workshop"]] == ["111111111", "222222222"]
    assert res["paired"] is False and res["mappingSource"] is None
    assert res["workshop"][0]["url"].endswith("id=111111111")


def test_mods_paired(tmp_path, monkeypatch):
    """Равные количества и без данных с диска — соответствие 1:1 по порядку."""
    config.CFG["data_dir"] = str(tmp_path)
    monkeypatch.setattr(ops, "_WS_TITLES", {"111": "Mod A", "222": "Mod B"})
    monkeypatch.setattr(ops, "_ws_titles", lambda ids: ops._WS_TITLES)
    server_dir = tmp_path / "Server"
    server_dir.mkdir()
    (server_dir / "srv.ini").write_text("Mods=modA;modB\nWorkshopItems=111;222\n", encoding="utf-8")
    res = ops.list_mods("srv.ini")
    assert res["paired"] is True and res["mappingSource"] == "order"
    assert [p["mod"] for p in res["pairs"]] == ["modA", "modB"]
    assert [p["title"] for p in res["pairs"]] == ["Mod A", "Mod B"]


def test_mods_disk_mapping(tmp_path, monkeypatch):
    """Один Workshop-элемент тянет несколько модов — маппинг из mod.info на диске."""
    config.CFG["data_dir"] = str(tmp_path)
    monkeypatch.setattr(ops, "_ws_titles", lambda ids: {})
    server_dir = tmp_path / "Server"
    server_dir.mkdir()
    (server_dir / "srv.ini").write_text(
        "Mods=libA;pluginB;localMod\nWorkshopItems=111\n", encoding="utf-8")
    ws_dir = tmp_path / "steamapps" / "workshop" / "content" / "108600" / "111" / "mods"
    (ws_dir / "tsar").mkdir(parents=True)
    (ws_dir / "tsar" / "mod.info").write_text("name=Big Pack\nmodID=libA\n", encoding="utf-8")
    (ws_dir / "plug").mkdir()
    (ws_dir / "plug" / "mod.info").write_text("modID=pluginB\n", encoding="utf-8")
    res = ops.list_mods("srv.ini")
    assert res["mappingSource"] == "disk"
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
                conn.sendall(_pkt(-1, 2, b""))   # AUTH_RESPONSE с id=-1: отказ
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
    assert ops.patch_settings({"watchdog": {"enabled": "yes"}}) == "watchdog.enabled должен быть true/false"
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
    ops.record_players_sample(7)          # троттлинг: второй семпл в тот же интервал игнорируется
    assert len(ops._PH) == 1 and ops._PH[0]["count"] == 2

    # уводим семпл на 25 часов назад: новый добавится, старый выпадет из окна 24 ч
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).astimezone().isoformat(timespec="seconds")
    ops._PH[0]["ts"] = old
    ops.record_players_sample(4)
    assert len(ops._PH) == 1 and ops._PH[0]["count"] == 4
    assert ops.get_players_history()[-1]["count"] == 4


def test_history_persisted(tmp_path):
    config.CFG["dashboard_dir"] = str(tmp_path)
    ops._PH = None
    ops.record_players_sample(5)
    ops._PH = None                        # имитация перезапуска: загрузка из файла
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
    ws = {"2983905789": {"title": "Wandering Zombies",
                         "url": "https://steamcommunity.com/sharedfiles/filedetails/?id=2983905789"}}
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
        f"2026-09-08T12:0{i}.000000000Z LOG : General f:1 st:1> line{i}" for i in range(5))
    result_line = ("2026-09-08T12:28:01.613439139Z LOG  : Mod          f:1 st:503,316,043> "
                   "CheckModsNeedUpdate: Mods updated")

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
    monkeypatch.setattr(dockerlib, "container_stop",
                        lambda name, seconds=180: stops.append(name))

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
    monkeypatch.setattr(dockerlib, "set_restart_policy",
                        lambda name, policy: calls.append(("set", policy)) or True)

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
    assert ops.patch_settings({"telegram": {
        "enabled": True, "botToken": "  123456:ABC-DEF1234  ", "chatId": " -100123 ", }}) is None
    st = ops.get_settings()["telegram"]
    assert st["enabled"] is True and st["chatId"] == "-100123"
    assert st["botToken"] == "" and st["botTokenMasked"] == "•••1234"
    assert "ABC-DEF1234" not in json.dumps(ops.get_settings())
    # пустое/маскированное значение не затирает сохранённый токен
    assert ops.patch_settings({"telegram": {"botToken": "", "chatId": "-100123"}}) is None
    assert ops.get_settings()["telegram"]["botTokenMasked"] == "•••1234"
    assert ops.patch_settings({"telegram": {"enabled": "yes"}}) == \
        "telegram.enabled должен быть true/false"
    assert ops.patch_settings({"telegram": {"groups": {"ops": False, "мусор": True}}}) is None
    assert ops.get_settings()["telegram"]["groups"] == {"ops": False}


def test_telegram_enqueue_filters(monkeypatch):
    """Группы подписки фильтруют события; сообщение без полного токена."""
    import notify
    sent = []
    monkeypatch.setattr(ops, "get_settings", lambda: {"telegram": {
        "enabled": True, "botToken": "123456:SECRET", "chatId": "42",
        "groups": {"ops": True, "backup": False, "update": True, "problems": True}}})
    monkeypatch.setattr(notify._QUEUE, "put_nowait", lambda m: sent.append(m))
    notify.enqueue("restart", "Сервер перезапущен")      # ops → в очередь
    notify.enqueue("backup", "Бэкап создан")             # backup → выключен
    notify.enqueue("error", "Бэкап не удался")           # problems → в очередь
    notify.enqueue("console", "команда")                 # без группы → мимо
    assert len(sent) == 2
    assert sent[0].startswith("🔄") and sent[0].endswith("Сервер перезапущен")
    assert sent[1].startswith("❌")
    # выключено → ничего
    monkeypatch.setattr(ops, "get_settings", lambda: {"telegram": {"enabled": False}})
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
    monkeypatch.setattr(ops, "get_settings", lambda: {"telegram": {
        "enabled": True, "botToken": "123456:SECRET", "chatId": "42",
        "groups": {"ops": True, "backup": True, "update": True, "problems": True}}})

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"ok": True, "result": {}}).encode()

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
    assert ok is True and "проверка связи" in calls[-1][1]["text"]
    # без настроек — честная ошибка, без сетевого вызова
    monkeypatch.setattr(ops, "get_settings", lambda: {"telegram": {}})
    ok, err = notify.send_message("x")
    assert ok is False and err and len(calls) == 2


# ─────────────────────── watchdog: пробы RCON ───────────────────────

def _wd_reset():
    ops._WD.update({"lastProbeAt": None, "lastResult": None, "lastError": None,
                    "consecutiveFailures": 0, "alerted": False, "lastRestartAt": None})


def test_watchdog_skips_when_stopped_or_busy(monkeypatch):
    """Остановленный контейнер и занятый пульт — не зависание: счётчик сбрасывается."""
    _wd_reset()
    events = []
    monkeypatch.setattr(ops, "log_event", lambda *a, **k: events.append(a))
    monkeypatch.setattr(ops, "op_busy", lambda: False)
    monkeypatch.setattr(ops, "docker_ok_cached", lambda ttl=60: True)
    monkeypatch.setattr(ops, "container_state", lambda: {
        "status": "exited", "running": False, "startedAt": "S1", "image": "img"})
    probes = []
    monkeypatch.setattr(ops.rconlib, "run_command",
                        lambda *a, **k: probes.append(a))
    ops._watchdog_probe({"enabled": True, "thresholdMin": 1, "autoRestart": True})
    assert ops._WD["lastResult"] == "skipped"
    assert ops._WD["consecutiveFailures"] == 0 and not probes

    monkeypatch.setattr(ops, "op_busy", lambda: True)   # идёт операция
    monkeypatch.setattr(ops, "container_state", lambda: {
        "status": "running", "running": True, "startedAt": "S1", "image": "img"})
    ops._watchdog_probe({"enabled": True, "thresholdMin": 1, "autoRestart": True})
    assert ops._WD["lastResult"] == "skipped" and not probes


def test_watchdog_alert_and_autorestart(monkeypatch):
    """Молчание дольше порога → событие и один авторестарт; кулдаун держит."""
    _wd_reset()
    events, restarts = [], []
    monkeypatch.setattr(ops, "log_event", lambda *a, **k: events.append(a))
    monkeypatch.setattr(ops, "op_busy", lambda: False)
    monkeypatch.setattr(ops, "docker_ok_cached", lambda ttl=60: True)
    monkeypatch.setattr(ops, "container_state", lambda: {
        "status": "running", "running": True, "startedAt": "S1", "image": "img"})
    monkeypatch.setattr(ops, "is_running", lambda: True)
    monkeypatch.setattr(ops, "start_op", lambda op, fn: restarts.append(op))

    def dead_rcon(*a, **k):
        raise rcon.RCONError("таймаут")

    monkeypatch.setattr(ops.rconlib, "run_command", dead_rcon)
    wd = {"enabled": True, "thresholdMin": 1, "autoRestart": True}
    for _ in range(2):                    # 2 × 30 с = 1 мин порога
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


def test_ini_replace_value():
    """Замена значения с сохранением написания ключа; дописывание отсутствующего."""
    out = ops._ini_replace_value(INI, "WorkshopItems", ["111"])
    assert "WorkshopItems=111\n" in out and "[Other]" in out
    out = ops._ini_replace_value(INI, "mods", ["a", "b"])          # регистр ключа файла
    assert "Mods=a;b\n" in out and "Mods=" in out
    out = ops._ini_replace_value(INI, "ClientMods", ["x"])          # нет строки — в конец
    assert out.rstrip().endswith("ClientMods=x")
    # многострочное значение (перенос с отступом) глотается целиком
    text = "Mods=aaa;\n  bbb;\n  ccc\nWorkshopItems=1\n"
    out = ops._ini_replace_value(text, "Mods", ["one"])
    assert out == "Mods=one\nWorkshopItems=1\n"


def test_mods_toggle_flow(tmp_path, monkeypatch):
    """Выключение мода: уходит из WorkshopItems и Mods, состав в реестре; включение обратно."""
    server_dir = tmp_path / "Server"
    server_dir.mkdir()
    ini = server_dir / "servertest.ini"
    ini.write_text(INI, encoding="utf-8")
    config.CFG["data_dir"] = str(tmp_path)
    config.CFG["settings_file"] = str(tmp_path / "settings.json")
    config.CFG["events_file"] = str(tmp_path / "events.jsonl")
    ops._SETTINGS["modsDisabled"] = {}
    ops._WS_EXEC["map"] = {}
    ops._WS_TITLES.clear()
    monkeypatch.setattr(ops, "log_event", lambda *a, **k: None)
    # локального тома нет, но контент находим docker exec'ом
    monkeypatch.setattr(ops, "_workshop_map_local", lambda items: {})
    monkeypatch.setattr(ops, "_workshop_map_via_exec",
                        lambda: {"2694464646": ["tsarslib"], "2804001857": ["commonpackage"]})

    res = ops.set_mod_enabled("servertest.ini", "2694464646", enable=False)
    text = ini.read_text(encoding="utf-8")
    assert "WorkshopItems=2804001857\n" in text and "Mods=commonpackage\n" in text
    assert res["canManage"] is True
    dis = {d["workshopId"]: d for d in res["disabled"]}
    assert dis["2694464646"]["modIds"] == ["tsarslib"]
    # бэкап создан
    assert any(p.name.startswith("servertest.ini.bak-") for p in server_dir.iterdir())

    # включение обратно — состав восстанавливается из реестра
    res = ops.set_mod_enabled("servertest.ini", "2694464646", enable=True)
    text = ini.read_text(encoding="utf-8")
    assert "WorkshopItems=2804001857;2694464646\n" in text
    assert "Mods=commonpackage;tsarslib\n" in text
    assert res["disabled"] == []


def test_mods_toggle_blocked_without_mapping(tmp_path, monkeypatch):
    """Без modID выключение запрещено — не теряем состав конфига."""
    server_dir = tmp_path / "Server"
    server_dir.mkdir()
    (server_dir / "servertest.ini").write_text(INI, encoding="utf-8")
    config.CFG["data_dir"] = str(tmp_path)
    config.CFG["settings_file"] = str(tmp_path / "settings.json")
    config.CFG["events_file"] = str(tmp_path / "events.jsonl")
    ops._SETTINGS["modsDisabled"] = {}
    ops._WS_EXEC["map"] = {}
    monkeypatch.setattr(ops, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(ops, "_workshop_map_local", lambda items: {})
    monkeypatch.setattr(ops, "_workshop_map_via_exec", lambda: {})
    with pytest.raises(ops.OpsError):
        ops.set_mod_enabled("servertest.ini", "2694464646", enable=False)
    assert (server_dir / "servertest.ini").read_text(encoding="utf-8") == INI
    # включение неизвестного ID — добавление нового элемента: конфиг пишется, но с предупреждением
    events = []
    monkeypatch.setattr(ops, "log_event", lambda kind, text, **k: events.append((kind, text)))
    res = ops.set_mod_enabled("servertest.ini", "999999", enable=True)
    text = (server_dir / "servertest.ini").read_text(encoding="utf-8")
    assert text.count("999999") == 1 and "WorkshopItems=2694464646;2804001857;999999" in text
    assert any(kind == "warn" and "modID" in t for kind, t in events)
    assert res["disabled"] == []


# ─────────────────────── регресс: SSE-поток ───────────────────────

def test_sse_stream_serves_data(monkeypatch):
    """/api/stream должен реально слать кадры данных.

    Регресс: в обработчике было обращение к несуществующему self.STREAM_PLAN —
    поток падал сразу после retry-кадра, клиент бесконечно переподключался."""
    import app
    monkeypatch.setattr(app.Handler, "log_message", lambda *a, **k: None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    srv.daemon_threads = True
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        port = srv.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=6)
        conn.request("GET", "/api/stream")
        resp = conn.getresponse()
        assert resp.status == 200
        buf = b""
        deadline = ops.time.time() + 5
        while b"event: ops" not in buf and ops.time.time() < deadline:
            chunk = resp.read1(512)
            if not chunk:
                break   # соединение закрыто сервером — падение обработчика
            buf += chunk
        assert b"event: ops" in buf, "SSE закрылся до первого кадра данных"
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()


# ─────────────────────── регресс: бэкофф поиска модов ───────────────────────

def test_workshop_exec_backoff(monkeypatch):
    """Пустой результат поиска в контейнере кэшируется: повторные вызовы
    в паузу не гоняют find по всей ФС (list_mods приходит каждые 60 с)."""
    calls = {"n": 0}

    def fake_exec(name, cmd, timeout=60):
        calls["n"] += 1
        return 0, "", ""

    monkeypatch.setattr(dockerlib, "container_exec", fake_exec)
    monkeypatch.setattr(ops, "docker_ok_cached", lambda ttl=600: True)
    ops._WS_EXEC.update({"at": 0.0, "map": {}})
    try:
        assert ops._workshop_map_via_exec() == {}
        assert calls["n"] == 1
        assert ops._workshop_map_via_exec() == {}
        assert calls["n"] == 1, "повторный поиск должен быть отложен на паузу"
    finally:
        ops._WS_EXEC.update({"at": 0.0, "map": {}})


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
    (tmp_path / "settings.json").write_text(json.dumps({
        "autoUpdate": {"intervalHours": 0, "warnSeconds": 99999},
        "modsUpdate": {"intervalHours": -5},
        "watchdog": {"thresholdMin": 1000},
        "backup": {"maxBackups": -1},
    }), encoding="utf-8")
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
    monkeypatch.setattr(ops, "get_settings", lambda: {"telegram": {
        "enabled": True, "botToken": "123456:SECRET", "chatId": "42"}})

    def boom(req, timeout=10):
        raise OSError("connection failed for bot123456:SECRET (https url)")

    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    ok, err = notify.send_message("x")
    assert ok is False and err
    assert "123456:SECRET" not in err
    assert "•••" in err


# ─────────────── регресс V19: восстановление при resurrected ───────────────

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
    bak.write_bytes(b"\\x1f\\x8b")   # имя валидно, до распаковки дело не дойдёт
    wiped = []
    monkeypatch.setattr(ops, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(ops, "_set_phase", lambda *a, **k: None)
    monkeypatch.setattr(ops, "is_running", lambda: True)
    monkeypatch.setattr(ops, "rcon_warn_broadcast", lambda s, r: True)
    monkeypatch.setattr(ops, "graceful_stop", lambda hook=None: "resurrected")
    monkeypatch.setattr(ops.shutil, "rmtree", lambda p, ignore_errors=False: wiped.append(str(p)))
    with pytest.raises(ops.OpsError):
        ops._do_restore(bak.name)
    assert wiped == [], "каталог данных не должен очищаться при resurrected"
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
        for _ in range(12):            # минута опросов каждые 5 с
            with pytest.raises(rcon.RCONError):
                ops.rcon("players")
        assert len(events) == 1, "на постоянный сбой — одна запись, не 12"
        ops._RCON_LOG["at"] -= 301     # прошло 5 минут тишины
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
    (tmp_path / "settings.json").write_text(json.dumps({
        "nextCheck": "скоро", "nextModsCheck": [1],
        "autoUpdate": {"enabled": True},
    }), encoding="utf-8")
    snap = json.loads(json.dumps(ops._SETTINGS))
    try:
        ops._load_settings()
        assert ops._SETTINGS["nextCheck"] is None
        assert ops._SETTINGS["nextModsCheck"] is None
        # валидное число сохраняется; true/false — не метка
        (tmp_path / "settings.json").write_text(
            json.dumps({"nextCheck": 123.5, "nextModsCheck": True}), encoding="utf-8")
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
    import app
    monkeypatch.setattr(ops, "container_state", lambda: None)
    data = app.stats_payload()
    assert data["ok"] is False and "error" in data
    # живые данные не сломались
    monkeypatch.setattr(ops, "container_state", lambda: {
        "status": "running", "running": True, "startedAt": None, "image": "img"})
    monkeypatch.setattr(dockerlib, "container_stats", lambda name: {
        "cpuPct": 1.0, "memUsed": 1, "memLimit": 2, "memPct": 1,
        "netIn": 1, "netOut": 1, "pids": 1})
    data = app.stats_payload()
    assert data["ok"] is True and data["cpuPct"] == 1.0


# ─────────────── регресс V19: скачивание исчезнувшего бэкапа ───────────────

def test_backup_download_missing_file_404(monkeypatch):
    """Файл удалён prune'ом между проверкой и чтением → честный 404 JSON,
    а не необработанное исключение и разрыв соединения."""
    import app
    monkeypatch.setattr(app.Handler, "log_message", lambda *a, **k: None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(ops, "backup_download_path",
                            lambda name: "/несуществующий/путь.tar.gz")
        port = srv.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/backup/download?name=x.tar.gz")
        resp = conn.getresponse()
        assert resp.status == 404
        body = json.loads(resp.read().decode("utf-8"))
        assert body["ok"] is False
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()
