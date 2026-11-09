"""MTProxy controls preserve settings and secrets across autosave and errors."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from browser_evidence import capture_evidence


pytestmark = pytest.mark.browser
FAKETLS_SECRET = "ee" + "a1" * 16 + "proxy.edge.example.com".encode("ascii").hex()


def public_settings(values):
    return {
        **values,
        "botToken": "",
        "botTokenMasked": "•••" if values.get("botToken") or values.get("botTokenMasked") else "",
        "apiHash": "",
        "mtproxySecret": "",
        "hasApiHash": bool(values.get("apiHash") or values.get("hasApiHash")),
        "hasMtproxySecret": bool(values.get("mtproxySecret") or values.get("hasMtproxySecret")),
    }


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("width", [320, 1440])
def test_proxy_controls_save_reload_and_explain_manual_chat_id(page, dashboard, language, width):
    requests = []
    saved = {}
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.set_viewport_size({"width": width, "height": 900})

    def settings(route):
        values = route.request.post_data_json["telegram"]
        requests.append(values)
        for key, value in values.items():
            if value or key not in ("botToken", "apiHash", "mtproxySecret"):
                saved[key] = value
        dashboard["overview"]["settings"]["telegram"] = public_settings(saved)
        route.fulfill(json={"ok": True, "settings": dashboard["overview"]["settings"]})

    page.route("**/api/settings", settings)
    page.goto(dashboard["url"] + "/#/maintenance")
    absent = "Не задано" if language == "ru" else "Not set"
    present = "Значение задано · скрыто" if language == "ru" else "Value set · hidden"
    expect(page.locator("#tgTokenState")).to_have_text(absent)
    expect(page.locator("#tgMtproxyFields")).to_be_hidden()
    proxy_switch = page.get_by_role(
        "switch",
        name="Подключаться через MTProxy" if language == "ru" else "Connect through MTProxy",
    )
    proxy_switch.focus()
    page.keyboard.press("Space")
    expect(page.locator("#tgMtproxyFields")).to_be_visible()
    for control in ("tgMtproxySecret", "tgApiHash"):
        expect(page.locator(f"#{control}State")).to_have_text(absent)
    expect(page.locator("#btnTgChats")).to_be_disabled()
    expect(page.locator("#tgMtproxyChatsHint")).to_contain_text(
        "вручную" if language == "ru" else "manually"
    )
    expect(page.locator("#tgMtproxySecretHint")).to_contain_text("ee + 32")
    expect(page.locator("#tgMtproxySecretHint")).to_contain_text(
        "секрет целиком" if language == "ru" else "complete secret"
    )
    assert page.locator("#tgMtproxySecret").get_attribute("maxlength") is None
    for control, value in (
        ("tgToken", "123456:fixture-token"),
        ("tgMtproxyHost", "proxy.example.com"),
        ("tgMtproxyPort", "8443"),
        ("tgMtproxySecret", FAKETLS_SECRET),
        ("tgApiId", "123456"),
        ("tgApiHash", "b2" * 16),
    ):
        page.locator(f"#{control}").fill(value)
        page.locator(f"#{control}").dispatch_event("change")
        expect(page.locator("#sec-notify > .settings-feedback")).to_have_count(0)
    expect(page.locator("#tgMtproxySecret")).to_have_value("")
    expect(page.locator("#tgApiHash")).to_have_value("")
    expect(page.locator("#tgMtproxySecretState")).to_be_visible()
    expect(page.locator("#tgApiHashState")).to_be_visible()
    for control in ("tgToken", "tgMtproxySecret", "tgApiHash"):
        expect(page.locator(f"#{control}State")).to_have_text(present)
        expect(page.locator(f"#{control}")).to_have_value("")
        assert (
            f"{control}State"
            in page.locator(f"#{control}").get_attribute("aria-describedby").split()
        )
    assert any(value["mtproxySecret"] == FAKETLS_SECRET for value in requests)
    assert saved["mtproxySecret"] == FAKETLS_SECRET
    assert requests[-1]["mtproxySecret"] == ""
    assert dashboard["overview"]["settings"]["telegram"]["mtproxySecret"] == ""
    assert any(value["apiHash"] == "b2" * 16 for value in requests)
    assert requests[-1]["mtproxyEnabled"] is True
    assert requests[-1]["apiId"] == 123456
    assert requests[-1]["mtproxyPort"] == 8443
    page.reload()
    expect(page.locator("#tgMtproxySwitch")).to_be_checked()
    expect(page.locator("#tgMtproxyHost")).to_have_value("proxy.example.com")
    expect(page.locator("#tgMtproxyPort")).to_have_value("8443")
    expect(page.locator("#tgApiId")).to_have_value("123456")
    expect(page.locator("#tgMtproxySecret")).to_have_attribute("type", "password")
    expect(page.locator("#tgApiHash")).to_have_attribute("type", "password")
    expect(page.locator("#tgMtproxySecret")).to_have_value("")
    for control in ("tgToken", "tgMtproxySecret", "tgApiHash"):
        expect(page.locator(f"#{control}State")).to_have_text(present)
    assert saved["mtproxySecret"] == FAKETLS_SECRET
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator("#tgMtproxyHost").evaluate("el => el.labels.length") == 1
    page.locator("#tgMtproxyHost").focus()
    page.keyboard.press("Tab")
    expect(page.locator("#tgMtproxyPort")).to_be_focused()
    out = Path(__file__).resolve().parents[2] / ".tmp-telegram-mtproxy-visual"
    out.mkdir(exist_ok=True)
    capture_evidence(
        page.locator("#sec-notify"), path=str(out / f"notifications-{language}-{width}.png")
    )
    proxy_switch.focus()
    page.keyboard.press("Space")
    expect(page.locator("#tgMtproxyFields")).to_be_hidden()
    expect(page.locator("#btnTgChats")).to_be_enabled()


def test_proxy_secret_new_generation_survives_failed_save_and_stale_overview(page, dashboard):
    pending = []
    dashboard["overview"]["settings"] = {
        "telegram": {"mtproxyEnabled": True, "hasMtproxySecret": True},
        "version": {"epoch": "secret-fixture", "revision": 2},
    }
    page.route("**/api/settings", lambda route: pending.append(route))
    page.goto(dashboard["url"] + "/#/maintenance")
    secret = page.locator("#tgMtproxySecret")
    expect(page.locator("#tgMtproxySecretState")).to_have_text("Значение задано · скрыто")
    secret.fill("a1" * 16)
    with page.expect_request("**/api/settings"):
        secret.dispatch_event("change")
    secret.fill("b2" * 16)
    with page.expect_request("**/api/settings"):
        secret.dispatch_event("change")
        pending.pop(0).fulfill(json={"ok": True})
    expect(secret).to_have_value("b2" * 16)
    expect(page.locator("#tgMtproxySecretState")).to_have_text("Значение задано · скрыто")
    pending.pop(0).fulfill(status=503, json={"error": "Сервис недоступен"})
    expect(page.locator('.settings-feedback[data-state="error"]')).to_be_visible()
    page.evaluate("renderOverview({...S.overview,settings:{telegram:{mtproxyEnabled:false}}})")
    expect(page.locator("#tgMtproxySwitch")).to_be_checked()
    expect(secret).to_have_value("b2" * 16)
    with page.expect_request("**/api/settings"):
        page.get_by_role("button", name="Повторить сохранение").click()
    values = pending[0].request.post_data_json["telegram"]
    assert values["mtproxySecret"] == "b2" * 16
    pending.pop(0).fulfill(
        json={
            "ok": True,
            "settings": {
                "telegram": public_settings(values),
                "version": {"epoch": "secret-fixture", "revision": 3},
            },
        }
    )
    expect(secret).to_have_value("")
    expect(page.locator("#tgMtproxySwitch")).to_be_checked()
    page.evaluate("""renderOverview({...S.overview, settings:{
        telegram:{mtproxyEnabled:false,hasMtproxySecret:false},
        version:{epoch:'secret-fixture',revision:1}}})""")
    expect(page.locator("#tgMtproxySecretState")).to_have_text("Значение задано · скрыто")


def test_proxy_test_waits_for_edits_made_while_save_is_pending(page, dashboard):
    pending = []
    requests = []
    dashboard["overview"]["settings"] = {"telegram": {"mtproxyEnabled": True}}

    def settings(route):
        secret = route.request.post_data_json["telegram"]["mtproxySecret"]
        requests.append(("settings", secret))
        if len(requests) == 1:
            pending.append(route)
        else:
            route.fulfill(json={"ok": True})

    page.route("**/api/settings", settings)

    def test_message(route):
        requests.append(("test", None))
        route.fulfill(json={"ok": True})

    page.route("**/api/notify-test", test_message)
    page.goto(dashboard["url"] + "/#/maintenance")
    with page.expect_request("**/api/settings"):
        page.locator("#btnTgTest").click()
    page.locator("#tgMtproxySecret").fill(FAKETLS_SECRET)
    assert requests == [("settings", "")]
    with page.expect_request("**/api/notify-test"):
        pending.pop(0).fulfill(json={"ok": True})
    expect(page.locator("#toasts")).to_contain_text("Отправлено")
    assert requests == [("settings", ""), ("settings", FAKETLS_SECRET), ("test", None)]
    expect(page.locator("#tgMtproxySecret")).to_have_value("")


@pytest.mark.parametrize("invalid_enabled", ["true", "yes", None, 0])
def test_invalid_proxy_toggle_cannot_silently_save_direct_mode(page, dashboard, invalid_enabled):
    requests = []
    discoveries = []
    dashboard["overview"]["settings"] = {"telegram": {"mtproxyEnabled": invalid_enabled}}

    def settings(route):
        values = route.request.post_data_json["telegram"]
        requests.append(values)
        route.fulfill(json={"ok": True})

    page.route("**/api/settings", settings)
    page.route("**/api/notify-test", lambda route: route.fulfill(json={"ok": True}))
    page.on(
        "request",
        lambda request: (
            discoveries.append(request.url) if "/api/telegram-chats" in request.url else None
        ),
    )
    page.goto(dashboard["url"] + "/#/maintenance")
    expect(page.locator("#tgMtproxySwitch")).to_be_checked()
    expect(page.locator("#tgMtproxyConfigHint")).to_be_visible()
    expect(page.locator("#btnTgChats")).to_be_disabled()
    page.evaluate("document.getElementById('btnTgChats').click()")
    page.locator("#btnTgTest").click()
    expect(page.locator("#toasts")).to_contain_text("Отправлено")
    assert requests and all(values["mtproxyEnabled"] is True for values in requests)
    assert not discoveries
