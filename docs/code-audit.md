# Graphify code audit

The October 10, 2026 audit removed obsolete mod-management helpers and kept the
live configuration editor as the single implementation for those operations.
The HTTP API and English/Russian interface contracts are preserved.

## Scope and evidence

Graphify 0.9.61 indexed 107 code files using local AST extraction. The initial
extraction contained 1,950 nodes and 4,666 edges. Clustering produced 4,008 edges
and 133 communities. Graph queries and incoming-call candidates guided source
inspection; repository-wide reference searches and existing tests confirmed each
removal. No model API was used and extraction consumed zero model tokens.

The audit covers application Python, JavaScript, tests, and maintenance scripts.
Bundled assistant skills are excluded by `.graphifyignore`. Documentation and
images were not semantically indexed. Graphify detected `pyproject.toml` but
extracted no nodes from it; 26 files with unsupported formats were also skipped.
These files remain subject to the repository's existing checks.

## Removed runtime code

| Location | Removed symbols | Evidence and replacement |
| --- | --- | --- |
| `dashboard/dockerlib.py` | `compose_pull` | No callers; live updates use image pull and Compose up. |
| `dashboard/ops.py` | `_workshop_map`, `mods_config_state` | No callers or reference-based dispatch. Live mod data uses `configeditor.mod_state` through `list_mods`. |
| `dashboard/ops.py` | `parse_mods_ini`, `_ini_value`, `_split_list`, `_ini_replace_value`, `set_mod_enabled` | Only obsolete helpers and their tests referenced these functions. The HTTP routes already call `configeditor` and `configformats`; tests now exercise those live paths. |
| `dashboard/ops.py` | `_MODS_LOCK`, `_clamp_int`, `_TIME_RE` | Unreferenced lock and compatibility aliases. |
| `dashboard/i18n.py` | `stream_frame` | Used only to simulate historical SSE behavior in `benchmark_cpu.py`; moved there as `previous_stream_frame`. |

The INI regression checks still cover Workshop values, unrelated sections,
appending missing fields, and continuation lines. The unknown-metadata toggle
check initially called `configeditor.legacy_toggle`; the follow-up below removes
that route and retains unknown-mod validation in the live draft editor.

## Refactoring and integration

`payloads.stream_payload` now dispatches telemetry and ordinary channels through
the same `PAYLOADS` registry. A fixed set identifies the three providers that
accept a shared container-state collector, replacing the dictionary previously
created on every telemetry call. Scheduling uses the time helpers from
`settingsmodel` directly instead of private import aliases.

Graphify runs in an isolated Python 3.12 tool environment, with its version pinned
in `requirements-graphify.txt`. The repository includes its Codex skill,
references, license files, and graph-navigation instructions in `AGENTS.md`.
Existing design hooks are preserved. The installer-generated `hook-check` hook
is an intentional no-op on Codex Desktop and was omitted. Graph output and caches
are ignored and do not enter the runtime image.

## Analysis limits

HTTP methods such as `do_GET` and `do_DELETE`, server attributes, callbacks,
registry entries, and frontend event handlers can be reached dynamically. They
were retained even where graph or secondary static analysis reported no callers.
The graph does not prove runtime reachability or the absence of further dead code.

The initial clustered graph had 21 self-loops and no dangling or missing
endpoints. Clustering collapsed 658 edges sharing endpoints; the clustered view
therefore cannot enumerate every individual relation or call site.

A clean extraction after the refactoring produced 1,940 nodes and 4,646 edges;
clustering produced 3,988 edges and 135 communities. The raw diagnostic reported
562 edges with unresolved endpoints, 21 self-loops, and 189 same-endpoint
collapses in an undirected build. These are Graphify integrity limits and were
not treated as evidence that referenced code was dead. `graphify-out/audit-raw.json`
preserves the unclustered extraction and `audit-health.json` preserves its
diagnostic output. The final graph was rebuilt in a fresh output directory to
avoid carrying prior inferred nodes through the incremental update.

## Reproduce the audit

