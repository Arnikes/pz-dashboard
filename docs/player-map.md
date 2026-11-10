# Player world map

The Players workspace links the online roster to a world map, with search,
selection, pan/zoom, player/world fitting, coordinates, and a separate activity
history. Kick and Ban keep the existing confirmation and operation checks.

## Enable real positions

Download the integration from **Players** while signed in. The same package is
included in release images under `/app/integrations/PZConsoleMap` and in source
under `dashboard/integrations/PZConsoleMap`. Follow its
[installation instructions](../dashboard/integrations/PZConsoleMap/README.md):
copy the intact local mod into the game's cachedir `mods` directory, append
`\PZConsoleMap` to the active Build 42 `Mods` list, and restart through the usual
server maintenance flow. Preserve other mods; no Workshop item is required.
The panel does not install mods or restart the game automatically.

Both processes need the same game cachedir: `/project-zomboid-config` in the
supplied game image and `/data` in the panel. Base map geometry additionally
needs the game installation at `/server-files:ro`; this mount is already present
in `docker-compose.yml` and optional in the dashboard overlay. Installed active
map mods are resolved from local game/Workshop files. No remote map service or
external UI dependency is used.

XML `cellSize` is honored. For XML without a declared scale, explicitly set the
panel's `PZ_MAP_CELL_SIZE` to `256` or `300`, according to the vector map exporter,
and recreate the panel. Compiled map dimensions do not determine vector scale.
Missing or unsupported geometry remains an explicit setup/partial state; the
panel does not invent a geographic background.

## Data contract and limits

The server-only script captures online coordinates every five seconds using
the game's Lua APIs. Two alternating bounded JSON files tolerate a concurrent
read or failed write without treating partial JSON as a valid capture. The
dashboard reads them without modifying the game files. Snapshot limits are
64 players, 128 KiB, and a 96-byte UTF-8 server profile name.

RCON `players` remains the source of online membership. Names match exactly,
including spaces and Unicode. A player with no valid coordinate remains in the
roster with an unavailable location. Captures expire after 15 seconds even if
no new SSE event arrives; stale data is dated and never presented as current.
Wrong-profile, future, invalid, ambiguous, and pre-current-server-start captures
are rejected. Monitoring follows the active server profile, independent of an
inactive profile selected in the configuration editor.

Geometry is served through a separate authenticated request and cached against
the active files' identities. It is not repeated in five-second player SSE
frames. Streaming parsing, input/output limits, and a bounded cache keep large
or malformed map files from monopolizing player polling. A constrained or
partially installed map is identified explicitly.

Overlapping active XML sources keep their real geometry and show an explicit
unverified-layering warning. The preview does not guarantee that replacement map
mods compose exactly as the running game does. Detection conservatively compares
each source's world extents, so the warning can also cover disjoint cells inside
overlapping extents. Non-overlapping sources have no such warning.

RCON-only remote connections cannot supply coordinates without accessible
snapshot files. Existing online lists remain usable without the integration.
Demo positions and map geometry are illustrative and labeled as demo data.

## Verification

Isolated backend tests cover identity, freshness, invalid files, XML coordinate
scales and cache/budget behavior. A Lua 5.1 harness executes the shipped exporter
against deterministic game API doubles. Chromium fixtures exercise the map and
roster in English/Russian on desktop and mobile. These checks do not replace
acceptance against the administrator's running game server and map files.

API references: the game's [Lua global API](https://projectzomboid.com/modding/zombie/Lua/LuaManager.GlobalObject.html)
and [IsoPlayer API](https://projectzomboid.com/modding/zombie/characters/IsoPlayer.html).
The XML coordinate schema is independently documented by the maintained
[WorldEd map reader](https://github.com/Unjammer/PZ_Mapping_Tools/blob/main/WorldEd/src/editor/InGameMap/ingamemapreader.cpp);
no implementation code or game artwork is bundled from those sources.
