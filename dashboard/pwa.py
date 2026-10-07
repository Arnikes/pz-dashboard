"""Public PWA shell. Private HTML and API responses never enter its cache."""

import hashlib
import json
import re
from pathlib import Path


def service_worker(static_dir):
    root = Path(static_dir)
    # Include every shipped UI file in the revision, including private HTML.
    # Only the explicit public shell below is eligible for precaching.
    revision = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            revision.update(path.relative_to(root).as_posix().encode())
            revision.update(path.read_bytes())
    assets = [
        "/static/offline.html",
        "/static/pwa.css",
        "/static/pwa.js",
        "/static/alerts.js",
        "/static/i18n.js",
        "/static/catalogs.js",
        "/static/localize.js",
        "/static/favicon.svg",
        "/static/fonts/fonts.css",
    ]
    assets.extend(
        "/static/" + p.relative_to(root).as_posix() for p in sorted((root / "icons").glob("*.png"))
    )
    font_css = root / "fonts/fonts.css"
    referenced_fonts = (
        set(
            re.findall(
                r"url\(\s*['\"]?([^)'\"\s]+\.woff2)['\"]?\s*\)",
                font_css.read_text(encoding="utf-8"),
            )
        )
        if font_css.exists()
        else set()
    )
    assets.extend(
        "/static/" + p.relative_to(root).as_posix()
        for p in sorted((root / "fonts").glob("*.woff2"))
        if p.name in referenced_fonts
    )
    template = (root / "sw.js").read_text(encoding="utf-8")
    return (
        template.replace("__PZ_REVISION__", revision.hexdigest()[:20])
        .replace("__PZ_ASSETS__", json.dumps(assets))
        .encode("utf-8")
    )
