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

Use warm near-black surfaces, beige text, olive success states, amber warnings,
and brick-red destructive actions. The frontmatter records the shared tokens.
The CSS in `dashboard/static/` defines their implementation.

## Typography and surfaces

Use bundled Golos Text for navigation, forms and headings, and JetBrains Mono
for source, IDs and metrics. Use `--type-button` for text-bearing buttons,
`--type-code` for technical values, and `--type-source` for editor text. Panel and
dialog titles share `--type-title` and `--tracking-title`. Keep permanent
workspaces flat and reserve elevation for dialogs, menus and tooltips.
Use `--r-control` for fields and nested previews, `--r-dialog` for dialogs,
and the shared 44-pixel field height. Pair status colors with readable text.

## Navigation and responsive layout

Desktop uses a collapsible sidebar and a centered, bounded workspace. Tablet
starts with the icon rail. Keep separate desktop and tablet preferences,
keyboard focus on the sidebar toggle, and accessible names for collapsed links.
Mobile uses Overview, Settings, Mods and More in a fixed bottom bar.
More opens a scrollable popover above its trigger. Keep page gutters aligned
with the footer, banners and draft actions. Very short viewports place draft
actions in the document flow.

The top bar contains profile selection, quick commands, language, notifications
and sign-out. Convey active profile status with color and an accessible
description. Settings shows the count of changed draft lines. Ctrl/Cmd+K opens
section, setting and mod search. One SSE connection serves route navigation.

## Startup, notifications and connection

Start the initial route and shared data requests together. Translate the shell
before showing it. Keep the boot wait bounded and expose pending profile work
inside Settings and Mods. Keep a usable profile after metadata failure, provide
retry or reload where applicable, and leave focus in place when a request
finishes late.

PWA updates and connection loss use floating alerts that preserve the workspace
layout and focus. Update retries keep current input intact. Connection retry
starts a fresh check and fresh data clears the alert. On mobile, stack these
above the fixed navigation and draft controls.

The notification center marks the current filter's messages as read on open.
Keep focus and scroll when new messages arrive. Toasts pause while hovered,
focused or in a background tab. Keep older messages reachable and focused
controls visible when the stack grows. Escape closes the center or collapses
the toast stack and returns focus to the relevant control.

## Configuration and Workshop

Settings and Mods share the Draft → Files → Startup stages and the same
profile draft. Stages reflect saved draft, file write and startup evidence.
The rail uses a vertical layout on narrow screens. Selecting a stage opens
the appropriate review or focuses a control.

Keep field and source input through failed saves. Edits use the loaded
snapshot, and live updates leave the editor alone. Show external file changes
in diff review before applying. Use View changes to review or discard the draft
and Apply with restart to write it. Pending Workshop packages use Download
packages with restart first, then explicit ModID selection and final application.

Use linked tabs and panels. Keep field labels visible, group help around the
field it describes and make it reachable by hover, focus and touch. Use visible
keyboard focus, a warning border for changed values, and inline recovery actions.
Mask saved secrets with a fixed placeholder and an accessible status. Preserve
native editing behavior in source views. Empty Telegram credential fields retain
saved values. Clearing a configuration field edits that value in the draft.

Workshop composition puts search and filters above the package list and
discloses import and addition separately. Preserve the page after draft or
metadata refresh and reset it when filters or profile change. Reveal and focus
newly added packages. Keep load-order search sticky. Use the drag handle for
pointer and keyboard reordering, preserve full-list positions during search
and save only completed moves.

## Operations and events

Use one progress bar across workspaces. Render the full planned route with
pending, current, completed and skipped steps. Space horizontal markers across
the lane and use the vertical route when labels need more room. Show current
detail beneath the route, with a tabular countdown for timed waits. Keep logs
and eligible cancellation in a stable action row. A busy operation preserves
input and locks mutation controls through readiness verification. Navigation,
search and diagnostics stay available.

Events keep date groups and 25/50/100-item pagination. Use compact local time
with the complete timestamp in the semantic time element. Pair warning and
error severity with a label. On mobile place time and severity above the
message. Keep Overview's compact feed readable without date-group headings.

## Workspace layout

- **Overview** groups status and server controls, players, backups, resources,
  updates and recent events into widgets. Customize Overview adds, removes and
  reorders cards with named controls. Persist the order per browser, move
  existing cards so reading and visual order agree, and retain focus after
  edits. If Server Status is hidden, server controls can reveal it temporarily.
- **Players** links the roster and its selected player to the world map. Keep
  pan and zoom operable with touch and keyboard. Use a clear marker and text
  for stale positions and explicit setup guidance when map data is unavailable.
  Wrap names and stack the roster, map and history on mobile. Keep Kick and Ban
  visible with their confirmation flow.
- **Maintenance** groups game, console and Workshop updates, RCON recovery,
  periodic announcements, scheduled restarts and notifications in responsive
  columns. Keep action availability and each schedule's save and status together.
- **Backups** aligns archive name, size, date and actions on desktop and stacks
  them on mobile. Show archive verification receipts and missing contents beside
  the archive. Use ten archives per page and the shared journal pagination.
- **Console** aligns command and log output on desktop. Name both scroll regions
  and make them keyboard focusable. Preserve command input on failure and pair
  each command with its own result. Keep log controls and freshness below the
  output. Preserve scroll and selection during polling, and let Latest restore
  following.
- **Login** keeps Remember me opt-in and errors next to the sign-in action.

## Accessibility and motion

Preserve visible focus, named controls, semantic headings, explicit destructive
confirmations, reduced motion and forced colors. Keep 44-pixel targets for key
touch controls. Check narrow screens for overflow and focused controls hidden
by fixed bars.

## Presentation assets

See the [README captures and regeneration steps](docs/screenshots/README.md).
