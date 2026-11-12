# Server operations and API

Use **Overview** for server controls and **Maintenance** for updates and schedules.
The operation bar shows planned stages, the current step, and any timed wait.
While a worker is active, the panel locks other changes and leaves navigation,
events, and logs available. Completion, failure, and cancellation appear in the
header's notification inbox. The **Events** workspace keeps the server journal.
The browser keeps up to 100 inbox entries and synchronizes read state across
tabs on the same origin.

## Backups and restore

The **Backups** workspace lists archives, verification status, and the run
journal. Daily backups are disabled initially. When enabled, the default time
is 03:00 in the container's time zone (`TZ`). An overdue run is caught up when
the panel returns and the operation slot is free. The default retention count
is seven. Set it to `0` to keep every archive.

Backups include the world and configuration data and exclude logs. A live
backup requests a world save through RCON. The stop-server option creates an
archive with the game stopped, then restarts it and waits for PZ/RCON readiness
before verification or export. A backup of an already stopped server leaves
it stopped.

The panel validates the full gzip stream and safely extracts every new archive
into temporary storage before rotating older archives. An invalid archive or a
verification receipt failure leaves the new archive and prior copies available.
The run journal records the failure. You can also verify an existing archive
while the server runs. The **Verified** shield shows the check time, file count,
and any missing world or configuration paths. Replaced or changed archives must
be checked again. The receipt is stored in
`DASHBOARD_DIR/backup-verifications.json`. In-game recovery requires a separate
restore rehearsal.

Restore requires explicit confirmation of world replacement. It always validates
the archive again, stages its files with ownership metadata on the data volume,
and reserves room for the original world before stopping the game. A preparation
failure leaves the running server online. Once Docker
confirms the server is stopped, the panel replaces the files and starts the
server, even if it was previously stopped. If replacement fails, the panel
attempts rollback and leaves the server stopped. If rollback fails, the error
identifies the retained original and staged directories for recovery.

Keep a separate copy of important archives. Rehearse on an isolated game
instance and confirm that the world and its configuration load there.

### Optional S3 export

