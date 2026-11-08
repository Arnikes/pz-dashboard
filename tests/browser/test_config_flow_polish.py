"""Configuration and Workshop next steps remain visible and revision-safe."""

import pytest
from playwright.sync_api import expect

from test_editors import INI, editing, editor, env, navigate  # noqa: F401
from test_mods_pagination import composition  # noqa: F401

from browser_evidence import capture_evidence

pytestmark = pytest.mark.browser
SCREENS = [(language, width) for language in ("ru", "en") for width in (390, 1440)]


def open_mods(page, dashboard, language="ru", width=390):
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(dashboard["url"] + "/#/mods")
    expect(page.locator("#view-mods")).to_have_attribute("aria-busy", "false")


def resolve_items(page, ids):
    page.route(
        "**/api/workshop-resolve",
        lambda route: route.fulfill(
            json={"ok": True, "items": [{"workshopId": item} for item in ids]}
        ),
    )


def submit_item(page, value):
    page.locator("#workshopInput").fill(value)
    page.locator("#workshopInput").press("Enter")


def evidence(page, data, name):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if page.viewport_size["width"] <= 740:
        for control in page.locator("#configApply:visible, #chooseInstalledMods:visible").all():
            assert control.bounding_box()["height"] >= 44
    capture_evidence(page, path=str(data.parent / f"flow-polish-{name}.png"))


def assert_summary_usable(summary):
    expect(summary).to_be_focused()
    expect(summary).to_be_in_viewport(ratio=1)
    assert summary.evaluate("""summary => {
        const box = summary.getBoundingClientRect();
        const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
        return hit === summary || summary.contains(hit);
    }""")


@pytest.mark.parametrize("language,width", SCREENS)
def test_mods_feedback_preserves_failed_input_and_explains_restrictions(
    page,
    dashboard,
    editing,  # noqa: F811
    language,
    width,
):
    data, _ = editing
    (data / "Server/other.ini").write_bytes(INI.encode())
    page.route(
        "**/api/workshop-resolve",
        lambda route: route.fulfill(status=503, json={"ok": False, "error": "Steam unavailable"}),
    )
    open_mods(page, dashboard, language, width)
    page.locator("#configProfile").select_option("other.ini")
    expect(page.locator("#configActionHelp")).to_be_visible()
    expect(page.locator("#configApply")).to_be_disabled()
    expect(page.locator("#configApply")).to_have_attribute("aria-describedby", "configActionHelp")
    submit_item(page, "222")
    expect(page.locator("#configError")).to_have_text("Steam unavailable")
    expect(page.locator("#configError")).to_be_visible()
    expect(page.locator("#workshopInput")).to_have_value("222")
    assert not editor.draft("other.ini")["changed"]
    evidence(page, data, f"feedback-{language}-{width}")
    navigate(page, "overview")
    expect(page.locator("#configFeedback")).to_be_hidden()
    assert dashboard["actions"] == []


