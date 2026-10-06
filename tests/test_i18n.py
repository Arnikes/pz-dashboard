"""Locale boundaries, catalog parity and preservation of operational data."""

import ast
import json
import re
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path

import pytest

import app
import i18n
import payloads
import workshop
from test_auth import auth_server as auth_server, request

ROOT = Path(app.STATIC_DIR)


@pytest.fixture
def english():
    token = i18n.LANGUAGE.set("en")
    yield
    i18n.LANGUAGE.reset(token)


@pytest.mark.parametrize(
    "headers,path,expected",
    [
        ({}, "", "ru"),
        ({"Accept-Language": "en-US,en;q=0.8"}, "", "en"),
        ({"Accept-Language": "en;q=0.5, ru-RU; q=0.9"}, "", "ru"),
        ({"Accept-Language": "ru;q=0,en;q=0.2"}, "", "en"),
        ({"Accept-Language": "de,xx;q=bad"}, "", "ru"),
        ({"Cookie": "pz_language=ru", "Accept-Language": "en"}, "", "ru"),
        ({"Cookie": "pz_language=ru", "X-PZ-Language": "en"}, "", "en"),
        ({"Cookie": "pz_language=ru"}, "/manifest.webmanifest?lang=en", "en"),
        ({"Cookie": "pz_language=en-unsafe"}, "", "ru"),
        ({"X-PZ-Language": "unsupported"}, "?lang=unsupported", "ru"),
    ],
)
def test_locale_negotiation(headers, path, expected):
    assert i18n.resolve(headers, path) == expected


def test_catalog_parity_placeholders_and_english_coverage():
    en = json.loads((ROOT / "locales/en.json").read_text(encoding="utf-8"))
    ru = json.loads((ROOT / "locales/ru.json").read_text(encoding="utf-8"))
    assert en.keys() == ru.keys()
    for key in en:
        slots = sorted(re.findall(r"{{\d+}}", key))
        for language in (en, ru):
            forms = language[key].values() if isinstance(language[key], dict) else [language[key]]
            for form in forms:
                assert sorted(re.findall(r"{{\d+}}", form)) == slots, (key, form)
        assert not re.search("[А-Яа-яЁё]", str(en[key])), key


def test_static_pages_have_complete_translations():
    class Texts(HTMLParser):
        skip = 0
        missing = []
        language_option = False

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "textarea", "pre", "code"):
                self.skip += 1
            if tag == "option" and dict(attrs).get("lang") in ("en", "ru"):
                # Autonyms identify the target language, independent of UI locale.
                self.language_option = True
                self.skip += 1
            for name, value in attrs:
                if name in (
                    "title",
                    "aria-label",
                    "aria-description",
                    "placeholder",
                    "data-help-text",
                    "data-help-label",
                    "content",
                ):
                    self.check(value)

        def handle_endtag(self, tag):
            if tag in ("script", "style", "textarea", "pre", "code"):
                self.skip -= 1
            if tag == "option" and self.language_option:
                self.language_option = False
                self.skip -= 1

        def handle_data(self, value):
            if not self.skip:
                self.check(value)

        def check(self, value):
            if value and re.search("[А-Яа-яЁё]", value):
                key = re.sub(r"\s+", " ", value).strip()
                if key not in i18n.CATALOG:
                    self.missing.append(key)

    parser = Texts()
    for name in ("index.html", "login.html", "offline.html", "config-help.html"):
        parser.feed((ROOT / name).read_text(encoding="utf-8"))
    assert not parser.missing


def test_backend_message_inventory_is_translated():
    for path in ROOT.parent.glob("*.py"):
        if path.stem == "i18n":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr)}
        fragments = {
            id(v)
            for n in ast.walk(tree)
            if isinstance(n, ast.JoinedStr)
            for v in n.values
            if isinstance(v, ast.Constant)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and id(node) not in docs | fragments
                and isinstance(node.value, str)
            ):
                text = node.value
            elif isinstance(node, ast.JoinedStr):
                positions = iter(range(100))
                text = "".join(
                    v.value if isinstance(v, ast.Constant) else "{{%s}}" % next(positions)
                    for v in node.values
                )
            else:
                continue
            if re.search("[А-Яа-яЁё]", text):
                assert re.sub(r"\s+", " ", text).strip() in i18n.CATALOG, (path.name, text)


