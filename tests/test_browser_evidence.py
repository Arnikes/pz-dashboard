"""Success screenshot opt-in must not bypass the layout waits used by UI checks."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest
from playwright.sync_api import Locator, Page

spec = importlib.util.spec_from_file_location(
    "browser_evidence", Path(__file__).parent / "browser" / "browser_evidence.py"
)
browser_evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(browser_evidence)


@pytest.mark.parametrize("locator", [False, True])
@pytest.mark.parametrize("setting", [None, "0"])
def test_default_skips_success_pixels_and_keeps_layout_ready(monkeypatch, locator, setting):
    if setting is None:
        monkeypatch.delenv("PZ_VISUAL_EVIDENCE", raising=False)
    else:
        monkeypatch.setenv("PZ_VISUAL_EVIDENCE", setting)
    page = Mock(spec=Page)
    target = Mock(spec=Locator, page=page) if locator else page

    assert browser_evidence.capture_evidence(target, path="unused.png") is None

    target.screenshot.assert_not_called()
    page.evaluate.assert_called_once_with("document.fonts.ready")
    if locator:
        target.scroll_into_view_if_needed.assert_called_once_with()


@pytest.mark.parametrize("locator", [False, True])
def test_opt_in_retains_real_screenshot_arguments_and_result(monkeypatch, locator):
    monkeypatch.setenv("PZ_VISUAL_EVIDENCE", "1")
    page = Mock(spec=Page)
    target = Mock(spec=Locator, page=page) if locator else page
    target.screenshot.return_value = b"evidence pixels"
    options = {"path": "evidence.png", "animations": "disabled"}
    if not locator:
        options["full_page"] = True

    assert browser_evidence.capture_evidence(target, **options) == b"evidence pixels"

    target.screenshot.assert_called_once_with(**options)
    # The real screenshot supplies its own font, scroll and stability waits.
    page.evaluate.assert_not_called()
    if locator:
        target.scroll_into_view_if_needed.assert_not_called()
