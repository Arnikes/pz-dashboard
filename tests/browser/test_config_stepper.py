"""Configuration progress reflects verified files and resets for new draft edits."""

import pytest
from playwright.sync_api import expect
from test_editors import editing, env, editor, saved_verification  # noqa: F401

pytestmark = pytest.mark.browser


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
        page.screenshot(path=str(data.parent / f"stepper-{language}-{width}-{route}-verified.png"))

    page.locator('.nav [data-route="settings"]').click()
    expect(page.locator("#view-settings")).to_be_visible()
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
    page.screenshot(path=str(data.parent / f"stepper-{language}-{width}-mods-draft.png"))
    page.locator('[data-flow="files"]').click()
    expect(page.get_by_role("alertdialog")).to_contain_text("New stepper draft")
    page.locator("#modalCancel").click()
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
