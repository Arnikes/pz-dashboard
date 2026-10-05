from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


def test_missing_backup_notice_remains_available_without_a_config_profile(page, dashboard):
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("")
    notice = page.locator('#editorAttention [data-attention-key="backup-missing"]')
    expect(notice).to_be_visible()
    expect(notice).to_have_attribute("href", "#/backups")
    expect(page.locator("#editorAttention")).to_contain_text("Нет резервной копии мира")
    expect(page.locator("#backupNudge")).to_have_count(0)


@pytest.mark.parametrize("running,remote", [(False, False), (True, False), (False, True)])
def test_rcon_header_distinguishes_stopped_server_from_connection_failure(
    page, dashboard, running, remote
):
    page.goto(dashboard["url"])
    expect(page.locator("#pillRcon")).to_have_text("RCON")
    page.evaluate(
        """({running,remote}) => renderOverview({...S.overview,
        mode:remote?'remote':'local', docker:!remote,
        containerInfo:{running,status:running?'running':'exited'},
        rcon:{state:'error',error:'Connection refused'}})""",
        {"running": running, "remote": remote},
    )
    inactive = not running and not remote
    expect(page.locator("#pillRcon")).to_have_text("RCON не активен" if inactive else "RCON ошибка")
    expect(page.locator("#pillRcon")).to_have_attribute(
        "data-state", "unknown" if inactive else "bad"
    )
    if inactive:
        expect(page.locator("#connectionIssue")).to_be_hidden()
    else:
        expect(page.locator("#connectionIssue")).to_be_visible()
        expect(page.locator("#connectionIssue")).to_contain_text("RCON ошибка")
    page.evaluate("renderOverview({...S.overview,rcon:{state:'ok'},containerInfo:{running:true}})")
    expect(page.locator("#pillRcon")).to_have_text("RCON")
    expect(page.locator("#pillRcon")).to_have_attribute("title", "")
    expect(page.locator("#connectionIssue")).to_be_hidden()


def test_console_error_warning_search_and_offline_log_retention(page, dashboard):
    frame = {
        "ok": True,
        "text": "2026-10-01T12:00:00Z INFO server ready\n"
        "2026-10-01T12:00:01Z ERROR database failure\n"
        "2026-10-01T12:00:02Z WARN database slow\n",
    }
    page.route("**/api/logs**", lambda route: route.fulfill(json=frame))
    page.goto(dashboard["url"] + "/#/console")
    page.evaluate("refreshLogs()")
    expect(page.locator("#logsOut")).to_contain_text("server ready")
    page.locator('#logLevels [data-level="error"]').click()
    expect(page.locator("#logsOut")).to_contain_text("ERROR database failure")
    expect(page.locator("#logsOut")).not_to_contain_text("WARN")
    expect(page.locator("#logsOut .l-err")).to_have_count(1)
    page.locator('#logLevels [data-level="warn"]').click()
    page.locator("#logsFilter").fill("database")
    expect(page.locator("#logsOut")).to_contain_text("WARN database slow")
    expect(page.locator("#logsOut")).not_to_contain_text("ERROR")
    retained = page.locator("#logsOut").inner_text()
    frame.clear()
    frame.update(ok=False, error="Docker is unavailable")
    page.evaluate("refreshLogs()")
    expect(page.locator("#logsError")).to_contain_text("Docker is unavailable")
    expect(page.locator("#logsError")).to_contain_text("последние данные")
    expect(page.locator("#logsOut")).to_have_text(retained)
    frame.update(ok=True, text="2026-10-01T12:00:03Z WARN database recovered\n")
    page.evaluate("refreshLogs()")
    expect(page.locator("#logsError")).to_be_hidden()
    expect(page.locator("#logsOut")).to_contain_text("database recovered")


def test_paused_log_scrolling_keeps_receiving_new_lines(page, dashboard):
    page.goto(dashboard["url"] + "/#/console")
    page.evaluate("""() => applyLogs({ok:true,text:Array.from({length:150}, (_,i) =>
        `2026-10-01T12:00:00Z INFO line ${i.toString(36)}`).join('\\n')})""")
    page.locator("#logsAuto").uncheck()
    page.locator("#logsOut").evaluate("el => el.scrollTop = 50")
    before = page.locator("#logsOut").evaluate("el => el.scrollTop")
    assert before > 0
    page.evaluate("""() => applyLogs({ok:true,text:S.logsLines.map(l => l.raw).join('\\n') +
        '\\n2026-10-01T12:00:01Z ERROR new line while paused'})""")
    expect(page.locator("#logsOut")).to_contain_text("new line while paused")
    assert abs(page.locator("#logsOut").evaluate("el => el.scrollTop") - before) <= 1
    assert dashboard["actions"] == []


