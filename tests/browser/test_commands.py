"""Quick commands keep search and exits reachable while the list scrolls."""

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844), (320, 568), (844, 390)])
def test_command_list_scrolls_without_moving_search_or_close(page, dashboard, width, height):
    page.set_viewport_size({"width": width, "height": height})
    page.goto(dashboard["url"])
    page.locator("#btnCommands").click()
    search = page.locator("#commandSearch")
    before = search.bounding_box()
    last = page.locator("#commandResults [data-command]:not(:disabled)").last
    last.focus()
    expect(last).to_be_in_viewport()
    expect(search).to_be_in_viewport()
    expect(page.locator("#commandClose")).to_be_in_viewport()
    assert page.locator("#commandClose").bounding_box()["height"] >= 44
    expect(page.locator(".command-footer")).to_be_in_viewport()
    assert search.bounding_box() == before
    assert page.locator("#commandResults").evaluate("el => el.scrollTop > 0")
    assert page.locator("#commandResults").evaluate("""el => [...el.children].every(button => {
        const box = button.getBoundingClientRect();
        return [...button.children].every(child => {
            const text = child.getBoundingClientRect();
            return text.top >= box.top && text.bottom <= box.bottom;
        });
    })""")
    assert page.locator("#commandDialog").evaluate(
        "el => el.scrollHeight <= el.clientHeight && el.scrollWidth <= el.clientWidth"
    )
    search.fill("события")
    assert search.bounding_box() == before
    assert page.locator("#commandResults").evaluate("el => el.scrollTop === 0")
    page.locator("#commandClose").click()
    expect(page.locator("#btnCommands")).to_be_focused()
    page.locator("#btnCommands").click()
    last.focus()
    assert page.locator("#commandResults").evaluate("el => el.scrollTop > 0")
    page.keyboard.press("Escape")
    page.locator("#btnCommands").click()
    expect(search).to_be_focused()
    assert page.locator("#commandResults").evaluate("el => el.scrollTop === 0")


def test_command_search_arrow_up_selects_last_available_and_skips_disabled(page, dashboard):
    page.goto(dashboard["url"])
    expect(page.locator("#btnStop")).to_be_enabled()
    page.locator("#btnCommands").click()
    search = page.locator("#commandSearch")
    enabled = page.locator("#commandResults [data-command]:not(:disabled)")
    search.press("ArrowUp")
    expect(enabled.last).to_be_focused()
    search.focus()
    search.press("Shift+ArrowDown")
    expect(search).to_be_focused()
    search.press("ArrowDown")
    expect(enabled.first).to_be_focused()
    enabled.first.press("ArrowUp")
    expect(search).to_be_focused()
    search.fill("текущую операцию")
    expect(page.locator("#commandResults [data-command]")).to_be_disabled()
    expect(page.locator("#commandCount")).to_have_text("Найдено: 1 · Доступно: 0")
    search.press("Enter")
    expect(search).to_be_focused()
    expect(page.locator("#commandDialog")).to_be_visible()
    assert dashboard["actions"] == []


def test_command_empty_search_can_be_reset_without_closing_dialog(page, dashboard):
    page.goto(dashboard["url"])
    page.locator("#btnCommands").click()
    search = page.locator("#commandSearch")
    before = search.bounding_box()
    search.fill("нет-такой-команды")
    expect(page.locator("#commandCount")).to_have_text("Команда не найдена")
    page.get_by_role("button", name="Сбросить поиск").click()
    expect(search).to_have_value("")
    expect(search).to_be_focused()
    expect(page.locator("#commandDialog")).to_be_visible()
    expect(page.get_by_role("button", name="Открыть: Обзор")).to_be_visible()
    assert search.bounding_box() == before
