# PZ Console

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Project Zomboid administrators managing a dedicated server from a desktop or mobile browser.

## Product Purpose

Inspect server state, prepare and review configuration changes, and verify the
result after restart. Settings and Workshop changes share a persistent profile
draft. Browser saves use the page's loaded snapshot. Applying a reviewed draft
can overwrite external file edits, which appear in the review and are retained
in configuration history.

## Operating Context

Self-hosted on the game server's Docker host. The interface supports English and
Russian. Remote RCON connections provide a limited set of controls. Demo mode
shows fictional data with operations disabled.

## Capabilities and Constraints

Overview, Settings, Mods, Players, Maintenance, Backups, Events, and Console.
Overview widgets can be reordered or hidden per browser. Players can show live
world positions with the optional B42 integration. Maintenance includes
scheduled announcements and restarts, image and mod updates, RCON recovery and
Telegram notifications.

Keep Workshop packages, ModIDs, maps and profiles distinct. Preserve review
revisions, secret masking, configuration history, startup verification and
explicit world-replacement confirmation.

## Brand Commitments

Use PZ Console in English and PZ Пульт in Russian. Follow the warm dark palette,
locally served fonts and status colors in [DESIGN.md](DESIGN.md).

## Evidence on Hand

See the [fixture-based screenshots](docs/screenshots/README.md) and the
[real-server acceptance procedure](docs/acceptance-b42.md).

## Product Principles

- Keep Settings and Mods directly accessible.
- Preserve user input on failure and show the next available action.
- Confirm destructive operations explicitly.
- Distinguish data freshness, drafts, pending operations and verified results.

## Accessibility & Inclusion

Preserve keyboard operation, visible focus, text alongside status colors,
named controls, linked tabs and panels, reduced motion and usable touch targets.
