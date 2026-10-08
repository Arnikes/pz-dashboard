"""EN/RU UI paths, persisted choice, drafts, formatting and interpolation."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from test_editors import editing, env  # noqa: F401
from test_pwa_browser import pwa_server, ready  # noqa: F401

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("width", [320, 1440])
def test_complete_route_matrix(page, dashboard, editing, language, width):  # noqa: F811
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator('[data-key="PublicName"]')).to_be_enabled()
    expect(page.locator("html")).to_have_attribute("lang", language)
    expect(page.locator('[data-key="PublicName"]')).to_have_value("Сервер")
    label = "Server name" if language == "en" else "Название сервера"
    expect(page.locator(".config-label label").filter(has_text=label)).to_be_visible()
    for route in (
        "overview",
        "settings",
        "mods",
        "players",
        "maintenance",
        "backups",
        "events",
        "console",
    ):
        page.evaluate("route => location.hash = '#/' + route", route)
        expect(page.locator(f"#view-{route}")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
            language,
            width,
            route,
        )
        if language == "en":
            assert not page.evaluate("""() => {
                const root = document.querySelector('.main');
                const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                const found = [];
                while(walker.nextNode()) {
                    const n = walker.currentNode, parent=n.parentElement;
                    if(parent.closest('textarea,pre,code,script,style') || !parent.checkVisibility()) continue;
                    if(/[А-Яа-яЁё]/.test(n.nodeValue)) found.push(n.nodeValue.trim());
                }
                return found;
            }"""), route
    out = Path(__file__).resolve().parents[2] / ".tmp-i18n-evidence"
    out.mkdir(exist_ok=True)
    page.evaluate("location.hash='#/settings'")
    expect(page.locator("#view-settings")).to_be_visible()
    page.wait_for_function("!document.getAnimations().some(a => a.playState === 'running')")
    page.screenshot(path=str(out / f"settings-{language}-{width}.png"), full_page=True)


def test_language_switch_flushes_draft_and_preserves_route(page, dashboard, editing):  # noqa: F811
    page.goto(dashboard["url"] + "/#/settings")
    field = page.locator('[data-key="PublicName"]')
    expect(field).to_be_enabled()
    field.fill("Мой сервер {{0}} <test>")
    with page.expect_navigation(wait_until="load"):
        page.locator("#languageSwitch").select_option("en")
    expect(page.locator("html")).to_have_attribute("lang", "en")
    expect(field).to_have_value("Мой сервер {{0}} <test>")
    assert page.url.endswith("#/settings")
    page.reload()
    expect(page.locator("#languageSwitch")).to_have_value("en")
    expect(field).to_have_value("Мой сервер {{0}} <test>")
    with page.expect_navigation(wait_until="load"):
        page.locator("#languageSwitch").select_option("ru")
    expect(page.locator("html")).to_have_attribute("lang", "ru")
    expect(field).to_have_value("Мой сервер {{0}} <test>")


def test_blocked_language_switch_keeps_unsaved_console_input(page, dashboard):
    page.goto(dashboard["url"] + "/#/console")
    page.locator("#consoleInput").fill('servermsg "Не терять"')
    page.locator("#languageSwitch").select_option("en")
    expect(page.locator("#languageStatus")).to_be_visible()
    expect(page.locator("#languageSwitch")).to_have_value("ru")
    expect(page.locator("#consoleInput")).to_have_value('servermsg "Не терять"')
    assert page.evaluate("localStorage.getItem('pz-language')") is None


@pytest.mark.browser_context_args(locale="en-US")
def test_browser_language_login_help_offline_and_formatting(page, dashboard):
    page.goto(dashboard["url"] + "/login.html")
    expect(page.get_by_role("button", name="Sign in", exact=True)).to_be_visible()
    expect(page.locator("html")).to_have_attribute("lang", "en")
    page.goto(dashboard["url"] + "/config-help.html")
    expect(
        page.get_by_role("heading", name="Settings, mods and recovery", exact=True)
    ).to_be_visible()
    page.goto(dashboard["url"] + "/offline.html")
    expect(page.get_by_role("heading", name="Console disconnected", exact=True)).to_be_visible()
    page.goto(dashboard["url"] + "/")
    assert page.evaluate("I18n.number(1234.5)") == "1,234.5"
    assert page.evaluate("fmtBytes(1024 * 1024)") == "1.0 MB"
    assert page.evaluate("fmtUptime(61)") == "1 min"
    assert page.evaluate("[1,2,5,21].map(n=>I18n.count('{{0}} игроков',n))") == [
        "1 player",
        "2 players",
        "5 players",
        "21 players",
    ]
    # Repeated placeholder-like user values are substituted once, without
    # translating Cyrillic content or treating it as markup.
    assert page.evaluate("I18n.msg`${'{{1}} <Игроки>'} из ${2}`") == "{{1}} <Игроки> of 2"
    assert (
        page.evaluate("I18n.html('<button title=\"Обновить\">Обновить</button>')")
        == '<button title="Refresh">Refresh</button>'
    )


def test_russian_plural_rules_and_storage_denial(page, dashboard):
    page.add_init_script(
        "Storage.prototype.getItem = () => {throw new Error('blocked')}; Storage.prototype.setItem = () => {throw new Error('blocked')}"
    )
    page.goto(dashboard["url"] + "/")
    assert page.evaluate("[1,2,5,21,22,25].map(n=>I18n.count('{{0}} игроков',n))") == [
        "1 игрок",
        "2 игрока",
        "5 игроков",
        "21 игрок",
        "22 игрока",
        "25 игроков",
    ]
    page.locator("#languageSwitch").select_option("en")
    expect(page.locator("html")).to_have_attribute("lang", "en")
    page.reload()
    expect(page.locator("html")).to_have_attribute("lang", "en")


def test_login_language_changes_in_place_without_storing_credentials(page, dashboard):
    page.goto(dashboard["url"] + "/login.html")
    page.locator("#login").fill("имя {{0}}")
    page.locator("#password").fill("секрет <private>")
    page.locator("[data-language-switch]").select_option("en")
    expect(page.get_by_role("button", name="Sign in", exact=True)).to_be_visible()
    expect(page.locator('[data-language-switch] option[lang="ru"]')).to_have_text("Русский")
    expect(page.locator('[data-language-switch] option[lang="en"]')).to_have_text("English")
    expect(page.locator("#login")).to_have_value("имя {{0}}")
    expect(page.locator("#password")).to_have_value("секрет <private>")
    assert page.evaluate("JSON.stringify(localStorage)") == '{"pz-language":"en"}'
    page.locator("[data-language-switch]").select_option("ru")
    expect(page.get_by_role("button", name="Войти", exact=True)).to_be_visible()
    expect(page.locator('[data-language-switch] option[lang="ru"]')).to_have_text("Русский")
    expect(page.locator('[data-language-switch] option[lang="en"]')).to_have_text("English")
    expect(page.locator("#password")).to_have_value("секрет <private>")


@pytest.mark.parametrize("value", ["ru", "en", "ru&injected=<img src=x onerror=alert(1)>"])
def test_language_change_uses_only_supported_language_codes(page, dashboard, value):
    page.goto(dashboard["url"] + "/login.html")
    page.evaluate(
        """value => {
            const select = document.querySelector('[data-language-switch]');
            select.add(new Option('Injected option', value));
            select.value = value;
            select.dispatchEvent(new Event('change'));
        }""",
        value,
    )
    language = "ru" if value == "ru" else "en"
    expect(page.locator("html")).to_have_attribute("lang", language)
    assert page.evaluate("I18n.language") == language
    assert page.evaluate("localStorage.getItem('pz-language')") == language
    assert page.evaluate("document.cookie") == f"pz_language={language}"
    assert page.locator('link[rel="manifest"]').get_attribute("href") == (
        f"/manifest.webmanifest?lang={language}"
    )


@pytest.mark.browser_context_args(locale="en-US", service_workers="allow")
def test_english_offline_shell_has_cached_translations(page, context, pwa_server):  # noqa: F811
    ready(page, pwa_server)
    expect(page.get_by_role("button", name="Sign in", exact=True)).to_be_visible()
    context.set_offline(True)
    page.goto(pwa_server["url"] + "/#/mods")
    expect(page.get_by_role("heading", name="Console disconnected")).to_be_visible()
    page.get_by_role("button", name="Reconnect", exact=True).click()
    expect(page.locator("#pwaRetryStatus")).to_contain_text("Console still unavailable")
    page.locator("[data-language-switch]").select_option("ru")
    expect(page.get_by_role("heading", name="Нет связи с пультом")).to_be_visible()
    assert page.url.endswith("#/mods")
    context.set_offline(False)


@pytest.mark.browser_context_args(locale="en-US")
def test_english_confirmation_and_safe_dynamic_values(page, dashboard):
    # Keep the API and reconnecting SSE fixture consistent with the tested frame.
    dashboard["players"].update(names=["Игроки {{0}} <script>"], count=1)
    page.goto(dashboard["url"] + "/")
    expect(page.get_by_role("button", name="Stop", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Stop", exact=True).click()
    expect(page.get_by_role("heading", name="Stop server?", exact=True)).to_be_visible()
    expect(page.locator("#warnSel")).to_contain_text("no warning")
    page.locator("#modalCancel").click()
    assert dashboard["actions"] == []
    page.evaluate("location.hash='#/players'")
    expect(page.locator("#playersBody")).to_contain_text("Игроки {{0}} <script>")
    assert page.locator("#playersBody script").count() == 0


def test_unavailable_catalog_keeps_console_usable(page, dashboard):
    page.route("**/static/catalogs.js", lambda route: route.abort())
    page.goto(dashboard["url"] + "/#/console")
    expect(page.locator("#consoleInput")).to_be_visible()
    expect(page.get_by_role("button", name="Отправить", exact=True)).to_be_visible()
    expect(page.locator("#languageSwitch")).to_have_value("ru")
