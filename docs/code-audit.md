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

## October 13, 2026 defect audit

This audit used the existing Graphify graph to navigate configuration, HTTP,
storage, operation, Workshop and browser code. Findings were verified against
source and isolated regression tests before changing behavior. Python 3.12.10
from the checkout's `.venv` was used; installed runtime and development versions
match the pinned requirements.

### Confirmed defects and corrections

| Area | Trigger and consequence | Correction |
| --- | --- | --- |
| INI continuations | An indented comment or blank line following a trailing semicolon was consumed as part of the value and deleted by an edit. | Stop continuations before comments and blank lines; preserve their original bytes. |
| INI key aliases | Editing `mods` or `password` could append a duplicate canonical key and leave the persisted draft unreadable. | Canonicalize edits with the parser's key mapping and reject duplicate aliases before saving. |
| Lua additions | Adding a child field before its scalar parent silently discarded the child. | Reject conflicting parent/child additions in either input order. |
| Configuration revisions and history | An external edit during durable history writes could bypass the earlier revision check or mix different versions in a history pair. | Capture immutable originals for both the revision and history; recheck the working revision before creating the recovery journal. |
| Source editor | Typing while a draft save or metadata request was pending could replace the new text with an older response and mark it saved. | Share the pending save and persist newer buffer snapshots before clearing dirty state. |
| Numeric fields | Clearing a field that allowed zero converted the empty text to `0`, including settings where zero disables a feature. | Require a numeric value while continuing to accept an explicit zero. |
| Protected static HTML | Native Windows separators could alias `index.html` through a public static URL and bypass the HTML authorization guard. | Check containment and authorization against the resolved file path. API authorization remained enforced. |
| HTTP input | Malformed Origin URLs and truncated or deeply nested JSON could escape validation or close the connection without a JSON error. | Reject malformed origins and incomplete bodies; bound JSON nesting before downstream processing. |
| Persistent JSON stores | Deeply nested damaged session or backup-verification stores raised uncaught recursion errors. | Recover through the existing empty-store behavior instead of failing startup or verification. |
| S3 export | A local archive changed after upload verification but before deletion could be removed without its new bytes being uploaded. | Recheck local identity immediately before removal, including after phase callbacks and client cleanup. |
| Operation launch | Failure to construct or start a worker left the operation slot permanently occupied. | Restore the previous idle state and return the existing operation error; later operations can start. |
| Container updates | Mount checks could accept different data storage, dropped bindings or a persistent mount replaced by temporary storage. | Validate mount identity and access before downloading images or handing off recreation; retain implicit image volumes and declared config/secret files. |
| Workshop validation | Long dependency chains exhausted Python's recursion limit, including valid acyclic chains. | Detect cycles iteratively with the same dependency and ordering edges. |
| Browser verification | A drag test inspected backend state while the optimistic preview's save was still pending. | Wait for the existing saved-order announcement before inspecting persisted order. |

The configuration and S3 identity checks detect the reproduced race windows;
they do not provide an atomic transaction with arbitrary external file writers.
Regression fixtures use fictional credentials and local temporary data. No live
game server, registry publication, Steam download or Telegram message was needed.

### Validation

The initial full gate reported **1,673 passed, 4 skipped and 1 failed**. The failed
phone-width drag test passed its automatic diagnostic replay; inspection confirmed
that its backend assertion ran before the save finished. Saved-order assertions
now wait for the application's completion announcement. No checks were disabled.

After the final Python edit, repository-wide `ruff format .`, `ruff check .` and
`ruff format --check .` passed. Node.js 24.21.0 was used for JavaScript checks.
`graphify update .` refreshed the AST graph to **2,311 nodes, 4,567 edges and
165 communities**, with no model API calls. Graphify reports no extracted nodes
for `pyproject.toml` and skips unsupported file formats; these files remain in
scope for the repository's ordinary checks. Community names affected by changed
membership were derived deterministically from their hub nodes.

The final `python scripts/check.py` passed dependency consistency, repository-wide
lint/format, generated translation catalogs, every JavaScript syntax check and
**1,750 tests passed, 5 skipped**, including Chromium browser scenarios. The full
test run took 610.63 seconds. `git diff --check` also passed after the report update.