@pytest.mark.parametrize("mode", ["local", "http", "denied"])
def test_copy_field_writes_full_text_to_clipboard(page, dashboard, mode):
    value = f"sha256:0123456789abcdef — полный текст ({mode})\nвторая строка"
    dashboard["overview"]["update"] = {"local": value}
    url = dashboard["url"]
    if mode == "http":
        # Keep a non-loopback HTTP origin, serving the real static assets.
        origin = "http://pz-console.test"
        static = Path(__file__).resolve().parents[2] / "dashboard" / "static"

        def remote_assets(route):
            if "/api/" in route.request.url:
                route.fallback()
            else:
                path = urlsplit(route.request.url).path.removeprefix("/static/").lstrip("/")
                asset = static / (path or "index.html")
                if asset.is_file():
                    route.fulfill(path=asset)
                else:
                    route.fulfill(status=404)

        page.route(f"{origin}/**", remote_assets)
        page.goto(origin)
        assert page.evaluate("!isSecureContext && !navigator.clipboard")
    else:
        page.goto(url)
        assert page.evaluate("isSecureContext && !!navigator.clipboard")
        if mode == "denied":
            page.evaluate("""() => {
                navigator.clipboard.writeText = async () => {
                    throw new DOMException('Denied', 'NotAllowedError');
                };
            }""")

    expect(page.locator("#btnStop")).to_be_enabled()
    page.evaluate(
        """value => {
        const field = document.getElementById('updLocalCopy');
        field.querySelector('.copy-label').textContent = 'sha256:012…';
        field.disabled = false;
        field.dataset.copy = value;
    }""",
        value,
    )
    page.evaluate("location.hash='#/maintenance'")
    expect(page.locator("#view-maintenance")).to_be_visible()
    page.locator("#updLocalCopy").click()
    expect(page.locator("#updLocalCopy")).to_have_attribute("data-copied", "true")
    expect(page.locator("#updLocalCopy .copy-icon-check")).to_be_visible()
    expect(page.locator("#updLocalCopy .copy-icon-default")).to_be_hidden()
    expect(page.locator('#toasts .toast[data-kind="ok"]')).to_have_count(0)
    # Paste via the browser to check the actual clipboard, not a mocked API call.
    page.evaluate("""() => {
        const target = document.createElement('textarea');
        target.id = 'pasteTarget';
        document.body.appendChild(target);
    }""")
    target = page.locator("#pasteTarget")
    target.focus()
    target.press("ControlOrMeta+V")
    expect(target).to_have_value(value)


def test_failed_copy_shows_error_and_restores_focus(page, dashboard):
    page.goto(dashboard["url"])
    expect(page.locator("#btnStop")).to_be_enabled()
    page.evaluate("""() => {
        Object.defineProperty(navigator, 'clipboard', { value: undefined });
        document.execCommand = () => false;
        document.getElementById('btnStop').focus();
    }""")
    count = page.locator("textarea").count()
    page.evaluate("copyText('test')")
    expect(page.locator('#toasts .toast[data-kind="error"]')).to_have_text("Не удалось скопировать")
    expect(page.locator("#btnStop")).to_be_focused()
    assert page.locator("textarea").count() == count


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844)])
def test_navigation_and_layout(page, dashboard, width, height):
    page.set_viewport_size({"width": width, "height": height})
    page.goto(dashboard["url"])
    expect(page.locator("#btnStop")).to_be_enabled()
    for route in [
        "players",
        "mods",
        "settings",
        "maintenance",
        "backups",
        "events",
        "console",
        "overview",
    ]:
        link = page.locator(f'.nav [data-route="{route}"]')
        if width <= 740 and route not in ("overview", "settings", "mods"):
            page.locator("#navMore").click()
            page.locator(f'#moreMenu a[href="#/{route}"]').click()
        else:
            link.click()
        expect(link).to_have_attribute("aria-current", "page")
        expect(page.locator(f"#view-{route}")).to_be_visible()
        expect(page.locator("h1:visible")).to_have_count(1)
        expect(page.locator(f"#view-{route}")).to_have_attribute("aria-labelledby", f"page-{route}")
        expect(page.locator(".view:visible")).to_have_count(1)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.go_back()
    expect(page.locator("#view-console")).to_be_visible()


def test_overview_leads_with_actions_and_backup_freshness(page, dashboard):
    page.goto(dashboard["url"])
    expect(page.locator("#btnStop")).to_be_enabled()
    expect(page.locator("#serverDetails")).to_have_count(0)
    page.evaluate("""() => { S.backupsItems=[{name:'old.zip',size:1024,
        mtime:new Date(Date.now()-72*3600000).toISOString()}]; renderOverview(S.overview); }""")
    expect(page.locator("#kpiBackup")).to_contain_text("3 дня назад")
    expect(page.locator("#kpiBackupSub")).to_contain_text("1")
    page.evaluate("S.backupsItems=[{name:'unknown.zip',size:1024}];renderOverview(S.overview)")
    expect(page.locator("#kpiBackup")).to_have_text("Дата неизвестна")
    page.evaluate("S.backupsItems=[];renderOverview(S.overview)")
    expect(page.locator("#kpiBackup")).to_have_text("Нет копий")
    expect(page.locator("#kpiBackup")).to_have_attribute("title", "")


