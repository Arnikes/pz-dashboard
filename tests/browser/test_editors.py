"""Browser editors use the real draft/metadata service with isolated file fixtures."""

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_configeditor import env, editor, INI  # noqa: E402,F401


@pytest.fixture
def editing(page, dashboard, env):  # noqa: F811 (imported pytest fixture)
    def api(route):
        url = urlsplit(route.request.url)
        query = parse_qs(url.query)
        file = query.get("file", ["world.ini"])[0]
        body = route.request.post_data_json if route.request.method == "POST" else {}
        try:
            if url.path == "/api/server-configs":
                result = editor.profiles()
            elif url.path == "/api/config-draft":
                result = editor.patch(body) if body else editor.draft(file)
            elif url.path == "/api/config-validate":
                result = editor.validate(
                    body["file"],
                    prepare=body.get("prepare", False),
                    draft_revision=body.get("draftRevision"),
                )
            elif url.path == "/api/config-history":
                result = editor.restore_history(body) if body else editor.history(file)
            elif url.path == "/api/mods":
                result = editor.mod_response(file, draft_mode=True)
            elif url.path == "/api/modpack":
                result = editor.modpack(body)
            else:
                route.fallback()
                return
            route.fulfill(json=result)
        except editor.EditorError as error:
            route.fulfill(status=error.status, json={"ok": False, "error": str(error)})

    page.route("**/api/**", api)
    return env


def navigate(page, route, mobile=False):
    if mobile and route not in ("overview", "players", "mods"):
        page.locator("#navMore").click()
        page.locator(f'#moreMenu a[href="#/{route}"]').click()
    else:
        page.locator(f'.nav [data-route="{route}"]').click()


@pytest.mark.parametrize("width", [1440, 390])
def test_real_form_draft_mod_selection_and_sources(page, dashboard, editing, width):
    data, _ = editing
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings", width == 390)
    name = page.locator('[data-key="PublicName"]')
    expect(name).to_have_value("Сервер")
    name.fill("UI draft")
    name.press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    assert (data / "Server/world.ini").read_bytes() == INI.encode()
    navigate(page, "mods")
    packet = page.locator("#modPackages details")
    packet.locator("summary").click()
    expect(page.locator('[data-modid="library"]')).to_be_checked()
    page.locator('[data-modid="plugin"]').uncheck()
    expect(page.locator("#modSummary")).to_contain_text("1 выбранных ModID")
    expect(page.locator("#modPackages details")).to_have_count(1)
    navigate(page, "settings", width == 390)
    expect(page.locator('[data-key="PublicName"]')).to_have_value("UI draft")
    page.get_by_role("tab", name="Исходники", exact=True).click()
    expect(page.locator("#iniSource")).to_contain_text("")
    assert "UI draft" in page.locator("#iniSource").input_value()
    assert "topsecret" not in page.locator("#iniSource").input_value()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(data.parent / f"editor-{width}.png"), full_page=True)


def test_background_refresh_preserves_profile_focus_and_draft(page, dashboard, editing):
    data, _ = editing
    (data / "Server/another.ini").write_bytes(INI.encode())
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    page.locator("#configProfile").select_option("another.ini")
    expect(page.locator("#configActive")).to_contain_text("другого")
    navigate(page, "settings")
    field = page.locator('[data-key="PublicName"]')
    field.fill("Other draft")
    field.press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    field.focus()
    page.evaluate(
        "renderMods({ok:true,file:'world.ini',files:['world.ini','another.ini'],mods:[],workshop:[]})"
    )
    expect(field).to_be_focused()
    expect(field).to_have_value("Other draft")
    expect(page.locator("#configProfile")).to_have_value("another.ini")
    expect(page.locator("#configApply")).to_be_disabled()


