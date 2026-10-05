"""Backup layout, journal navigation and schedule feedback with isolated data."""

import os
from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


@pytest.fixture
def backups_page(page, dashboard):
    entries = [
        {
            "ts": f"2026-10-05T12:{i % 60:02}:00Z",
            "trigger": "scheduled" if i % 2 else "manual",
            "name": f"world-{i:03}.tar.gz",
            "size": i * 123456,
            "status": "error" if i == 2 else "success",
            "error": "Нет места" if i == 2 else "",
        }
        for i in range(61)
    ]
    requests = []
    fail = [False]

    def journal(route):
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(route.request.url).query)
        offset, limit = int(query["offset"][0]), int(query["limit"][0])
        requests.append((offset, limit))
        if fail[0]:
            route.fulfill(status=503, json={"ok": False, "error": "Связь потеряна"})
        else:
            route.fulfill(
                json={
                    "ok": True,
                    "items": entries[offset : offset + limit],
                    "hasMore": len(entries) > offset + limit,
                }
            )

    page.route("**/api/backups/journal?*", journal)
    page.goto(dashboard["url"] + "/#/backups")
    expect(page.locator("#btnBackup")).to_be_enabled()
    page.evaluate("liveSource?.close();liveSource=null;clearTimeout(sseStartupTimer)")
    page.evaluate(
        """entries => renderBackups({ok:true,journal:entries.slice(0,25),journalHasMore:true,
        autoBackup:{enabled:true,time:'03:00',nextRun:'2026-10-06T03:00:00Z'},items:[
        {name:'world-short.tar.gz',size:1,mtime:new Date().toISOString()},
        {name:'world-with-a-very-long-name-and-many-parts.tar.gz',size:987654321000,
         mtime:'2025-01-01T04:00:00Z'}]})""",
        entries,
    )
    return entries, requests, fail


def test_journal_pages_sizes_and_live_refresh_keep_current_page(page, backups_page):
    entries, requests, _ = backups_page
    rows = page.locator("#bkJournalBody .journal-row")
    expect(rows).to_have_count(25)
    expect(page.locator("#bkJournalPageSize")).to_have_value("25")
    expect(page.locator("#bkJournalPrev")).to_be_disabled()
    page.locator("#bkJournalNext").click()
    expect(page.locator("#bkJournalRange")).to_have_text("26–50 · Страница 2")
    expect(rows.first).to_contain_text("world-025.tar.gz")
    page.evaluate("j=>renderBkJournal(j.slice(0,25),true)", entries)
    assert requests == [(25, 25)]
    entries[0]["status"] = "error"
    page.evaluate("j=>renderBkJournal(j.slice(0,25),true)", entries)
    expect(page.locator("#bkJournalBody")).to_have_attribute("aria-busy", "false")
    expect(page.locator("#bkJournalRange")).to_have_text("26–50 · Страница 2")
    page.locator("#bkJournalNext").click()
    expect(rows).to_have_count(11)
    expect(page.locator("#bkJournalRange")).to_have_text("51–61 · Страница 3")
    expect(page.locator("#bkJournalNext")).to_be_disabled()
    page.locator("#bkJournalPrev").click()
    expect(rows.first).to_contain_text("world-025.tar.gz")
    page.locator("#bkJournalPageSize").select_option("50")
    expect(rows).to_have_count(50)
    expect(page.locator("#bkJournalRange")).to_have_text("1–50 · Страница 1")
    page.locator("#bkJournalPageSize").select_option("100")
    expect(rows).to_have_count(61)
    expect(page.locator("#bkJournalNext")).to_be_disabled()


def test_journal_failed_navigation_retries_and_empty_history_hides_pager(page, backups_page):
    entries, _, fail = backups_page
    fail[0] = True
    page.locator("#bkJournalNext").click()
    expect(page.locator("#bkJournalError")).to_contain_text("Связь потеряна")
    expect(page.locator("#bkJournalRange")).to_have_text("1–25 · Страница 1")
    expect(page.locator("#bkJournalBody .journal-row")).to_have_count(25)
    fail[0] = False
    page.locator("#bkJournalRetry").click()
    expect(page.locator("#bkJournalRange")).to_have_text("26–50 · Страница 2")
    expect(page.locator("#bkJournalError")).to_be_hidden()
    entries.clear()
    page.evaluate("renderBkJournal([])")
    expect(page.locator("#bkJournalBody")).to_contain_text("Бэкапов ещё не было")
    expect(page.locator("#bkJournalPager")).to_be_hidden()


@pytest.mark.parametrize("width", [1440, 768, 390, 320])
def test_backup_columns_are_stable_and_schedule_is_compact(page, backups_page, width):
    page.set_viewport_size({"width": width, "height": 900})
    expect(page.locator("#bkAutoNext")).to_have_count(0)
    expect(page.locator("#sec-bksched")).not_to_contain_text("Копии сверх лимита")
    geometry = page.locator("#backupsBody .backup-row").evaluate_all(
        """rows => rows.map(row => ['.b-name','.b-size','.b-age','[data-b=dl]'].map(selector => {
          const rect = row.querySelector(selector).getBoundingClientRect();
          return {x:rect.x,width:rect.width};
        }))"""
    )
    assert geometry[0] == geometry[1]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator("#bkJournalNext").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#bkJournalRange")).to_have_text("26–50 · Страница 2")
    if os.getenv("PZ_BACKUP_EVIDENCE"):
        out = Path(__file__).resolve().parents[2] / ".tmp-impeccable-audit"
        page.evaluate("window.scrollTo(0,0)")
        page.screenshot(path=str(out / f"backups-after-{width}.png"), full_page=True)


def test_schedule_saved_message_uses_toast_and_keeps_errors_inline(page, backups_page):
    held = []
    page.route("**/api/settings", lambda route: held.append(route))
    page.locator("#bkAutoTime").fill("04:00")
    page.locator("#bkAutoTime").press("Tab")
    expect(page.locator("#sec-bksched .settings-feedback")).to_contain_text("сохраняется")
    page.wait_for_function("settingState('autoBackup').pending === 1")
    held.pop().fulfill(json={"ok": True})
    expect(page.locator("#toasts")).to_contain_text("Расписание бэкапов: сохранено")
    expect(page.locator("#sec-bksched .settings-feedback")).to_have_count(0)
    page.locator("#bkAutoStop").check()
    page.wait_for_function("settingState('autoBackup').pending === 1")
    held.pop().fulfill(status=503, json={"ok": False, "error": "Сбой сохранения"})
    expect(page.locator("#sec-bksched .settings-feedback")).to_contain_text("Ввод сохранён")
    expect(page.locator("#bkAutoStop")).to_be_checked()
    expect(page.get_by_role("button", name="Повторить сохранение")).to_be_visible()
