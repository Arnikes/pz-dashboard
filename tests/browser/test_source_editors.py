"""Source overlays stay bounded and follow the native textarea viewport."""

import pytest
from playwright.sync_api import expect

from test_editors import editing, env, editor  # noqa: F401


@pytest.mark.parametrize("width", [390, 1440])
@pytest.mark.parametrize("kind", ["ini", "sandbox"])
def test_source_highlight_is_windowed_and_tracks_native_scroll(
    page,
    dashboard,
    editing,  # noqa: F811 (imported fixture)
    width,
    kind,
):
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    page.locator('[data-tab="sources"]').click()
    source = page.locator(f"#{kind}Source")
    output = page.locator(f"#{kind}Highlight")
    lines = ["\tOption=" + "value" * 400, '# <img src=x onerror="throw 1">']
    lines.extend(f"Option{i}=value" for i in range(4997))
    lines.append("")
    text = "\n".join(lines)
    source.evaluate(
        """(el, text) => {
        el.value = text;
        el.dispatchEvent(new Event('input', {bubbles:true}));
        el.scrollTop = el.scrollLeft = 0;
    }""",
        text,
    )
    expect(output.locator(".line-number").first).to_have_text("1")
    expect(output.locator(".syntax-comment")).to_have_text(lines[1])
    assert output.locator("img").count() == 0
    assert 1 < output.locator(".source-line").count() < 160
    page.evaluate("document.fonts.ready")
    # A tab begins at the source text origin, independently of the number gutter.
    tab_width = output.evaluate("""el => {
        const code = el.querySelector('.source-line > span:last-child');
        const range = document.createRange();
        range.setStart(code.firstChild, 0); range.setEnd(code.firstChild, 1);
        const canvas = document.createElement('canvas').getContext('2d');
        canvas.font = getComputedStyle(code).font;
        return {actual:range.getBoundingClientRect().width,
            expected:canvas.measureText(' ').width * 4};
    }""")
    assert tab_width["actual"] == pytest.approx(tab_width["expected"], abs=1)

    # Horizontal scroll moves existing rows instead of replacing their DOM.
    output.evaluate("""el => {
        window.sourceRow = el.querySelector('.source-line');
        window.sourceMutations = [];
        window.sourceObserver = new MutationObserver(r => sourceMutations.push(...r));
        sourceObserver.observe(el, {childList:true, subtree:true});
    }""")
    source.evaluate("el => { el.scrollLeft = 200; el.dispatchEvent(new Event('scroll')); }")
    page.wait_for_function(
        """kind => document.querySelector(`#${kind}Highlight .source-window`)
            .style.transform.startsWith('translate(-200px')""",
        arg=kind,
    )
    assert output.evaluate("el => el.querySelector('.source-line') === sourceRow")
    assert page.evaluate("sourceMutations.length") == 0
    # Small vertical scrolls stay within the buffered rows too.
    source.evaluate("el => { el.scrollTop = 220; el.dispatchEvent(new Event('scroll')); }")
    page.wait_for_function(
        """kind => document.querySelector(`#${kind}Highlight .source-window`)
            .style.transform === 'translate(-200px, -220px)'""",
        arg=kind,
    )
    assert output.evaluate("el => el.querySelector('.source-line') === sourceRow")
    assert page.evaluate("sourceMutations.length") == 0
    page.evaluate("sourceObserver.disconnect()")

    for top in [22012.5, 66000, 10**9, 0]:
        source.evaluate(
            "(el, top) => { el.scrollTop = top; el.dispatchEvent(new Event('scroll')); }",
            top,
        )
        page.wait_for_function(
            """kind => {
                const input = document.getElementById(kind + 'Source');
                const output = document.getElementById(kind + 'Highlight');
                const first = output.querySelector('.source-line');
                const line = Number(first.firstChild.textContent) - 1;
                const expected = input.getBoundingClientRect().top + 12 + line * 22 - input.scrollTop;
                return Math.abs(first.getBoundingClientRect().top - expected) < 1;
            }""",
            arg=kind,
        )
        assert output.locator(".source-line").count() < 160
        assert source.input_value() == text
    expect(output.locator(".line-number").first).to_have_text("1")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(editing[0].parent / f"source-{kind}-{width}.png"))


