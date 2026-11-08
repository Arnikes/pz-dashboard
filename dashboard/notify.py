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
import urllib.parse

import config
import i18n

# Каждому типу события — группа подписки и значок в сообщении.
KIND_GROUP = {
    "start": "ops",
    "stop": "ops",
    "restart": "ops",
    "docker": "ops",
    "backup": "backup",
    "restore": "backup",
    "backup-delete": "backup",
    "update": "update",
    "update-check": "update",
    "auto": "update",
    "mods": "update",
    "error": "problems",
    "warn": "problems",
    "rcon-error": "problems",
}
KIND_ICON = {
    "start": "▶️",
    "stop": "⏹",
    "restart": "🔄",
    "docker": "🐳",
    "backup": "💾",
    "restore": "♻️",
    "backup-delete": "🗑",
    "update": "⬆️",
    "update-check": "🔎",
    "auto": "🤖",
    "mods": "🧩",
    "error": "❌",
    "warn": "⚠️",
    "rcon-error": "🔌",
}

_QUEUE = queue.Queue(maxsize=50)
_LAST = {"at": None, "ok": None, "error": None}
_LOCK = threading.Lock()
_API_RESPONSE_LIMIT = 2 * 1024 * 1024


def _safe_error(error, settings):
    """Redact credentials before an error reaches UI, events, or process logs."""
    error = str(error) or "ошибка отправки"
    for key in ("botToken", "apiHash", "mtproxySecret"):
        secret = settings.get(key)
        if not isinstance(secret, str) or not secret.strip():
            continue
        secret = secret.strip()
        variants = {secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret)}
        if key in ("apiHash", "mtproxySecret"):
            variants.update((secret.lower(), secret.upper()))
        if key == "mtproxySecret" and len(secret) >= 34 and secret[:2].lower() in ("dd", "ee"):
            # A transport may report only its extracted key. Redact that even
            # when an invalid domain suffix prevented parsing an env secret.
            extracted = secret[2:34]
            try:
                raw_key = bytes.fromhex(extracted)
            except ValueError:
                pass
            else:
                if len(raw_key) == 16:
                    variants.update((extracted, extracted.lower(), extracted.upper()))
        for value in sorted(variants, key=len, reverse=True):
            error = error.replace(value, "•••")
    return error[:1000]


def _proxy_failure(settings, error):
    import ops  # noqa: PLC0415 — avoid the operations/notifications import cycle

    error = _safe_error(error, settings)
    if not error.startswith("Telegram MTProxy:"):
        error = "Telegram MTProxy: " + error
    ops.log_event("error", error, notify=False)
    return False, error


def _read_bot_response(response):
    """Bound external responses and require the Bot API's object envelope."""
    raw = response.read(_API_RESPONSE_LIMIT + 1)
    if len(raw) > _API_RESPONSE_LIMIT:
        raise ValueError("Telegram вернул ошибку")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Telegram вернул ошибку") from None
    if not isinstance(payload, dict):
        raise ValueError("Telegram вернул ошибку")
    return payload


def _bot_api_request(request, settings):
    """Share direct Bot API decoding and redact every diagnostic path."""
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = _read_bot_response(response)
    except urllib.error.HTTPError as error:
        description = ""
        try:
            with error:
                payload = _read_bot_response(error)
            description = str(payload.get("description") or "").strip()
        except Exception:  # noqa: BLE001 — an error response may be empty or unreadable
            pass
        return None, _safe_error(description or error, settings)
    except Exception as error:  # noqa: BLE001 — delivery failures must not kill the worker
        return None, _safe_error(error, settings)
    if not payload.get("ok"):
        return None, _safe_error(payload.get("description") or "Telegram вернул ошибку", settings)
    return payload, None


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


def _format(kind, text, language="en"):
    icon = KIND_ICON.get(kind, "•")
    name = (config.CFG.get("server_name") or "").strip()
    text = i18n.translate(text, locale=language)
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
        _QUEUE.put_nowait(_format(kind, text, tg.get("language", "en")))
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
    proxy = tg.get("mtproxyEnabled", False)
    if not isinstance(proxy, bool):
        return None, _proxy_failure(tg, "telegram.mtproxyEnabled должен быть true/false")[1]
    if proxy:
        return None, "Поиск чатов через MTProxy недоступен. Введите Chat ID вручную."
    token = (tg.get("botToken") or "").strip()
    if not token:
        return None, "не задан токен бота"
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/getUpdates?limit=100")
    payload, error = _bot_api_request(req, tg)
    if error:
        return None, error
    updates = payload.get("result") or []
    if not isinstance(updates, list):
        return None, "Telegram вернул ошибку"
    chats = {}
    for upd in updates:
        if not isinstance(upd, dict):
            continue
        chat = None
        for key in (
            "message",
            "edited_message",
            "channel_post",
            "edited_channel_post",
            "my_chat_member",
        ):
            evt = upd.get(key)
            if isinstance(evt, dict) and isinstance(evt.get("chat"), dict):
                chat = evt["chat"]
                break
        cid = chat.get("id") if chat else None
        if cid is None:
            continue
        title = chat.get("title") or chat.get("first_name") or chat.get("username") or str(cid)
        chats[str(cid)] = {"id": str(cid), "title": title, "type": str(chat.get("type") or "")}
    return sorted(chats.values(), key=lambda c: c["id"]), None


def send_message(text):
    """Отправить сообщение прямо сейчас. (ok, error)."""
    tg = _telegram_settings()
    proxy = tg.get("mtproxyEnabled", False)
    if not isinstance(proxy, bool):
        return _proxy_failure(tg, "telegram.mtproxyEnabled должен быть true/false")
    if proxy:
        try:
            import telegram_mtproxy  # noqa: PLC0415 — direct Bot API needs no client

            ok, error = telegram_mtproxy.send_message(tg, text[:3500])
        except Exception as error:  # noqa: BLE001 — notifications must not kill their worker
            return _proxy_failure(tg, error)
        return (True, None) if ok else _proxy_failure(tg, error or "ошибка отправки")
    token = (tg.get("botToken") or "").strip()
    chat = (tg.get("chatId") or "").strip()
    if not token or not chat:
        return False, "не задан токен бота или chat id"
    body = json.dumps({"chat_id": chat, "text": text[:3500]}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    _, error = _bot_api_request(req, tg)
    return error is None, error


def test_message():
    """Кнопка «Проверить» в карточке уведомлений. При ошибке добавляет к тексту
    сохранённый chat id — чтобы сразу видеть, что реально лежит в настройках."""
    tg = _telegram_settings()
    ok, err = send_message(
        i18n.translate(
            "✅ PZ Пульт: проверка связи — уведомления работают.",
            locale=tg.get("language", "en"),
        )
    )
    with _LOCK:
        _LAST.update({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "ok": ok, "error": err})
    if not ok:
        err = i18n.translate(err)
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
