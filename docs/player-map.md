# Player world map

In **Players**, select an online player to see their location. Use search,
pan and zoom, or fit the map to the roster or world. Player activity history
and RCON kick and ban controls remain available from the same workspace.

## Enable real positions

1. Sign in and download the integration from **Players**.
2. Copy the intact `PZConsoleMap` mod directory to the game server's cachedir
   `mods` directory. Append `\PZConsoleMap` to the active Build 42 `Mods` list,
   preserving other entries. It is a local mod.
3. Restart the game server. Follow the
   [bundled mod installation instructions](../dashboard/integrations/PZConsoleMap/README.md)
   for volume paths and troubleshooting.

The game and panel must share the cachedir. The supplied Compose project mounts
it at `/project-zomboid-config` in the game container and `/data` in the panel.
Base map geometry also requires the game installation at `/server-files:ro`.
The dashboard overlay can mount it from the existing game volume or host path.
Active map mods are resolved from the locally installed game and Workshop files.

XML with a declared `cellSize` provides its own scale. For XML without it, set
`PZ_MAP_CELL_SIZE` to the exporter's confirmed value (`256` or `300`) and
[recreate the panel](installation.md#updates). The map labels incomplete
coverage and warns about overlapping source extents. Check overlapping map
mods in the running game to confirm their final layering.

## Freshness and availability

The integration records online coordinates every five seconds. The panel uses
the active server profile and matches coordinates to the RCON roster by exact
player name. A capture expires after 15 seconds. The map marks any retained
coordinates as stale and shows their capture time. Players without valid
coordinates stay in the roster with an unavailable location.

RCON-only connections retain the roster but need accessible snapshot files for
coordinates. Demo mode shows illustrative players and geometry. The panel
serves map geometry separately from the live player stream. See
[map and player endpoints](operations.md#api).

## Bundle maintenance

The canonical mod is maintained in the separate `pz-console-map` repository.
This checkout ships the `v1.0.0` snapshot with its license and artwork.
`dashboard/integrations/PZConsoleMap.bundle.json` records its source commit
and file hashes. Downloads include it as `BUNDLE_SOURCE.json`.

Update the canonical repository first. From its clean, reviewed commit, use
its release helper to sync the generated snapshot into this checkout.

```powershell
.\.venv\Scripts\python.exe tools/release.py sync --dashboard C:\git\pz-console
```

The helper verifies the previous bundle before replacing it. Backend tests and
a Lua 5.1 harness cover geometry and exporter behavior. Chromium tests cover
the map and roster on desktop and mobile in English and Russian.
