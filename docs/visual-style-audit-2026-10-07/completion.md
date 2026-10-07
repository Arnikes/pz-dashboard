# Visual-style audit fixes · 7 October 2026

All eight findings from [the baseline audit](audit.html) are addressed in the
accepted four-stage order. The warm palette, flat workspaces, local fonts and
existing confirmation flows are retained.

| Stage | Findings | Change | Commit |
| --- | --- | --- | --- |
| 1. Color semantics | VS-01 | Ban and archive deletion have a quiet danger outline before hover and visible keyboard focus. The ordinary RCON prompt is muted. | `dae3b9f` |
| 2. Shared controls and typography | VS-02, VS-03 | Map input uses the shared 44px field. Text-bearing icon buttons use Golos Text; technical IDs and map names use JetBrains Mono. CSS roles centralize fonts and the control radius. | `ebaecdb` |
| 3. Navigation and layout | VS-04, VS-05, VS-06 | More uses the same icon/label scale as its mobile neighbors. Load-order search is flat and sticky. Desktop console titles, tools and output share compact aligned rows; log feedback and additional actions sit below output. | `4ec27b4` |
| 4. Dialogs and offline shell | VS-07, VS-08 | Modal and command-search windows share 18px/600 headings and a 16px radius; diffs use deep backgrounds and an 8px radius. The standalone offline header groups brand and globe/language control, with 24px before the title. | Commit containing this report |

## Verification

- Final isolated browser verification: **30 passed**, covering all eight findings.
- RU/EN controls and dialogs checked at 320, 390 and 1440px; console layout also
  measured at the 1181px breakpoint and at 2048px.
- Keyboard focus, Enter/Escape, navigation state, filtering/reset, map draft
  persistence and unchanged server configuration verified.
- Offline checks use the real service worker with the network disabled. Both
  languages remain available from cache; the shell needs no `app.js`.
- No server-management actions executed by the audit fixtures.
- Full project check (`scripts/check.py`): **943 passed, 3 skipped**. Dependency
  consistency, Ruff lint/format, translation catalog checks and JavaScript
  syntax checks all passed. The full run took 27 minutes; most time was spent
  in the browser scenarios.

These checks use Chromium viewport emulation. Physical devices and other browser
engines were outside this audit. Full-page screenshots may place fixed navigation
inside the tall image because it is anchored to the captured viewport.

## Evidence and replay

Baseline screenshots and measurements in `evidence/` remain unchanged and refer
to commit `f707b183157fd2403fb413a4087fe1d96c785810`.
`fixes-evidence/` contains the final measurements and desktop/mobile RU/EN
screenshots. Representative results:

- [Load order, desktop](fixes-evidence/order-ru-1440.png) and
  [mobile](fixes-evidence/order-ru-390.png).
- [Console, desktop](fixes-evidence/console-ru-1440.png) and
  [mobile](fixes-evidence/console-ru-390.png).
- [Diff, desktop](fixes-evidence/diff-ru-1440.png) and
  [mobile](fixes-evidence/diff-ru-390.png).
- [Offline, mobile](fixes-evidence/offline-ru-390.png),
  [English offline](fixes-evidence/offline-en-390.png) and
  [login](fixes-evidence/login-ru-390.png).

Run `python docs/visual-style-audit-2026-10-07/verify.py` with the project's
Python 3.12 environment to replay the isolated verification fixture.
`capture.py` recaptures the current UI to a fresh ignored temporary directory;
it does not replace the baseline evidence. Both launchers remove their temporary
test modules after execution. The archived fixtures are opt-in, outside the
normal test collection.
