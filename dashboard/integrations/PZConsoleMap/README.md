# PZ Console Player Map integration (Build 42)

This optional local mod supplies real player positions to PZ Console. Its only
Lua script runs on the dedicated server, reads the online player API and writes
two small JSON snapshots in the game's cachedir `Lua` directory every five
seconds. It has no client/shared Lua, RCON commands, network listener, or gameplay
changes. The dashboard continues to use RCON `players` for online membership.

## Install once

1. Keep the extracted `PZConsoleMap` directory intact, including `common/` and
   `42/`. Copy it into the game server's **cachedir** `mods` directory, beside its
   `Server`, `Saves`, and `Lua` directories. The result must be
   `<cachedir>/mods/PZConsoleMap/42/mod.info`.
2. Stop the server through its usual maintenance flow. Append `\PZConsoleMap` to
   the existing `Mods` setting of the **active** Build 42 server profile, preserving
   other IDs and their order. For example, `Mods=\ExistingMod;\PZConsoleMap`.
   This is a local mod: it has no Workshop ID to put in `WorkshopItems`.
   If your game image owns `Mods` through an environment variable, update that
   variable instead of the generated INI file.
3. Start the server. The dashboard and game must share the same cachedir volume
   (`/data` in the dashboard). Open **Players**; positions should arrive after
   the first game tick and the next dashboard refresh.

The supplied new-server Compose example uses `/project-zomboid-config` as the
game cachedir. After extracting the download beside your terminal, copying the
mod into that example is:

```bash
docker exec pzserver mkdir -p /project-zomboid-config/mods
docker cp PZConsoleMap pzserver:/project-zomboid-config/mods/PZConsoleMap
```

Use your actual container and cachedir if they differ. Copy only this integration;
do not replace an existing `mods` directory. Multiplayer mod availability checks
belong to the game: if joining clients require the local mod, give them this same
directory for their own `Zomboid/mods` folder. The script exits on clients.

## Base map and freshness

For real terrain outlines, mount the game installation read-only at
`/server-files` in the dashboard (or set `SERVER_FILES_DIR`). The panel reads
`media/maps/<active Map>/worldmap.xml` and available active mod map files. It does
not download map tiles or redistribute game map assets.

Vector XML declaring `cellSize="256"` or `cellSize="300"` carries its coordinate
scale. If your XML does not declare it, set the **dashboard** environment variable
`PZ_MAP_CELL_SIZE=256` or `PZ_MAP_CELL_SIZE=300` to the scale used by that map's
exporter, then recreate the dashboard. Do not infer this value from compiled map
files. Without a confirmed scale the panel reports the missing setup rather
than placing terrain at guessed coordinates.

Snapshots expire after 15 seconds. Stale positions retain their date and lose
their current status. Missing, truncated, invalid, or mismatched data is shown
explicitly. Only exact online usernames can appear as current markers. Server
profile names are limited to 96 UTF-8 bytes for portable snapshot filenames.
RCON-only remote connections need shared snapshot/map files for this feature.

## Troubleshooting and removal

- **Integration missing:** verify the mod ID, active profile, directory layout,
  cachedir mount, and the game's permission to write JSON files under `Lua`.
- **Profile unavailable/mismatch:** set the dashboard's `PZ_CONFIG_FILE` to the
  active INI filename if startup detection cannot identify it. Changing the
  profile selected for configuration editing does not change the running world.
- **Stale:** check that the game is ticking and the snapshot files are being
  updated. A stopped/paused game or unavailable shared volume cannot supply
  current locations; the panel never replays an old capture as current.
- **Map unavailable/partial:** check `SERVER_FILES_DIR`, active map/mod
  installation, XML scale, and the map's supported XML schema/size limits.
- **Unverified mod map layering:** active XML source extents overlap. Real
  geometry remains visible, but replacement-map composition has not been verified
  against the running game; the preview can contain overlapping outlines.
- **Some positions missing:** a character may not have a valid position yet, an
  online list may have changed between captures, or the 64-player/128 KiB snapshot
  bound may have been reached. Other roster actions continue to work.

To remove the integration, stop the game, remove only `PZConsoleMap` from the
active `Mods` list, and remove its local mod directory. Its optional snapshot
files have names `pz-console-map-<hex-encoded-profile>-a.json` and `-b.json` in
cachedir `Lua`; they contain player names/coordinates and may be deleted after
the mod is disabled. No world save is modified by this integration.
