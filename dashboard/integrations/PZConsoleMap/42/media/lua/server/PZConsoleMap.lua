-- PZ Console's optional, dedicated-server position exporter. No client commands.
if not isServer() then return end

local INTERVAL_MS = 5000
local MAX_PLAYERS = 64
local MAX_BYTES = 128 * 1024
local MAX_NAME_BYTES = 512
local lastAttempt = nil
local sessionId = nil
local nextSlot = "a"

local function finite(value)
    return type(value) == "number" and value == value
        and value ~= math.huge and value ~= -math.huge
end

local function quote(value)
    return '"' .. value:gsub('[%z\1-\31\\"]', function(character)
        if character == '"' then return '\\"' end
        if character == '\\' then return '\\\\' end
        return string.format("\\u%04x", string.byte(character))
    end) .. '"'
end

local function hex(value)
    return (value:gsub(".", function(character)
        return string.format("%02x", string.byte(character))
    end))
end

local function capture(now)
    local profile = getServerName()
    -- Keep the hex-encoded filename below filesystem component limits.
    if type(profile) ~= "string" or #profile == 0 or #profile > 96 then return end
    if not sessionId then sessionId = string.format("%.0f", now) end
    local online = getOnlinePlayers()
    local entries = {}
    local length = 0
    local truncated = false
    for index = 0, online:size() - 1 do
        if #entries >= MAX_PLAYERS then truncated = true; break end
        local player = online:get(index)
        local name = player:getUsername()
        local x, y, z = player:getX(), player:getY(), player:getZ()
        if type(name) == "string" and #name > 0 and #name <= MAX_NAME_BYTES
            and finite(x) and finite(y) and finite(z) then
            local entry = '{"name":' .. quote(name)
                .. ',"x":' .. string.format("%.9g", x)
                .. ',"y":' .. string.format("%.9g", y)
                .. ',"z":' .. string.format("%.9g", z) .. '}'
            -- Reserve enough for the bounded profile and the envelope.
            if length + #entry + 1 > MAX_BYTES - 2048 then
                truncated = true
                break
            end
            entries[#entries + 1] = entry
            length = length + #entry + 1
        else
            truncated = true
        end
    end
    local body = '{"schemaVersion":1,"profile":' .. quote(profile)
        .. ',"sessionId":' .. quote(sessionId)
        .. ',"capturedAt":' .. string.format("%.0f", now)
        .. ',"truncated":' .. tostring(truncated)
        .. ',"players":[' .. table.concat(entries, ",") .. ']}'
    if #body > MAX_BYTES then return end
    local filename = "pz-console-map-" .. hex(profile) .. "-" .. nextSlot .. ".json"
    local writer = getFileWriter(filename, true, false)
    if not writer then return end
    local written = pcall(function() writer:write(body) end)
    local closed = pcall(function() writer:close() end)
    -- A failed/partial slot never displaces the previous complete slot.
    if written and closed then nextSlot = nextSlot == "a" and "b" or "a" end
end

local function tick()
    local now = getTimestampMs()
    if not finite(now) or now < 0 then return end
    if lastAttempt and now >= lastAttempt and now - lastAttempt < INTERVAL_MS then return end
    lastAttempt = now
    -- Filesystem or individual player failures must not interrupt the game tick.
    pcall(capture, now)
end

Events.OnTick.Add(tick)
