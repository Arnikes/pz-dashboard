# Source editor performance

The INI and SandboxVars overlays previously split and escaped the entire file,
replaced all highlighted rows, and forced layout on every textarea scroll event.
Input events did the same work. A 5,000-line file created 15,000 overlay elements
per editor. This work grew with file length even when only the scroll offset changed.

The native textarea still owns the complete text, caret, selection, clipboard,
undo, and scroll extent. Its decorative overlay now renders the visible lines
plus eight rows of overscan on either side. Lines are split only after a text
change. Input, scroll, and resize notifications share one pending animation-frame
callback per editor. Horizontal movement and movement within the same row window
reuse existing elements. The overlay is clipped to the textarea's viewport and
isolates its layout and painting. Unchanged draft text is not assigned back to
the textarea, preserving its selection when saving.

## Measurements

Measured in local headless Chromium on Windows on 2026-10-07 using the isolated
browser draft fixtures, at 1440 × 1000. Each file alternated comment lines with
`OptionN=` lines containing 125 characters of value text. After an input event
and two animation frames, the probe performed 40 scroll steps of 137 pixels,
dispatching a scroll notification and awaiting the next animation frame before
reading overlay geometry. The same probe ran before and after the changes.

| Editor | Lines | Median scroll step before | After | p95 before | After | Overlay elements before → after |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| INI | 1,000 | 87.3 ms | 16.8 ms | 118.7 ms | 21.4 ms | 3,000 → 100 |
| SandboxVars | 1,000 | 87.8 ms | 16.6 ms | 109.1 ms | 18.2 ms | 3,000 → 100 |
| INI | 5,000 | 1,104.9 ms | 16.7 ms | 1,467.0 ms | 19.6 ms | 15,000 → 100 |
| SandboxVars | 5,000 | 1,214.4 ms | 17.0 ms | 1,805.8 ms | 20.0 ms | 15,000 → 100 |

These are synthetic local interaction measurements, including the animation-frame
wait, rather than handler timings or a guarantee of real-device frame rates.
Inserting a complete large file still incurs native textarea layout and a single
line split; the optimization removes that file-wide work from scrolling and bounds
the highlighted DOM.

## Regression coverage

`tests/browser/test_source_editors.py` checks both editors at desktop and phone
widths with 5,000 lines: bounded overlay size, horizontal DOM reuse, exact vertical
alignment across large jumps, escaping, tab alignment, trailing blank lines,
keyboard editing, resize, forced colors, selection after saving, and refreshing
an editor after editing a field while Sources is hidden. Existing editor tests
cover shared drafts, masking secrets, profile changes, and failed saves.

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/browser/test_source_editors.py tests/browser/test_editors.py
```
