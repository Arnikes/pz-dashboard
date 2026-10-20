"""Draft edits, review identity and direct editor navigation survive failures."""

import pytest
from playwright.sync_api import expect

from test_editors import INI, editing, editor, env, navigate, saved_verification  # noqa: F401


pytestmark = pytest.mark.browser


def open_editor(page, dashboard, language, width, route="settings"):
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(dashboard["url"] + f"/#/{route}")
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    expect(page.locator(f"#view-{route}")).to_have_attribute("aria-busy", "false")


def stale_review(language):
    return (
        "Черновик изменился после просмотра. Проверьте изменения заново"
        if language == "ru"
        else "Draft changed since review. Check changes again"
    )


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("width", [390, 1440])
def test_saved_mod_edit_survives_metadata_failure(page, dashboard, editing, language, width):  # noqa: F811
    open_editor(page, dashboard, language, width, "mods")
    page.locator("#modPackages summary").click()
    pending = []
    page.route("**/api/mods?*", lambda route: pending.append(route))
    with page.expect_request("**/api/mods?*"):
        page.locator('[data-modid="plugin"]').uncheck()
    expect(page.locator("#view-mods")).to_have_attribute("aria-busy", "true")
    assert editor.mod_response("world.ini", draft_mode=True)["mods"] == ["library"]
    pending.pop().fulfill(status=503, json={"ok": False, "error": "Metadata unavailable"})
    expect(page.locator("#configError")).to_have_text("Metadata unavailable")
    expect(page.locator("#draftSaved")).to_have_text(
        "Черновик сохранён" if language == "ru" else "Draft saved"
    )
    expect(page.locator('[data-modid="plugin"]')).not_to_be_checked()
    expect(page.locator('[data-modid="library"]')).to_be_disabled()
    expect(page.locator("#modRescan")).to_be_enabled()
    # Even an already queued/programmatic change cannot use stale metadata.
    page.locator('[data-modid="library"]').evaluate("""el => {
        el.checked = false; el.dispatchEvent(new Event('change', {bubbles:true}));
    }""")
    expect(page.locator('[data-modid="library"]')).to_be_checked()
    assert editor.mod_response("world.ini", draft_mode=True)["mods"] == ["library"]
    page.unroute("**/api/mods?*")
    page.locator("#modRescan").click()
    expect(page.locator('[data-modid="library"]')).to_be_enabled()
    expect(page.locator("#configError")).to_be_hidden()
    expect(page.locator('[data-modid="plugin"]')).not_to_be_checked()
    page.locator('[data-modid="library"]').uncheck()
    expect(page.locator("#view-mods")).to_have_attribute("aria-busy", "false")
    assert editor.mod_response("world.ini", draft_mode=True)["mods"] == []
    assert dashboard["actions"] == []


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("width", [390, 1440])
@pytest.mark.parametrize("switch_profile", [False, True])
def test_late_review_cannot_target_changed_draft(
    page,
    dashboard,
    editing,  # noqa: F811
    language,
    width,
    switch_profile,  # noqa: F811
):
    data, _ = editing
    (data / "Server/another.ini").write_bytes(
        INI.replace("PublicName=Сервер", "PublicName=Profile B").encode()
    )
    open_editor(page, dashboard, language, width)
    field = page.locator('[data-key="PublicName"]')
    field.fill("Draft A")
    field.press("Tab")
    expect(page.locator("#draftSaved")).to_have_text(
        "Черновик сохранён" if language == "ru" else "Draft saved"
    )
    old = editor.validate("world.ini", overwrite=True)
    old["errors"] = [{"key": "PublicName", "message": "Stale field error"}]
    pending = []
    page.route("**/api/config-validate", lambda route: pending.append(route))
    page.locator("#configDiff").click()
    expect(page.locator("#view-settings")).to_have_attribute("aria-busy", "true")
    if switch_profile:
        page.locator("#configProfile").select_option("another.ini")
        expect(field).to_have_value("Profile B")
    expect(field).to_be_enabled()
    field.fill("Draft B")
    field.press("Tab")
    expect(page.locator("#draftSaved")).to_have_text(
        "Черновик сохранён" if language == "ru" else "Draft saved"
    )
    pending.pop().fulfill(json=old)
    expect(page.locator("#configError")).to_have_text(stale_review(language))
    expect(page.get_by_role("alertdialog")).to_be_hidden()
    expect(page.locator('[data-error-key="PublicName"]')).to_be_hidden()
    expect(field).to_have_value("Draft B")
    assert editor.draft("world.ini")["changed"]
    if switch_profile:
        assert editor.draft("another.ini")["changed"]
    assert dashboard["actions"] == []


