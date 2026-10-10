# DRY and KISS code audit

The console benefits most from sharing small mechanisms with identical contracts.
This refactoring consolidates file publication, profile diffs with masked secrets and
post-start mod option validation, and simplifies copying settings. It keeps
transaction recovery, operation sequencing and API error policy with their owners.

Audit date: 10 October 2026.

## Scope and findings

The review covered backend persistence, settings validation, profile editing,
Workshop metadata, HTTP dispatch and monitoring payloads. Frontend sampling covered
API requests, draft queues, shared DOM helpers, notifications and localization.
The code changes focus on backend duplication; this is not an exhaustive review
of every frontend rendering branch.

| Finding | Change | Reason |
| --- | --- | --- |
| Six implementations create a temporary file and replace a target. | `fileio.atomic_write` handles sibling temporary files, flushing, file synchronization, optional metadata, bounded Windows replacement retries and cleanup. | Fixes to the publication mechanism now apply to sessions, settings, editor transactions, console updates and both Workshop caches. |
| Workshop index cache failures can leave temporary files behind. | Both Workshop caches use the shared writer. | Failed publication retains the previous cache and removes the temporary file. Cache failures remain nonfatal to metadata discovery. |
| Draft, conflict and rebase previews repeat masking and unified diff construction. | `configprofiles.profile_diff` masks both sides and builds both file diffs. | Every preview uses the same secret handling while retaining its labels and line endings. |
| Applying a configuration and verifying an external restart repeat selected-mod option traversal. | `configschema.mod_option_errors` checks present values against all declarations from selected providers. | Disabled mods and absent options are skipped consistently. Callers still decide whether an existing problem is a warning or blocks confirmation. |
| Five settings copies serialize to JSON and parse it again. | `copy.deepcopy` creates detached settings and patch candidates directly. | Copying no longer depends on an unrelated serialization mechanism. Token masking and validation still operate on isolated objects. |
| The console update browser test injects screen state that disagrees with its polling fixture. | The test waits for action completion, updates the fixture and requests a normal overview refresh. | Background refreshes now agree with the expected mod update state, removing a race without relaxing the assertion. |

## Simplification results

`configeditor.validate` shrinks from 192 to 162 lines, `configeditor.atomic` from
20 to 7, and `ops._save_settings` from 22 to 12. The shared functions live in
existing modules that already own the corresponding mechanisms; no new runtime
module or dependency is introduced.

The public HTTP routes and response fields retain their contracts. The editor
still synchronizes parent directories, preserves file metadata, checks revisions
and retains recovery journals when rollback cannot finish. The shared writer
synchronizes file contents; callers remain responsible for directory durability.
Console update snapshots and Workshop caches now also synchronize contents before
replacement.

## Boundaries worth keeping

- API patches accept some numeric strings, while persisted settings normalize
  invalid legacy types to defaults. Their different compatibility rules are
  intentional and remain explicit in `settingsmodel`.
- `configformats` provides strict lossless editing and secret masking. The older
  INI helpers in `ops` are permissive compatibility utilities with different
  whitespace and newline behavior; merging them would require a separate contract
  change.
- `payloads` already shares providers between polling and streaming. Telemetry
  caching deliberately does not control lifecycle waits or mutations.
- Frontend draft queues, revision checks and settings feedback represent different
  concurrency contracts. A generic mutation framework would need substantial
  policy switches, so this refactoring keeps those workflows explicit.
- Generated catalogs and schema inventories remain generated data rather than
  candidates for manual deduplication.

## Remaining complexity

`configeditor.run` still coordinates a long transaction involving warnings,
stopping, backups, two Workshop stages, PZ startup normalization and recovery.
Its extracted option validation reduces nesting, but splitting the entire
workflow would require clear phase inputs and recovery invariants.
`ops.py`, `app.js` and `editor.js` also remain large. Future extractions should
follow concrete changes to backup, scheduling or editor behavior and preserve
their tests, rather than moving code solely to meet a file-size target.

## Verification

Regression coverage exercises publication failures before replacement, retention
of existing files, cleanup of temporary files, optional metadata, cache fallback,
secret masking in literal and unsupported Lua, and selection of mod providers.
Existing tests cover session persistence failures, settings rollback, conflict and
rebase previews, partial profile writes and changed Workshop constraints at startup.

The repository gate is `python scripts/check.py` under the checkout's pinned
Python 3.12 environment. It covers dependency consistency, repository-wide Ruff
lint and formatting, generated catalogs, JavaScript syntax, backend tests and
Chromium browser tests. POSIX ownership assertions run on Linux; Windows skips
that assertion. Real Docker, Steam and PZ integration remains a separate acceptance
workflow described in [Contributing](../CONTRIBUTING.md).