Follow [Graphify setup](../CONTRIBUTING.md#graphify-code-audits), use
`graphify query`, `graphify explain`, and `graphify affected` to inspect candidate
symbols, then confirm references in source. Run the normal repository checks
after changing code; the graph is an additional navigation tool, not a CI gate.

## Validation

Python 3.12.10 and the checkout's `.venv` were used. Installed runtime and
development versions match every pin in `requirements-dev.txt` and
`dashboard/requirements.txt`.

- Repository-wide `ruff format .`, `ruff check .`, and `ruff format --check .`
  passed after the final Python edit.
- `python scripts/check.py` passed: dependency consistency, Ruff, generated
  translation catalogs, all JavaScript syntax checks, and **1,520 tests passed,
  3 skipped**, including Chromium browser tests.
- The focused backend run passed all 229 tests.
- `python scripts/benchmark_cpu.py` completed and confirmed identical output
  between the historical and current caches for EN/RU and one/eight subscribers.
  Its historical performance comparison is not a measurement of this audit's
  refactoring speedup.
- `git diff --check` passed.

## Legacy removal follow-up

A second October 10 audit traced the remaining legacy paths through Graphify and
repository-wide references, then inspected the actual browser and HTTP callers.
The current Mods page always uses `ConfigEditor`; the old `sec-mods` card was
permanently hidden and its renderer returned immediately when that editor existed.
Its mutation handler therefore belonged to a superseded interface.

Removed:

- The hidden mod list, its search/filter/sort controls, switches, restart dialog,
  renderer, state, event handlers and dedicated CSS.
- `POST /api/mods-config`, `configeditor.legacy_toggle` and its separate
  profile `disabled.json` writer. Mod selection now goes through draft patching,
  review and `apply-config`, with remembered order managed by the existing editor.
- The `mods` SSE provider, listener and fallback polling. No active interface used
  those background snapshots. All remaining SSE channels collect once across
  EN/RU subscribers and cache their translated frames separately.
- Constant legacy mod-response fields (`paired`, `pairs`, `mappingSource`,
  `canManage`), the unused ModID-to-Workshop table in update reporting, and
  obsolete `configeditor` re-exports. Source-merge tests now call `configprofiles`
  directly.
- `KEEP_BACKUPS`, which was parsed but never read. Retention continues to use
  `backup.maxBackups` from settings.
- Russo One and duplicate weight-specific Golos Text/JetBrains Mono assets.
  The two active families use four variable font files for Latin/Cyrillic;
  they continue to serve every declared weight locally. Their licenses remain.
- 23 translation keys used exclusively by removed controls and errors, with both
  locale catalogs and the generated browser bundle updated together.

The old write endpoint now returns 404; external clients must use the documented
[editor API](config-editor.md#editor-api). The stream no longer emits `mods` events;
explicit `GET /api/mods` remains available. This is an intentional removal of the
superseded interface, rather than a promise of compatibility with old clients.

### Retained data and recovery

The read-only `modsDisabled` recovery notice remains: its historical records have
no reliable profile ownership, so automatic migration could restore IDs into the
wrong profile. Only an explicit user selection copies those records into a draft.
Existing settings, profile state, history and recovery files are not deleted by
this change. Persisted-settings normalization, startup verification of older state,
clipboard fallback and network recovery also remain because they protect current
supported workflows, rather than implementing a second interface.

### Follow-up validation

The full repository gate passed after the final Python edit. Tests now exercise disable/
re-enable and operation-slot behavior through the live draft editor. Browser
snapshot checks use active overview frames, and clipboard feedback is checked in
the current mod-update list. A removed-route regression confirms that the old
endpoint cannot queue work or change game files. Local-font checks still load
Latin and Cyrillic at every UI weight.

Final follow-up checks used Python 3.12.10 from the checkout's `.venv` and Node.js
24.21.0. Repository-wide `ruff format .`, `ruff check .` and `ruff format --check .`
passed. `python scripts/check.py` passed dependency consistency, generated catalogs,
all JavaScript syntax checks and **1,525 tests passed, 3 skipped**, including
Chromium tests. The affected backend/browser run passed all 343 tests. Eight
removed weight-specific font assets were byte-for-byte duplicates of the four
retained variable fonts. `graphify update . --force` refreshed the local AST graph
without model API calls. `git diff --check` passed.
