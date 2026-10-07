---
name: "PZ Console"
description: "Warm dark surfaces for server configuration and operations"
colors:
  bg: "#0d0b09"
  bg-deep: "#090806"
  surface: "#14110e"
  surface-2: "#1a1612"
  surface-3: "#211c15"
  line: "rgba(233, 226, 205, 0.10)"
  line-strong: "#80745d"
  ink: "#e9e2cd"
  muted: "#b3a78c"
  faint: "#968b73"
  accent: "#e9e2cd"
  accent-hover: "#f1ead5"
  danger: "#b3352b"
  danger-hover: "#c23f34"
  danger-text: "#e07b6e"
  danger-tint: "rgb(224 123 110 / .08)"
  success: "#86b060"
  success-tint: "rgb(134 176 96 / .08)"
  warning: "#d9a441"
  warning-tint: "rgb(217 164 65 / .08)"
  focus: "#e9e2cd"
  selection: "#304025"
typography:
  headline:
    fontFamily: '"Golos Text", "Segoe UI", system-ui, -apple-system, sans-serif'
    fontSize: "26px"
    fontWeight: 600
    lineHeight: 1.25
    letterSpacing: "-.02em"
  title:
    fontFamily: '"Golos Text", "Segoe UI", system-ui, -apple-system, sans-serif'
    fontSize: "18px"
    fontWeight: 600
    lineHeight: 1.3
    letterSpacing: "-.015em"
  body:
    fontFamily: '"Golos Text", "Segoe UI", system-ui, -apple-system, sans-serif'
    fontSize: "14.5px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: '"Golos Text", "Segoe UI", system-ui, -apple-system, sans-serif'
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "0"
  caption:
    fontFamily: '"Golos Text", "Segoe UI", system-ui, -apple-system, sans-serif'
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.5
  button:
    fontFamily: '"Golos Text", "Segoe UI", system-ui, -apple-system, sans-serif'
    fontSize: "13.5px"
    fontWeight: 500
    lineHeight: 1.3
  source:
    fontFamily: '"JetBrains Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace'
    fontSize: "13px"
    fontWeight: 400
    lineHeight: "22px"
rounded:
  sm: "8px"
  md: "12px"
  lg: "16px"
  tab: "6px"
  pill: "999px"
spacing:
  tight: "6px"
  inline: "8px"
  compact: "12px"
  group: "16px"
  roomy: "20px"
  section: "24px"
components:
  button-default:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.ink}"
    typography: "{typography.button}"
    rounded: "{rounded.sm}"
    padding: "9px 15px"
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.bg-deep}"
    typography: "{typography.button}"
    rounded: "{rounded.sm}"
    padding: "9px 15px"
  button-primary-hover:
    backgroundColor: "{colors.accent-hover}"
    textColor: "{colors.bg-deep}"
  button-danger:
    backgroundColor: "transparent"
    textColor: "{colors.danger-text}"
    rounded: "{rounded.sm}"
    padding: "9px 15px"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "9px 15px"
  button-solid-danger:
    backgroundColor: "{colors.danger}"
    textColor: "#fff"
    rounded: "{rounded.sm}"
    padding: "9px 15px"
  input:
    backgroundColor: "{colors.bg-deep}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "10px 12px"
    height: "44px"
  nav-active:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.accent}"
    rounded: "{rounded.sm}"
    padding: "12px"
  state-pill:
    backgroundColor: "transparent"
    textColor: "{colors.muted}"
    rounded: "{rounded.pill}"
    padding: "5px 10px"
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "18px"
  config-flow:
    backgroundColor: "transparent"
    textColor: "{colors.muted}"
    padding: "8px 12px"
---

# PZ Console design system

The current UI uses warm near-black surfaces, beige text, olive success states,
amber warnings, and brick-red destructive actions. The frontmatter records tokens;
`dashboard/static/style.css` is the implementation source of truth.

## Typography and surfaces

Golos Text serves navigation, forms, and headings. JetBrains Mono serves source,
IDs, and metrics. Fonts are bundled locally. Use the existing spacing, radii,
and surface layers. Keep permanent workspaces flat; use elevation for temporary
menus, dialogs, and tooltips. Status text accompanies color.
Destructive text and icon buttons share the danger-text color and a quiet red
outline before hover, with the shared visible keyboard focus. Ordinary RCON
prompts use muted text; danger colors retain their action and error meanings.
Shared CSS roles are `--type-button` (Golos Text, 13.5px/500), `--type-code`
(JetBrains Mono, 13px), and `--type-source` (13px with 22px source lines).
Text-bearing icon buttons use the button role. Technical IDs and map names use
the code role; form keys remain 12px and guide code follows its reading size.
`--r-control` maps to the 8px control radius. Text inputs, including inputs
without an explicit type, share the 44px field component; map-list editing uses
an explicit text input.
Panel and dialog headings share `--type-title` (18px/600) and
`--tracking-title` (-.015em). Modal and command-search windows use the shared
16px `--r-dialog`; nested diff previews use the 8px control radius and deep
source background.

## Navigation and responsive behavior

