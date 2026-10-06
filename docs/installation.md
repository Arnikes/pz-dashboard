# Installation

PZ Console runs beside one Project Zomboid dedicated server on a Docker host with
Compose v2. Use [the README quick start](../README.md#quick-start) for a new server
or the dashboard overlay for an existing project.

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
| `WORKSHOP_DIR` | Auto-detected | Explicit Workshop content directory |
| `LOG_LINES` | `250` | Default log tail length |
| `KEEP_BACKUPS` | `10` | Legacy setting; configure retention in the UI |

`TZ` configures container time; examples use UTC. `SERVER_NAME`, `SERVER_BRANCH`,
`ADMIN_USERNAME`, `ADMIN_PASSWORD`, `MEMORY_XMX_GB`, and `MAX_PLAYERS` are
game-image variables in the full example. See the
[upstream image reference](https://github.com/indifferentbroccoli/projectzomboid-server-docker)
for additional game settings. A blank `SERVER_BRANCH` selects the default Steam branch.

## Updates

Rebuild the panel after changing code:

```bash
docker compose up -d --build pz-dashboard
```

For an overlay, include both `-f` arguments. Recreate the panel after changing its
environment. Do not remove persistent volumes during an update.

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

An available PWA update appears as “Update app”; the panel does not reload automatically.
Its update action checks unsaved input and in-progress operations. Clear browser site
data to remove offline assets completely.

## RCON-only use

Without local Docker access, RCON can provide players and command execution.
Container lifecycle, image updates, archives, and file editors require local
Docker/data access. This is a reduced-capability connection, not full remote host management.
