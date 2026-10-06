# PZ Console

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Experienced Project Zomboid administrators managing a dedicated server.
The main daily workflow is editing server settings and Workshop selections,
with diagnostics, backups, and lifecycle operations close at hand.

## Product Purpose

Inspect server state, prepare changes, review them, apply them safely, and verify
the result. Configuration and mod selections pass through persistent drafts and
revision checks before reaching game files.

## Operating Context

Self-hosted on the game server's Docker host, with desktop and mobile browsers.
English and Russian are supported. RCON-only connections provide reduced capabilities.
Explicit demo mode uses illustrative data and does not execute operations.

## Capabilities and Constraints

Eight routes: overview, settings, mods, players, maintenance, backups, events, console.
The stack is Python plus HTML/CSS/JavaScript. Keep runtime dependencies small.
Drafts, diffs, history, secret masking, revision checks, world-replacement confirmation,
player warnings, and startup verification are part of the product contract.
Workshop packages, ModIDs, maps, and profiles are distinct entities.
The SSE lifecycle is shared across routes.

## Brand Commitments

The English interface name is PZ Console; the Russian interface uses PZ Пульт.
Use warm near-black surfaces, beige text, olive success states, amber warnings,
and brick-red destructive actions. Follow [DESIGN.md](DESIGN.md) and the current CSS.

## Evidence on Hand

Isolated backend/browser fixtures and reproducible screenshots in `docs/screenshots`.
Real-server acceptance has a [separate record](docs/acceptance-b42.md).
Do not claim measured administrator productivity or full accessibility certification.

## Product Principles

- Keep settings and mods directly accessible.
- Preserve user input after an error and show the next action.
- Confirm destructive operations explicitly.
- Distinguish freshness, pending operations, drafts, and verified results.
- Support keyboard efficiency and usable mobile controls.

## Accessibility & Inclusion

Maintain keyboard operation, focus visibility, text alongside status colors,
named controls, linked tabs/panels, reduced-motion support, and touch targets.
Browser checks do not replace assistive-technology testing or full WCAG certification.
