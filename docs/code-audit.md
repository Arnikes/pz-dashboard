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
check now calls `configeditor.legacy_toggle` directly and still verifies that
the server INI remains unchanged after rejection.

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
