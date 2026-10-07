"""Request-local EN/RU presentation, independent of stored server data."""

import json
import re
from contextvars import ContextVar
from pathlib import Path

LANGUAGE = ContextVar("presentation_language", default="ru")
CATALOG = json.loads((Path(__file__).parent / "static/locales/en.json").read_text(encoding="utf-8"))
RU_ALL = json.loads((Path(__file__).parent / "static/locales/ru.json").read_text(encoding="utf-8"))
RU_CATALOG = {
    key: value for key, value in RU_ALL.items() if isinstance(value, str) and key != value
}
PLACEHOLDER = re.compile(r"{{(\d+)}}")
NESTED_MESSAGES = {
    "Ошибка: {{0}}": (0,),
    "Не удалось: {{0}}": (0,),
    "Внутренняя ошибка: {{0}}": (0,),
    "Ошибка авторизации: {{0}}": (0,),
    "Не удалось запустить: {{0}}": (0,),
    "Не удалось запустить после рестарта: {{0}}": (0,),
    "Не удалось запустить сервер после бэкапа: {{0}}": (0,),
    "Данные восстановлены, но запуск не удался: {{0}}": (0,),
    "Восстановление не удалось: {{0}}": (0,),
    "Обновление не удалось: {{0}}": (0,),
    "Обновление отменено — не удалось сделать бэкап: {{0}}": (0,),
    "Архив не создан: {{0}}": (0,),
    "Архив повреждён: {{0}}": (0,),
    "Архив повреждён или небезопасен: {{0}}": (0,),
    "Бэкап ({{0}}) не удался: {{1}}": (0, 1),
    "Конфигурация {{0}}: {{1}}": (1,),
    "Автопроверка конфигурации {{0}}: {{1}}": (1,),
    "Операция «{{0}}» не удалась: {{1}}": (1,),
    "Операция «{{0}}»: {{1}}": (1,),
    "Бэкап создан ({{0}}): {{1}} ({{2}})": (0,),
    "Сервер перезапущен ({{0}})": (0,),
    "Автобэкап по расписанию{{0}}: запуск ({{1}})": (0,),
    "Watchdog: RCON не отвечает {{0}} мин — {{1}}": (1,),
    "Не удалось удалить временный архив: {{0}}": (0,),
    "Не удалось удалить временные данные {{0}}: {{1}}": (1,),
    "Рескан модов после рестарта не удался: {{0}}": (0,),
}
SIZE_PARAMETERS = {
    "Бэкап создан ({{0}}): {{1}} ({{2}})": (2,),
    "Бэкап перед обновлением: {{0}} ({{1}})": (1,),
    "Проверка бэкапа {{0}}: OK — файлов {{1}}, {{2}}": (2,),
    "Проверка бэкапа {{0}}: OK — файлов {{1}}, {{2}}, замечания: {{3}}": (2,),
}
LIST_PARAMETERS = {
    "Проверка бэкапа {{0}}: OK — файлов {{1}}, {{2}}, замечания: {{3}}": (3,),
}


def _compile_messages(catalog):
    result = []
    for source, translated in catalog.items():
        if not isinstance(translated, str) or not PLACEHOLDER.search(source):
            continue
        slots, parts, offset = [], [], 0
        for match in PLACEHOLDER.finditer(source):
            parts.extend((re.escape(source[offset : match.start()]), "([\\s\\S]*?)"))
            slots.append(match[1])
            offset = match.end()
        parts.append(re.escape(source[offset:]))
        # Most specific patterns first; a generic "Error: {0}" must not consume
        # a longer message which has its own complete translation.
        specificity = len(PLACEHOLDER.sub("", source))
        result.append(
            (specificity, re.compile("^" + "".join(parts) + "$"), slots, translated, source)
        )
    return sorted(result, key=lambda item: item[0], reverse=True)


MESSAGES = _compile_messages(CATALOG)
RU_MESSAGES = _compile_messages(RU_CATALOG)
DISPLAY_KEYS = {
    "error",
    "message",
    "phase",
    "label",
    "group",
    "hint",
    "note",
    "diagnostic",
    "dataDiagnostic",
    "rebaseError",
    "lastError",
    "titleText",
    "description",
    "sizeText",
    "downloadDiagnostic",
    "sandboxDiagnostic",
    "versionDiagnostic",
    "reason",
    "detail",
    "warning",
}
DISPLAY_LISTS = {"errors", "warnings", "problems", "diagnostics", "notes"}
RAW_KEYS = {
    "texts",
    "base",
    "value",
    "default",
    "raw",
    "output",
    "names",
    "name",
    "title",
    "key",
    "mods",
    "maps",
    "map",
    "file",
    "path",
    "ini",
    "sandbox",
    "diff",
    "conflictDiff",
    "rebaseDiff",
    "mergedTexts",
    "settings",
    "serverName",
    "command",
}


def language():
    return LANGUAGE.get()


