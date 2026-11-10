"""Execute the shipped exporter against deterministic game and file API doubles."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXPORTER = ROOT / "dashboard/integrations/PZConsoleMap/42/media/lua/server/PZConsoleMap.lua"
HARNESS = r"""
local source, output, scenario = arg[1], arg[2], arg[3]
local now, tick, writes, players = 1000000, nil, {}, {}
local profile = "world Юникод"
function isServer() return scenario ~= "client" end
function getServerName() return profile end
function getTimestampMs() return now end
Events = {OnTick = {Add = function(callback) tick = callback end}}
local function player(name, x)
    return {
        getUsername = function() return name end,
        getX = function() return x or 123.25 end,
        getY = function() return -45.5 end,
        getZ = function() return 2 end,
    }
end
function getOnlinePlayers()
    return {size = function() return #players end,
        get = function(_, index) return players[index + 1] end}
end
local failWrite = false
function getFileWriter(filename, create, append)
    assert(create == true and append == false)
    local body = ""
    return {
        write = function(_, text)
            body = text
            if failWrite then body = "{partial"; error("fixture write failed") end
        end,
        close = function()
            writes[#writes + 1] = {name = filename, body = body}
        end,
    }
end
dofile(source)
if scenario == "client" then
    assert(tick == nil, "client must not register a file-export hook")
elseif scenario == "escaping" then
    local controls = ""
    for byte = 0, 31 do controls = controls .. string.char(byte) end
    players = {player('Maple "quoted" \\ Дмитрий ' .. controls)}
    tick(); now = now + 1; tick(); now = now + 4999; tick()
    assert(#writes == 2, "export must be throttled and resume after five seconds")
elseif scenario == "empty" then
    tick()
elseif scenario == "limit" then
    for index = 1, 70 do players[index] = player("Player " .. index) end
    tick()
elseif scenario == "budget" then
    for index = 1, 64 do players[index] = player(string.rep(string.char(1), 512)) end
    tick()
elseif scenario == "invalid" then
    players = {player("invalid", 0/0), player("infinite", math.huge), player("valid")}
    tick()
elseif scenario == "failure" then
    players = {player("still here")}
    tick(); now = now + 5000; failWrite = true; tick()
    now = now + 5000; failWrite = false; tick()
    assert(writes[1].name:match("%-a.json$"))
    assert(writes[2].name:match("%-b.json$"))
    assert(writes[3].name == writes[2].name, "retry must preserve previous complete slot")
end
for index, write in ipairs(writes) do
    local handle = assert(io.open(output .. "/" .. index .. ".json", "wb"))
    handle:write(write.body); handle:close()
    handle = assert(io.open(output .. "/" .. index .. ".name", "wb"))
    handle:write(write.name); handle:close()
end
"""


def run_exporter(tmp_path, scenario):
    binary = next(
        (path for name in ("lua5.1", "lua", "luajit") if (path := shutil.which(name))),
        None,
    )
    assert binary, "Install Lua 5.1 (lua5.1 or lua on PATH) to run the exporter checks."
    harness = tmp_path / "exporter_harness.lua"
    harness.write_text(HARNESS, encoding="utf-8")
    result = subprocess.run(
        [binary, str(harness), str(EXPORTER), str(tmp_path), scenario],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [
        (path.read_bytes(), path.with_suffix(".name").read_text(encoding="utf-8"))
        for path in sorted(tmp_path.glob("*.json"))
    ]


def test_exporter_roundtrips_unicode_controls_profile_and_alternates_slots(tmp_path):
    writes = run_exporter(tmp_path, "escaping")
    frames = [json.loads(body) for body, _ in writes]
    expected = 'Maple "quoted" \\ Дмитрий ' + "".join(map(chr, range(32)))
    assert frames[0]["players"] == [{"name": expected, "x": 123.25, "y": -45.5, "z": 2}]
    assert frames[0]["profile"] == "world Юникод"
    assert frames[0]["schemaVersion"] == 1
    assert frames[0]["capturedAt"] == 1000000
    assert frames[1]["capturedAt"] == 1005000
    assert frames[0]["sessionId"] == frames[1]["sessionId"]
    prefix = "pz-console-map-" + "world Юникод".encode().hex()
    assert [name for _, name in writes] == [prefix + "-a.json", prefix + "-b.json"]


@pytest.mark.parametrize("scenario", ["empty", "limit", "budget", "invalid"])
def test_exporter_bounds_and_valid_json(tmp_path, scenario):
    writes = run_exporter(tmp_path, scenario)
    assert len(writes) == 1
    body = writes[0][0]
    frame = json.loads(body)
    assert len(body) <= 128 * 1024
    if scenario == "empty":
        assert frame["players"] == [] and frame["truncated"] is False
    elif scenario == "limit":
        assert len(frame["players"]) == 64 and frame["truncated"] is True
    elif scenario == "budget":
        assert 0 < len(frame["players"]) < 64 and frame["truncated"] is True
    else:
        assert [player["name"] for player in frame["players"]] == ["valid"]
        assert frame["truncated"] is True


def test_failed_export_preserves_previous_slot_and_does_not_break_tick(tmp_path):
    writes = run_exporter(tmp_path, "failure")
    assert len(writes) == 3
    assert json.loads(writes[0][0])["capturedAt"] == 1000000
    assert writes[1][0] == b"{partial"
    assert json.loads(writes[2][0])["capturedAt"] == 1010000


def test_client_does_not_register_export_or_write_files(tmp_path):
    assert run_exporter(tmp_path, "client") == []
