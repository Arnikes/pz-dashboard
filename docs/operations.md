# Server operations and API

The panel serializes heavy operations and shows their progress in the operation bar.
Lifecycle and world archive actions require local Docker/data access.

## Backups and restore

Manual backups request an RCON save and archive the data directory; logs are excluded.
Choose a stopped-server backup when a fully stopped snapshot is required.
Daily backups use container time (`TZ`), default to 03:00, and retain seven archives
by default. A missed scheduled run is caught up when the panel starts.
Configure the time, retention count, and stop-server option in Backups.

Runs are recorded in `dashboard-data/backups.jsonl`; events appear in
`events.jsonl`. Failures are visible in the UI and can notify Telegram.
Archive verification extracts into an isolated temporary directory without stopping
the live server. Restore verifies and prepares the archive, warns players, stops the
server, replaces the data, and starts it again. Explicit replacement confirmation is required.

Keep a separate copy of important backups. For a recovery rehearsal, restore into
a separate game instance with its own volumes and confirm that the world and configuration
load. Archive verification alone does not prove the world behaves correctly in-game.

## Image and Workshop updates

Image checks compare the running container's local digest with the registry digest.
Applying an update pulls first and restarts only when the image changes.
A configured pre-update world backup must succeed before the update proceeds.

Automatic checks, warning periods, backups, and mod restart behavior are configured
in Maintenance and Mods. Scheduled restarts warn players through RCON.
After any server restart, mod freshness is rechecked so an already completed update
does not trigger another restart.

During a mod update's player-warning phase, “Cancel mod update” cancels the pending
restart and notifies players. Once stopping starts, cancellation is unavailable.
The API action `cancel-mods-update` returns HTTP 409 if there is no cancellable operation.

## RCON watchdog

The watchdog probes RCON every 30 seconds. It records prolonged silence and can
optionally restart an unresponsive local server. Such a recovery cannot warn players
when RCON is unavailable.

The post-restart grace period defaults to five minutes, accepts 0–60 minutes,
and uses `watchdog.gracePeriodMin` in settings. Controlled stops and restarts reset
the grace period, including configuration application, updates, restore, and stopped
backups. During operations/grace, the watchdog does not accumulate failures, alert,
or auto-restart. Operation failures are still recorded. Set zero to disable the grace period.

## Live data and resources

A shared SSE snapshot gathers and encodes each channel once per interval for all
subscribers. Ordinary API reads remain direct, and control actions do not use this cache.
Hidden tabs close SSE and suspend polling; server schedulers and operations continue.
Returning to the tab reconnects. Fallback polling avoids overlapping requests.
Container logs are polled only on Console.

Event/backup journals are read from the end in 64 KiB blocks. Workshop and translation
metadata caches hold up to eight sets. Full-log downloads spool to a temporary file
and stream in 256 KiB blocks. Run `python scripts/benchmark_resources.py` to inspect
synthetic algorithm-level measurements; these are not production server benchmarks.

## API

All API endpoints except `GET /api/health` require a session cookie.
Login with `POST /api/auth/login`, JSON `{"login":"…","password":"…","remember":true}`,
and save its `Set-Cookie`. The optional `remember` defaults to false.
Every POST/DELETE, including login/logout, requires `X-PZ-Request: 1`.
Browser mutations also validate `Origin`; CORS is not enabled.

`GET /api/auth/session` returns the current login.
`POST /api/auth/logout` revokes the session.
Unauthenticated protected requests return HTTP 401.
Health reports HTTP server liveness, not game readiness.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/overview` | Server, updates, settings, watchdog state |
| `GET /api/players`, `/api/players/history` | Players and 24-hour activity |
| `GET /api/stats`, `/api/stats/history` | Container usage and recent history |
| `GET /api/stream` | Shared SSE updates |
| `GET /api/logs?tail=250`, `/api/logs/full` | Log tail and full download |
| `GET /api/backups`, `/api/backups/journal` | Archives, schedule, run journal |
| `GET /api/events`, `/api/ops` | Events and operation progress |
| `GET /api/backup/download?name=…` | Archive download |
| `DELETE /api/backup?name=…` | Delete an archive |
| `POST /api/rcon` | Execute a `command` |
| `POST /api/settings` | Update scheduling, backup, watchdog, notifications |
| `POST /api/action` | Start, stop, restart, check/apply update, backup, restore, verification |
| `GET /api/telegram-chats` | Discover chats recently seen by the configured bot |

Example operation body: `{"op":"backup","stopServer":false}`.
Restarts can include `warnSeconds`; archive actions include `name`.
Backup settings use `autoBackup.enabled`, `autoBackup.time`,
`autoBackup.stopServer`, and `backup.maxBackups`.
See [editor endpoints](config-editor.md#editor-api) for draft/Workshop operations.
