"""Configure recurring maintenance from the localized settings forms."""

import pytest
from playwright.sync_api import expect

from browser_evidence import capture_evidence

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("language,width", [("en", 1440), ("ru", 320)])
def test_schedules_save_as_separate_groups_and_survive_language_change(
    page, dashboard, tmp_path, language, width
):
    page.add_init_script(
        f"if (!localStorage.getItem('pz-language')) localStorage.setItem('pz-language', '{language}')"
    )
    page.set_viewport_size({"width": width, "height": 900})
    overview = dashboard["overview"]
    settings = overview["settings"]
    settings["version"] = {"epoch": "test", "revision": 0}
    requests = []

    def save(route):
        patch = route.request.post_data_json
        requests.append(patch)
        settings.update(patch)
        settings["version"]["revision"] += 1
        if "chatAnnouncements" in patch:
            settings["nextChatAnnouncement"] = (
                1794497400 if patch["chatAnnouncements"]["enabled"] else None
            )
        if "scheduledRestart" in patch:
            settings["nextScheduledRestart"] = 1794664800
        route.fulfill(json={"ok": True, "settings": settings})

    page.route("**/api/settings", save)
    page.goto(dashboard["url"] + "/#/maintenance")
    chat_names = {"en": "In-game chat announcements", "ru": "Объявления в игровом чате"}
    restart_names = {"en": "Scheduled server restart", "ru": "Плановый перезапуск сервера"}
    chat = page.get_by_role("form", name=chat_names[language])
    restart = page.get_by_role("form", name=restart_names[language])
    chat_status = chat.locator("#chatAnnouncementsStatus")
    disabled = "Schedule disabled" if language == "en" else "Расписание выключено"
    next_run = "Next run" if language == "en" else "Следующий запуск"
    message = chat.get_by_role("textbox")
    chat_switch = chat.get_by_role("switch", name=chat_names[language], exact=True)
    restart_switch = restart.get_by_role("switch", name=restart_names[language], exact=True)
    chat_save = chat.get_by_role(
        "button", name="Save schedule" if language == "en" else "Сохранить расписание"
    )
    expect(chat_switch).not_to_be_checked()
    expect(restart_switch).not_to_be_checked()
    expect(chat.get_by_role("spinbutton")).to_have_value("30")
    expect(chat_status).to_have_text(disabled)
    expect(restart.get_by_role("spinbutton")).to_have_value("24")

    chat.locator("label.switch").click()
    chat_save.click()
    assert message.evaluate("el => !el.validity.valid")
    message.fill("🚀" * 201)
    chat_save.click()
    assert message.evaluate("el => !el.validity.valid")
    message.fill("🚀" * 200)
    chat.get_by_role("spinbutton").fill("12")
    expect(chat_status).to_have_text(disabled)
    assert requests == []
    message.press("Enter")
    expect(chat_status).to_contain_text(next_run)
    assert requests == [
        {"chatAnnouncements": {"enabled": True, "message": "🚀" * 200, "intervalMinutes": 12}}
    ]

    message.fill("Сервер остановлен")
    chat_save.click()
    expect(chat_save).to_be_enabled()
    expect(
        page.locator('#sec-chat-announcements .settings-feedback[data-state="ok"]')
    ).to_be_visible()
    assert requests[-1] == {
        "chatAnnouncements": {
            "enabled": True,
            "message": "Сервер остановлен",
            "intervalMinutes": 12,
        }
    }

    restart_interval = restart.get_by_role("spinbutton")
    restart_save = restart.get_by_role("button")
    restart.locator("label.switch").click()
    restart_interval.fill("169")
    restart_save.click()
    assert restart_interval.evaluate("el => el.validity.rangeOverflow")
    assert len(requests) == 2
    restart_interval.fill("48")
    restart_save.click()
    expect(restart.locator("#scheduledRestartStatus")).to_contain_text(next_run)
    assert requests[-1] == {"scheduledRestart": {"enabled": True, "intervalHours": 48}}

    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    capture_evidence(page.locator("#sec-scheduled-restart"), path=str(tmp_path / "restart.png"))
    capture_evidence(page.locator("#sec-chat-announcements"), path=str(tmp_path / "chat.png"))
    next_language = "ru" if language == "en" else "en"
    page.locator("#languageSwitch").select_option(next_language)
    expect(page.locator("html")).to_have_attribute("lang", next_language)
    expect(page.locator("#chatAnnouncementMessage")).to_have_value("Сервер остановлен")
    expect(page.locator("#scheduledRestartInterval")).to_have_value("48")
    expect(page.get_by_role("switch", name=chat_names[next_language], exact=True)).to_be_checked()
    expect(
        page.get_by_role("switch", name=restart_names[next_language], exact=True)
    ).to_be_checked()

    page.locator("#chatAnnouncementsForm label.switch").click()
    page.locator('#chatAnnouncementsForm button[type="submit"]').click()
    expect(page.locator("#chatAnnouncementsStatus")).to_have_text(
        "Расписание выключено" if next_language == "ru" else "Schedule disabled"
    )
    assert requests[-1] == {
        "chatAnnouncements": {
            "enabled": False,
            "message": "Сервер остановлен",
            "intervalMinutes": 12,
        }
    }


