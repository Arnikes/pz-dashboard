"""Event history pagination with isolated snapshots and category filters."""

import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import expect

from browser_evidence import capture_evidence

pytestmark = pytest.mark.browser


@pytest.fixture
def events_page(page, dashboard):
    page.goto(dashboard["url"] + "/#/events")
    expect(page.locator("#startupLoader")).to_be_hidden()
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
        capture_evidence(page, path=str(out / f"events-{width}.png"), full_page=True)


def test_problem_severity_keeps_original_categories_and_legacy_fallback(page, events_page):
    entries = [
        {"type": "update-check", "level": " error ", "text": "Update failed"},
        {"type": "docker", "level": "warn", "text": "Container warning"},
        {"type": "backup", "level": "CRITICAL", "text": "Archive failed"},
        {"type": "update-check", "level": "INFO", "text": "No update"},
        {"type": "rcon-error", "text": "Legacy error"},
        {"type": "warn", "level": "invalid", "text": "Legacy warning"},
        {"type": "delete", "text": "Legacy deletion"},
        {"type": "update", "level": "invalid", "text": "Invalid level"},
        {"type": "error", "level": "info", "text": "Explicit info"},
    ]
    page.evaluate("items => renderEvents({ok:true,items})", entries)
    page.locator('#eventFilters [data-ef="problems"]').click()
    rows = page.locator("#eventsBody .event-row")
    expect(rows).to_have_count(7)
    expect(rows.filter(has_text="Update failed")).to_have_attribute("data-level", "ERROR")
    expect(rows.filter(has_text="Container warning")).to_have_attribute("data-level", "WARNING")
    expect(rows.filter(has_text="Archive failed").locator(".event-severity")).to_have_text(
        "Критическая ошибка"
    )
    expect(rows.filter(has_text="Legacy warning").locator(".event-severity")).to_have_text(
        "Предупреждение"
    )
    expect(rows.filter(has_text="Legacy deletion").locator(".event-severity")).to_have_count(0)
    expect(rows.filter(has_text="Explicit info").locator(".event-severity")).to_have_count(0)
    page.locator('#eventFilters [data-ef="update"]').click()
    expect(rows).to_have_count(3)
    expect(rows.filter(has_text="Update failed")).to_have_count(1)
    expect(rows.filter(has_text="Invalid level")).to_have_attribute("data-level", "INFO")
    page.locator('#eventFilters [data-ef="backup"]').click()
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("Archive failed")


def test_empty_category_reset_restores_all_focus_and_first_page(page, events_page):
    page.locator("#eventsNext").click()
    page.locator('#eventFilters [data-ef="problems"]').click()
    reset = page.get_by_role("button", name="Сбросить фильтр", exact=True)
    reset.focus()
    page.keyboard.press("Enter")
    all_filter = page.locator('#eventFilters [data-ef="all"]')
    expect(all_filter).to_be_focused()
    expect(all_filter).to_have_attribute("aria-pressed", "true")
    expect(page.locator('#eventFilters [aria-pressed="true"]')).to_have_count(1)
    expect(page.locator("#eventsRange")).to_have_text("1–25 из 61 · Страница 1")
    expect(page.locator("#eventsBody .event-row").first).to_contain_text("Событие 000")


