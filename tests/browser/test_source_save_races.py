"""Saving an editable source buffer must preserve edits made during requests."""

import pytest
from playwright.sync_api import expect

from test_editors import editing, env, editor  # noqa: F401


@pytest.mark.parametrize("kind", ["ini", "sandbox"])
@pytest.mark.parametrize("phase", ["draft", "metadata"])
def test_source_edits_during_save_are_persisted_without_replacing_buffer(
    page,
    dashboard,
    editing,  # noqa: F811 (imported fixture)
    kind,
    phase,
):
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#configProfile")).to_be_enabled()
    page.locator('[data-tab="sources"]').click()
    source = page.locator(f"#{kind}Source")
    original = source.input_value()
    comment = "#" if kind == "ini" else "--"
    first = original + f"\n{comment} First saved edit\n"
    latest = first + f"{comment} Edit while saving\n"
    source.fill(first)

    requests = []
    page.route("**/api/config-draft", lambda route: requests.append(route))
    metadata = []
    if phase == "metadata":
        page.route("**/api/mods?*", lambda route: metadata.append(route))
    with page.expect_request("**/api/config-draft"):
        page.locator("#sourceSave").click()
    if phase == "metadata":
        with page.expect_request("**/api/mods?*"):
            requests.pop().fallback()
    expect(source).to_be_editable()
    source.fill(latest)
    # Repeated clicks join the current save instead of queuing an old snapshot.
    page.locator("#sourceSave").click()
    if phase == "metadata":
        page.unroute("**/api/mods?*")
    with page.expect_request("**/api/config-draft"):
        (requests if phase == "draft" else metadata).pop().fallback()
    expect(source).to_have_value(latest)
    assert requests[-1].request.post_data_json["texts"][kind] == latest
    requests.pop().fallback()

    expect(page.locator("#view-settings")).to_have_attribute("aria-busy", "false")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    expect(source).to_have_value(latest)
    assert "Edit while saving" in editor.draft("world.ini")["texts"][kind]
    assert dashboard["actions"] == []
