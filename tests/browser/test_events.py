"""Event history pagination with isolated snapshots and category filters."""

import os
from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


@pytest.fixture
def events_page(page, dashboard):
    page.goto(dashboard["url"] + "/#/events")
    expect(page.locator("#btnStop")).to_be_enabled()
    page.evaluate("liveSource?.close();liveSource=null;clearTimeout(sseStartupTimer)")
    entries = [
        {
            "ts": "2026-10-05T12:00:00Z" if i < 26 else "2026-10-04T12:00:00Z",
            "type": "backup" if i % 2 == 0 else "start",
            "text": f"Событие {i:03}: результат операции сервера",
        }
        for i in range(61)
    ]
    page.evaluate("items => renderEvents({ok:true,items})", entries)
    return entries


def test_events_navigation_sizes_and_live_updates(page, events_page):
    rows = page.locator("#eventsBody .event-row")
    expect(rows).to_have_count(25)
    expect(page.locator("#eventsPageSize")).to_have_value("25")
    expect(page.locator("#eventsPrev")).to_be_disabled()
    expect(page.locator("#eventsRange")).to_have_text("1–25 из 61 · Страница 1")
    page.locator("#eventsNext").focus()
    page.keyboard.press("Enter")
    expect(rows.first).to_contain_text("Событие 025")
    expect(page.locator("#eventsRange")).to_have_text("26–50 из 61 · Страница 2")
    expect(page.locator("#eventsBody .event-day")).to_have_count(2)
    page.evaluate("items => renderEvents({ok:true,items})", events_page)
    expect(page.locator("#eventsRange")).to_have_text("26–50 из 61 · Страница 2")
    events_page[25]["text"] = "Обновлённое событие"
    page.evaluate("items => renderEvents({ok:true,items})", events_page)
    expect(rows.first).to_contain_text("Обновлённое событие")
    expect(page.locator("#recentBody .event-row")).to_have_count(5)
    expect(page.locator("#recentBody .event-row").first).to_contain_text("Событие 000")
    page.locator("#eventsNext").click()
    expect(rows).to_have_count(11)
    expect(page.locator("#eventsRange")).to_have_text("51–61 из 61 · Страница 3")
    expect(page.locator("#eventsNext")).to_be_disabled()
    expect(page.locator("#eventsBody .event-day")).to_have_count(1)
    page.locator("#eventsPrev").click()
    expect(rows.first).to_contain_text("Обновлённое событие")
    page.locator("#eventsPageSize").select_option("50")
    expect(rows).to_have_count(50)
    expect(page.locator("#eventsRange")).to_have_text("1–50 из 61 · Страница 1")
    page.locator("#eventsPageSize").select_option("100")
    expect(rows).to_have_count(61)
    expect(page.locator("#eventsPager")).to_be_hidden()


def test_events_filters_reset_page_and_refresh_clamps_page(page, events_page):
    page.locator("#eventsNext").click()
    page.locator('#eventFilters [data-ef="backup"]').click()
    rows = page.locator("#eventsBody .event-row")
    expect(rows).to_have_count(25)
    expect(rows.first).to_contain_text("Событие 000")
    expect(page.locator("#eventsRange")).to_have_text("1–25 из 31 · Страница 1")
    assert rows.evaluate_all("rows => rows.every(row => row.dataset.kind === 'backup')")
    page.locator("#eventsNext").click()
    expect(rows).to_have_count(6)
    expect(rows.first).to_contain_text("Событие 050")
    page.locator('#eventFilters [data-ef="problems"]').click()
    expect(page.locator("#eventsBody")).to_contain_text("Нет событий в этой категории")
    expect(page.locator("#eventsPager")).to_be_hidden()
    page.locator('#eventFilters [data-ef="all"]').click()
    page.locator("#eventsNext").click()
    page.locator("#eventsNext").click()
    page.evaluate("items => renderEvents({ok:true,items:items.slice(0,40)})", events_page)
    expect(page.locator("#eventsRange")).to_have_text("26–40 из 40 · Страница 2")
    expect(rows).to_have_count(15)
    page.evaluate("items => renderEvents({ok:true,items:items.slice(0,25)})", events_page)
    expect(page.locator("#eventsPager")).to_be_hidden()
    expect(rows.first).to_contain_text("Событие 000")
    page.evaluate("renderEvents({ok:false,error:'История недоступна'})")
    expect(page.locator("#eventsBody")).to_contain_text("История недоступна")
    expect(page.locator("#eventsPager")).to_be_hidden()


@pytest.mark.parametrize("count", [0, 1, 25, 26])
def test_events_pager_visibility_at_boundary(page, events_page, count):
    page.evaluate("items => renderEvents({ok:true,items})", events_page[:count])
    expect(page.locator("#eventsBody .event-row")).to_have_count(min(count, 25))
    if count > 25:
        expect(page.locator("#eventsPager")).to_be_visible()
    else:
        expect(page.locator("#eventsPager")).to_be_hidden()
    if count == 0:
        expect(page.locator("#eventsBody")).to_contain_text("Событий ещё нет")


@pytest.mark.parametrize("width", [1440, 390, 320])
def test_events_layout_has_no_nested_scroll_or_overflow(page, events_page, width):
    page.set_viewport_size({"width": width, "height": 900})
    assert page.locator("#eventsBody").evaluate("el => getComputedStyle(el).maxHeight") == "none"
    assert page.locator("#eventsBody").evaluate("el => el.scrollHeight === el.clientHeight")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator("#eventsNext").click()
    expect(page.locator("#eventsRange")).to_have_text("26–50 из 61 · Страница 2")
    if os.getenv("PZ_EVENTS_EVIDENCE"):
        out = Path(__file__).resolve().parents[2] / ".tmp-events-pagination"
        out.mkdir(exist_ok=True)
        page.screenshot(path=str(out / f"events-{width}.png"), full_page=True)
