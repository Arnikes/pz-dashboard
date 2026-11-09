"""Configuration progress reflects verified files and resets for new draft edits."""

import pytest
from playwright.sync_api import expect
from test_editors import editing, env, editor, saved_verification  # noqa: F401

from browser_evidence import capture_evidence

pytestmark = pytest.mark.browser


def assert_visible_stepper_geometry(page, width):
    metrics = page.locator("#configFlow").evaluate("""flow => {
        const box = el => {
            const r = el.getBoundingClientRect();
            return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height};
        };
        return {flow:box(flow), steps:[...flow.children].map(step => ({
            button:box(step.querySelector('button')), marker:box(step.querySelector('.route-marker')),
            copy:box(step.querySelector('.route-copy')),
            connector:step.querySelector('.route-connector') ? box(step.querySelector('.route-connector')) : null,
        }))};
    }""")
    flow, steps = metrics["flow"], metrics["steps"]
    for step in steps:
        button, copy = step["button"], step["copy"]
        assert round(button["height"], 2) >= 44 and round(button["width"], 2) >= 44
        assert button["left"] - 1 <= copy["left"] <= copy["right"] <= button["right"] + 1
    if width <= 540:
        for index, step in enumerate(steps):
            assert abs(step["marker"]["left"] - flow["left"]) <= 1
            if step["connector"]:
                assert abs(step["connector"]["width"] - 1) <= 0.01
                assert step["marker"]["bottom"] < step["connector"]["top"]
                assert step["connector"]["bottom"] < steps[index + 1]["marker"]["top"]
        return
    first, middle, last = [step["marker"] for step in steps]
    assert abs(first["left"] - flow["left"]) <= 1
    assert abs(last["right"] - flow["right"]) <= 1
    assert abs((middle["left"] + middle["right"]) / 2 - (flow["left"] + flow["right"]) / 2) <= 1
    widths = [step["button"]["width"] for step in steps]
    assert max(widths) - min(widths) <= 1
    for index, step in enumerate(steps[:-1]):
        connector, marker, following = step["connector"], step["marker"], steps[index + 1]["marker"]
        assert abs(connector["left"] - marker["right"] - 12) <= 1
        assert abs(following["left"] - connector["right"] - 12) <= 1
        assert connector["width"] > 0 and abs(connector["height"] - 1) <= 0.01
        assert abs(connector["top"] - (marker["top"] + marker["bottom"]) / 2) <= 1
        assert abs(marker["top"] - following["top"]) <= 1


@pytest.mark.parametrize("width", [390, 541, 1440, 2048, 2560])
@pytest.mark.parametrize("language", ["ru", "en"])
def test_visible_stage_markers_and_connectors_fill_the_editor_lane(
    page,
    dashboard,
    editing,  # noqa: F811
    width,
    language,
):
    data, _ = editing
    page.set_viewport_size({"width": width, "height": 900})
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.goto(dashboard["url"] + "/#/settings")
    for route in ("settings", "mods"):
        page.locator(f'.nav [data-route="{route}"]').click()
        expect(page.locator(f"#view-{route}")).to_be_visible()
        expect(page.locator("#configProfile")).to_be_enabled()
        page.evaluate("document.fonts.ready")
        assert_visible_stepper_geometry(page, width)
        first = page.locator('#configFlow [data-flow="draft"]')
        first.focus()
        page.keyboard.press("Tab")
        expect(page.locator('#configFlow [data-flow="files"]')).to_be_focused()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        capture_evidence(
            page.locator("#configFlow"),
            path=str(data.parent / f"stepper-anchors-{language}-{width}-{route}.png"),
        )
        if width == 541:
            # Stress the existing localized status text without replacing product copy.
            page.locator("#flowLaunch").evaluate("el => {el.textContent += ' ' + el.textContent;}")
            assert_visible_stepper_geometry(page, width)
    assert dashboard["actions"] == []


