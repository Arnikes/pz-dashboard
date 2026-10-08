"""Persist the player language without changing Telegram or interface locale."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("width", [320, 1440])
def test_player_warning_language_is_independent_and_persistent(page, dashboard, language, width):
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.set_viewport_size({"width": width, "height": 900})
    requests = []

    def save(route):
        body = route.request.post_data_json
        requests.append(body)
        dashboard["overview"]["settings"].update(body)
        route.fulfill(json={"ok": True, "settings": dashboard["overview"]["settings"]})

    page.route("**/api/settings", save)
    page.goto(dashboard["url"] + "/#/maintenance")
    selector = page.get_by_role(
        "combobox",
        exact=True,
        name=("Player warning language" if language == "en" else "Язык предупреждений игрокам"),
    )
    expect(selector).to_have_value("en")
    expect(page.locator("#tgLanguage")).to_have_value("en")
    expect(selector).to_have_attribute("aria-describedby", "playerLanguageHint")
    selector.select_option("ru")
    expect(page.locator("#toasts")).to_contain_text("saved" if language == "en" else "сохранено")
    assert requests == [{"playerNotifications": {"language": "ru"}}]
    expect(page.locator("#tgLanguage")).to_have_value("en")
    page.reload()
    expect(page.locator("#startupLoader")).to_be_hidden()
    expect(selector).to_have_value("ru")
    expect(page.locator("#languageSwitch")).to_have_value(language)
    selector.focus()
    expect(selector).to_be_focused()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    out = Path(__file__).resolve().parents[2] / ".tmp-player-i18n-visual"
    out.mkdir(exist_ok=True)
    page.locator("#sec-notify").screenshot(path=str(out / f"notifications-{language}-{width}.png"))


def test_failed_save_preserves_choice_and_retries(page, dashboard):
    pending, requests = [], []

    def save(route):
        requests.append(route.request.post_data_json)
        pending.append(route)

    page.route("**/api/settings", save)
    page.goto(dashboard["url"] + "/#/maintenance")
    selector = page.locator("#playerLanguage")
    selector.select_option("ru")
    expect(
        page.locator('#playerNotifications .settings-feedback[data-state="pending"]')
    ).to_be_visible()
    pending.pop(0).fulfill(status=503, json={"error": "Сервис недоступен"})
    expect(
        page.locator('#playerNotifications .settings-feedback[data-state="error"]')
    ).to_be_visible()
    page.locator("#hNotify").click()
    page.evaluate("renderOverview({...S.overview,settings:{playerNotifications:{language:'en'}}})")
    expect(selector).to_have_value("ru")
    page.locator("#playerNotifications").get_by_role("button", name="Повторить сохранение").click()
    expect(
        page.locator('#playerNotifications .settings-feedback[data-state="pending"]')
    ).to_be_visible()
    pending.pop(0).fulfill(json={"ok": True})
    expect(page.locator("#toasts")).to_contain_text("Предупреждения игрокам: сохранено")
    assert requests[0] == requests[1] == {"playerNotifications": {"language": "ru"}}


def test_telegram_save_preserves_failed_player_language_choice(page, dashboard):
    requests = []

    def save(route):
        body = route.request.post_data_json
        requests.append(body)
        if "playerNotifications" in body:
            route.fulfill(status=503, json={"error": "Сервис недоступен"})
        else:
            dashboard["overview"]["settings"].update(body)
            route.fulfill(json={"ok": True, "settings": dashboard["overview"]["settings"]})

    page.route("**/api/settings", save)
    page.goto(dashboard["url"] + "/#/maintenance")
    page.locator("#playerLanguage").select_option("ru")
    error = page.locator('#playerNotifications .settings-feedback[data-state="error"]')
    expect(error).to_be_visible()
    page.locator("#tgLanguage").select_option("ru")
    expect(page.locator("#toasts")).to_contain_text("Telegram: сохранено")
    expect(error).to_be_visible()
    expect(page.locator("#playerLanguage")).to_have_value("ru")
    expect(page.locator("#tgLanguage")).to_have_value("ru")
    error.get_by_role("button", name="Повторить сохранение").click()
    expect(error).to_be_visible()
    assert requests[0] == requests[-1] == {"playerNotifications": {"language": "ru"}}
