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


def test_archive_pages_preserve_refresh_and_clamp_after_deletion(page, backups_page):
    page.evaluate("""() => {
        S.backupsItems = Array.from({length:21}, (_,i) => ({
            name:`world-${String(i).padStart(3,'0')}.tar.gz`, size:i,
            mtime:'2026-10-05T03:00:00Z'
        }));
        renderBackupsPage();
    }""")
    rows = page.locator("#backupsBody .backup-row")
    expect(rows).to_have_count(10)
    expect(page.locator("#backupsRange")).to_have_text("1–10 из 21 · Страница 1")
    expect(page.locator("#backupsPrev")).to_be_disabled()
    page.locator("#backupsNext").focus()
    page.keyboard.press("Enter")
    expect(rows.first.locator(".b-name")).to_have_text("world-010.tar.gz")
    expect(page.locator("#backupsRange")).to_have_text("11–20 из 21 · Страница 2")
    page.evaluate("renderBackups({ok:true,items:S.backupsItems})")
    expect(page.locator("#backupsRange")).to_have_text("11–20 из 21 · Страница 2")
    page.locator("#backupsNext").click()
    expect(rows).to_have_count(1)
    expect(page.locator("#backupsNext")).to_be_disabled()
    page.evaluate("renderBackups({ok:true,items:S.backupsItems.slice(0,20)})")
    expect(rows).to_have_count(10)
    expect(page.locator("#backupsRange")).to_have_text("11–20 из 20 · Страница 2")
    page.locator("#backupsPrev").click()
    expect(rows.first.locator(".b-name")).to_have_text("world-000.tar.gz")
    page.locator("#backupsNext").click()
    page.locator("#backupsBody [data-b=verify]").first.click()
    expect(page.locator("#toasts")).to_contain_text("Тест: сервер занят")
    page.evaluate("renderBackups({ok:true,items:S.backupsItems.slice(0,10)})")
    expect(page.locator("#backupsPager")).to_be_hidden()
    expect(rows).to_have_count(10)
    expect(rows.first.locator(".b-name")).to_have_text("world-000.tar.gz")
    page.evaluate("renderBackups({ok:true,items:[]})")
    expect(page.locator("#backupsPager")).to_be_hidden()
    expect(page.locator("#backupsBody")).to_contain_text("Бэкапов ещё нет")


@pytest.mark.parametrize("count", [0, 1, 10, 11])
def test_archive_pager_visibility_at_page_boundary(page, backups_page, count):
    page.evaluate(
        """count => renderBackups({ok:true,items:Array.from({length:count}, (_,i) => ({
        name:`world-${i}.tar.gz`,size:1,mtime:'2026-10-05T03:00:00Z'
    }))})""",
        count,
    )
    expect(page.locator("#backupsBody .backup-row")).to_have_count(min(count, 10))
    if count > 10:
        expect(page.locator("#backupsPager")).to_be_visible()
    else:
        expect(page.locator("#backupsPager")).to_be_hidden()


@pytest.mark.parametrize("width", [1440, 768, 390, 320])
def test_backup_columns_are_stable_and_schedule_is_compact(page, backups_page, width):
    page.set_viewport_size({"width": width, "height": 900})
    fields = page.locator("#sec-bksched .auto-grid input").evaluate_all(
        """inputs => inputs.map(input => {
          const rect = input.getBoundingClientRect();
          const field = input.closest('.field').getBoundingClientRect();
          return {left:rect.left,right:rect.right,width:rect.width,height:rect.height,
                  fieldLeft:field.left,fieldRight:field.right};
        })"""
    )
    for field in fields:
        assert field["left"] >= field["fieldLeft"] - 1
        assert field["right"] <= field["fieldRight"] + 1
        assert field["height"] == pytest.approx(44, abs=1)
    assert fields[0]["width"] == pytest.approx(fields[1]["width"], abs=1)
    expect(page.locator("#bkAutoNext")).to_have_count(0)
    expect(page.locator("#sec-bksched")).not_to_contain_text("Копии сверх лимита")
    expect(page.locator("#backupsBody .copy-value")).to_have_count(0)
    expect(page.locator("#backupsBody span.b-name")).to_have_count(2)
    assert (
        page.locator("#sec-bksched .auto-block").evaluate(
            "el => getComputedStyle(el).borderTopStyle"
        )
        == "none"
    )
    assert page.locator("#backupsBody").evaluate("el => getComputedStyle(el).maxHeight") == "none"
    geometry = page.locator("#backupsBody .backup-row").evaluate_all(
        """rows => rows.map(row => ['.b-name','.b-size','.b-age','[data-b=dl]'].map(selector => {
          const rect = row.querySelector(selector).getBoundingClientRect();
          return {x:rect.x,width:rect.width};
        }))"""
    )
    assert geometry[0] == geometry[1]
    expect(page.locator("#bkJournalBody .j-date").first).to_be_visible()
    expect(page.locator("#bkJournalBody .j-trig").first).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator("#bkJournalNext").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#bkJournalRange")).to_have_text("26–50 · Страница 2")
    if os.getenv("PZ_BACKUP_EVIDENCE"):
        out = Path(__file__).resolve().parents[2] / ".tmp-impeccable-audit"
        page.evaluate("""() => renderBackups({ok:true,items:Array.from({length:11}, (_,i) => ({
            name:`world-${i}-with-a-long-name.tar.gz`,size:987654321,
            mtime:'2026-10-05T03:00:00Z'
        }))})""")
        assert page.locator("#backupsBody").evaluate("el => el.scrollHeight === el.clientHeight")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
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
