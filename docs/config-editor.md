# B42 server settings and Workshop mods

**Settings** and **Mods** share one draft per profile. Each profile uses
`<name>.ini` and `<name>_SandboxVars.lua` under `DATA_DIR/Server`.
Preserve `DASHBOARD_DIR` for drafts, history, and recovery.

## Connect the right profile

Select the profile the server launches. The panel identifies it from
`-servername` or `SERVER_NAME`. If needed, set `PZ_CONFIG_FILE` to the
exact active INI filename, including `.ini`. This override takes precedence.
`PZ_SERVER_NAME` controls the display name. Choosing a profile in the editor
leaves the game's launch arguments unchanged.

The panel needs the same writable data directory as the game, including any
`-cachedir` argument. Separate mounts over `Server/` or individual profile
files can block file changes. If the game image uses `GENERATE_SETTINGS=true`,
disable regeneration in its Compose configuration before applying changes.

The panel reads the exact B42 version from the current launch's startup header
in Docker logs. A new launch invalidates the previous result. Retain the header
in the container log. Set `PZ_VERSION` to the exact installed version only as a
fallback if the current header cannot be read. Mount game files at
`/server-files:ro` or configure `SERVER_FILES_DIR` / `WORKSHOP_DIR` for
Workshop metadata.

### Verification is blocked before checking file contents

While signed in, open `/api/server-configs` on the panel's origin to check the
following prerequisites.

| Field | What to verify |
| --- | --- |
| `activeFile` | Matches the selected profile's exact INI filename |
| `version`, `versionKnown`, `versionSource` | Exact B42 version, `versionKnown: true`, normally `versionSource: startup-log` |
| `mountsKnown`, `serverDataPath` | `mountsKnown: true` and the game's data directory |

