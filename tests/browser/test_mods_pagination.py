"""Composition pagination preserves the full profile draft across pages."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from test_editors import editing, editor, env  # noqa: F401

pytestmark = pytest.mark.browser


@pytest.fixture
def composition(page, dashboard, editing, monkeypatch):  # noqa: F811 (imported fixture)
    data, _ = editing
    ids = [str(1000 + i) for i in range(61)]
    selected = [f"mod-{i:03}" for i in range(0, 61, 2)]
    profile = data / "Server/world.ini"
    profile.write_text(
        "PublicName=Pagination server\nWorkshopItems="
        + ";".join(ids)
        + "\nMods="
        + ";".join(selected)
        + "\nMap=Muldraugh, KY\n",
        encoding="utf-8",
    )
    (data / "Server/other.ini").write_bytes(profile.read_bytes())
    for i, item in enumerate(ids):
        folder = data / f"steamapps/workshop/content/108600/{item}/mods/Mod/42"
        folder.mkdir(parents=True)
        (folder / "mod.info").write_text(f"id=mod-{i:03}\nname=Mod {i:03}\n", encoding="utf-8")
    monkeypatch.setattr(
        editor.ops,
        "_ws_titles",
        lambda items: {item: f"Package {1060 - int(item):03}" for item in items},
    )
    editor.workshop.invalidate()
    page.goto(dashboard["url"] + "/#/mods")
    expect(page.locator("#modPackages details")).to_have_count(25)
    return data, ids, selected


def test_composition_navigation_sizes_and_draft_preservation(page, composition):
    _, ids, selected = composition
    rows = page.locator("#modPackages details")
    expect(page.locator("#modCompositionPageSize")).to_have_value("25")
    expect(page.locator("#modCompositionPrev")).to_be_disabled()
    expect(page.locator("#modCompositionRange")).to_have_text("1–25 из 61 · Страница 1")
    page.locator("#modCompositionNext").focus()
    page.keyboard.press("Enter")
    expect(rows.first).to_have_attribute("data-item", ids[25])
    expect(page.locator("#modCompositionNext")).to_be_focused()
    rows.first.locator("summary").click()
    rows.first.locator('[data-modid="mod-025"]').check()
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    expect(page.locator("#modCompositionRange")).to_have_text("26–50 из 61 · Страница 2")
    expect(rows.first.locator('[data-modid="mod-025"]')).to_be_checked()
    metadata = editor.mod_response("world.ini", draft_mode=True)
    assert [item["workshopId"] for item in metadata["workshop"]] == ids
    assert metadata["mods"] == selected + ["mod-025"]
    with page.expect_response("**/api/mods?*refresh=1"):
        page.locator("#modRescan").click()
    expect(page.locator("#modCompositionRange")).to_have_text("26–50 из 61 · Страница 2")
    page.locator("#modCompositionNext").click()
    expect(rows).to_have_count(11)
    expect(page.locator("#modCompositionNext")).to_be_disabled()
    expect(page.locator("#modCompositionRange")).to_have_text("51–61 из 61 · Страница 3")
    page.locator("#modCompositionPrev").click()
    page.locator("#modCompositionPageSize").select_option("50")
    expect(rows).to_have_count(50)
    expect(page.locator("#modCompositionRange")).to_have_text("1–50 из 61 · Страница 1")
    page.locator("#modCompositionPageSize").select_option("100")
    expect(rows).to_have_count(61)
    expect(page.locator("#modCompositionNext")).to_be_disabled()
    page.locator("#modCompositionPageSize").select_option("25")
    expect(rows).to_have_count(25)


def test_composition_filters_sort_and_profile_reset(page, composition):
    _, ids, _ = composition
    rows = page.locator("#modPackages details")
    page.locator("#modCompositionNext").click()
    page.locator("#modQuery").fill("mod-060")
    expect(rows).to_have_count(1)
    expect(rows.first).to_have_attribute("data-item", ids[60])
    expect(page.locator("#modCompositionRange")).to_have_text("1–1 из 1 · Страница 1")
    page.locator("#modQuery").fill("no-match")
    expect(rows).to_have_count(0)
    expect(page.locator("#modCompositionPager")).to_be_hidden()
    page.locator("#modQuery").fill("")
    page.locator("#modCompositionNext").click()
    page.locator("#modFilter").select_option("selected")
    expect(page.locator("#modCompositionRange")).to_have_text("1–25 из 31 · Страница 1")
    page.locator("#modCompositionNext").click()
    expect(rows).to_have_count(6)
    expect(rows.first).to_have_attribute("data-item", ids[50])
    page.locator("#modFilter").select_option("all")
    page.locator("#modCompositionNext").click()
    page.locator("#modSortNew").select_option("title")
    expect(rows.first).to_have_attribute("data-item", ids[60])
    expect(page.locator("#modCompositionRange")).to_have_text("1–25 из 61 · Страница 1")
    page.locator("#modCompositionNext").click()
    page.locator("#configProfile").select_option("other.ini")
    expect(page.locator("#modCompositionRange")).to_have_text("1–25 из 61 · Страница 1")


@pytest.mark.parametrize("count", [0, 1, 25, 26, 40])
def test_composition_refresh_clamps_page_and_handles_boundaries(page, composition, count):
    _, ids, _ = composition
    page.locator("#modCompositionNext").click()
    page.locator("#modCompositionNext").click()
    draft = editor.draft("world.ini")
    editor.patch(
        {
            "file": "world.ini",
            "draftRevision": draft["draftRevision"],
            "mods": {"items": ids[:count]},
        }
    )
    # Reload the same profile to pick up its newer draft revision and metadata.
    page.locator("#configProfile").dispatch_event("change")
    expected_count = count - 25 if count > 25 else count
    expect(page.locator("#modPackages details")).to_have_count(expected_count)
    if count:
        start, number = (26, 2) if count > 25 else (1, 1)
        expect(page.locator("#modCompositionRange")).to_have_text(
            f"{start}–{count} из {count} · Страница {number}"
        )
        expect(page.locator("#modCompositionNext")).to_be_disabled()
    else:
        expect(page.locator("#modCompositionPager")).to_be_hidden()
        expect(page.locator("#modPackages")).to_contain_text("Пакеты не найдены")


@pytest.mark.parametrize("width,language", [(1440, "ru"), (390, "en"), (320, "ru")])
def test_composition_pager_layout_and_language(page, dashboard, composition, width, language):
    page.set_viewport_size({"width": width, "height": 900})
    if language == "en":
        page.evaluate("localStorage.setItem('pz-language','en')")
        page.reload()
        expect(page.locator("#modPackages details")).to_have_count(25)
    pager = page.locator("#modCompositionPager")
    pager.scroll_into_view_if_needed()
    expect(pager).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    for control in pager.locator("button, select").all():
        bounds = control.bounding_box()
        assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
    if language == "en":
        expect(pager).to_have_attribute("aria-label", "Mod composition pages")
        expect(page.locator("#modCompositionNext")).to_have_text("Next")
    out = Path(__file__).resolve().parents[2] / ".tmp-mods-pagination"
    out.mkdir(exist_ok=True)
    page.screenshot(path=str(out / f"composition-{width}-{language}.png"))
    assert dashboard["actions"] == []
