# Installation

Use a Docker host with Compose v2. Each panel manages one Project Zomboid
dedicated server.

## New server

1. Clone this repository and copy `.env.example` to `.env`
   (`cp .env.example .env` in Bash or `Copy-Item .env.example .env` in PowerShell).
2. Set `RCON_PASSWORD` and `ADMIN_PASSWORD` for the game server. Set
   `PZ_ADMIN_LOGIN`, `PZ_ADMIN_PASSWORD`, and `PZ_AUTH_KEY` for
   [panel authentication](#administrator-authentication).
3. Review `docker-compose.yml` and `.env`. The example uses
   [indifferentbroccoli's server image](https://github.com/indifferentbroccoli/projectzomboid-server-docker).
   Set the Steam branch, memory, ports, and time zone for your deployment.
4. Run `docker compose up -d --build`. Open `http://<host>:8081` and sign in
   with the panel account. Allow time for the initial Steam download.

## Existing Compose server

Copy `dashboard/` and `docker-compose.dashboard.yml` beside your existing
`docker-compose.yml`. Merge the panel settings from `.env.example` into your
`.env` and set the same RCON password that the game server uses. In the overlay,
set the RCON host, game container and service names, image, and Compose project
to match the existing deployment.

Mount the game's actual data directory as `/data` in the panel. Set the external
`pz-data` volume name to the existing volume. If the game uses a bind mount,
use the same host directory and remove the external volume declaration.
Mount the game installation at `/server-files:ro` for local Workshop metadata
and [player map geometry](player-map.md).

Start the dashboard service. Substitute the existing base Compose filename if needed.

```bash
docker compose -f docker-compose.yml -f docker-compose.dashboard.yml up -d --build pz-dashboard
```

## Volumes and server identity

The full example mounts `pz-data` at `/project-zomboid-config` in the game
container and `/data` in the panel. Profiles live under `/data/Server/`.
The game installation shares `pz-server-files` with the panel at
`/server-files:ro`. The panel stores archives at `/backups` and its settings,
sessions, drafts, history, and journals at `/dashboard-data`. Preserve these
directories across updates.

Mount the deployment's Compose directory at `/compose` and list its in-container
filenames in `COMPOSE_FILE`, separated by colons. Keep `PZ_CONTAINER`,
`PZ_SERVICE`, and `COMPOSE_PROJECT_NAME` aligned with the running game server.
See [profile and mount verification](config-editor.md#connect-the-right-profile).

## Administrator authentication

The panel uses one administrator configured through the environment. Choose a
login of 1–128 characters and a password of 12–1024 characters. This command
generates a 32-byte URL-safe base64 key for `PZ_AUTH_KEY`.

```bash
python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

Sessions use encrypted `HttpOnly`, `SameSite=Strict` cookies. Regular sessions
last up to 12 hours and end when the panel restarts. **Remember me** saves a
revocable session for 30 days in `DASHBOARD_DIR/auth-sessions.bin`. Logout revokes
the session and closes its event stream. Changes to the login, password, or
encryption key invalidate remembered sessions. Login limits are five attempts
per minute per IP and 30 per minute across the panel.

## HTTPS and access

Docker socket access grants administrator-level control of the host. Use a
trusted network or HTTPS with network access controls. Keep RCON on the Compose
network unless you need a remote RCON connection.

Set `PZ_AUTH_COOKIE_SECURE=true` for HTTPS. Serve the panel at the root of its
own origin and preserve the original `Host` header for origin checks. Preserve
asset query strings and headers through the proxy. Pass `/sw.js`,
`/manifest.webmanifest`, and `/static/` through without redirects. Honor
`Cache-Control: no-store` for HTML and API responses.

The Compose examples publish port 8081 on all host interfaces. With a reverse
proxy on the same host, bind the panel to `127.0.0.1:8081:8080` to prevent direct
access around the proxy. For private access, use a VPN or restrict clients at the
proxy or firewall.

### Search visibility

Keep `X-Robots-Tag: noindex, nofollow, nosnippet` on responses, including the
public login page. Shipped HTML also contains the robots meta directive.
`/robots.txt` allows crawlers to read that directive. For removal of a previously
indexed address, use the search provider's site-owner tools. Use network access
controls for privacy.

## Environment reference

These are panel defaults. The Compose examples set some values explicitly.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | `8080` | Internal HTTP port |
| `PZ_ADMIN_LOGIN`, `PZ_ADMIN_PASSWORD`, `PZ_AUTH_KEY` | Required | Panel administrator and session encryption |
| `PZ_AUTH_COOKIE_SECURE` | `false` | Require HTTPS for session cookies |
| `RCON_HOST` / `RCON_PORT` | `pzserver` / `27015` | Game RCON endpoint |
| `RCON_PASSWORD` | Empty | Must match the game server |
| `PZ_CONTAINER` / `PZ_SERVICE` | `pzserver` | Game container and Compose service |
| `PZ_DASHBOARD_CONTAINER` / `PZ_DASHBOARD_SERVICE` | `pz-dashboard` | Panel container and Compose service used for self-updates |
| `PZ_IMAGE` | `indifferentbroccoli/projectzomboid-server-docker:latest` | Fallback game image for update checks |
| `PZ_SERVER_NAME` | `Project Zomboid` | Display name |
| `COMPOSE_PROJECT_NAME` | Empty | Compose project identity. The example sets `pz`. |
| `COMPOSE_FILE` | Empty | Paths to Compose files inside the panel |
| `DATA_DIR` | `/data` | Shared game configuration and world data |
| `BACKUP_DIR` | `/backups` | Local world archives |
| `DASHBOARD_DIR` | `/dashboard-data` | Persistent panel state |
| `PZ_CONFIG_FILE` | Auto-detected | Active INI filename override |
| `PZ_VERSION` | Auto-detected | Exact B42 version fallback |
| `SERVER_FILES_DIR` | `/server-files` | Game files and Workshop metadata |
| `PZ_MAP_CELL_SIZE` | Empty | Vector XML scale (`256` or `300`) when the file lacks `cellSize` |
| `WORKSHOP_DIR` | Auto-detected | Explicit Workshop content directory |
| `LOG_LINES` | `250` | Default log tail length |
| `PZ_LOG_LEVEL` | `INFO` | Panel process log level |
| `PZ_DASHBOARD_CPUS` | `0.5` | Panel CPU quota in the supplied production Compose examples |

The full example sets `SERVER_BRANCH=unstable` for B42. A blank value selects
the default Steam branch. `SERVER_NAME`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`,
`MEMORY_XMX_GB`, and `MAX_PLAYERS` configure the game image. `TZ` sets container
time, including backup scheduling. The examples use UTC.

### Panel CPU budget

The default quota is half of one CPU core for the panel and its child processes,
including backup compression. Use `PZ_DASHBOARD_CPUS=0` to lift the quota while
retaining `cpu_shares: 128`, which gives the game priority under contention.
Docker daemon tasks and game container commands have their own resource
accounting. The quota can lengthen archive compression and verification.

### Process logging

`PZ_LOG_LEVEL` accepts `DEBUG`, `INFO`, `WARNING` (`WARN`), `ERROR`,
and `CRITICAL`. An invalid value falls back to `INFO` with a warning.
Panel logs go to stderr with timestamp, level, source, and message. Routine HTTP
statuses appear at `DEBUG`, 4xx at `WARNING`, and 5xx at `ERROR`.
HTTP records contain the recognized method and response status.
Events, Telegram subscriptions, and game container logs have separate controls.

## Updates

For a source build or a change to panel environment variables, recreate only the
panel service. Include both `-f` arguments for an overlay.

```bash
docker compose up -d --no-deps --build pz-dashboard
```

### Released images from GHCR

GitHub releases publish a `linux/amd64` panel image. Replace the dashboard
service's `build:` with the image in the release notes. This example uses `v1.9.0`.

```yaml
image: ghcr.io/arnikes/pz-dashboard:v1.9.0
```

Use the release digest to pin an immutable image or `:latest` to follow stable
releases. Keep the service environment, mounts, and ports. For a private package,
sign in to GHCR with package read access. Pull and recreate only the panel.

```bash
docker compose pull pz-dashboard
docker compose up -d --no-deps pz-dashboard
```

Include both `-f` arguments for an overlay.
See [publishing a release](../CONTRIBUTING.md#publish-a-github-release).

### Update the console from Maintenance

**Maintenance → Console update** checks the running panel's tagged image in its
registry. It also checks on startup. Applying the update pulls the image, then a
detached helper recreates only the panel service and checks its health. The
browser reconnects and the result remains in operation history.

This requires Docker and the Compose plugin, readable Compose configuration
and interpolation inputs, and a writable persistent `DASHBOARD_DIR`.
Use the host to update local builds, images pinned by digest, or Compose services
using environment-backed configs or secrets. Recreate the panel on the host
after changing its image reference. `PZ_IMAGE` configures the game image.

## Optional S3 backup export

Set the following variables in the panel environment and recreate the service.
Both production Compose examples forward these settings from `.env`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `BACKUP_S3_ENABLED` | `false` | Set `true` or `1` to export every new world archive |
| `BACKUP_S3_BUCKET` | Empty | Existing destination bucket |
| `BACKUP_S3_ENDPOINT_URL` | Empty | Custom HTTP(S) endpoint, or empty for AWS |
| `BACKUP_S3_REGION` | `us-east-1` | Signing region |
| `BACKUP_S3_PREFIX` | `pz-backups` | Use a distinct prefix per server. Empty uses the bucket root. |
| `BACKUP_S3_ADDRESSING_STYLE` | `auto` | `auto`, `path` (e.g. MinIO), or `virtual` |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Empty | Both required when using explicit credentials |
| `AWS_SESSION_TOKEN` | Empty | Additional token for temporary credentials |

The AWS SDK credential chain supports workload roles. Verify credentials inside
the panel container if storage reports a credential error. Use HTTPS for remote
providers and keep credentials, query parameters, and fragments out of the endpoint.
Grant `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject`, and
`s3:AbortMultipartUpload` for the prefix and `s3:ListBucket` on the bucket
restricted to that prefix. Encryption policies may require KMS permissions.
Reserve disk space for the archive and its verification extraction.
See [export, retention, and restore behavior](operations.md#optional-s3-export).

## Telegram and MTProxy

In **Maintenance → Notifications**, enter the bot token and numeric Chat ID, choose
a language and event subscriptions, then select **Check**. Start the bot from the
recipient's chat or grant it permission to post in the target group or channel.
Use **Find bot chats** with the direct HTTPS Bot API to discover the ID.

Enable **Connect through MTProxy** to route notifications and tests through an
MTProto proxy. Enter its host (DNS name or IP), port, and secret, plus the
Telegram API ID and 32-hexadecimal-character API hash from
[my.telegram.org](https://my.telegram.org).
Enter the numeric Chat ID manually. An enabled proxy fails closed if it cannot
connect or validate its settings.

Use a 32-character hexadecimal secret, a `dd` prefix followed by 32 hexadecimal
characters, or the full `ee` FakeTLS secret. A FakeTLS secret consists of `ee`,
32 hexadecimal key characters, and the hex-encoded ASCII hostname (up to 182
bytes before encoding). Use an ASCII A-label for internationalized names and
paste the full secret, including the hostname. An empty secret field retains
the saved value. Diagnostics appear in Events and panel logs with credentials redacted.

To bootstrap a new install from `.env`, see the `TELEGRAM_*` variables in
[.env.example](../.env.example). Saved settings take precedence per key,
including a saved disabled proxy. Edit saved settings in **Notifications**.

## Language and PWA

Choose English or Russian on the login, dashboard, or configuration help page.
The browser remembers the choice. On HTTPS or localhost, use the browser's
install action to add the app. Offline mode shows a reconnect page. Server
actions require a live connection. Unsaved input stays in the open window and
saved drafts return after reconnecting.

Use the update alert to apply an available web app update. It prompts you to
finish any active operation, dialog, or unsaved edits before reloading the
current section. A stalled update offers a retry.

## RCON-only use

A panel connected over RCON can list players, run console commands, and send
scheduled chat announcements. Container lifecycle, image updates, archives,
and configuration editing require local Docker and data access.