@pytest.mark.parametrize("language,width", [("ru", 390), ("en", 1440)])
@pytest.mark.parametrize("entry", ["apply", "discard"])
def test_confirmation_rechecks_edits_queued_after_click(
    page,
    dashboard,
    editing,  # noqa: F811
    language,
    width,
    entry,  # noqa: F811
):
    open_editor(page, dashboard, language, width)
    field = page.locator('[data-key="PublicName"]')
    field.fill("Reviewed value")
    page.locator("#configApply" if entry == "apply" else "#configDiff").click()
    expect(page.get_by_role("alertdialog")).to_be_visible()
    if entry == "discard":
        page.locator("#modalCancel").click()
    posts = []

    def record(route):
        posts.append(route.request.post_data_json)
        route.fallback()

    page.route("**/api/config-draft", record)
    # Queue an edit before the confirmation's continuation, after its first guard.
    page.evaluate("""() => {
        queueMicrotask(() => {
            const field = document.querySelector('[data-key="PublicName"]');
            field.value = 'New queued value';
            field.dispatchEvent(new Event('input', {bubbles:true}));
            field.dispatchEvent(new Event('change', {bubbles:true}));
        });
        document.getElementById('modalOk').click();
    }""")
    expect(page.locator("#modalError")).to_contain_text(stale_review(language))
    expect(page.locator("#view-settings")).to_have_attribute("aria-busy", "false")
    expect(field).to_have_value("New queued value")
    assert "PublicName=New queued value" in editor.draft("world.ini")["texts"]["ini"]
    assert all(not body.get("discard") for body in posts)
    assert dashboard["actions"] == []


@pytest.mark.parametrize("language,width", [("ru", 390), ("en", 1440)])
def test_failed_discard_preserves_input_received_during_request(
    page,
    dashboard,
    editing,  # noqa: F811
    language,
    width,  # noqa: F811
):
    open_editor(page, dashboard, language, width)
    field = page.locator('[data-key="PublicName"]')
    field.fill("Saved draft")
    page.locator("#configDiff").click()
    expect(page.get_by_role("alertdialog")).to_be_visible()
    page.locator("#modalCancel").click()
    pending = []
    page.route("**/api/config-draft", lambda route: pending.append(route))
    page.locator("#modalOk").click()
    expect(page.locator("#view-settings")).to_have_attribute("aria-busy", "true")
    field.evaluate("""el => {
        el.value = 'Keep local input';
        el.dispatchEvent(new Event('input', {bubbles:true}));
    }""")
    pending.pop().fulfill(status=503, json={"ok": False, "error": "Discard unavailable"})
    expect(page.locator("#modalError")).to_contain_text("Discard unavailable")
    expect(field).to_have_value("Keep local input")
    page.locator("#modalOk").click()
    expect(page.locator("#modalError")).to_contain_text(stale_review(language))
    assert not pending
    assert "PublicName=Saved draft" in editor.draft("world.ini")["texts"]["ini"]
    assert page.evaluate("""() => {
        const event = new Event('beforeunload', {cancelable:true});
        window.dispatchEvent(event); return event.defaultPrevented;
    }""")
    assert dashboard["actions"] == []


def test_direct_editor_tabs_reuse_saved_snapshot_and_reopening_refreshes(
    page,
    dashboard,
    editing,  # noqa: F811
):
    dashboard["overview"]["containerInfo"]["startedAt"] = "start-1"
    open_editor(page, dashboard, "ru", 1440)
    field = page.locator('[data-key="PublicName"]')
    field.fill("Cached ordinary edit")
    field.press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    field.evaluate("el => el.cachedNode = true")
    requests = []
    page.on(
        "request",
        lambda request: (
            requests.append(request.url)
            if "/api/config-draft?" in request.url or "/api/mods?" in request.url
            else None
        ),
    )
    navigate(page, "mods")
    navigate(page, "settings")
    expect(field).to_be_enabled()
    expect(field).to_have_value("Cached ordinary edit")
    assert field.evaluate("el => el.cachedNode")
    assert requests == []
    navigate(page, "overview")
    with page.expect_request("**/api/config-draft?*"):
        navigate(page, "settings")
    expect(page.locator("#view-settings")).to_have_attribute("aria-busy", "false")
    assert len(requests) == 2
    assert not field.evaluate("el => !!el.cachedNode")
    assert dashboard["actions"] == []