@pytest.mark.parametrize("language,width", SCREENS)
def test_add_reveals_sorted_page_duplicate_is_noop_and_normal_rescan_preserves_view(
    page,
    dashboard,
    composition,  # noqa: F811
    language,
    width,
):
    data, _, selected = composition
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.set_viewport_size({"width": width, "height": 900})
    page.reload()
    expect(page.locator("#modPackages details")).to_have_count(25)
    original = (data / "Server/world.ini").read_bytes()
    resolve_items(page, ["999"])
    page.locator("#modSortNew").select_option("title")
    page.locator("#modCompositionPageSize").select_option("50")
    page.locator("#modCompositionNext").click()
    page.locator("#modFilter").select_option("selected")
    page.locator("#modQuery").fill("no-match")
    expect(page.locator("#modClearFilters")).to_be_visible()
    evidence(page, data, f"filtered-{language}-{width}")
    page.locator("#modClearFilters").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#modQuery")).to_be_focused()
    expect(page.locator("#modQuery")).to_have_value("")
    expect(page.locator("#modFilter")).to_have_value("all")
    page.locator("#modFilter").select_option("selected")
    page.locator("#modQuery").fill("no-match")
    submit_item(page, "999")
    packet = page.locator('.workshop-package[data-item="999"]')
    expect(packet).to_have_attribute("open", "")
    assert_summary_usable(packet.locator("summary"))
    expect(page.locator("#modQuery")).to_have_value("")
    expect(page.locator("#modFilter")).to_have_value("all")
    expect(page.locator("#modSortNew")).to_have_value("title")
    expect(page.locator("#modCompositionPageSize")).to_have_value("50")
    expect(page.locator("#modCompositionRange")).to_contain_text("51–62")
    expect(page.locator("#workshopAddResult")).to_contain_text(
        "Добавлено пакетов в черновик: 1." if language == "ru" else "Packages added to draft: 1."
    )
    expect(page.locator("#configApply")).to_have_text(
        "Загрузить пакеты с рестартом…" if language == "ru" else "Download packages with restart…"
    )
    evidence(page, data, f"added-{language}-{width}")
    before = editor.draft("world.ini")["draftRevision"]
    posts = []

    def record_patch(route):
        if route.request.method == "POST":
            posts.append(route.request.post_data_json)
        route.fallback()

    page.route("**/api/config-draft", record_patch)
    submit_item(page, "999")
    expect(page.locator("#workshopAddResult")).to_contain_text(
        "Пакет уже в конфигурации. Черновик не изменён."
        if language == "ru"
        else "Package already in configuration. Draft unchanged."
    )
    assert_summary_usable(packet.locator("summary"))
    assert editor.draft("world.ini")["draftRevision"] == before
    assert posts == []
    page.locator("#modCompositionPageSize").select_option("25")
    page.locator("#modQuery").fill("mod-")
    page.locator("#modFilter").select_option("selected")
    page.locator("#modCompositionNext").click()
    current_range = page.locator("#modCompositionRange").inner_text()
    page.locator("#modQuery").focus()
    with page.expect_response("**/api/mods?*refresh=1"):
        page.locator("#modRescan").evaluate("button => button.click()")
    expect(page.locator("#view-mods")).to_have_attribute("aria-busy", "false")
    expect(page.locator("#modQuery")).to_be_focused()
    expect(page.locator("#modQuery")).to_have_value("mod-")
    expect(page.locator("#modFilter")).to_have_value("selected")
    expect(page.locator("#modCompositionRange")).to_have_text(current_range)
    assert editor.mod_response("world.ini", draft_mode=True)["mods"] == selected
    assert (data / "Server/world.ini").read_bytes() == original
    assert dashboard["actions"] == []


@pytest.mark.parametrize("language,width", SCREENS)
def test_saved_add_survives_metadata_failure_and_rescan_reveals_recovery(
    page,
    dashboard,
    editing,  # noqa: F811
    language,
    width,
):
    data, _ = editing
    open_mods(page, dashboard, language, width)
    resolve_items(page, ["222"])
    page.locator("#modQuery").fill("library")
    page.route(
        "**/api/mods?*",
        lambda route: route.fulfill(
            status=503, json={"ok": False, "error": "Metadata unavailable"}
        ),
    )
    submit_item(page, "222")
    expect(page.locator("#workshopAddResult")).to_contain_text(
        "Перечитайте метаданные" if language == "ru" else "Rescan metadata"
    )
    expect(page.locator("#configError")).to_have_text("Metadata unavailable")
    expect(page.locator("#configError")).to_be_visible()
    expect(page.locator("#draftSaved")).to_have_text(
        "Добавлено пакетов в черновик: 1." if language == "ru" else "Packages added to draft: 1."
    )
    expect(page.locator("#draftRetry")).to_be_hidden()
    saved = editor.draft("world.ini")
    assert "WorkshopItems=111;222" in saved["texts"]["ini"]
    page.unroute("**/api/mods?*")
    page.locator("#modRescan").click()
    packet = page.locator('.workshop-package[data-item="222"]')
    expect(packet).to_have_attribute("open", "")
    assert_summary_usable(packet.locator("summary"))
    expect(page.locator("#configError")).to_be_hidden()
    expect(page.locator("#modQuery")).to_have_value("")
    assert editor.draft("world.ini")["draftRevision"] == saved["draftRevision"]
    evidence(page, data, f"recovered-{language}-{width}")
    assert (data / "Server/world.ini").read_bytes() == INI.encode()
    assert dashboard["actions"] == []


