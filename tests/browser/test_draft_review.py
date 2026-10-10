"""Draft actions stay simple and review exposes one exit and one discard action."""

import pytest
from playwright.sync_api import expect
from test_editors import editing, env  # noqa: F401

pytestmark = [pytest.mark.browser, pytest.mark.usefixtures("editing")]


@pytest.fixture
def browser_context_args(browser_context_args, request):
    mobile = request.node.callspec.params["width"] <= 740
    return {**browser_context_args, "is_mobile": mobile, "has_touch": mobile}


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize(
    "width,height",
    [(320, 844), (390, 844), (768, 844), (1440, 844), (1920, 844), (390, 560), (1440, 560)],
)
def test_simple_draft_bar_and_review_actions(page, dashboard, width, height, language):
    page.set_viewport_size({"width": width, "height": height})
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.goto(dashboard["url"] + "/#/settings")
    field = page.locator('[data-key="PublicName"]')
    expect(field).to_be_enabled()
    field.fill("Review draft")
    field.press("Tab")
    expect(page.locator("#draftSaved")).to_have_text(
        "Черновик сохранён" if language == "ru" else "Draft saved"
    )
    expect(page.locator("#draftBar button:visible")).to_have_count(2)
    expect(page.locator("#draftMore, #draftExtra, #configSave, #editorLogs")).to_have_count(0)
    for name in ["configDiff", "configApply"]:
        action = page.locator(f"#{name}")
        action.scroll_into_view_if_needed()
        bounds = action.bounding_box()
        assert 0 <= bounds["x"] < bounds["x"] + bounds["width"] <= width
        assert 0 <= bounds["y"] < bounds["y"] + bounds["height"] <= height
    page.screenshot(path=f".tmp-pytest/review-evidence/bar-{language}-{width}-{height}.png")
    review = page.locator("#configDiff")
    if width <= 740:
        review.tap()
    else:
        review.click()
    close = "Закрыть" if language == "ru" else "Close"
    discard = "Сбросить изменения…" if language == "ru" else "Discard changes…"
    cancel = "Отмена" if language == "ru" else "Cancel"
    expect(page.get_by_role("alertdialog").get_by_role("button")).to_have_count(2)
    expect(page.locator("#modalOk")).to_have_text(close)
    expect(page.locator("#modalCancel")).to_have_text(discard)
    expect(page.locator("#modalCancel")).to_have_class("btn danger")
    expect(page.get_by_role("button", name=cancel, exact=True)).to_have_count(0)
    for name in ["modalCancel", "modalOk"]:
        bounds = page.locator(f"#{name}").bounding_box()
        assert 0 <= bounds["x"] < bounds["x"] + bounds["width"] <= width
        assert 0 <= bounds["y"] < bounds["y"] + bounds["height"] <= height
        if width <= 740:
            assert bounds["height"] >= 44
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=f".tmp-pytest/review-evidence/review-{language}-{width}-{height}.png")
    page.locator("#modalOk").click()
    expect(review).to_be_focused()
    expect(field).to_have_value("Review draft")
    assert dashboard["actions"] == []