def test_remote_announcements_retry_with_draft_while_restart_is_unavailable(page, dashboard):
    settings = {
        "scheduledRestart": {"enabled": True, "intervalHours": 24},
        "nextScheduledRestart": 1794664800,
        "version": {"epoch": "test", "revision": 1},
    }
    dashboard["overview"].update(mode="remote", rcon={"state": "error"}, settings=settings)
    pending, requests = [], []

    def save(route):
        requests.append(route.request.post_data_json)
        pending.append(route)

    page.route("**/api/settings", save)
    page.goto(dashboard["url"] + "/#/maintenance")
    expect(page.locator("#scheduledRestartStatus")).to_have_text(
        "Доступно только при запуске пульта на хосте сервера"
    )
    expect(page.locator("#scheduledRestartEnabled")).to_be_disabled()
    expect(page.locator('#scheduledRestartForm button[type="submit"]')).to_be_disabled()

    chat = page.locator("#sec-chat-announcements")
    message = chat.get_by_role("textbox")
    chat_save = chat.get_by_role("button", name="Сохранить расписание")
    chat.locator("label.switch").click()
    message.fill('Мир "открыт"')
    chat_save.click()
    expect(chat.locator('.settings-feedback[data-state="pending"]')).to_be_visible()
    expect(chat_save).to_be_disabled()
    pending.pop().fulfill(status=503, json={"error": "Сервис недоступен"})
    retry = chat.get_by_role("button", name="Повторить сохранение")
    expect(retry).to_be_enabled()

    page.evaluate("renderOverview(S.overview)")
    expect(message).to_have_value('Мир "открыт"')
    expect(chat.get_by_role("switch")).to_be_checked()
    page.evaluate("renderOp({active:{op:'restart',phase:'Сохранение мира'},history:[]})")
    expect(retry).to_be_disabled()
    expect(chat_save).to_be_disabled()
    expect(chat).to_have_attribute("aria-busy", "true")
    page.evaluate("renderOp({active:null,history:[]})")
    retry.click()
    expect(chat.locator('.settings-feedback[data-state="pending"]')).to_be_visible()
    settings.update(requests[-1], nextChatAnnouncement=1794497400)
    settings["version"]["revision"] += 1
    pending.pop().fulfill(json={"ok": True, "settings": settings})
    expect(chat.locator('.settings-feedback[data-state="ok"]')).to_be_visible()
    assert (
        requests[0]
        == requests[1]
        == {
            "chatAnnouncements": {"enabled": True, "message": 'Мир "открыт"', "intervalMinutes": 30}
        }
    )