def test_conflict_and_invalid_field_are_visible(page, dashboard, editing):
    data, _ = editing
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings")
    (data / "Server/world.ini").write_bytes(INI.replace("Сервер", "External").encode())
    page.locator('[data-key="PublicName"]').fill("Should not overwrite")
    page.locator('[data-key="PublicName"]').press("Tab")
    expect(page.locator("#configError")).to_contain_text("изменились")
    assert "External" in (data / "Server/world.ini").read_text(encoding="utf-8")
    page.locator("#configDiff").click()
    expect(page.get_by_role("alertdialog")).to_contain_text("Сейчас на диске")
    expect(page.get_by_role("alertdialog")).not_to_contain_text("topsecret")


def test_export_pack_download(page, dashboard, editing):
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    with page.expect_download() as received:
        page.locator("#modExport").click()
    download = received.value
    pack = json.loads(Path(download.path()).read_text(encoding="utf-8"))
    assert pack["items"] == ["111"] and pack["selected"] == ["library", "plugin"]
    assert "hidden-token" not in json.dumps(pack)


@pytest.mark.parametrize("width", [1440, 390])
def test_keyboard_tabs_and_mod_order(page, dashboard, editing, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    tab = page.get_by_role("tab", name="Состав", exact=True)
    tab.focus()
    tab.press("ArrowRight")
    expect(page.get_by_role("tab", name="Порядок", exact=True)).to_have_attribute(
        "aria-selected", "true"
    )
    page.get_by_role("button", name="Опустить library", exact=True).click()
    expect(page.locator("#modOrder .order-row").first.locator("code")).to_have_text("plugin")
    page.get_by_role("button", name="Поднять library", exact=True).click()
    expect(page.locator("#modOrder .order-row").first.locator("code")).to_have_text("library")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_long_mod_list_on_phone(page, dashboard, editing):
    data, _ = editing
    for i in range(100):
        folder = data / f"steamapps/workshop/content/108600/111/mods/Additional{i}/42"
        folder.mkdir(parents=True)
        (folder / "mod.info").write_text(
            f"id=additional-{i}\nname=Additional mod {i}\n", encoding="utf-8"
        )
    editor.workshop.invalidate()
    page.set_viewport_size({"width": 390, "height": 900})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    page.locator("#modPackages summary").click()
    expect(page.locator("#modPackages [data-modid]")).to_have_count(102)
    page.locator('[data-modid="additional-99"]').scroll_into_view_if_needed()
    page.locator('[data-modid="additional-99"]').check()
    expect(page.locator("#modSummary")).to_contain_text("3 выбранных ModID")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_disconnected_form_keeps_edit_until_explicit_retry(page, dashboard, editing):
    disconnected = [False]

    def drop_write(route):
        if disconnected[0] and route.request.method == "POST":
            route.abort()
        else:
            route.fallback()

    page.route("**/api/config-draft", drop_write)
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings")
    disconnected[0] = True
    field = page.locator('[data-key="PublicName"]')
    field.fill("Offline draft")
    field.press("Tab")
    expect(page.locator("#draftRetry")).to_be_visible()
    page.get_by_role("tab", name="Мир", exact=True).click()
    expect(field).to_have_value("Offline draft")
    expect(page.get_by_role("tab", name="Сервер", exact=True)).to_have_attribute(
        "aria-selected", "true"
    )
    disconnected[0] = False
    page.locator("#draftRetry").click()
    expect(page.locator("#draftRetry")).to_be_hidden()
    expect(page.locator('[data-key="PublicName"]')).to_have_value("Offline draft")
    assert "Offline draft" in editor.draft("world.ini")["texts"]["ini"]


@pytest.mark.parametrize("width", [1440, 390])
def test_multiline_and_list_fields_round_trip_into_same_draft(page, dashboard, editing, width):
    data, _ = editing
    original = (
        INI
        + "PublicDescription=First\\nSecond\r\nChatStreams=s,r,a\r\nServerWelcomeMessage=Welcome<LINE>Survivor\r\n"
    )
    (data / "Server/world.ini").write_bytes(original.encode())
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings", width == 390)
    description = page.locator('[data-key="PublicDescription"]')
    expect(description).to_have_value("First\nSecond")
    description.fill("Первая строка\nВторая строка")
    description.press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    streams = page.locator('[data-key="ChatStreams"]')
    expect(streams).to_have_value("s\nr\na")
    streams.fill("s\nr\ny")
    streams.press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    page.get_by_role("tab", name="Исходники", exact=True).click()
    source = page.locator("#iniSource").input_value()
    assert "PublicDescription=Первая строка\\nВторая строка" in source
    assert "ChatStreams=s,r,y" in source
    assert "ServerWelcomeMessage=Welcome<LINE>Survivor" in source
    assert (data / "Server/world.ini").read_bytes() == original.encode()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_inactive_mod_settings_are_preserved_separately_from_stock(page, dashboard, editing):
    data, _ = editing
    path = data / "steamapps/workshop/content/108600/111/mods/PluginFolder/42/media"
    path.mkdir()
    (path / "sandbox-options.txt").write_text(
        "option Mod.Count { type=integer, min=0, max=10, default=2, page=PluginPage, }",
        encoding="utf-8",
    )
    lua_path = data / "Server/world_SandboxVars.lua"
    lua_path.write_text(
        lua_path.read_text(encoding="utf-8").replace(
            "Zombies = 4,", "Zombies = 4, AnimalMetaPredator = true,"
        ),
        encoding="utf-8",
    )
    editor.workshop.invalidate()
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    page.locator("#modPackages summary").click()
    page.locator('[data-modid="plugin"]').uncheck()
    expect(page.locator("#modSummary")).to_contain_text("1 выбранных ModID")
    navigate(page, "settings")
    page.get_by_role("tab", name="Мир", exact=True).click()
    expect(page.locator('[data-key="AnimalMetaPredator"]')).to_be_checked()
    expect(page.locator('[data-key="Mod.Count"]')).to_have_count(0)
    page.get_by_role("tab", name="Настройки модов", exact=True).click()
    expect(page.locator('[data-key="Mod.Count"]')).to_have_value("2")
    expect(page.locator("#configFields")).to_contain_text("Выключенные / отсутствующие моды")
    assert "Count = 2" in editor.draft("world.ini")["texts"]["sandbox"]


def test_future_version_can_be_disabled_but_cannot_be_selected_again(page, dashboard, editing):
    data, _ = editing
    path = data / "steamapps/workshop/content/108600/111/mods/Future/42.99"
    path.mkdir(parents=True)
    (path / "mod.info").write_text("id=future\nname=Future only", encoding="utf-8")
    ini = data / "Server/world.ini"
    ini.write_bytes(
        INI.replace("Mods=\\library;\\plugin", "Mods=\\library;\\plugin;\\future").encode()
    )
    editor.workshop.invalidate()
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    page.locator("#modPackages summary").click()
    future = page.locator('[data-modid="future"]')
    expect(future).to_be_checked()
    expect(future).to_be_enabled()
    future.uncheck()
    expect(future).not_to_be_checked()
    expect(future).to_be_disabled()
    expect(page.locator("#modPackages")).to_contain_text("Нет подходящего каталога B42")


def test_source_then_mod_edit_use_one_draft_without_restoring_old_selection(
    page, dashboard, editing
):
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings")
    page.get_by_role("tab", name="Исходники", exact=True).click()
    source = page.locator("#iniSource")
    source.fill(source.input_value().replace("PublicName=Сервер", "PublicName=Source draft"))
    navigate(page, "mods")
    page.locator("#modPackages summary").click()
    page.locator('[data-modid="plugin"]').uncheck()
    expect(page.locator("#modSummary")).to_contain_text("1 выбранных ModID")
    navigate(page, "settings")
    assert "PublicName=Source draft" in source.input_value()
    assert "Mods=\\library\n" in source.input_value()
    page.locator("#sourceSave").click()
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    draft = editor.draft("world.ini")
    assert "PublicName=Source draft" in draft["texts"]["ini"]
    assert "Mods=\\library\r\n" in draft["texts"]["ini"]


@pytest.mark.parametrize("width", [1440, 390])
def test_failed_start_result_restores_configuration_into_shared_draft(
    page, dashboard, editing, monkeypatch, width
):
    page.emulate_media(reduced_motion="reduce")
    current = editor.draft("world.ini")
    current = editor.patch(
        {
            "file": "world.ini",
            "draftRevision": current["draftRevision"],
            "ini": {"PublicName": "Failed start"},
        }
    )
    monkeypatch.setattr(editor.dockerlib, "container_start", lambda name: (1, "", "failed"))
    with pytest.raises(editor.EditorError):
        editor.run(
            {"file": "world.ini", "draftRevision": current["draftRevision"], "restart": True}
        )
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    result = page.locator("#configOperationResult")
    expect(result).to_be_visible()
    expect(result).to_contain_text("Не удалось запустить контейнер")
    result.locator("[data-operation-restore]").click()
    page.locator("#modalOk").click()
    expect(page.get_by_role("alertdialog")).to_contain_text("Изменения при восстановлении")
    page.locator("#modalOk").click()
    navigate(page, "settings", width == 390)
    expect(page.locator('[data-key="PublicName"]')).to_have_value("Сервер")
    assert "PublicName=Failed start" in editor.read_profile("world.ini")["ini"]
    assert editor.draft("world.ini")["changed"]


@pytest.mark.parametrize("width", [1440, 390])
def test_individual_toggle_restores_original_order(page, dashboard, editing, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    page.locator("#modPackages summary").click()
    page.locator('[data-modid="library"]').uncheck()
    expect(page.locator("#modSummary")).to_contain_text("1 выбранных ModID")
    page.locator('[data-modid="library"]').check()
    expect(page.locator("#modSummary")).to_contain_text("2 выбранных ModID")
    page.get_by_role("tab", name="Порядок", exact=True).click()
    expect(page.locator("#modOrder [data-order-id]")).to_have_count(2)
    assert page.locator("#modOrder [data-order-id]").evaluate_all(
        "rows => rows.map(row => row.dataset.orderId)"
    ) == ["library", "plugin"]
    assert "Mods=\\library;\\plugin\r\n" in editor.draft("world.ini")["texts"]["ini"]


def test_prepare_modal_previews_only_download_stage(page, dashboard, editing):
    current = editor.draft("world.ini")
    editor.patch(
        {
            "file": "world.ini",
            "draftRevision": current["draftRevision"],
            "ini": {"PublicName": "Later name"},
            "sandbox": {"Zombies": 99},
            "mods": {"items": ["111", "222"], "selected": ["library", "unknown-new"]},
        }
    )
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    page.locator("#prepareWorkshop").click()
    expect(page.get_by_role("alertdialog")).to_contain_text("Загрузить Workshop items?")
    expect(page.get_by_role("alertdialog")).to_contain_text("+WorkshopItems=111;222")
    expect(page.get_by_role("alertdialog")).not_to_contain_text("Later name")
    expect(page.get_by_role("alertdialog")).not_to_contain_text("+ Zombies = 99")


def test_new_mod_password_is_masked_before_first_save(page, dashboard, editing):
    data, _ = editing
    path = data / "steamapps/workshop/content/108600/111/mods/PluginFolder/42/media"
    path.mkdir()
    (path / "sandbox-options.txt").write_text(
        "option Mod.NewPassword { type=string, default=public-default, }", encoding="utf-8"
    )
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings")
    page.get_by_role("tab", name="Настройки модов", exact=True).click()
    field = page.locator('[data-key="Mod.NewPassword"]')
    expect(field).to_have_attribute("type", "password")
    expect(field).to_have_value("")
    field.fill("private-editor-token")
    field.press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    page.get_by_role("tab", name="Исходники", exact=True).click()
    assert "private-editor-token" not in page.locator("#sandboxSource").input_value()
    assert "Mod.NewPassword" not in editor.modpack({"file": "world.ini"})["pack"]["sandbox"]
