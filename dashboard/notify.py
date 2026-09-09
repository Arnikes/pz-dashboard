#!/usr/bin/env python3
"""Telegram-уведомления о событиях пульта.

События пульта (log_event) попадают в очередь и отправляются ботом в чат.
Очередь ограничена, отправка не блокирует операции, ошибки не создают
новых событий (иначе цикл «ошибка отправки → событие → отправка»).
Токен бота живёт в настройках на сервере пульта и наружу отдаётся маской.
"""
import json
import queue
import threading
import time
import urllib.request

import config

# Каждому типу события — группа подписки и значок в сообщении.
KIND_GROUP = {
    "start": "ops", "stop": "ops", "restart": "ops", "docker": "ops",
    "backup": "backup", "restore": "backup", "backup-delete": "backup",
    "update": "update", "update-check": "update", "auto": "update", "mods": "update",
    "error": "problems", "warn": "problems", "rcon-error": "problems",
}
KIND_ICON = {
    "start": "▶️", "stop": "⏹", "restart": "🔄", "docker": "🐳",
    "backup": "💾", "restore": "♻️", "backup-delete": "🗑",
    "update": "⬆️", "update-check": "🔎", "auto": "🤖", "mods": "🧩",
    "error": "❌", "warn": "⚠️", "rcon-error": "🔌",
}

_QUEUE = queue.Queue(maxsize=50)
_LAST = {"at": None, "ok": None, "error": None}
_LOCK = threading.Lock()


def state():
    with _LOCK:
        return dict(_LAST)


def _telegram_settings():
    """Настройки Telegram. Локальный импорт ops — ops импортирует этот модуль."""
    import ops  # noqa: PLC0415
    return (ops.get_settings().get("telegram") or {})


def _format(kind, text):
    icon = KIND_ICON.get(kind, "•")
    name = (config.CFG.get("server_name") or "").strip()
    return f"{icon} [{name}] {text}" if name else f"{icon} {text}"


def enqueue(kind, text):
    """Поставить событие в очередь отправки (никогда не бросает и не блокирует)."""
    try:
        tg = _telegram_settings()
        if not tg.get("enabled"):
            return
        group = KIND_GROUP.get(kind)
        if not group or not (tg.get("groups") or {}).get(group, True):
            return
        if not (tg.get("botToken") or "").strip() or not (tg.get("chatId") or "").strip():
            return
        _QUEUE.put_nowait(_format(kind, text))
    except queue.Full:
        pass  # переполнение: молча теряем старое хвостовое, не копим бесконечно
    except Exception:  # noqa: BLE001 — уведомления не должны ронять события
        pass


def send_message(text):
    """Отправить сообщение прямо сейчас. (ok, error)."""
    tg = _telegram_settings()
    token = (tg.get("botToken") or "").strip()
    chat = (tg.get("chatId") or "").strip()
    if not token or not chat:
        return False, "не задан токен бота или chat id"
    try:
        body = json.dumps({"chat_id": chat, "text": text[:3500]}).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if payload.get("ok"):
            return True, None
        return False, str(payload.get("description") or "Telegram вернул ошибку")
    except Exception as e:  # noqa: BLE001 — наружу отдаём текст ошибки
        # текст ошибки может содержать полный URL запроса — маскируем токен
        err = str(e).replace(token, "•••" + token[-4:])
        return False, err or "ошибка отправки"


def test_message():
    """Кнопка «Проверить» в карточке уведомлений."""
    ok, err = send_message("✅ PZ Пульт: проверка связи — уведомления работают.")
    with _LOCK:
        _LAST.update({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "ok": ok, "error": err})
    return ok, err


def _worker():
    while True:
        msg = _QUEUE.get()
        ok, err = send_message(msg)
        with _LOCK:
            _LAST.update({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "ok": ok, "error": err})
        time.sleep(1.1)  # щадим лимиты Bot API


def start_worker():
    threading.Thread(target=_worker, daemon=True, name="pz-notify").start()