def message(source, *values, locale, count=None):
    """Format an authored message, including EN/RU cardinal plural forms.

    Substitute opaque values only after choosing the translation.
    """
    template = (CATALOG if locale == "en" else RU_ALL).get(source, source)
    if isinstance(template, dict):
        if locale == "en":
            form = "one" if count == 1 else "other"
        elif count is None or count != int(count):
            form = "other"
        elif count % 10 == 1 and count % 100 != 11:
            form = "one"
        elif 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
            form = "few"
        else:
            form = "many"
        template = template.get(form, template["other"])
    return PLACEHOLDER.sub(lambda slot: str(values[int(slot[1])]), template)


def resolve(headers, path=""):
    """Explicit language, preference cookie, then weighted Accept-Language.

    Clients which do not send a locale retain the original Russian API contract.
    """
    from urllib.parse import parse_qs, urlsplit

    headers = {key.lower(): value for key, value in headers.items()}

    explicit = parse_qs(urlsplit(path).query).get("lang", [""])[0]
    if explicit in ("en", "ru"):
        return explicit
    explicit = (headers.get("x-pz-language") or "").lower().split("-")[0]
    if explicit in ("en", "ru"):
        return explicit
    cookie = re.search(r"(?:^|;\s*)pz_language=(en|ru)(?:;|$)", headers.get("cookie") or "")
    if cookie:
        return cookie[1]
    choices = []
    for order, part in enumerate((headers.get("accept-language") or "").split(",")):
        locale, _, params = part.strip().partition(";")
        try:
            quality = float(params.strip().removeprefix("q=")) if params else 1.0
        except ValueError:
            continue
        locale = locale.lower().split("-")[0]
        if locale in ("en", "ru") and 0 < quality <= 1:
            choices.append((quality, -order, locale))
    return max(choices)[2] if choices else "ru"


def translate(text, _depth=0, *, locale=None):
    """Translate presentation copy in a request or an explicitly chosen locale."""
    locale = language() if locale is None else locale
    if not isinstance(text, str) or not re.search("[А-Яа-яЁё]", text):
        return text
    catalog = CATALOG if locale == "en" else RU_CATALOG
    messages = MESSAGES if locale == "en" else RU_MESSAGES
    key = re.sub(r"\s+", " ", text).strip()
    if isinstance(catalog.get(key), str):
        return catalog[key]
    for _, expression, slots, translation, source in messages:
        match = expression.fullmatch(text.strip())
        if match:
            values = dict(zip(slots, match.groups()))
            # Only slots known to carry diagnostics or formatted sizes recurse.
            # Profile names, paths, ModID and other opaque values stay literal.
            if _depth < 8:
                for index in NESTED_MESSAGES.get(source, ()):
                    raw = values[str(index)]
                    leading = raw[: len(raw) - len(raw.lstrip())]
                    trailing = raw[len(raw.rstrip()) :] if raw.strip() else ""
                    values[str(index)] = (
                        leading + translate(raw.strip(), _depth + 1, locale=locale) + trailing
                    )
                for index in SIZE_PARAMETERS.get(source, ()):
                    values[str(index)] = translate(values[str(index)], _depth + 1, locale=locale)
                for index in LIST_PARAMETERS.get(source, ()):
                    values[str(index)] = ", ".join(
                        translate(item, _depth + 1, locale=locale)
                        for item in values[str(index)].split(", ")
                    )
            return PLACEHOLDER.sub(lambda slot: values[slot[1]], translation)
    # Prefix fragments are authored separately from dynamic diagnostics. Match
    # only a complete known prefix at a punctuation/space boundary.
    for source in PREFIXES if locale == "en" and _depth < 8 else ():
        if key.startswith(source):
            tail = key[len(source) :]
            spacing = tail[: len(tail) - len(tail.lstrip())]
            return CATALOG[source] + spacing + translate(tail.lstrip(), _depth + 1, locale=locale)
    return text


PREFIXES = sorted(
    (k for k, v in CATALOG.items() if isinstance(v, str) and k.endswith((":", ": "))),
    key=len,
    reverse=True,
)


def present(value, *, field=""):
    """Return a copy with only presentation fields localized; never mutate cache."""
    if language() != "en" and not RU_CATALOG:
        return value
    if isinstance(value, dict):
        is_event = "ts" in value and "type" in value and "text" in value
        result = {}
        for key, item in value.items():
            if key in RAW_KEYS:
                result[key] = item
            elif key == "text":
                result[key] = translate(item) if is_event else item
            elif key in DISPLAY_KEYS and isinstance(item, str):
                result[key] = translate(item)
            else:
                result[key] = present(item, field=key)
        return result
    if isinstance(value, list):
        return [
            translate(item)
            if field in DISPLAY_LISTS and isinstance(item, str)
            else present(item, field=field)
            for item in value
        ]
    return value


def stream_frame(frame):
    if language() != "en" and not RU_CATALOG:
        return frame
    event, payload = frame.decode("utf-8").split("\ndata: ", 1)
    translated = present(json.loads(payload))
    return (
        event
        + "\ndata: "
        + json.dumps(translated, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
    ).encode("utf-8")


def manifest(body):
    data = json.loads(body)
    if language() == "en":
        data.update(
            name="PZ Console",
            short_name="PZ Console",
            description="Project Zomboid server management",
            lang="en",
        )
        for shortcut in data.get("shortcuts", []):
            shortcut["name"] = translate(shortcut["name"])
    return json.dumps(data, ensure_ascii=False).encode("utf-8")