def test_presentation_does_not_translate_operational_values(english):
    raw = {
        "serverName": "Сервер",
        "names": ["Игроки", "Сервер"],
        "output": "Сервер перезапущен",
        "text": "Логи",
        "texts": {"ini": "PublicName=Сервер\n", "sandbox": "Lua"},
        "diff": {"ini": "-Имя\n+Сервер"},
        "fields": [
            {
                "key": "PublicName",
                "value": "Название сервера",
                "label": "Название сервера",
                "hint": "Применяется после запуска сервера",
            }
        ],
        "error": "Неверный логин или пароль",
        "sandboxDiagnostic": "Lua-функции и вычисления не поддерживаются",
        "items": [{"ts": "now", "type": "restart", "text": "Сервер перезапущен"}],
    }
    before = json.dumps(raw, ensure_ascii=False)
    result = i18n.present(raw)
    assert result["error"] == "Invalid username or password"
    assert result["fields"][0]["label"] == "Server name"
    assert result["sandboxDiagnostic"] == "Lua functions and expressions are unsupported"
    assert result["items"][0]["text"] == "Server restarted"
    for key in ("serverName", "names", "output", "text", "texts", "diff"):
        assert result[key] == raw[key]
    assert result["fields"][0]["value"] == raw["fields"][0]["value"]
    assert json.dumps(raw, ensure_ascii=False) == before


def test_parameterized_errors_preserve_interpolation(english):
    assert (
        i18n.translate("Неподдерживаемое выражение Lua, позиция 42")
        == "Unsupported Lua expression at position 42"
    )
    assert i18n.translate("Не удалось запустить: {{0}}") == "Could not start: {{0}}"
    assert i18n.translate("Игроки: требуется Сервер") == "Игроки: requires Сервер"
    assert i18n.translate("External provider diagnostic") == "External provider diagnostic"
    assert (
        i18n.translate("Не удалось запустить: Неверный логин или пароль")
        == "Could not start: Invalid username or password"
    )
    assert (
        i18n.translate("Бэкап создан (вручную): backup.tar.gz (52.3 МБ)")
        == "Backup created (manual): backup.tar.gz (52.3 MB)"
    )


def test_http_errors_and_manifest_follow_locale(auth_server):
    status, headers, body = request(
        auth_server, "GET", "/api/auth/session", headers={"X-PZ-Language": "en"}
    )
    assert status == 401 and body["error"] == "Administrator sign-in required"
    assert headers["Content-Language"] == "en"
    _, _, body = request(
        auth_server,
        "POST",
        "/api/auth/login",
        {"login": "bad", "password": "bad"},
        headers={"X-PZ-Language": "en"},
    )
    assert body["error"] == "Invalid username or password"
    _, headers, body = request(auth_server, "GET", "/manifest.webmanifest?lang=en")
    assert json.loads(body)["lang"] == "en" and json.loads(body)["name"] == "PZ Console"
    assert headers["Content-Language"] == "en" and "Cookie" in headers["Vary"]
    _, _, body = request(auth_server, "GET", "/api/auth/session")
    assert body["error"] == "Требуется вход администратора"


def test_simultaneous_languages_keep_stream_caches_isolated(monkeypatch):
    monkeypatch.setattr(
        payloads,
        "stream_payload",
        lambda _name: {"ok": True, "fields": [{"label": "Название сервера"}], "text": "Логи"},
    )
    cache = payloads.StreamCache()

    def frame(locale):
        token = i18n.LANGUAGE.set(locale)
        try:
            return i18n.stream_frame(cache.frame("mods", 60)).decode("utf-8")
        finally:
            i18n.LANGUAGE.reset(token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        en, ru = list(pool.map(frame, ("en", "ru")))
    assert "Server name" in en and "Название сервера" in ru
    assert "Логи" in en and "Логи" in ru
    assert i18n.language() == "ru"


def test_workshop_uses_requested_language_without_overwriting_values():
    files = {
        "111/mods/Example/42/mod.info": "id=Example\nname=Имя мода\n",
        "111/mods/Example/42/media/sandbox-options.txt": "option Example.Count { type = integer, default = 3, translation = Example_Count, }",
        "111/mods/Example/42/media/lua/shared/Translate/EN/Sandbox_EN.txt": 'Sandbox_Example_Count = "Count",',
        "111/mods/Example/42/media/lua/shared/Translate/RU/Sandbox_RU.txt": 'Sandbox_Example_Count = "Количество",',
    }
    for locale, label in (("en", "Count"), ("ru", "Количество")):
        token = i18n.LANGUAGE.set(locale)
        try:
            record = workshop.build_index(files, ["111"], "42.15.1")["111"][0]
            assert record["name"] == "Имя мода"
            assert record["options"][0]["label"] == label
            assert record["options"][0]["default"] == 3
        finally:
            i18n.LANGUAGE.reset(token)