@pytest.mark.parametrize("width", [320, 390, 768, 1440])
@pytest.mark.parametrize("language", ["ru", "en"])
def test_stepper_keeps_statuses_and_draft_actions(
    page,
    dashboard,
    editing,  # noqa: F811
    monkeypatch,
    width,
    language,
):
    data, request = saved_verification(editing, monkeypatch)
    editor.verify_running(request)
    original = (data / "Server/world.ini").read_bytes()
    page.set_viewport_size({"width": width, "height": 900})
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#flowLaunch")).to_have_text(
        "Подтверждён" if language == "ru" else "Confirmed"
    )
    for route in ["settings", "mods"]:
        page.locator(f'.nav [data-route="{route}"]').click()
        expect(page.locator(f"#view-{route}")).to_be_visible()
        expect(page.locator('#configFlow [data-state="ok"]')).to_have_count(3)
        expect(page.locator('#configFlow [aria-current="step"]')).to_have_attribute(
            "data-flow", "launch"
        )
        expect(page.locator("#configFlow .flow-check:visible")).to_have_count(3)
        expect(page.locator("#configFlow .flow-number:visible")).to_have_count(0)
        for step in page.locator("#configFlow button").all():
            assert step.bounding_box()["height"] >= 44
            expect(step.locator(".flow-copy > span")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        capture_evidence(
            page, path=str(data.parent / f"stepper-{language}-{width}-{route}-verified.png")
        )

    page.locator('.nav [data-route="settings"]').click()
    expect(page.locator("#view-settings")).to_be_visible()
    # Returning to an editor route reloads the profile after showing the view.
    expect(page.locator("#configProfile")).to_be_enabled()
    field = page.locator('[data-key="PublicName"]')
    field.fill("New stepper draft")
    field.press("Tab")
    expect(page.locator("#draftSaved")).to_have_text(
        "Черновик сохранён" if language == "ru" else "Draft saved"
    )
    expect(page.locator('#configFlow [data-state="ok"]')).to_have_count(0)
    expect(page.locator('#configFlow [aria-current="step"]')).to_have_attribute(
        "data-flow", "draft"
    )
    expect(page.locator("#configFlow .flow-number:visible")).to_have_count(3)
    page.locator('.nav [data-route="mods"]').click()
    expect(page.locator("#view-mods")).to_be_visible()
    expect(page.locator('#configFlow [data-state="ok"]')).to_have_count(0)
    capture_evidence(page, path=str(data.parent / f"stepper-{language}-{width}-mods-draft.png"))
    page.locator('[data-flow="files"]').click()
    expect(page.get_by_role("alertdialog")).to_contain_text("New stepper draft")
    page.locator("#modalOk").click()
    page.locator('[data-flow="launch"]').click()
    expect(page.locator("#configApply")).to_be_focused()
    assert (data / "Server/world.ini").read_bytes() == original
    assert dashboard["actions"] == []


def test_recorded_files_do_not_claim_verified_startup(page, dashboard, editing):  # noqa: F811
    current = editor.draft("world.ini")
    editor.save_json(
        editor.state_dir("world.ini") / "state.json",
        {"status": "saved", "savedRevision": current["currentRevision"]},
    )
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#flowFiles")).to_have_text("Записаны")
    expect(page.locator("#flowLaunch")).to_have_text("Нужна проверка")
    expect(page.locator('[data-flow="draft"]')).to_have_attribute("data-state", "ok")
    expect(page.locator('[data-flow="files"]')).to_have_attribute("data-state", "ok")
    expect(page.locator('[data-flow="launch"]')).to_have_attribute("data-state", "normal")
    expect(page.locator('[data-flow="launch"]')).to_have_attribute("aria-current", "step")
    expect(page.locator('[data-flow="launch"] .flow-number')).to_be_visible()
    assert dashboard["actions"] == []


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("draft_state", ["draft", "saved", "verified"])
def test_unrelated_operations_preserve_the_configuration_lifecycle(
    page,
    dashboard,
    editing,  # noqa: F811
    monkeypatch,
    language,
    draft_state,
):
    if draft_state == "verified":
        _, request = saved_verification(editing, monkeypatch)
        editor.verify_running(request)
    elif draft_state == "saved":
        current = editor.draft("world.ini")
        editor.save_json(
            editor.state_dir("world.ini") / "state.json",
            {"status": "saved", "savedRevision": current["currentRevision"]},
        )
    page.set_viewport_size({"width": 320, "height": 900})
    page.add_init_script(f"localStorage.setItem('pz-language','{language}')")
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#configProfile")).to_be_enabled()
    page.evaluate("liveSource?.close();liveSource=null;clearTimeout(sseStartupTimer)")
    if draft_state == "draft":
        field = page.locator('[data-key="PublicName"]')
        field.fill("Keep this draft during backup")
        field.press("Tab")
        expect(page.locator("#draftSaved")).to_have_text(
            "Черновик сохранён" if language == "ru" else "Draft saved"
        )
    launch_status = page.locator("#flowLaunch").inner_text()
    stage_state = page.locator("#configFlow button").evaluate_all(
        "nodes=>nodes.map(node=>[node.dataset.flow,node.dataset.state,node.getAttribute('aria-current')])"
    )
    for operation in ["backup", "check-update"]:
        page.evaluate("op=>renderOp({active:{op,phase:'Working'},history:[]})", operation)
        expect(page.locator("#configProfile")).to_be_disabled()
        expect(page.locator("#opbar")).to_be_visible()
        expect(page.locator("#flowLaunch")).to_have_text(launch_status)
        assert (
            page.locator("#configFlow button").evaluate_all(
                "nodes=>nodes.map(node=>[node.dataset.flow,node.dataset.state,node.getAttribute('aria-current')])"
            )
            == stage_state
        )
        page.evaluate("renderOp({active:null,history:[]})")
        expect(page.locator("#configProfile")).to_be_enabled()
    if draft_state == "draft":
        expect(page.locator('[data-key="PublicName"]')).to_have_value(
            "Keep this draft during backup"
        )
    assert dashboard["actions"] == []


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("width", [320, 541, 1440])
def test_failed_startup_uses_a_warning_marker_in_high_contrast_mode(
    page,
    dashboard,
    editing,  # noqa: F811
    language,
    width,
):
    current = editor.draft("world.ini")
    editor.save_json(
        editor.state_dir("world.ini") / "state.json",
        {
            "status": "error",
            "savedRevision": current["currentRevision"],
            "error": "PZ readiness timed out",
        },
    )
    page.set_viewport_size({"width": width, "height": 900})
    page.add_init_script(f"localStorage.setItem('pz-language','{language}')")
    page.emulate_media(forced_colors="active", reduced_motion="reduce")
    page.goto(dashboard["url"] + "/#/settings")
    launch = page.locator('[data-flow="launch"]')
    expect(launch).to_have_attribute("data-state", "bad")
    expect(page.locator("#flowLaunch")).to_have_text(
        "Проверьте ошибку" if language == "ru" else "Check error"
    )
    expect(launch.locator(".flow-error")).to_be_visible()
    expect(launch.locator(".flow-check")).to_be_hidden()
    assert_visible_stepper_geometry(page, width)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