def test_source_resize_trailing_line_and_forced_colors(page, dashboard, editing):  # noqa: F811
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    page.locator('[data-tab="sources"]').click()
    source = page.locator("#iniSource")
    text = "PublicName=Test\n" + "# Comment\n" * 200
    source.fill(text)
    source.press("Control+End")
    expect(page.locator("#iniHighlight .line-number").last).to_have_text("202")
    source.press("End")
    page.keyboard.type("# Final line")
    expect(page.locator("#iniHighlight .source-line").last).to_have_text("202# Final line")
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_function("""() => {
        const input = document.getElementById('iniSource');
        const output = document.getElementById('iniHighlight');
        return output.clientHeight === input.clientHeight && output.clientWidth === input.clientWidth;
    }""")
    page.emulate_media(forced_colors="active")
    expect(page.locator("#iniHighlight")).to_be_hidden()
    assert source.evaluate("el => getComputedStyle(el).color") != "rgba(0, 0, 0, 0)"
    expect(source).to_have_value(text + "# Final line")


def test_source_save_preserves_selection_and_updates_after_hidden_field_edit(
    page,
    dashboard,
    editing,  # noqa: F811 (imported fixture)
):
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    page.locator('[data-tab="sources"]').click()
    ini = page.locator("#iniSource")
    sandbox = page.locator("#sandboxSource")
    ini_text = ini.input_value().replace("PublicName=Сервер", "PublicName=Source edit")
    sandbox_text = sandbox.input_value() + "\n-- Source comment\n"
    ini.fill(ini_text)
    expect(page.locator("#draftBar")).to_be_visible()
    expect(page.locator("#draftSaved")).to_have_text("Исходник ещё не сохранён в черновик")
    sandbox.fill(sandbox_text)
    sandbox.evaluate("el => { el.setSelectionRange(3, 10); }")
    page.locator("#sourceSave").click()
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    expect(sandbox).to_have_value(sandbox_text)
    assert sandbox.evaluate("el => [el.selectionStart, el.selectionEnd]") == [3, 10]
    page.locator('[data-tab="server"]').click()
    field = page.locator('[data-key="PublicName"]')
    field.fill("Field edit")
    field.press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    page.locator('[data-tab="sources"]').click()
    expect(page.locator("#iniHighlight")).to_contain_text("PublicName=Field edit")
    assert "topsecret" not in ini.input_value()
    assert dashboard["actions"] == []


def test_explicit_file_save_overwrites_external_changes_and_reloads_once(
    page, dashboard, editing  # noqa: F811 (imported fixture)
):
    data, _ = editing
    reads = []
    page.on(
        "request",
        lambda request: (
            reads.append(request.url)
            if "/api/config-draft?" in request.url and request.method == "GET"
            else None
        ),
    )

    def save(route):
        body = route.request.post_data_json
        assert body["overwrite"] is True
        editor.run(body)
        route.fulfill(json={"ok": True})

    page.route("**/api/action", save)
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    page.evaluate("liveSource?.close();liveSource=null;clearTimeout(sseStartupTimer)")
    field = page.locator('[data-key="PublicName"]')
    field.fill("Page wins")
    field.press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    path = data / "Server/world.ini"
    path.write_bytes(path.read_bytes().replace(b"Unknown=preserve", b"Unknown=external"))
    page.evaluate("renderOverview({...S.overview,containerInfo:{running:false,status:'exited'}})")
    page.locator("#draftMore").click()
    page.locator("#configSave").click()
    expect(page.get_by_role("alertdialog")).to_contain_text("Unknown=external")
    before = len(reads)
    page.locator("#modalOk").click()
    expect(page.locator("#configStatus")).to_have_text("Сохранено, требуется запуск")
    expect(page.locator("#configProfile")).to_be_enabled()
    assert "Page wins" in path.read_text(encoding="utf-8")
    assert "Unknown=preserve" in path.read_text(encoding="utf-8")
    assert len(reads) == before + 1
    page.evaluate("() => { for (let i=0; i<30; i++) ConfigEditor.operationChanged(); }")
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(resolve))")
    assert len(reads) == before + 1
