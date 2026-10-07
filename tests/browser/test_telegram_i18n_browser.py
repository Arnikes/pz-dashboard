"""Notification locale saves independently of UI locale, including failed saves."""

from pathlib import Path

import pytest
from playwright.sync_api import expect


pytestmark = pytest.mark.browser


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("width", [320, 1440])
def test_language_selector_persists_and_test_waits_for_save(page, dashboard, language, width):
    requests = []
    notification_language = "en" if language == "ru" else "ru"
    page.add_init_script(
        f"if (!localStorage.getItem('pz-language')) localStorage.setItem('pz-language', '{language}')"
    )
    page.set_viewport_size({"width": width, "height": 900})

    def settings(route):
        body = route.request.post_data_json
        requests.append(("settings", body))
        dashboard["overview"]["settings"] = {
            "telegram": {**body["telegram"], "botToken": "", "botTokenMasked": "•••cret"},
        }
        route.fulfill(json={"ok": True, "settings": dashboard["overview"]["settings"]})

    def test_message(route):
        requests.append(("test", {}))
        route.fulfill(json={"ok": True})

    page.route("**/api/settings", settings)
    page.route("**/api/notify-test", test_message)
    page.goto(dashboard["url"] + "/#/maintenance")
    selector = page.get_by_role(
        "combobox",
        name="Язык уведомлений" if language == "ru" else "Notification language",
        exact=True,
    )
    expect(selector).to_have_value("en")
    expect(selector).to_have_attribute("aria-describedby", "tgLanguageHint")
    selector.select_option(notification_language)
    expect(page.locator("#toasts")).to_contain_text("сохранено" if language == "ru" else "saved")
    assert requests[0][1]["telegram"]["language"] == notification_language
    expect(page.locator("#languageSwitch")).to_have_value(language)
    page.reload()
    expect(selector).to_have_value(notification_language)
    page.locator("#btnTgTest").click()
    expect(page.locator("#toasts")).to_contain_text("Отправлено" if language == "ru" else "Sent")
    assert requests[-2][0] == "settings" and requests[-1][0] == "test"
    assert requests[-2][1]["telegram"]["language"] == notification_language
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator("#sec-notify").scroll_into_view_if_needed()
    out = Path(__file__).resolve().parents[2] / ".tmp-telegram-i18n-visual"
    out.mkdir(exist_ok=True)
    page.locator("#sec-notify").screenshot(path=str(out / f"notifications-{language}-{width}.png"))
    page.locator("#languageSwitch").select_option(notification_language)
    expect(page.locator("html")).to_have_attribute("lang", notification_language)
    # The new document sets its locale before the scripts and saved settings load.
    page.wait_for_load_state("load")
    expect(page.locator("#btnStop")).to_be_enabled()
    expect(page.locator("#tgLanguage")).to_have_value(notification_language)


def test_failed_language_save_survives_stale_snapshot_and_retries(page, dashboard):
    pending = []
    requests = []

    def settings(route):
        requests.append(route.request.post_data_json)
        pending.append(route)

    page.route("**/api/settings", settings)
    page.goto(dashboard["url"] + "/#/maintenance")
    selector = page.locator("#tgLanguage")
    selector.select_option("en")
    expect(page.locator('.settings-feedback[data-state="pending"]')).to_be_visible()
    pending.pop(0).fulfill(status=503, json={"error": "Сервис недоступен"})
    expect(page.locator('.settings-feedback[data-state="error"]')).to_be_visible()
    page.locator("#hNotify").click()
    page.evaluate("renderOverview({...S.overview,settings:{telegram:{language:'ru'}}})")
    expect(selector).to_have_value("en")
    page.get_by_role("button", name="Повторить сохранение").click()
    expect(page.locator('.settings-feedback[data-state="pending"]')).to_be_visible()
    pending.pop(0).fulfill(json={"ok": True})
    expect(page.locator("#toasts")).to_contain_text("Telegram: сохранено")
    assert requests[0] == requests[1]
    assert requests[1]["telegram"]["language"] == "en"