After changing panel environment variables, [recreate the panel](installation.md#updates).
Reload the editor before trying **Verify running server** again.

## Draft, files, startup

1. **Prepare the draft.** Form fields and mod selections save automatically.
   Use the save button in source view. The editor also attempts to save source
   edits before changing profile or tab. A failed request keeps unsaved input
   in the open tab. Reconnect and use **Retry saving draft**.
2. **Review.** Select **View changes** for validation and diffs with secrets
   masked. **Discard changes…** asks for confirmation before loading the current
   files into both editors. Closing the review keeps the draft.
3. **Apply.** Confirm **Apply with restart…** to warn players, stop the server,
   write the files, and start the server. The confirmation offers a world
   backup. It defaults to off for ordinary settings and on for mod changes
   and Workshop preparation. Configuration history is saved in either case.
4. **Verify startup.** The panel waits for PZ/RCON readiness and checks the
   written values, game version, and fresh Workshop metadata. To verify a
   running server without writing files or restarting it, use
   **Verify running server** when available.

The stage bar links to review or focuses an action. Use `Ctrl/Cmd+K` to find
sections, settings, and load-order entries.

## External edits and conflicts

The browser edits the snapshot it loaded on entry. Saving a draft sends the
tab's complete snapshot. It can replace draft edits saved by another tab.
The game files remain as they were until application. Use one editing tab per
profile.

The review compares the draft with the current files. Confirming application
writes the reviewed draft over external file edits, and configuration history
preserves the actual files present immediately before the write. If the draft
revision or profile changes during review, open a new review before applying.
Reopening a clean draft reads current files. A draft with pending changes stays
available until applied or discarded.

API clients use strict revision checks by default. A stale revision or a file
conflict can block the request. Validate the draft to review a possible rebase,
then send `rebase: true` with the reviewed draft and current file revisions.
A rebase applies non-overlapping edits to the current files' contents in the draft.
Resolve overlapping edits in source. See [Editor API](#editor-api).

## Sandbox settings

The form reads the B42 schema and effective `media/sandbox-options.txt` declarations
from selected mods, with available English and Russian translations. Retained
options for disabled or missing mods have their own group. The game manages
`VERSION`.

Lua expressions require source view. Keep `__PZ_SECRET_UNCHANGED__` markers in
their original fields to preserve secrets. For expression-based Lua, leave
`__PZ_RAW_LITERAL_…__` markers in their original context to retain the masked
values. Replace a marker with an explicit literal to change that value. New
expression-based source must pass the image's `luac -p` syntax check.

Some Sandbox settings affect only new characters, vehicles, or regions. Check
gameplay effects in the game.

## Workshop packages and individual mods

A Workshop ID identifies a Steam package. `Mods=` uses actual `id=` values
from `mod.info`, prefixed with `\` for B42. One package can contain several
ModIDs. The panel chooses compatible versioned directories for the detected game
version and includes their `common` content. A future-only or `common`-only
package cannot supply a compatible branch.
See [the B42 mod layout announcement](https://projectzomboid.com/blog/news/2024/08/tidy-up-time/).

Add a Workshop link, ID, or collection in **Mods**. Select packages from a
collection before adding them to the draft. Use the two-stage workflow for
packages the server still needs to download.

1. Choose **Download packages with restart…** or **Download packages through server…**.
   The first restart writes only the new `WorkshopItems` selection and preserves
   the current `Mods` list. Other edits stay in the draft.
2. After startup verification, select the downloaded ModIDs and check dependencies.
   Use **Order** to drag, move up or down, or enter a position. Set maps and map
   order explicitly. Re-enabled ModIDs return to their remembered profile position.
3. Confirm **Apply with restart…** to activate the completed draft.

Startup checks use fresh B42 metadata and the Steam manifest when available.
A missing manifest appears as `manifestVerified: false`. Resolve compatibility
and dependency errors when changing the mod composition. Metadata checks cannot
verify that the mods work together in game.

Modpack export includes packages, selected IDs, load order, maps, and retained
mod settings. Ordinary world settings, `VERSION`, secrets, and Workshop binaries
are excluded. Import rechecks packages through Steam and updates the draft.

If **Legacy disabled mod registry** appears, explicitly restore an entry into
the selected profile's draft. Historical `modsDisabled` entries have no reliable
profile assignment and stay in the original registry.

## Failed operations and recovery

File writes preserve unknown INI keys, comments, line endings, and file metadata.
A new Sandbox file inherits permissions from its INI partner. The panel saves
both original files and a recovery journal before writing. Partial writes roll
back. An unfinished transaction recovers while the game is stopped. A failed
rollback retains the journal and blocks automatic game startup.

After a failed startup, inspect the operation logs. The written files remain
the base revision. Load the previous configuration from profile history into
a draft, review it, then apply it. Cancel an unfinished Workshop installation
to remove its new entries from the draft. Downloaded packages stay in the cache.

The scheduler retries verification when readiness or metadata is unavailable.
Verification allows recognized game-generated `ResetID` changes, INI line-ending
serialization, and Sandbox serialization when existing values survive. It keeps
user-edited `ResetID` and pending draft edits, and checks new mod values against
fresh declarations. The original source remains in history.

## Editor API

Use an authenticated session and `X-PZ-Request: 1` for mutations.
See [API authentication](operations.md#api).

| Endpoint | Purpose |
| --- | --- |
| `GET /api/server-configs` | Profiles, active profile, game version, and setting sources |
| `GET /api/config-draft?file=…` | Masked forms and source, revisions, and draft state. Use `refresh=1` to reload a clean draft. |
| `POST /api/config-draft` | Patch `ini`, `sandbox`, `texts`, or `mods`. Also accepts discard or rebase requests. |
| `POST /api/config-validate` | Validate a draft revision and review diffs. Use `prepare: true` for Workshop preparation. |
| `POST /api/config-verify` | Verify files against a confirmed startup |
| `GET /api/mods?file=…&draft=1` | Draft mod selection. Omit `draft` for live files. |
| `POST /api/workshop-resolve` | Resolve an `input` item link, ID, or collection |
| `POST /api/action` | Launch `prepare-workshop` or `apply-config` |
| `GET /api/config-history?file=…` | Profile history |
| `POST /api/config-history` | Restore `historyId` into the draft |
| `POST /api/modpack` | Export or import a `pack` |

Draft mutations include `file` and `draftRevision`. Rebase also requires
`currentRevision`. A revision conflict returns HTTP 409. Application accepts
`restart`, `warnSeconds`, and optional boolean `backupBeforeApply`.
With the server stopped, `apply-config` with `restart: false` writes files
and leaves startup to the caller. Workshop preparation requires restart.

The browser uses `overwrite: true` for saving, validating, and applying the
draft. This permits draft replacement from a stale browser snapshot and writing
over external file edits. Application still requires the reviewed draft revision
and uses the normal operation lock, validation, recovery, and startup checks.

See [isolated B42 acceptance testing](acceptance-b42.md) for real-server checks.
