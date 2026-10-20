"""Optional success screenshots without changing browser assertions or failure capture."""

import os

from playwright.sync_api import Locator, Page


def capture_evidence(target: Page | Locator, **options):
    """Keep success PNGs on request; preserve screenshot layout waits otherwise."""
    if os.getenv("PZ_VISUAL_EVIDENCE") == "1":
        return target.screenshot(**options)
    page = target.page if isinstance(target, Locator) else target
    # Playwright screenshots await fonts before capture. Some tests measure the
    # layout immediately afterward, so omitting pixels must keep that barrier.
    page.evaluate("document.fonts.ready")
    if isinstance(target, Locator):
        # Locator screenshots also scroll their target into view and await a
        # stable bounding box. Preserve both effects without encoding an image.
        target.scroll_into_view_if_needed()
    return None
