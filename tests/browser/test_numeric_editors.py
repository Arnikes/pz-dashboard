"""Empty numeric input is an unfinished edit, not an implicit zero."""

import pytest
from playwright.sync_api import expect

from test_editors import editing, env, editor, INI  # noqa: F401


@pytest.mark.parametrize("language", ["en", "ru"])
def test_clearing_numeric_setting_requires_value_before_saving(
    page,
    dashboard,
    editing,  # noqa: F811 (imported fixture)
    language,
):
    data, _ = editing
    original = INI + "SaveWorldEveryMinutes=15\r\n"
    (data / "Server/world.ini").write_bytes(original.encode())
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.goto(dashboard["url"] + "/#/settings")
    field = page.locator('[data-key="SaveWorldEveryMinutes"]')
    expect(field).to_be_enabled()
    expect(field).to_have_value("15")
    field.fill("")
    assert field.evaluate("el => el.validity.valueMissing")
    field.press("Tab")
    expect(field).to_have_value("")
    page.locator("#configDiff").click()
    expect(page.locator("#configError")).to_contain_text("SaveWorldEveryMinutes")
    expect(page.get_by_role("alertdialog")).to_be_hidden()
    assert "SaveWorldEveryMinutes=15" in editor.draft("world.ini")["texts"]["ini"]

    # Zero remains a valid, explicit administrator choice.
    field.fill("0")
    field.press("Tab")
    expect(page.locator("#draftSaved")).to_have_text(
        "Draft saved" if language == "en" else "Черновик сохранён"
    )
    expect(page.locator("#configError")).to_be_hidden()
    assert "SaveWorldEveryMinutes=0" in editor.draft("world.ini")["texts"]["ini"]
    assert (data / "Server/world.ini").read_bytes() == original.encode()
    assert dashboard["actions"] == []