Desktop uses a collapsible sidebar (204 pixels expanded, 72 pixels collapsed)
with a page gutter (28 pixels). Tablet widths (741–1180 pixels) start collapsed;
desktop starts expanded. The sidebar toggle retains keyboard focus and exposes
its expanded state; collapsed links retain accessible names and native tooltips.
Desktop and tablet preferences are stored separately in the browser.
The workspace remains bounded to 1450 pixels and centers in the available space
beside either sidebar state on wide and ultrawide screens. Banners, draft actions
(including the in-flow bar in short windows), and footer content align with its
28-pixel inner gutters.
Mobile uses Overview, Settings, Mods, and More in the bottom bar. All four items
share the same-size icon and label treatment. More
remains a button that exposes its expanded and current-section states.
The independent offline shell groups its 36px brand icon and name in a header
beside the same globe-and-language control used by login and the dashboard,
with 24px separation before the page title. Shared language-control styling
lives in `pwa.css` so it also works from the offline cache.
The top bar contains the active profile, language selector, quick commands,
and sign-out action. The profile's status remains available through its
accessible description and selector color in every state, without a visible
status label or adjacent help control. Settings navigation shows a count
of changed draft lines when a draft has changes.

A separate PWA update alert floats below the top bar at the trailing edge. It
uses a compact pill, olive status dot and border, beige update action with an
arrow, and a 44px dismiss control. It never shifts the workspace or steals focus.
Input-protection and offline explanations expand within the same alert. Closing
it dismisses that version for the current window; a newer release appears again.
On mobile it floats above the footer, bottom navigation and draft actions, with
ordinary notifications stacked above it. The update button has a short visible
label and retains its full accessible name.

Mods put search and filtering first and disclose package/collection addition
separately; search has a visible label, and the page heading links to custom mod
settings while preserving the shared draft. Export, import and metadata refresh
remain available. Ctrl/Cmd+K opens section, setting, and mod search.
Composition shows Workshop packages in 25/50/100-item pages, defaulting to 25.
Search, filtering and sorting apply before pagination and reset to the first page;
draft and metadata refreshes preserve the page, clamping it when the list shrinks.
Changing profiles resets the page. Pagination uses the shared journal controls.
Load-order search uses the editor's flat labeled field treatment; its opaque
workspace background remains sticky without an additional enclosing border.
One SSE connection persists across route changes.

Desktop and mobile show Draft → Files → Startup inside the editor, below the
page heading and above the tabs and editing fields. The shared stepper uses round
numbered indicators, inline titles with status descriptions beneath, and thin
connecting lines. The current stage has a beige indicator; recorded stages use
olive checkmarks and errors use a brick-red warning mark. On mobile the labels
stack below the indicators while keeping all three status descriptions visible.
On mobile, draft actions stay
fixed above the bottom navigation. Viewports up to 600 pixels high keep the draft
bar in the document flow. Stage selection reviews changes
or focuses an action; it does not execute an operation.

## Forms, source, and feedback

Preserve draft input after failed requests. Separate loading, stale data,
conflicts, pending operations, saved drafts, and verified startup results.
Use inline errors with a retry action and concise success notifications.
Source editing preserves native formats and masks secrets. Tabs expose linked
panels and clear selected states. Field help opens on hover, focus, or tap,
uses aria-describedby, stays within the viewport, and closes with Escape.
Mobile help controls have 44-pixel touch targets. Desktop labels are bounded
to 260 pixels and controls to 560 pixels; labeled setting search is bounded to
600 pixels. Boolean draft fields use one transparent clickable 44-pixel row.
Setting search names its current section and links to matching neighboring sections.
Technical keys, numeric bounds (including one-sided bounds), and input instructions
sit below the controls. General lifecycle and stock-setting hints are omitted.
Unique explanations, applicability details and absent defaults open on demand;
metadata provenance accompanies substantive help. Fields without additional help
have no help control, including fields with only metadata provenance. Exact
applicability labels, environment ownership, errors and secret-marker instructions
remain available beside their relevant controls. Descriptions reference only
rendered elements. Settings keeps its lifecycle stages without a repeating page
description. Settings and Mods omit the collapsible guide blocks.
Changed-field labels remain visible, and changed text controls use the warning
border. Help stays above the mobile navigation and draft actions.

## Workspace layout

Overview groups server state/actions, player and backup summaries, container load,
updates, and recent events. Maintenance places image updates beside watchdog and
Telegram settings, stacking at narrow widths. Digest fields share copy feedback.
Desktop console panels share three rows for titles, compact filter/command tools,
and aligned output windows. Log counts, download and auto-scroll controls, errors,
and period feedback sit below the log output so their changes do not stretch the
RCON command area or shift the aligned windows.
Backup archives use aligned name, size, date, and action columns, sharing desktop
column widths with their headings; mobile places them on separate rows.
Download, verify and restore actions have visible labels;
delete retains a named icon and confirmation. Show verified archive results
only from operation history, and scheduled times with the server offset and
the next run in browser-local time. Archive pages contain ten items. The run journal uses
25/50/100-item pages and preserves the current page during refresh. Journal dates
and triggers remain visible in the stacked mobile rows.
The login form uses the red brand mark, login/password fields, and an opt-in
Remember me checkbox with a 30-day explanation. Errors stay beside the login action.

## Accessibility and motion

Keep visible keyboard focus, semantic headings, named controls, linked tabs,
textual statuses, and explicit destructive confirmations. Respect reduced motion
and forced colors. Check narrow screens for horizontal overflow. Browser tests
provide regression coverage, not full assistive-technology certification.

## Presentation assets

Current README captures live in [docs/screenshots](docs/screenshots/README.md).
Historical audits and prototype screenshots are available through Git history.