@pytest.mark.browser_context_args(timezone_id="America/New_York")
def test_dates_use_local_calendar_and_unknown_records_break_day_groups(page, events_page):
    # After spring-forward, subtracting 24 hours would incorrectly select March 7.
    page.clock.install(time="2026-03-09T04:30:00Z")
    entries = [
        {"type": "start", "ts": "2026-03-08T12:00:00Z", "text": "Yesterday"},
        {"type": "start", "ts": "invalid", "text": "Broken timestamp"},
        {"type": "start", "text": "Missing timestamp"},
        {"type": "start", "ts": "2026-03-08T13:00:00Z", "text": "Yesterday again"},
        {"type": "start", "ts": "2025-03-08T13:00:00Z", "text": "Last year"},
    ]
    page.evaluate("items => renderEvents({ok:true,items})", entries)
    headings = page.locator("#eventsBody .event-day")
    expect(headings).to_have_count(4)
    expect(headings.nth(0)).to_have_text("Вчера")
    expect(headings.nth(1)).to_have_text("Время неизвестно")
    expect(headings.nth(2)).to_have_text("Вчера")
    expect(headings.nth(3)).to_contain_text("2025")
    expect(page.locator("#eventsBody time")).to_have_count(3)
    expect(page.locator("#eventsBody time").first).to_have_attribute(
        "datetime", "2026-03-08T12:00:00.000Z"
    )
    assert "2026" in page.locator("#eventsBody time").first.get_attribute("title")
    # Recent has no day headings, so older entries keep a visible date.
    expect(page.locator("#recentBody .event-row").last.locator("time")).to_contain_text("08.03")


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("width", [320, 390, 1440])
def test_events_localized_long_content_and_severity_layout(page, dashboard, language, width):
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(dashboard["url"] + "/#/events")
    expect(page.locator("#startupLoader")).to_be_hidden()
    page.evaluate("liveSource?.close();liveSource=null;clearTimeout(sseStartupTimer)")
    entries = [
        {
            "ts": "2026-11-09T12:00:00Z",
            "type": "update-check",
            "level": "ERROR",
            "text": "Workshop: " + "1234567890" * 12 + " — проверка пакета не завершилась",
        },
        {
            "ts": "2026-11-09T11:00:00Z",
            "type": "backup",
            "level": "INFO",
            "text": "World archive saved / Архив мира сохранён",
        },
        {"type": "docker", "level": "WARN", "text": "Container needs attention"},
    ]
    page.evaluate("items => renderEvents({ok:true,items})", entries)
    expect(page.locator("#eventsBody .event-severity")).to_have_text(
        ["Error", "Warning"] if language == "en" else ["Ошибка", "Предупреждение"]
    )
    expect(page.locator("#eventsBody .event-day").last).to_have_text(
        "Time unknown" if language == "en" else "Время неизвестно"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator("#eventsBody").evaluate("el => el.scrollHeight === el.clientHeight")
    first = page.locator("#eventsBody .event-row").first
    meta, text = (
        first.locator(".event-meta").bounding_box(),
        first.locator(".e-text").bounding_box(),
    )
    if width <= 740:
        assert text["y"] >= meta["y"] + meta["height"]
        assert text["width"] >= first.bounding_box()["width"] - 1
    else:
        assert text["x"] > meta["x"] + meta["width"]
    problems = page.locator('#eventFilters [data-ef="problems"]')
    problems.focus()
    page.keyboard.press("Enter")
    expect(problems).to_be_focused()
    expect(page.locator("#eventsBody .event-row")).to_have_count(2)
    page.locator('#eventFilters [data-ef="all"]').click()
    out = Path(".tmp-events-polish")
    out.mkdir(exist_ok=True)
    capture_evidence(page, path=str(out / f"events-{language}-{width}.png"), full_page=True)


@pytest.mark.parametrize("language", ["en", "ru"])
def test_recent_mobile_retains_dates_and_problem_labels(page, dashboard, language):
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.set_viewport_size({"width": 320, "height": 900})
    page.goto(dashboard["url"])
    expect(page.locator("#startupLoader")).to_be_hidden()
    page.evaluate("liveSource?.close();liveSource=null;clearTimeout(sseStartupTimer)")
    entries = [
        {
            "type": "update-check",
            "level": "ERROR",
            "ts": "2025-10-04T12:00:00Z",
            "text": "Package " + "7" * 64,
        },
        {
            "type": "backup",
            "level": "INFO",
            "ts": "2025-10-04T11:00:00Z",
            "text": "World archive saved",
        },
        {"type": "docker", "level": "WARN", "text": "Container needs attention"},
    ]
    page.evaluate("items => renderRecent(items)", entries)
    body = page.locator("#recentBody")
    expect(body.locator("time").first).to_contain_text("10/04" if language == "en" else "04.10")
    expect(body.locator(".event-severity")).to_have_text(
        ["Error", "Warning"] if language == "en" else ["Ошибка", "Предупреждение"]
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    for row in body.locator(".event-row").all():
        meta = row.locator(".event-meta").bounding_box()
        text = row.locator(".e-text").bounding_box()
        assert text["width"] >= row.bounding_box()["width"] - 1
        assert text["y"] >= meta["y"] + meta["height"]
    out = Path(".tmp-events-polish")
    out.mkdir(exist_ok=True)
    capture_evidence(page.locator("#sec-recent"), path=str(out / f"recent-{language}-320.png"))


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("width", [320, 390, 1440])
def test_short_severity_rows_stay_compact_and_long_messages_wrap(page, dashboard, language, width):
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.set_viewport_size({"width": width, "height": 1100})
    page.goto(dashboard["url"] + "/#/events")
    expect(page.locator("#startupLoader")).to_be_hidden()
    page.evaluate("liveSource?.close();liveSource=null;clearTimeout(sseStartupTimer)")
    page.evaluate("document.fonts.ready")
    entries = [
        {"ts": "2026-11-10T12:00:00Z", "type": "start", "level": level, "text": "OK"}
        for level in ("INFO", "WARNING", "ERROR", "CRITICAL")
    ]
    long_message = "Package " + "1234567890" * 16 + " — metadata unavailable"
    extremes = [
        {**entries[0], "text": long_message},
        {**entries[2], "text": long_message},
        {"type": "start", "level": "CRITICAL", "text": "OK"},
    ]
    evidence = []
    out = Path(".tmp-pytest/events-density-after")
    out.mkdir(parents=True, exist_ok=True)
    for surface in ("events", "recent"):
        if surface == "recent":
            page.evaluate("location.hash = '#/overview'")
            expect(page.locator("#view-overview")).to_be_visible()
        render = "renderEvents({ok:true,items})" if surface == "events" else "renderRecent(items)"
        page.evaluate(f"items => {render}", entries)
        rows = page.locator(f"#{surface}Body .event-row")
        expect(rows).to_have_count(4)
        expect(rows.locator(".event-severity")).to_have_text(
            ["Warning", "Error", "Critical error"]
            if language == "en"
            else ["Предупреждение", "Ошибка", "Критическая ошибка"]
        )
        # Compare the same content, removing only the final row's missing separator.
        heights = rows.evaluate_all("""rows => rows.map(row => {
            const style = getComputedStyle(row);
            return row.getBoundingClientRect().height - parseFloat(style.borderTopWidth)
              - parseFloat(style.borderBottomWidth);
        })""")
        for height in heights[1:]:
            assert abs(height - heights[0]) <= (3 if width <= 740 else 1)
        for row in rows.all():
            time = row.locator(".e-time").bounding_box()
            text = row.locator(".e-text").bounding_box()
            if width <= 740:
                assert text["width"] >= row.bounding_box()["width"] - 1
                assert text["y"] >= time["y"] + time["height"]
            else:
                assert time["x"] + time["width"] <= text["x"]
            severity = row.locator(".event-severity")
            if severity.count():
                expect(severity).to_be_visible()
                mark = severity.bounding_box()
                if width <= 740:
                    assert time["x"] + time["width"] <= mark["x"]
                    assert text["y"] >= mark["y"] + mark["height"]
                else:
                    assert mark["x"] + mark["width"] <= text["x"]
        evidence.append({"surface": surface, "short_heights_without_borders": heights})
        card = page.locator(f"#sec-{surface}")
        page.evaluate(
            "selector => document.querySelector(selector).scrollIntoView({block:'center'})",
            f"#sec-{surface}",
        )
        capture_evidence(card, path=str(out / f"{surface}-{language}-{width}-short.png"))

        page.evaluate(f"items => {render}", extremes)
        for row in rows.all():
            bounds = row.bounding_box()
            time = row.locator(".e-time").bounding_box()
            text = row.locator(".e-text").bounding_box()
            assert text["x"] >= bounds["x"]
            assert text["x"] + text["width"] <= bounds["x"] + bounds["width"] + 1
            severity = row.locator(".event-severity")
            if severity.count():
                mark = severity.bounding_box()
                assert mark["x"] >= bounds["x"]
                assert mark["x"] + mark["width"] <= bounds["x"] + bounds["width"] + 1
                if width <= 740:
                    assert text["y"] >= max(time["y"] + time["height"], mark["y"] + mark["height"])
        for row in rows.all()[:2]:
            assert row.locator(".e-text").evaluate(
                "el => el.getBoundingClientRect().height > parseFloat(getComputedStyle(el).lineHeight)"
            )
        expect(rows.last.locator("time")).to_have_count(0)
        expect(rows.last.locator(".e-time")).to_have_text(
            "Time unknown" if language == "en" else "Время неизвестно"
        )
        expect(rows.last.locator(".event-severity")).to_have_text(
            "Critical error" if language == "en" else "Критическая ошибка"
        )
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.evaluate(
            "selector => document.querySelector(selector).scrollIntoView({block:'center'})",
            f"#sec-{surface}",
        )
        capture_evidence(card, path=str(out / f"{surface}-{language}-{width}-long.png"))
    if os.getenv("PZ_VISUAL_EVIDENCE") == "1":
        (out / f"geometry-{language}-{width}.json").write_text(
            json.dumps(evidence), encoding="utf-8"
        )
