"""Overview customization retains live widgets, preferences and operational shortcuts."""

import json

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser
LAYOUT_KEY = "pz.overview.layout"
DEFAULT_WIDGETS = ["status", "online", "backup", "load", "updates", "events"]
UPDATE_ALLOWED = "document.dispatchEvent(new CustomEvent('pz:before-update', {cancelable:true}))"


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {**browser_context_args, "has_touch": True}


def widget(page, widget_id):
    return page.locator(f'#overviewWidgets [data-overview-widget="{widget_id}"]')


def widget_action(page, action, widget_id):
    return page.locator(
        f'#overviewCustomizeDialog [data-widget-action="{action}"][data-widget-id="{widget_id}"]'
    )


def visible_widgets(page):
    return page.locator("#overviewWidgets [data-overview-widget]:not([hidden])")


def expect_widget_order(page, order):
    widgets = visible_widgets(page)
    expect(widgets).to_have_count(len(order))
    assert widgets.evaluate_all("nodes => nodes.map(node => node.dataset.overviewWidget)") == order


def stored_layout(page):
    return page.evaluate("key => JSON.parse(localStorage.getItem(key))", LAYOUT_KEY)


def seed_layout(page, raw):
    page.add_init_script(f"localStorage.setItem({json.dumps(LAYOUT_KEY)}, {json.dumps(raw)})")


def seed_widgets(page, order):
    seed_layout(page, json.dumps({"version": 1, "visible": order}))