[S3 configuration](installation.md#optional-s3-backup-export) applies to every
new backup, including backups created before applying a configuration or update.
Existing local archives stay local. The panel deletes the temporary local copy
only after verifying the archive, uploading to a unique object key, reading the
complete object back to check size and SHA-256, and saving a verification receipt.
A failed export retains the local archive and blocks any dependent operation.
Read-back transfers the entire archive. Plan for the provider's transfer charges
and a shared 40-minute upload and read-back deadline.

While S3 is enabled, the catalog shows remote and retained local archives.
A storage listing failure leaves local archives available. Remote listings are
cached for up to 30 seconds. Overview shows an unknown count until it has a
fresh listing. Count-based retention runs in the configured S3
prefix after a successful export. Local retention is suspended so failed exports
remain available. If remote retention fails, the new S3 object remains and any
dependent operation stops. The journal records the object's location.

The panel downloads a remote archive into temporary storage and checks its size
and checksum before staging a restore. Use a bucket lifecycle rule to remove
incomplete multipart uploads left by a stopped panel.

## Image and Workshop updates

**Maintenance** has separate game image, console image, and Workshop update
controls. See [console self-update requirements](installation.md#update-the-console-from-maintenance)
for the panel itself.

A game image update validates the running service's Compose and data mappings
before pulling. It uses the pulled image to recreate only the game service and
checks PZ/RCON readiness. When enabled, the pre-update backup must succeed
before the update continues. Check the mounted Compose files and their
interpolation inputs if the panel reports a mismatch.

The console checks mod freshness in the background on startup when the game
container is running, RCON responds, and the operation slot is free. This check
also runs with scheduled checks disabled. With scheduled mod checks enabled, the
console checks freshness again after a game restart.

Configure automatic checks, player warnings, backup choices, and mod restart
behavior in Maintenance. Use **Cancel server update** before the game begins
stopping. A pull or backup already in progress finishes before the operation
slot is free. Use **Cancel mod update** during the warning period for an
automatic Workshop restart. The panel notifies players if it has already
warned them about the update.

## Periodic chat announcements and server restarts

In **Maintenance**, enable **In-game chat announcements** or **Scheduled server
restart**, choose an interval, and select **Save schedule**. Both start disabled.
The panel saves the next due times across its own restarts. Enabling a schedule
or changing its interval starts a new interval. Active operations defer due
work, and multiple missed intervals result in at most one attempt.

- **Chat announcements** send a single line of up to 200 characters through RCON,
  including in RCON-only mode. The interval is 1–1,440 minutes (default 30).
  Editing the text keeps the existing due time. Double quotes appear as
  apostrophes in chat. After an RCON error, the next attempt is at the next
  interval.
- **Scheduled server restart** requires a running local game container. The
  interval is 1–168 hours (default 24). The server runs for at least this
  interval after its latest start, including a start outside the schedule.
  A scheduled restart uses the game image update warning period and the
  regular graceful stop and readiness checks.

If the local game container is stopped at the due time, the schedule advances
to its next interval.

## Player warning language

In **Maintenance → Notifications**, choose English or Russian for in-game RCON
warnings. English is the default. The preference applies to countdowns for
lifecycle operations, updates, stopped backups, restore, and configuration
application. It also applies to cancellation notices. Each operation uses the
language selected when it starts. Free-text RCON commands stay as entered.

## Telegram notifications

Set the destination, event groups, and message language in
**Maintenance → Notifications**. Changes save automatically. The language of
Telegram messages and tests defaults to English and is independent of the panel
and player warnings. See [Telegram and MTProxy setup](installation.md#telegram-and-mtproxy).

## RCON watchdog

Enable the watchdog in **Maintenance** to probe RCON every 30 seconds. Its
silence threshold defaults to five minutes. Optional recovery can restart
an unresponsive local server. Player warnings require a working RCON connection.

The watchdog suspends failure counting and recovery during operations and its
post-restart grace period. The grace period defaults to five minutes and
accepts 0–60 minutes. Set it to zero to disable the grace period.

## Live data and resources

The browser receives shared monitoring updates over server-sent events (SSE),
with polling as a fallback. Hidden tabs suspend updates and reconnect when
visible. Container logs refresh only while **Console** is visible. Settings and
Mods load Workshop metadata on entry and on explicit refresh. Server schedulers
and operations continue independently of the browser.

Monitoring uses short-lived snapshots. Lifecycle operations and watchdog probes
read live container state. See [the panel CPU budget](installation.md#panel-cpu-budget)
for resource settings.

## API

`GET /api/health` reports panel HTTP liveness. Start a session with
`POST /api/auth/login` and JSON `{"login":"…","password":"…","remember":true}`.
The optional `remember` defaults to false. Save the returned session cookie for
all other API requests. Protected requests without a session return HTTP 401.
Every POST and DELETE, including login and logout, requires
`X-PZ-Request: 1`. Browser mutations must be same-origin.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/auth/session` | Current administrator login |
| `POST /api/auth/logout` | Revoke the current session |
| `GET /api/overview` | Server, settings, updates, and watchdog state |
| `GET /api/players`, `/api/players/history` | Online players and 24-hour activity |
| `GET /api/players/map/geometry`, `/api/players/map/integration.zip` | Active map geometry and downloadable position exporter |
| `GET /api/stats`, `/api/stats/history` | Container usage and recent history |
| `GET /api/stream` | Live events. Use `?logs=0` to omit log events. |
| `GET /api/logs?tail=250`, `/api/logs/full` | Log tail and full download. The tail accepts `since` and `until` as ISO timestamps with time zones. |
| `GET /api/backups`, `/api/backups/journal` | Archives, schedule, and run journal |
| `GET /api/backup/download?name=…`, `DELETE /api/backup?name=…` | Download or delete an archive |
| `GET /api/events`, `/api/ops` | Event journal and operation state |
| `POST /api/rcon` | Run a `command` |
| `POST /api/settings` | Update schedules, backup, watchdog, and notification preferences |
| `POST /api/action` | Start a worker or send a worker control action |
| `GET /api/telegram-chats`, `POST /api/notify-test` | Discover direct Bot API chats or send a test notification |

For example, send `{"op":"backup","stopServer":false}` to `POST /api/action`.
Restart actions can include `warnSeconds`. Archive actions include `name`.
An accepted worker returns its operation ID and an admission snapshot. Follow
`GET /api/ops` or the `ops` SSE event for progress and the final result. For image
check workers, read the updated image status through Overview or SSE.

Concurrent mutations return HTTP 409. During the cancellable stage, send
`cancel-update` or `cancel-mods-update` as the action with the active
`operationId`. A stale ID or a worker past the cancellation point returns 409.

Clients can include a unique `requestId` and the latest operation
`snapshot.epoch` as `requestEpoch`. After losing a launch response, send
`op: "resolve-request"` with the same identifiers. Read `requestPending`
and `operationState` before attempting a new mutation. Track
`snapshot.epoch` and `snapshot.sequence` to order snapshots from polling and SSE.

Backup settings use `autoBackup.enabled`, `autoBackup.time`,
`autoBackup.stopServer`, and `backup.maxBackups`. Recurring settings use
`chatAnnouncements.enabled`, `chatAnnouncements.message`,
`chatAnnouncements.intervalMinutes`, `scheduledRestart.enabled`, and
`scheduledRestart.intervalHours`. Returned `nextChatAnnouncement` and
`nextScheduledRestart` values are Unix timestamps in seconds or `null` for an
inactive schedule. Player warning language uses `playerNotifications.language`.
See [editor endpoints](config-editor.md#editor-api) for draft and Workshop actions.
