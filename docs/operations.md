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

## Player warning language

In Maintenance → Notifications, choose the player warning language (English or Русский).
Changes save automatically as `playerNotifications.language` (`en` or `ru`) in
`dashboard-data/settings.json`. English is the default for new and legacy settings
without a valid preference. The setting applies to RCON countdowns for stop, restart,
image/mod updates, stopped backups, restore and configuration application, including
automatic operations and mod-update cancellation. EN/RU countdowns use singular and
plural time units. Each operation keeps the language selected when it starts.
The preference is independent of the interface language and Telegram language.
Free-text RCON commands remain as entered by the administrator.

## Telegram notifications

In Maintenance → Notifications, configure the bot token, chat ID, event groups,
and notification language (Русский or English). Changes save automatically.
The language applies to events and the “Test” message in the configured chat,
independently of the interface language and of the administrator who starts an operation.
New and existing installations without a valid preference default to English.
Explicitly saved Russian preferences are preserved. The preference persists in
`dashboard-data/settings.json` as `telegram.language` (`ru` or `en`).
Server names, archive filenames, Workshop IDs, and upstream diagnostics retain
their original values. The local event journal also retains its source text.

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

A shared SSE snapshot gathers each monitoring channel once per interval across
English and Russian subscribers, then localizes/encodes once per language.
Workshop metadata retains separate language-specific snapshots. Overview, players
and stats share one container inspection for up to three seconds. Ordinary API
reads remain direct; lifecycle actions, watchdog checks and stop/start waits do
not use the monitoring cache.
Hidden tabs close SSE and suspend polling; server schedulers and operations continue.
Returning to the tab reconnects. Fallback polling avoids overlapping requests.
The browser connects to `/api/stream?logs=0` and polls container logs only on a
visible Console, including when SSE is working. Opening Console or returning to
its tab refreshes immediately; slow log requests do not overlap. Plain
`/api/stream` retains log events for existing API clients. Stream threads sleep
until the next update is due and recheck session revocation at least once per
second between collections.

Event/backup journals are read from the end in 64 KiB blocks. Workshop and translation
metadata caches hold up to eight sets. Full-log downloads spool to a temporary file
and stream in 256 KiB blocks. Run `python scripts/benchmark_resources.py` to inspect
synthetic algorithm-level measurements; these are not production server benchmarks.
Run `python scripts/benchmark_cpu.py` for repeatable monitoring-call counts and
SSE localization CPU comparisons with the previous algorithm. The Compose panel
CPU budget is documented in [installation](installation.md#panel-cpu-budget).

The isolated 60-second monitoring model (eight clients, Console closed, normal
channel intervals, immediate mocked Docker/RCON responses) produced these counts:

| Clients | Docker CLI calls before / after | RCON player queries before / after |
| --- | --- | --- |
| Russian only | 68 / 32 | 12 / 12 |
| Mixed English/Russian | 136 / 32 | 24 / 12 |

This counts container inspections, stats, log tails and RCON player reads only,
excluding availability/digest probes, scheduler, watchdog and manual work.
Real collector latency changes exact rates.
The reduction in command count is not a measured reduction in total host CPU.
Monitoring can briefly show cached state; control checks always read fresh state.

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