def test_stop_requires_confirmation_and_shows_api_error(page, dashboard):
    page.goto(dashboard["url"])
    page.locator("#btnStop").click()
    expect(page.get_by_role("alertdialog")).to_be_visible()
    assert dashboard["actions"] == []
    page.locator("#modalCancel").click()
    expect(page.get_by_role("alertdialog")).to_be_hidden()
    assert dashboard["actions"] == []
    page.locator("#btnStop").click()
    page.locator("#warnSel").select_option("60")
    page.locator("#modalOk").click()
    expect(page.locator("#toasts")).to_contain_text("Тест: сервер занят")
    assert dashboard["actions"] == [{"op": "stop", "warnSeconds": 60}]


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844)])
def test_cancel_auto_mods_update(page, dashboard, width, height):
    page.set_viewport_size({"width": width, "height": height})
    page.goto(dashboard["url"])
    expect(page.locator("#btnStop")).to_be_enabled()
    cancel = page.locator("#btnCancelMods")
    expect(cancel).to_be_hidden()
    active = {
        "op": "mods-restart",
        "phase": "Предупреждение игроков",
        "message": "отсчёт 600 с",
        "cancellable": True,
        "cancelRequested": False,
    }
    page.evaluate("active => renderOp({active, history: []})", active)
    expect(cancel).to_be_visible()
    expect(cancel).to_be_enabled()
    expect(page.locator("#btnStop")).to_be_disabled()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    requests = []

    def handle_cancel(route):
        requests.append(route.request.post_data_json)
        route.fulfill(json={"ok": True, "cancelRequested": True})

    page.route("**/api/action", handle_cancel)
    cancel.click()
    expect(cancel).to_have_text("Отмена…")
    expect(cancel).to_be_disabled()
    assert requests == [{"op": "cancel-mods-update"}]
    page.evaluate("""renderOp({active: null, history: [{
        op: 'mods-restart', ok: true, cancelled: true,
        message: 'Автообновление модов отменено администратором'
    }]})""")
    expect(cancel).to_be_hidden()
    expect(page.locator("#toasts")).to_contain_text("Автообновление модов отменено администратором")


def test_cancel_button_hidden_for_other_operations_and_stopping(page, dashboard):
    page.goto(dashboard["url"])
    expect(page.locator("#btnStop")).to_be_enabled()
    for operation, cancellable in [
        ("restart", True),
        ("apply-mods-update", True),
        ("mods-restart", False),
    ]:
        page.evaluate(
            "active => renderOp({active, history: []})",
            {"op": operation, "phase": "Остановка", "cancellable": cancellable},
        )
        expect(page.locator("#btnCancelMods")).to_be_hidden()


def test_cancel_mods_update_shows_api_rejection(page, dashboard):
    page.goto(dashboard["url"])
    expect(page.locator("#btnStop")).to_be_enabled()
    page.evaluate("""renderOp({active: {
        op: 'mods-restart', phase: 'Предупреждение игроков', cancellable: true
    }, history: []})""")
    page.locator("#btnCancelMods").click()
    expect(page.locator("#toasts")).to_contain_text("Тест: сервер занят")
    assert dashboard["actions"] == [{"op": "cancel-mods-update"}]
    expect(page.locator("#btnCancelMods")).to_be_enabled()


@pytest.mark.parametrize("action", ["kick", "ban"])
@pytest.mark.parametrize("name", ["Alice", "-Дмитрий-V"])
def test_player_buttons_send_exact_name(page, dashboard, action, name):
    commands = []
    dashboard["players"].update(names=[name], count=1, raw=f"-{name}")
    page.route(
        "**/api/players",
        lambda route: route.fulfill(
            json={"ok": True, "names": [name], "count": 1, "raw": f"-{name}"}
        ),
    )

    def handle_rcon(route):
        commands.append(route.request.post_data_json)
        route.fulfill(json={"ok": True, "output": "OK"})

    page.route("**/api/rcon", handle_rcon)
    page.goto(dashboard["url"])
    page.locator('.nav [data-route="players"]').click()
    expect(page.locator(".p-name")).to_have_text(name)
    page.locator(f'[data-p="{action}"]').click()
    expect(page.get_by_role("alertdialog")).to_contain_text(name)
    assert commands == []
    page.locator("#modalOk").click()
    expect(page.locator("#toasts")).to_contain_text(name)
    assert commands == [{"command": f'{action}user "{name}"'}]


def test_unavailable_api_keeps_real_mode(page, dashboard):
    page.route("**/api/health", lambda route: route.fulfill(status=503, json={"ok": False}))
    page.goto(dashboard["url"])
    expect(page.locator("#demoBadge")).to_be_hidden()
    expect(page.locator("#demoBanner")).to_be_hidden()
    page.locator('.nav [data-route="console"]').click()
    expect(page.locator("#consoleOut")).not_to_contain_text("Демо-режим")
    assert dashboard["actions"] == []


def test_demo_requires_explicit_choice(page, dashboard):
    page.goto(dashboard["url"] + "?demo=1")
    expect(page.locator("#demoBadge")).to_be_visible()
    expect(page.locator("#demoBanner")).to_be_visible()
    expect(page.locator("#btnStop")).to_be_disabled()
