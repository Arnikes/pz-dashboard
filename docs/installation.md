# Installation

PZ Console runs beside one Project Zomboid dedicated server on a Docker host with
Compose v2. Use [the README quick start](../README.md#quick-start) for a new server
or the dashboard overlay for an existing project.

## Panel CPU budget

Both production Compose examples limit `pz-dashboard` to `PZ_DASHBOARD_CPUS=0.5`
by default (half one CPU, not half the host's total capacity) and set
`cpu_shares: 128`. The lower relative weight favors the game container when CPU
is contested; the CPU quota also limits panel work when the host is otherwise idle.
These settings follow [Docker's CPU resource controls](https://docs.docker.com/engine/containers/resource_constraints/#cpu).

Change `PZ_DASHBOARD_CPUS` in `.env` for your host, then recreate only the panel
with `docker compose up -d --no-deps --build pz-dashboard` (include the overlay files for an
existing-server installation). Set it to `0` to remove the hard quota while keeping
the lower relative weight. Compression and archive verification can take longer
under a quota; large archives still have the existing operation timeouts.

The budget covers the panel and its child processes, including `tar`/compression.
Docker daemon work, image builds/pulls and commands executed inside the game
container have their own resource accounting. Reducing monitoring calls still
matters; the panel quota does not cap the game server or all Docker work.

## Volumes and server identity

The full example uses `pz-data` for `/project-zomboid-config` in the game container
and `/data` in the panel. `pz-server-files` is writable at `/project-zomboid` in
the game container and read-only at `/server-files` in the panel. The active files
live under `/data/Server/<SERVER_NAME>.ini` and `<SERVER_NAME>_SandboxVars.lua`.

Keep `PZ_CONTAINER`, `PZ_SERVICE`, and `COMPOSE_PROJECT_NAME` aligned with the
actual server project. Mount its Compose files at `/compose`; `COMPOSE_FILE`
contains their paths inside the panel container, separated by colons.
Existing installations may use different image paths or bind directories.
Verify those mappings before enabling file changes or restores.

The panel compares actual mounts and the game's startup context before allowing
configuration writes. A mismatched/read-only data mount, an ambiguous profile,
or environment-generated settings can block applying a draft. See
[the editor guide](config-editor.md).

The optional [player world map](player-map.md) uses those same volumes plus a
small supplied server integration for current player coordinates. Download it
from Players and follow its one-time setup instructions.

## Administrator authentication

Set `PZ_ADMIN_LOGIN`, `PZ_ADMIN_PASSWORD`, and `PZ_AUTH_KEY` before startup.
There is one administrator, loaded from environment variables, with no account database.
The login must contain 1–128 characters and the password 12–1024 characters.
Generate a 32-byte URL-safe base64 encryption key:

```bash
python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

Passwords are checked using salted PBKDF2-SHA256 with 600,000 iterations.
Session cookies use Fernet encryption, `HttpOnly`, and `SameSite=Strict`.
Normal sessions expire after at most 12 hours and do not survive a panel restart.
“Remember me” sessions last 30 days from login and persist in
`DASHBOARD_DIR/auth-sessions.bin`. Logout revokes the session, including its SSE stream.
Changing the login, password, or encryption key invalidates remembered sessions.

Keep `dashboard-data` and the encryption key across updates. Login attempts are
limited to five per minute per IP and 30 per minute across the panel.

## HTTPS and access

Use a trusted network or an HTTPS reverse proxy. Set `PZ_AUTH_COOKIE_SECURE=true`
for HTTPS and preserve the original `Host` header through the proxy so origin checks
can validate browser requests. HTTP on a trusted LAN uses `false`.
The panel's Docker socket access is administrator-level access to the host.
The game administrator credentials and RCON password are separate from panel login.

Serve the panel at the root of its own origin; path prefixes are not supported.
Pass `/sw.js`, `/manifest.webmanifest`, and `/static/` through without redirects and
preserve their headers. Do not proxy-cache `/api/`, `/`, `/index.html`, or `/login`.

## Search visibility

Every panel response includes `X-Robots-Tag: noindex, nofollow, nosnippet`, including
the login page, redirects, errors, API responses, static assets, and downloads.
All shipped HTML pages also include the same robots meta directive as a fallback.
Keep the header when passing responses through an HTTPS proxy or CDN.

`/robots.txt` deliberately allows crawling so search engines can read `noindex`.
Do not replace it with `Disallow: /`: a blocked crawler cannot read the directive,
and the address may still appear in search results if another site links to it.
See [Google's noindex documentation](https://developers.google.com/search/docs/crawling-indexing/block-indexing),
[Bing's removal guidance](https://www.bing.com/webmasters/help/?topicid=37c07477), and
[Yandex's indexing controls](https://yandex.com/support/webmaster/en/adding-site/indexing-prohibition).
Private panel data still requires administrator authentication.

Rebuild or pull the updated panel image and recreate the panel to apply this policy.
Check the public `/login` response for the header and robots meta tag, and check
`/robots.txt` through the same proxy. Previously indexed addresses disappear after
the search engine recrawls them; for urgent removal, use the owner's Google Search
Console, Bing Webmaster Tools, or Yandex Webmaster removal controls.

This policy preserves access at the existing internet address. It is an instruction
to cooperative search engines, not a network access restriction: anyone who knows
or discovers the address can still reach the login page. To hide the panel from
public scanning as well, restrict access using a VPN or a proxy/firewall allowlist
and close the directly published dashboard port. The Compose examples publish
port `8081` on all host interfaces; when the reverse proxy runs on the same host,
bind it to `127.0.0.1:8081:8080` to prevent bypassing the proxy.

## Environment reference

These are application defaults; the Compose examples explicitly set some values.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | `8080` | HTTP port inside the panel |
| `PZ_ADMIN_LOGIN` / `PZ_ADMIN_PASSWORD` | Required | Single administrator |
| `PZ_AUTH_KEY` | Required | Cookie and remembered-session encryption |
| `PZ_AUTH_COOKIE_SECURE` | `false` | Secure cookies for HTTPS |
| `RCON_HOST` / `RCON_PORT` | `pzserver` / `27015` | RCON endpoint |
| `RCON_PASSWORD` | Empty | Must match the game server |
| `PZ_CONTAINER` / `PZ_SERVICE` | `pzserver` | Docker container and Compose service |
| `PZ_IMAGE` | `indifferentbroccoli/projectzomboid-server-docker:latest` | Fallback image for update checks |
| `PZ_SERVER_NAME` | `Project Zomboid` | Display name |
| `COMPOSE_PROJECT_NAME` | Empty | Compose project identity; example sets `pz` |
| `COMPOSE_FILE` | Empty | Paths to Compose files inside the panel |
| `DATA_DIR` | `/data` | Shared game world/configuration directory |
| `BACKUP_DIR` | `/backups` | World archives |
| `DASHBOARD_DIR` | `/dashboard-data` | Settings, sessions, journals, drafts, and history |
| `PZ_CONFIG_FILE` | Auto-detected | Active INI filename override |
| `PZ_VERSION` | Auto-detected | Exact B42 version fallback |
| `SERVER_FILES_DIR` | `/server-files` | Mounted game files/Workshop metadata |
| `PZ_MAP_CELL_SIZE` | Empty | Explicit vector XML scale (`256` or `300`) when the map does not declare `cellSize` |
| `WORKSHOP_DIR` | Auto-detected | Explicit Workshop content directory |
| `LOG_LINES` | `250` | Default log tail length |
| `PZ_LOG_LEVEL` | `INFO` | Minimum level for panel process logs |

`TZ` configures container time; examples use UTC. `SERVER_NAME`, `SERVER_BRANCH`,
`ADMIN_USERNAME`, `ADMIN_PASSWORD`, `MEMORY_XMX_GB`, and `MAX_PLAYERS` are
game-image variables in the full example. See the
[upstream image reference](https://github.com/indifferentbroccoli/projectzomboid-server-docker)
for additional game settings. A blank `SERVER_BRANCH` selects the default Steam branch.

## Process logging

Panel startup, HTTP responses, operation events, and journal-write diagnostics use
the same stderr format: timestamp, level, source, and message. Set `PZ_LOG_LEVEL`
in `.env` and recreate the panel to change its process output. All three Compose
examples pass this setting to the panel.

Accepted levels are `DEBUG`, `INFO`, `WARNING`, `ERROR`, and `CRITICAL`, ignoring
case and surrounding spaces; `WARN` aliases `WARNING`. An invalid value produces
a warning and falls back to `INFO`. Routine HTTP responses appear at `DEBUG`,
4xx responses at `WARNING`, and 5xx responses at `ERROR`. HTTP records contain
only a recognized method and response status, without URLs, query strings, bodies,
cookies, or diagnostic request text.

This threshold affects process output only. Persistent Events and Telegram
subscriptions keep their existing behavior; new event records additionally carry
a `level` field. Existing event files remain readable without migration. The
Console's game-container logs and their filters are a separate source. Console
commands retain their existing event-journal representation; process logs record
only that a command was submitted.

## Optional S3 backup export

The panel includes the pinned AWS SDK and supports AWS S3 and S3-compatible endpoints.
Export is disabled by default. Set these variables in `.env` for either supplied
Compose example, then recreate the panel:

| Variable | Default | Purpose |
| --- | --- | --- |
| `BACKUP_S3_ENABLED` | `false` | Set `true` or `1` to export every new world backup |
| `BACKUP_S3_BUCKET` | Empty | Existing destination bucket; required when enabled |
| `BACKUP_S3_ENDPOINT_URL` | Empty | Optional HTTP/HTTPS S3 endpoint; empty selects AWS |
| `BACKUP_S3_REGION` | `us-east-1` | Signing region |
| `BACKUP_S3_PREFIX` | `pz-backups` | Object prefix; distinct per server; empty uses bucket root |
| `BACKUP_S3_ADDRESSING_STYLE` | `auto` | `auto`, `path` (e.g. MinIO), or `virtual` |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | Empty | SDK access credentials, forwarded only to the panel |
| `AWS_SESSION_TOKEN` | Empty | Optional token for temporary credentials |

Use HTTPS for a remote provider. Endpoint URLs must not contain credentials, query
parameters, or fragments. Credentials are read through the SDK credential chain,
never stored in panel settings or returned by its API. For an IAM workload role,
omit access keys and configure role access on the host/container as appropriate.

If the backup catalog reports `NoCredentialsError` or `PartialCredentialsError`,
check the environment of the **panel container**. Both `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY` must be present when using explicit credentials.
`AWS_SESSION_TOKEN` is an additional field for temporary credentials, not a
replacement for `AWS_SECRET_ACCESS_KEY`. `WS_ACCESS_KEY_ID` is not a recognized
variable. Custom or older Compose files must forward the AWS variables to
`pz-dashboard`; adding them to `.env` alone does not forward them automatically.
Recreate the panel after editing its environment (include both `-f` arguments for
an overlay):

```bash
docker compose up -d --build pz-dashboard
```

S3 failures are returned as a structured error in the backup catalog. The server
overview continues working without requesting S3; its backup count is unknown
until a successful catalog refresh, and while that snapshot is older than 30 seconds.

Grant the panel `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject`, and
`s3:AbortMultipartUpload` for its object prefix, plus `s3:ListBucket` on the bucket
(restrict listing to that prefix). The bucket must already exist; the panel does
not create buckets. Provider encryption policies may
require additional KMS permissions. Each upload uses a unique key and confirms
its content by a full read-back before local deletion. Reserve enough local space
for both the compressed archive and temporary verification extraction. See
[export behavior and automatic S3 restore](operations.md#optional-s3-export).

## Updates

Rebuild the panel after changing code:

```bash
docker compose up -d --build pz-dashboard
```

For an overlay, include both `-f` arguments. Recreate the panel after changing its
environment. Do not remove persistent volumes during an update.

### Update the console from Maintenance

The console update card follows the tagged image reference in the running
`pz-dashboard` container, such as `gitea.arnike.ru/arnike/pz-console:latest` or
`ghcr.io/<owner>/<repository>:latest`. Both checking and downloading use that
reference; there is no registry override or GHCR fallback. `PZ_IMAGE` configures
the game image, not the console. After changing the console image in Compose,
recreate the console on the host before checking for updates in Maintenance.

Checks query the registry without downloading the image and refresh the running
container's identity. The local and registry values compare the same kind of
digest for the running platform: image configuration digests with the classic
Docker image store, and platform manifest digests with containerd. An index ID
and a configuration digest can differ even when the installed image is current.
If Docker or the registry does not expose enough information to compare them,
the check reports an error and leaves availability unknown.

Self-update requires a published image with a tag, the Compose plugin and mounted
Compose files, and a persistent writable `DASHBOARD_DIR`. Set
`PZ_DASHBOARD_CONTAINER` and `PZ_DASHBOARD_SERVICE` when using names other than
`pz-dashboard`. Local builds and digest-pinned deployments are updated from the
host. The console downloads the image before a detached helper recreates only
its Compose service and confirms that it is running and healthy.

### Released images from GHCR

GitHub tag releases publish the panel to `ghcr.io/<owner>/<repository>` for
`linux/amd64`. Use the exact image path shown in the GitHub Release notes.
In the `pz-dashboard` service, replace `build: ./dashboard` with:

```yaml
image: ghcr.io/YOUR-OWNER/pz-console:v1.0.0
```

Use a lowercase owner/repository path. Keep the service's environment, volumes,
ports, and CPU settings. `PZ_IMAGE` still identifies the **game server** image;
it is separate from the panel image above. To update only the panel:

```bash
docker compose pull pz-dashboard
docker compose up -d --no-deps pz-dashboard
```

For the existing-server overlay, include both `-f` arguments in both commands.
Use the `ghcr.io/...@sha256:...` reference from the release notes to pin an
immutable image, or `:latest` to follow the most recently published stable tag.
Private packages require `docker login ghcr.io` with read access; public packages
can be pulled anonymously. See [release setup](../CONTRIBUTING.md#publish-a-github-release).

## Telegram and MTProxy

Open **Maintenance → Notifications** to configure the bot token, Chat ID,
notification language and event subscriptions. Telegram uses the HTTPS Bot API
by default. Enable **Use MTProxy** to send notifications and connection tests
through an MTProto proxy. This requires the proxy host, port and secret, plus
your Telegram application API ID and API hash from [my.telegram.org](https://my.telegram.org).
Bot authorization still uses the bot token; no phone-number login is needed.

The host accepts a DNS name, IPv4 or IPv6 address, without a URL scheme or path.
The port must be 1–65535 and the API ID must be a positive integer. The API hash
contains 32 hexadecimal characters. Proxy secrets support 32 hexadecimal
characters, `dd` followed by 32 hexadecimal characters, or a complete `ee`
FakeTLS secret. A FakeTLS secret contains `ee`, the 32 hexadecimal characters
of the key, then the hostname encoded as hexadecimal ASCII bytes. Paste the
**entire secret**, including the hostname suffix, into the existing secret field
or `TELEGRAM_MTPROXY_SECRET`. The hostname must be a valid DNS name of at most
182 bytes; use its ASCII A-label form for an internationalized name. Its case
is preserved in SNI. For example, this fictional secret uses `example.com`:

```text
ee0123456789abcdef0123456789abcdef6578616d706c652e636f6d
```

The panel selects FakeTLS automatically for this format and authenticates the
proxy handshake before sending MTProto data inside TLS-shaped records. This
does not guarantee that a particular network's traffic filters will permit the
connection. Base64 secrets and pasted proxy links are not supported. A plain
32-character secret beginning with `dd` or `ee` remains supported.

The same settings can bootstrap a new installation through `.env`:

```dotenv
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=123456:replace-with-your-bot-token
TELEGRAM_CHAT_ID=-1001234567890
TELEGRAM_LANGUAGE=en
TELEGRAM_MTPROXY_ENABLED=true
TELEGRAM_MTPROXY_HOST=proxy.example.com
TELEGRAM_MTPROXY_PORT=443
TELEGRAM_MTPROXY_SECRET=0123456789abcdef0123456789abcdef
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=abcdef0123456789abcdef0123456789
```

Both Compose examples pass these variables only to the panel. Environment
values initialize each setting; keys already saved in
`dashboard-data/settings.json` take precedence, including a saved disabled proxy.
Changes in Notifications persist the effective settings. Recreate the panel
after changing its environment. Edit the saved settings in the app when an
environment change is overridden by an existing saved value.

API hash and proxy secret stay on the server, like the bot token. Leaving a
secret input empty keeps its saved value; enter a new value to replace it.
Credentials and the settings volume must remain private. MTProto authorization
state is kept only in memory and recreated after a panel restart.

Use a numeric Chat ID: a positive private-chat ID, negative group ID, or
`-100…` supergroup/channel ID. The bot must already be started by the recipient
or added to the target group/channel with permission to send messages.
Automatic **Find bot chats** discovery is available with the direct Bot API;
when MTProxy is enabled, enter the Chat ID manually. The panel never bypasses
an enabled MTProxy through a direct Bot API request.

Use **Test** after saving the configuration. Proxy failures appear in the panel
event journal and panel container logs, with credentials redacted. Failed proxy
connections do not send additional Telegram notifications. Incomplete or invalid
proxy settings fail the test instead of falling back to a direct connection.
Configuration errors name the proxy host, port, API ID, API hash or secret that
needs attention. These checks happen before contacting Telegram and do not mean
the Chat ID is invalid. For FakeTLS, include the complete hex-encoded DNS hostname
after the secret key. MTProxy test errors omit the unrelated saved Chat ID suffix;
direct Bot API tests retain it to help diagnose delivery failures.

## Language and PWA

The interface supports English and Russian. Its selector is available on the login,
dashboard, and help pages. The selection is remembered on the device; the browser's
language supplies the initial choice.

Install through your browser's app/install action on HTTPS or localhost.
On an ordinary LAN HTTP address, browser PWA installation may be unavailable.
The service worker caches public UI assets, not authenticated API data or backups.
Offline mode displays a reconnect page. Logout clears private client state.

API/SSE, configurations, logs, archives, and panel/login HTML are not stored in the
offline cache. Commands are not queued for later execution. Review the current state
before retrying an operation after reconnection. Unsaved input survives only in the
open window; server-saved drafts return after reconnecting.

Dashboard and login HTML use content-derived version URLs for styles and scripts,
so a browser or reverse proxy cannot reuse an older asset URL after that file changes.
Reverse proxies must preserve query strings in their cache keys and honor `no-store`
for HTML and API responses. The offline shell keeps its assets in the worker's
separate content-derived cache.

An available PWA update appears in a floating alert below the header (above the
bottom controls on mobile) without shifting the page or moving keyboard focus.
The alert stays available until the update is applied. The panel does not reload automatically.
The open, visible app checks for updates every minute and on returning to the app or
reconnecting. Its update action checks unsaved input and in-progress operations, then
applies the new version and reloads the current section with the latest HTML, styles,
and scripts. Activation waits for up to 15 seconds; a failure restores an explicit
retry action and preserves input. The action explains whether to finish signing in,
save or clear pending input (including an unsubmitted Workshop link), finish an operation,
or close a dialog. Network availability and input protection are checked again before
reloading. If activation finishes after a failed attempt, the alert offers a reload;
it does not reload by itself. Other open
windows keep their input and offer their own update action. Clear browser site data
to remove offline assets completely.

## RCON-only use

Without local Docker access, RCON can provide players and command execution.
Container lifecycle, image updates, archives, and file editors require local
Docker/data access. This is a reduced-capability connection, not full remote host management.
