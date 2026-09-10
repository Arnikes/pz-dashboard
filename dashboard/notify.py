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
import urllib.error
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
    """Настройки Telegram. Локальный импорт ops — ops импортирует этот модуль.

    Обязательно telegram_settings_raw(): get_settings() отдаёт токен наружу
    маской и пустым botToken — отправка через него считала токен незаданным
    («не задан токен бота или chat id») при сохранённом токене."""
    import ops  # noqa: PLC0415
    return ops.telegram_settings_raw()


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


def fetch_recent_chats():
    """Чаты, где бот недавно видел активность (getUpdates). Помогает подставить
    настоящий chat id вместо копипасты с ошибками: Telegram принимает id
    супергрупп с префиксом -100, а из ссылок/дев-тулзов его часто копируют без него.
    (chats, error)."""
    tg = _telegram_settings()
    token = (tg.get("botToken") or "").strip()
    if not token:
        return None, "не задан токен бота"
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/getUpdates?limit=100")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8", "replace"))
            desc = str(body.get("description") or "").strip()
        except Exception:  # noqa: BLE001 — тело может быть пустым/не-json
            desc = ""
        return None, (desc or str(e)).replace(token, "•••" + token[-4:])
    except Exception as e:  # noqa: BLE001
        return None, str(e).replace(token, "•••" + token[-4:])
    if not payload.get("ok"):
        return None, str(payload.get("description") or "Telegram вернул ошибку")
    chats = {}
    for upd in payload.get("result") or []:
        chat = None
        for key in ("message", "edited_message", "channel_post",
                    "edited_channel_post", "my_chat_member"):
            evt = upd.get(key)
            if isinstance(evt, dict) and isinstance(evt.get("chat"), dict):
                chat = evt["chat"]
                break
        cid = chat.get("id") if chat else None
        if cid is None:
            continue
        title = (chat.get("title") or chat.get("first_name")
                 or chat.get("username") or str(cid))
        chats[str(cid)] = {"id": str(cid), "title": title,
                           "type": str(chat.get("type") or "")}
    return sorted(chats.values(), key=lambda c: c["id"]), None


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
    except urllib.error.HTTPError as e:
        # в теле ответа Telegram пишет настоящую причину («Bad Request: chat not
        # found» и т.п.), а str(e) — безликое «HTTP Error 400: Bad Request»
        try:
            payload = json.loads(e.read().decode("utf-8", "replace"))
            desc = str(payload.get("description") or "").strip()
        except Exception:  # noqa: BLE001 — тело может быть пустым/не-json
            desc = ""
        err = desc or str(e)
        return False, err.replace(token, "•••" + token[-4:])
    except Exception as e:  # noqa: BLE001 — наружу отдаём текст ошибки
        # текст ошибки может содержать полный URL запроса — маскируем токен
        err = str(e).replace(token, "•••" + token[-4:])
        return False, err or "ошибка отправки"


def test_message():
    """Кнопка «Проверить» в карточке уведомлений. При ошибке добавляет к тексту
    сохранённый chat id — чтобы сразу видеть, что реально лежит в настройках."""
    tg = _telegram_settings()
    ok, err = send_message("✅ PZ Пульт: проверка связи — уведомления работают.")
    with _LOCK:
        _LAST.update({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "ok": ok, "error": err})
    if not ok:
        chat = (tg.get("chatId") or "").strip()
        if chat:
            err = f"{err} · chat id: {chat}"
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