def test_direct_tab_cache_invalidates_after_restart(
    page,
    dashboard,
    editing,  # noqa: F811
    monkeypatch,  # noqa: F811
):
    _, request = saved_verification(editing, monkeypatch)
    editor.verify_running(request)
    dashboard["overview"]["containerInfo"]["startedAt"] = "external-start"
    open_editor(page, dashboard, "en", 390)
    expect(page.locator("#flowLaunch")).to_have_text("Confirmed")
    from test_configeditor import ops

    monkeypatch.setattr(
        ops, "container_state", lambda: {"running": True, "startedAt": "next-start"}
    )
    page.evaluate("""applyOverview({...S.overview,
        containerInfo:{...S.overview.containerInfo,startedAt:'next-start'}})""")
    with page.expect_request("**/api/config-draft?*"):
        navigate(page, "mods")
    expect(page.locator("#view-mods")).to_have_attribute("aria-busy", "false")
    expect(page.locator("#flowLaunch")).to_have_text("Verification needed")
    assert dashboard["actions"] == []


def test_direct_tab_cache_invalidates_after_failed_profile_refresh(
    page,
    dashboard,
    editing,  # noqa: F811
):
    dashboard["overview"]["containerInfo"]["startedAt"] = "start-1"
    open_editor(page, dashboard, "ru", 1440)
    pending = []
    page.route("**/api/config-draft?*", lambda route: pending.append(route))
    navigate(page, "overview")
    navigate(page, "settings")
    expect(page.locator("#view-settings")).to_have_attribute("aria-busy", "true")
    pending.pop().fulfill(status=503, json={"ok": False, "error": "Refresh unavailable"})
    expect(page.locator("#configError")).to_have_text("Refresh unavailable")
    with page.expect_request("**/api/config-draft?*"):
        navigate(page, "mods")
    assert len(pending) == 1
    pending.pop().fallback()
    expect(page.locator("#view-mods")).to_have_attribute("aria-busy", "false")
    assert dashboard["actions"] == []


def test_direct_tab_cache_invalidates_after_operation(page, dashboard, editing):  # noqa: F811
    dashboard["overview"]["containerInfo"]["startedAt"] = "start-1"
    open_editor(page, dashboard, "en", 390)
    page.evaluate("""() => {
        renderOp({active:{op:'restart',phase:'Restarting',message:'Wait'},history:[]});
        renderOp({active:null,history:[]});
    }""")
    with page.expect_request("**/api/config-draft?*"):
        navigate(page, "mods")
    expect(page.locator("#view-mods")).to_have_attribute("aria-busy", "false")
    assert dashboard["actions"] == []


@pytest.mark.parametrize("boundary", ["restart", "operation"])
def test_profile_loaded_across_server_change_is_not_cached(
    page,
    dashboard,
    editing,  # noqa: F811
    monkeypatch,
    boundary,  # noqa: F811
):
    _, request = saved_verification(editing, monkeypatch)
    editor.verify_running(request)
    dashboard["overview"]["containerInfo"]["startedAt"] = "external-start"
    open_editor(page, dashboard, "ru", 1440)
    snapshot = editor.draft("world.ini", refresh=True)
    pending = []
    page.route("**/api/config-draft?*", lambda route: pending.append(route))
    navigate(page, "overview")
    navigate(page, "settings")
    expect(page.locator("#view-settings")).to_have_attribute("aria-busy", "true")
    if boundary == "restart":
        from test_configeditor import ops

        monkeypatch.setattr(
            ops, "container_state", lambda: {"running": True, "startedAt": "next-start"}
        )
        page.evaluate("""applyOverview({...S.overview,
            containerInfo:{...S.overview.containerInfo,startedAt:'next-start'}})""")
    else:
        page.evaluate("""() => {
            renderOp({active:{op:'restart',phase:'Restarting',message:'Wait'},history:[]});
            renderOp({active:null,history:[]});
        }""")
    pending.pop().fulfill(json=snapshot)
    expect(page.locator("#view-settings")).to_have_attribute("aria-busy", "false")
    with page.expect_request("**/api/config-draft?*"):
        navigate(page, "mods")
    pending.pop().fallback()
    expect(page.locator("#view-mods")).to_have_attribute("aria-busy", "false")
    if boundary == "restart":
        expect(page.locator("#flowLaunch")).to_have_text("Нужна проверка")
    assert dashboard["actions"] == []