@pytest.mark.parametrize("source_edit", [False, True])
def test_primary_download_decision_uses_flushed_revision(page, dashboard, editing, source_edit):  # noqa: F811
    data, _ = editing
    open_mods(page, dashboard)
    original = (data / "Server/world.ini").read_bytes()
    validations = []

    def record_validation(route):
        validations.append(route.request.post_data_json)
        route.fallback()

    page.route("**/api/config-validate", record_validation)
    if source_edit:
        navigate(page, "settings")
        page.locator('[data-tab="sources"]').click()
        source = page.locator("#iniSource")
        source.fill(source.input_value().replace("WorkshopItems=111", "WorkshopItems=111;222"))
        page.locator("#configApply").click()
    else:
        resolve_items(page, ["222"])
        pending = []
        page.route("**/api/mods?*", lambda route: pending.append(route))
        with page.expect_request("**/api/mods?*"):
            submit_item(page, "222")
        page.locator("#configApply").click()
        assert validations == []
        pending.pop().fulfill(json=editor.mod_response("world.ini", draft_mode=True))
    expect(page.get_by_role("alertdialog")).to_be_visible()
    assert len(validations) == 1
    assert validations[0]["prepare"] is True
    assert validations[0]["draftRevision"] == editor.draft("world.ini")["draftRevision"]
    expect(page.get_by_role("alertdialog")).to_contain_text("Первый рестарт загрузит пакеты")
    page.locator("#modalOk").click()
    expect(page.locator("#modalError")).to_contain_text("сервер занят")
    assert dashboard["actions"][0]["op"] == "prepare-workshop"
    assert dashboard["actions"][0]["draftRevision"] == validations[0]["draftRevision"]
    assert (data / "Server/world.ini").read_bytes() == original


@pytest.mark.parametrize("destination", ["settings", "order"])
def test_late_add_does_not_reset_view_or_steal_focus(page, dashboard, editing, destination):  # noqa: F811
    open_mods(page, dashboard)
    resolve_items(page, ["222"])
    pending = []
    page.route("**/api/mods?*", lambda route: pending.append(route))
    page.locator("#modQuery").fill("library")
    with page.expect_request("**/api/mods?*"):
        submit_item(page, "222")
    if destination == "settings":
        navigate(page, "settings")
        focus = page.locator("#configSearch")
    else:
        page.locator('#modTabs [data-tab="order"]').click()
        focus = page.locator("#orderQuery")
    focus.focus()
    pending.pop().fulfill(json=editor.mod_response("world.ini", draft_mode=True))
    expect(page.locator("#draftSaved")).to_have_text("Добавлено пакетов в черновик: 1.")
    expect(focus).to_be_focused()
    expect(page.locator("#modQuery")).to_have_value("library")
    expect(
        page.locator("#view-settings" if destination == "settings" else "#modOrder")
    ).to_be_visible()
    assert dashboard["actions"] == []


@pytest.mark.parametrize("known", [False, True])
@pytest.mark.parametrize("language,width", SCREENS)
def test_downloaded_selection_only_reveals_known_added_packages(
    page,
    dashboard,
    editing,  # noqa: F811
    known,
    language,
    width,
):
    data, _ = editing
    for item in ["222", "333"]:
        folder = data / f"steamapps/workshop/content/108600/{item}/mods/New/42"
        folder.mkdir(parents=True)
        (folder / "mod.info").write_text(f"id=new-{item}\nname=New {item}\n", encoding="utf-8")
    editor.workshop.invalidate()
    draft = editor.draft("world.ini")
    editor.patch(
        {
            "file": "world.ini",
            "draftRevision": draft["draftRevision"],
            "mods": {"items": ["111", "222", "333"]},
        }
    )
    editor.save_json(
        editor.state_dir("world.ini") / "state.json",
        {
            "status": "select-mods",
            "installation": {"stage": "select-mods", "addedItems": ["222", "333"] if known else []},
        },
    )
    open_mods(page, dashboard, language, width)
    choice = page.locator("#chooseInstalledMods")
    if known:
        expect(choice).to_have_text(
            "Выбрать ModID · пакетов: 2" if language == "ru" else "Choose ModID · packages: 2"
        )
        page.locator("#modQuery").fill("library")
        choice.focus()
        page.keyboard.press("Enter")
        packet = page.locator('.workshop-package[data-item="222"]')
        expect(packet).to_have_attribute("open", "")
        assert_summary_usable(packet.locator("summary"))
        expect(packet.locator('[data-modid="new-222"]')).not_to_be_checked()
    else:
        expect(choice).to_have_count(0)
        expect(page.locator("#modPackages details[open]")).to_have_count(0)
    evidence(page, data, f"select-{known}-{language}-{width}")
    assert editor.mod_response("world.ini", draft_mode=True)["mods"] == ["library", "plugin"]
    assert (data / "Server/world.ini").read_bytes() == INI.encode()
    assert dashboard["actions"] == []
