"""Copy feedback follows the actual clipboard result and the current field value."""

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


def open_fields(page, dashboard):
    dashboard["overview"].update(
        container="pz-server",
        image="server:latest",
        update={"local": "sha256:" + "a" * 64, "remote": "sha256:" + "b" * 64},
    )
    page.goto(dashboard["url"])
    expect(page.locator("#btnStop")).to_be_enabled()
    page.evaluate("""() => {
        renderOverview({...S.overview,container:'pz-server',image:'server:latest',
            update:{local:'sha256:'+'a'.repeat(64),remote:'sha256:'+'b'.repeat(64)}});
        document.getElementById('serverDetails').open=true;
    }""")


def test_copy_feedback_restarts_and_survives_unchanged_refresh(page, dashboard):
    open_fields(page, dashboard)
    page.clock.install()
    page.clock.pause_at(page.evaluate("Date.now()"))
    field = page.locator("#mImage")
    field.click()
    expect(field).to_have_attribute("data-copied", "true")
    expect(field).to_have_attribute("aria-label", "Скопировано")
    page.clock.run_for(1500)
    page.evaluate("renderOverview(S.overview)")
    expect(field).to_have_attribute("data-copied", "true")
    field.click()
    expect(field).to_have_attribute("data-copied", "true")
    page.clock.run_for(1500)
    expect(field).to_have_attribute("data-copied", "true")
    page.clock.run_for(501)
    expect(field).not_to_have_attribute("data-copied", "true")
    expect(field).to_have_attribute("aria-label", "Скопировать имя образа")
    expect(field.locator(".copy-icon-default")).to_be_visible()
    expect(field.locator(".copy-icon-check")).to_be_hidden()
    expect(field).to_have_text("server:latest")


def test_copy_fields_have_independent_feedback_and_reset_when_value_changes(page, dashboard):
    open_fields(page, dashboard)
    page.locator("#mImage").click()
    expect(page.locator("#mImage")).to_have_attribute("data-copied", "true")
    expect(page.locator("#mContainer .copy-icon-default")).to_be_visible()
    page.locator("#mContainer").click()
    expect(page.locator("#mContainer")).to_have_attribute("data-copied", "true")
    page.evaluate("renderOverview({...S.overview,image:'server:new',container:null})")
    expect(page.locator("#mImage")).not_to_have_attribute("data-copied", "true")
    expect(page.locator("#mImage")).to_have_text("server:new")
    expect(page.locator("#mContainer")).to_be_disabled()
    expect(page.locator("#mContainer .copy-icon")).to_be_hidden()


@pytest.mark.parametrize("result", ["success", "failure", "changed"])
def test_copy_icon_waits_for_result_and_never_confirms_a_changed_value(page, dashboard, result):
    open_fields(page, dashboard)
    page.evaluate("""() => {
        navigator.clipboard.writeText = () => new Promise((resolve,reject) => {
            window.finishCopy=resolve; window.failCopy=reject;
        });
        document.execCommand=()=>false;
    }""")
    field = page.locator("#mImage")
    field.click()
    expect(field.locator(".copy-icon-default")).to_be_visible()
    expect(field.locator(".copy-icon-check")).to_be_hidden()
    if result == "changed":
        page.evaluate("renderOverview({...S.overview,image:'server:new'})")
    page.evaluate("failCopy(new Error('Denied'))" if result == "failure" else "finishCopy()")
    if result == "success":
        expect(field.locator(".copy-icon-check")).to_be_visible()
    else:
        expect(field.locator(".copy-icon-check")).to_be_hidden()
    if result == "failure":
        expect(page.locator('#toasts .toast[data-kind="error"]')).to_have_text(
            "Не удалось скопировать"
        )
        expect(field).to_be_focused()
    expect(page.locator('#toasts .toast[data-kind="ok"]')).to_have_count(0)


@pytest.mark.parametrize("width", [320, 1440])
def test_long_copy_values_keep_the_icon_visible_and_copy_exact_content(page, dashboard, width):
    page.set_viewport_size({"width": width, "height": 900})
    open_fields(page, dashboard)
    value = 'very_long_value_"<&>_' + "x" * 180
    page.evaluate(
        """value => {
        document.getElementById('mImage').parentElement.insertAdjacentHTML(
            'beforeend',copyValue(value,{className:'mono'}));
        window.copiedValue=null;
        navigator.clipboard.writeText=async value=>{window.copiedValue=value};
    }""",
        value,
    )
    field = page.locator("[data-copy]").filter(has_text=value).first
    expect(field.locator(".copy-icon-default")).to_be_visible()
    box = field.bounding_box()
    icon = field.locator(".copy-icon").bounding_box()
    assert box["x"] <= icon["x"] and icon["x"] + icon["width"] <= box["x"] + box["width"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    field.locator(".copy-label").click()
    expect(field.locator(".copy-icon-check")).to_be_visible()
    assert page.evaluate("copiedValue") == value


@pytest.mark.parametrize("surface", ["updates", "mods"])
def test_dynamic_copy_field_feedback_survives_unchanged_data(page, dashboard, surface):
    open_fields(page, dashboard)
    page.evaluate(
        """surface => {
        navigator.clipboard.writeText=async()=>{};
        const host=document.getElementById('sec-status');
        if (surface==='updates') {
            host.append(document.getElementById('modsNeedList'));
            window.refreshCopyField=()=>renderOverview({...S.overview,modsCheck:{
                state:'needs-update',items:[{workshopId:'1234567890',raw:'Обновление'}]}});
        } else {
            window.ConfigEditor=null;
            host.append(document.getElementById('modsBody'));
            window.refreshCopyField=()=>_renderMods({ok:true,mods:['ModID'],workshop:[]});
        }
        refreshCopyField();
    }""",
        surface,
    )
    selector = {"updates": "#modsNeedList", "mods": "#modsBody"}[surface]
    field = page.locator(f"{selector} .copy-value").first
    field.click()
    expect(field).to_have_attribute("data-copied", "true")
    page.evaluate("refreshCopyField()")
    expect(field).to_have_attribute("data-copied", "true")
    expect(field).to_be_focused()
    if surface == "mods":
        page.evaluate("_renderMods({ok:true,mods:[],workshop:[]});refreshCopyField()")
        expect(field.locator(".copy-icon-default")).to_be_visible()