@pytest.mark.parametrize(
    "language,width,button_name,dialog_name",
    [("en", 1440, "Customize", "Customize Overview"), ("ru", 320, "Настроить", "Настроить обзор")],
    ids=["desktop-en", "mobile-ru"],
)
def test_widget_order_keyboard_and_touch_persists_after_reload(
    page, dashboard, language, width, button_name, dialog_name
):
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard["url"] + "/#/overview")
    expect(page.locator("#startupLoader")).to_be_hidden()
    expect_widget_order(page, DEFAULT_WIDGETS)
    mutations = []
    page.on(
        "request",
        lambda request: (
            mutations.append(request.url)
            if "/api/" in request.url and request.method != "GET"
            else None
        ),
    )

    customize = page.get_by_role("button", name=button_name, exact=True)
    dialog = page.get_by_role("dialog", name=dialog_name, exact=True)
    customize.focus()
    page.keyboard.press("Enter")
    expect(dialog).to_be_visible()
    expect(page.locator("#overviewCustomizeTitle")).to_be_focused()
    assert not page.evaluate(UPDATE_ALLOWED)
    expect(widget_action(page, "earlier", "status")).to_be_disabled()
    expect(widget_action(page, "later", "events")).to_be_disabled()

    move_online = widget_action(page, "earlier", "online")
    move_online.focus()
    page.keyboard.press("Enter")
    expect(move_online).to_be_disabled()
    expect(widget_action(page, "later", "online")).to_be_focused()
    remove_updates = widget_action(page, "remove", "updates")
    bounds = remove_updates.bounding_box()
    assert bounds["width"] >= 44 and bounds["height"] >= 44
    remove_updates.tap()
    expect(widget(page, "updates")).to_be_hidden()
    expect(widget_action(page, "remove", "events")).to_be_focused()
    widget_action(page, "add", "updates").tap()
    order = ["online", "status", "backup", "load", "events", "updates"]
    expect_widget_order(page, order)
    assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth")
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(customize).to_be_focused()
    assert page.evaluate(UPDATE_ALLOWED)
    assert stored_layout(page) == {"version": 1, "visible": order}

    page.reload()
    expect(page.locator("#startupLoader")).to_be_hidden()
    expect_widget_order(page, order)
    boxes = [card.bounding_box() for card in visible_widgets(page).all()]
    for first, second in zip(boxes, boxes[1:]):
        if abs(first["y"] - second["y"]) <= 1:
            assert first["x"] < second["x"]
        else:
            assert first["y"] < second["y"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert mutations == []


def test_all_widgets_can_be_removed_and_restored_from_empty_overview(page, dashboard):
    page.goto(dashboard["url"] + "/#/overview")
    expect(page.locator("#startupLoader")).to_be_hidden()
    customize = page.locator("#btnCustomizeOverview")
    customize.click()
    for widget_id in DEFAULT_WIDGETS:
        widget_action(page, "remove", widget_id).click()
    page.locator("#btnDoneOverviewLayout").click()
    expect(customize).to_be_focused()
    expect_widget_order(page, [])
    assert stored_layout(page) == {"version": 1, "visible": []}

    page.reload()
    expect(page.locator("#startupLoader")).to_be_hidden()
    expect_widget_order(page, [])
    attention = page.locator('#editorAttention [data-attention-key="backup-missing"]')
    expect(attention).to_be_visible()
    empty_customize = page.locator("#btnCustomizeOverviewEmpty")
    empty_customize.click()
    page.keyboard.press("Escape")
    expect(empty_customize).to_be_focused()
    empty_customize.click()
    page.locator("#btnResetOverviewLayout").click()
    expect_widget_order(page, DEFAULT_WIDGETS)
    page.locator("#btnDoneOverviewLayout").click()
    expect(empty_customize).to_be_hidden()
    expect(customize).to_be_focused()
    page.reload()
    expect_widget_order(page, DEFAULT_WIDGETS)
    assert dashboard["actions"] == []


def test_overview_customizer_closes_when_history_leaves_overview(page, dashboard):
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#startupLoader")).to_be_hidden()
    page.locator('.nav [data-route="overview"]').click()
    page.locator("#btnCustomizeOverview").click()
    dialog = page.locator("#overviewCustomizeDialog")
    expect(dialog).to_be_visible()
    page.go_back()
    expect(dialog).to_be_hidden()
    expect(page.locator("#view-settings")).to_be_focused()
    assert page.evaluate(UPDATE_ALLOWED)


@pytest.mark.parametrize("language,width", [("ru", 741), ("en", 1280)])
def test_customize_and_fixed_alert_actions_remain_clickable(page, dashboard, language, width):
    page.add_init_script(f"""
        localStorage.setItem('pz-language', '{language}');
        localStorage.setItem('pz-sidebar-tablet', 'expanded');
    """)
    page.emulate_media(reduced_motion="reduce")
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(dashboard["url"] + "/#/overview")
    expect(page.locator("#startupLoader")).to_be_hidden()
    expect(page.locator("#navToggle")).to_have_attribute("aria-expanded", "true")
    page.evaluate("""async () => {
        await document.fonts.ready;
        liveSource?.close();
        liveSource = null;
        clearTimeout(sseStartupTimer);
        renderConnectionWarning();
        connectionAlert.element.hidden = false;
    }""")

    customize = page.locator("#btnCustomizeOverview")
    dialog = page.locator("#overviewCustomizeDialog")
    for failed in (False, True):
        # Use the existing notice markup so this layout check does not depend on worker activation.
        page.evaluate(
            """failed => {
                const action = document.getElementById('pwaUpdate');
                const alert = action.closest('.floating-alert');
                const note = document.getElementById('pwaUpdateNote');
                alert.querySelector('.pwa-update-message').textContent = I18n.t(
                    failed ? 'Обновление не завершено' : 'Доступна новая версия');
                action.querySelector('span').textContent = I18n.t(failed ? 'Повторить' : 'Обновить пульт');
                note.textContent = I18n.t('Не удалось применить новую версию. Повторите попытку; введённые данные сохранены.');
                note.hidden = !failed;
                alert.hidden = false;
            }""",
            failed,
        )
        for notice in (page.locator(".pwa-update-alert"), page.locator("#connBanner")):
            expect(notice).to_be_in_viewport(ratio=1)
            action = notice.get_by_role("button")
            expect(action).to_be_in_viewport(ratio=1)
            assert action.bounding_box()["height"] >= 44
            action.click(trial=True)
        assert customize.evaluate("""button => {
            const control = button.getBoundingClientRect();
            return [...document.querySelectorAll('.floating-alert:not([hidden])')].every(alert => {
                const box = alert.getBoundingClientRect();
                return box.left >= control.right || box.right <= control.left ||
                    box.top >= control.bottom || box.bottom <= control.top;
            });
        }""")
        customize.click()
        expect(dialog).to_be_visible()
        page.keyboard.press("Escape")
        expect(customize).to_be_focused()


@pytest.mark.parametrize(
    "raw,order",
    [
        ("{", DEFAULT_WIDGETS),
        ('{"version":9,"visible":[]}', DEFAULT_WIDGETS),
        ('{"version":1,"visible":["retired"]}', DEFAULT_WIDGETS),
        ('{"version":1,"visible":["events","retired","events","online"]}', ["events", "online"]),
    ],
    ids=["invalid-json", "unsupported-version", "all-unknown", "duplicates-and-unknown"],
)
def test_unknown_and_corrupt_layouts_load_safely(page, dashboard, raw, order):
    seed_layout(page, raw)
    page.goto(dashboard["url"] + "/#/overview")
    expect(page.locator("#startupLoader")).to_be_hidden()
    expect_widget_order(page, order)


def test_widget_customization_works_when_layout_storage_is_blocked(page, dashboard):
    page.add_init_script(f"""for (const method of ['getItem', 'setItem']) {{
        const original = Storage.prototype[method];
        Storage.prototype[method] = function(key, ...args) {{
            if (key === {json.dumps(LAYOUT_KEY)}) throw new DOMException('Denied', 'SecurityError');
            return original.call(this, key, ...args);
        }};
    }}""")
    page.goto(dashboard["url"] + "/#/overview")
    expect(page.locator("#startupLoader")).to_be_hidden()
    expect_widget_order(page, DEFAULT_WIDGETS)
    page.locator("#btnCustomizeOverview").click()
    widget_action(page, "remove", "online").click()
    expect(page.locator("#overviewStorageNotice")).to_be_visible()
    page.locator("#btnDoneOverviewLayout").click()
    expect(widget(page, "online")).to_be_hidden()
    page.reload()
    expect_widget_order(page, DEFAULT_WIDGETS)


def test_hidden_widgets_receive_live_updates_and_keep_their_controls(page, dashboard):
    page.add_init_script("localStorage.setItem('pz-language', 'en')")
    stats = {"ok": True, "cpuPct": 4, "memPct": 12, "memUsed": 1024, "memLimit": 4096}
    events = {"ok": True, "items": []}
    page.route("**/api/stats", lambda route: route.fulfill(json=stats))
    page.route("**/api/events?*", lambda route: route.fulfill(json=events))
    page.goto(dashboard["url"] + "/#/overview")
    expect(page.locator("#startupLoader")).to_be_hidden()
    page.wait_for_function("liveSource !== null")
    expect(page.locator("#kpiOnline")).to_have_text("0")
    expect(page.locator("#cpuVal")).to_have_text("4.0%")
    sparkline = page.locator("#cpuSpark")
    page.evaluate("renderStatsHistory([{cpu:4, mem:12}])")
    expect(sparkline).to_have_attribute("hidden", "")
    ids = ["online", "load", "events"]
    page.evaluate(
        """ids => window.originalOverviewWidgets = ids.map(id =>
            document.querySelector(`[data-overview-widget="${id}"]`))""",
        ids,
    )
    page.locator("#btnCustomizeOverview").click()
    for widget_id in ids:
        widget_action(page, "remove", widget_id).click()
        expect(widget(page, widget_id)).to_be_hidden()
    page.locator("#btnDoneOverviewLayout").click()

    dashboard["players"].update(names=["Dana", "Alex"], count=2)
    stats.update(cpuPct=27.5, memPct=42)
    events["items"].append({"type": "backup", "text": "World archive complete", "ts": 1791504000})
    page.evaluate(
        """frames => Object.entries(frames).forEach(([type, payload]) =>
            liveSource.dispatchEvent(new MessageEvent(type, {data: JSON.stringify(payload)})))""",
        {
            "players": dashboard["players"],
            "stats": stats,
            "events": events,
            "stats-history": {"points": [{"cpu": 4, "mem": 12}, {"cpu": 27.5, "mem": 42}]},
        },
    )
    expect(page.locator("#kpiOnline")).to_have_text("2")
    expect(page.locator("#cpuVal")).to_have_text("27.5%")
    expect(page.locator("#recentBody")).to_contain_text("World archive complete")
    page.locator("#btnCustomizeOverview").click()
    for widget_id in ids:
        widget_action(page, "add", widget_id).click()
    page.locator("#btnDoneOverviewLayout").click()
    assert page.evaluate("""originalOverviewWidgets.every(node =>
        !node.hidden && node === document.querySelector(`[data-overview-widget="${node.dataset.overviewWidget}"]`))""")
    expect(sparkline).to_be_visible()
    page.locator("#overviewWidgets a[href='#/players']").click()
    expect(page.locator("#view-players")).to_be_visible()
    expect(page.locator(".p-name")).to_have_text(["Dana", "Alex"])
    assert dashboard["actions"] == []


@pytest.mark.parametrize(
    "query,button_id,title",
    [
        ("Остановить сервер", "btnStop", "Остановить сервер?"),
        ("Перезапустить сервер", "btnRestart", "Перезапустить сервер?"),
    ],
    ids=["stop", "restart"],
)
def test_palette_confirmation_temporarily_reveals_hidden_status(
    page, dashboard, query, button_id, title
):
    seed_widgets(page, ["online"])
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#startupLoader")).to_be_hidden()
    page.locator("#btnCommands").click()
    page.locator("#commandSearch").fill(query)
    page.locator("#commandSearch").press("Enter")
    confirm = page.get_by_role("alertdialog")
    expect(confirm).to_be_visible()
    expect(confirm).to_contain_text(title)
    expect(widget(page, "status")).to_be_visible()
    assert dashboard["actions"] == []
    page.locator("#modalCancel").click()
    expect(confirm).to_be_hidden()
    expect(page.locator(f"#{button_id}")).to_be_focused()
    assert stored_layout(page) == {"version": 1, "visible": ["online"]}

    page.go_back()
    expect(page.locator("#view-settings")).to_be_visible()
    page.locator('.nav [data-route="overview"]').click()
    expect(page.locator("#view-overview")).to_be_visible()
    expect(widget(page, "status")).to_be_hidden()
    assert dashboard["actions"] == []


@pytest.mark.parametrize("running", [False, True], ids=["stopped", "running"])
def test_saved_config_attention_can_focus_start_with_status_removed(page, dashboard, running):
    seed_widgets(page, ["online"])
    dashboard["overview"]["containerInfo"] = {
        "running": running,
        "status": "running" if running else "exited",
    }
    page.route(
        "**/api/server-configs",
        lambda route: route.fulfill(json={"ok": True, "profiles": [{"file": "world.ini"}]}),
    )
    page.route(
        "**/api/config-draft?*",
        lambda route: route.fulfill(
            json={
                "ok": True,
                "file": "world.ini",
                "status": "saved",
                "state": {"savedRevision": "saved"},
                "currentRevision": "saved",
                "draftRevision": "draft",
                "texts": {"ini": "PublicName=Saved test server", "sandbox": None},
            }
        ),
    )
    page.route(
        "**/api/mods?*",
        lambda route: route.fulfill(
            json={
                "ok": True,
                "file": "world.ini",
                "draftRevision": "draft",
                "workshop": [],
                "maps": [],
                "mods": [],
                "problems": [],
            }
        ),
    )
    page.goto(dashboard["url"] + "/#/overview")
    expect(page.locator("#startupLoader")).to_be_hidden()
    expect(widget(page, "status")).to_be_hidden()
    attention = page.locator('#editorAttention [data-attention-key="saved"]')
    expect(attention).to_be_visible()
    attention.click()
    expect(widget(page, "status")).to_be_visible()
    expect(page.locator("#sec-status" if running else "#btnStart")).to_be_focused()
    assert stored_layout(page) == {"version": 1, "visible": ["online"]}
    assert dashboard["actions"] == []
