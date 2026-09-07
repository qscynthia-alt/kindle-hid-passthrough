local Device = require("device")
local Event = require("ui/event")
local ConfirmBox = require("ui/widget/confirmbox")
local InfoMessage = require("ui/widget/infomessage")
local InputContainer = require("ui/widget/container/inputcontainer")
local UIManager = require("ui/uimanager")
local logger = require("logger")
local ffi = require("ffi")
local lfs = require("libs/libkoreader-lfs")
local C = ffi.C
local http = require("socket.http")
local ltn12 = require("ltn12")
local rapidjson = require("rapidjson")
local socket = require("socket")

pcall(require, "ffi/posix_h")
pcall(require, "ffi/fbink_input_h")

local JoyConProbe = InputContainer:extend{
    name = "joyconprobe",
    is_doc_only = true,
}

local input_fds = {}
local API_URL = "http://127.0.0.1:8321"
local API_TIMEOUT = 2

local button_names = {
    "Btn0", "Btn1", "Btn2", "Btn3", "Btn4", "Btn5", "Btn6", "Btn7", "Btn8", "Btn9",
    "BtnA", "BtnB", "BtnC", "BtnX", "BtnY", "BtnZ",
    "BtnTL", "BtnTR", "BtnTL2", "BtnTR2", "BtnSelect", "BtnStart", "BtnMode",
    "BtnThumbL", "BtnThumbR", "BtnDpadUp", "BtnDpadDown", "BtnDpadLeft", "BtnDpadRight",
}

local event_codes = {
    [256] = "Btn0", [257] = "Btn1", [258] = "Btn2", [259] = "Btn3", [260] = "Btn4",
    [261] = "Btn5", [262] = "Btn6", [263] = "Btn7", [264] = "Btn8", [265] = "Btn9",
    [304] = "BtnA", [305] = "BtnB", [306] = "BtnC", [307] = "BtnX", [308] = "BtnY",
    [309] = "BtnZ", [310] = "BtnTL", [311] = "BtnTR", [312] = "BtnTL2", [313] = "BtnTR2",
    [314] = "BtnSelect", [315] = "BtnStart", [316] = "BtnMode", [317] = "BtnThumbL",
    [318] = "BtnThumbR", [544] = "BtnDpadUp", [545] = "BtnDpadDown",
    [546] = "BtnDpadLeft", [547] = "BtnDpadRight",
}

local function extendEventMap()
    local map = Device.input and Device.input.event_map
    if not map then return end
    for code, name in pairs(event_codes) do
        if map[code] == nil then map[code] = name end
    end
end

function JoyConProbe:_attach(path)
    if input_fds[path] then return end
    if Device.input.opened_devices[path] then return end
    local FBInkInput = ffi.loadlib("fbink_input", 1)
    local dev = FBInkInput.fbink_input_check(path, C.INPUT_JOYSTICK, 0, 0)
    if dev == nil then return end
    local matched = dev.matched
    local fd = tonumber(dev.fd)
    local real_path = ffi.string(dev.path)
    local name = ffi.string(dev.name)
    C.free(dev)
    if not matched then return end
    local lname = name:lower()
    if not lname:find("joy%-con") and not lname:find("wireless gamepad") then
        C.close(fd)
        return
    end
    input_fds[real_path] = Device.input:fdopen(fd, real_path, name)
    extendEventMap()
    logger.info("JoyConProbe: attached", name, real_path)
end

function JoyConProbe:_detach(path)
    if not input_fds[path] then return end
    Device.input:close(path)
    input_fds[path] = nil
end

function JoyConProbe:_scan()
    for name in lfs.dir("/dev/input") do
        if name:match("^event%d+$") then self:_attach("/dev/input/" .. name) end
    end
end

function JoyConProbe:_httpGet(path)
    local chunks = {}
    local saved_timeout = http.TIMEOUT
    http.TIMEOUT = API_TIMEOUT
    local ok, code = http.request{
        url = API_URL .. path,
        sink = ltn12.sink.table(chunks),
        create = function()
            local client = socket.tcp()
            client:settimeout(API_TIMEOUT)
            return client
        end,
    }
    http.TIMEOUT = saved_timeout
    if not ok then return nil, tostring(code) end
    if code ~= 200 then return nil, "HTTP " .. tostring(code) end
    return table.concat(chunks)
end

function JoyConProbe:_status()
    local body, err = self:_httpGet("/status")
    if not body then return nil, err end
    local ok, data = pcall(rapidjson.decode, body)
    if not ok or type(data) ~= "table" then return nil, "invalid status response" end
    local trusted = data.version == "3.15.2-a4175a9"
    for _, capability in ipairs(data.capabilities or {}) do
        if capability == "retry_classic" then trusted = true end
    end
    if not trusted then return nil, "untrusted API version" end
    return data
end

function JoyConProbe:_show(text, timeout)
    UIManager:show(InfoMessage:new{ text = text, timeout = timeout or 4 })
end

function JoyConProbe:showKindleToolsStatus()
    local data, err = self:_status()
    if not data then
        self:_show("HID API unavailable: " .. tostring(err), 5)
        return
    end
    local names = {}
    for _, conn in ipairs(data.connections or {}) do
        table.insert(names, conn.name or conn.address or "unknown device")
    end
    local state = data.daemon_running and "RUNNING" or "PARKED"
    local connected = #names > 0 and table.concat(names, ", ") or "no device connected"
    self:_show(data.version .. "; " .. state .. "; " .. connected, 6)
end

function JoyConProbe:_verifyLater(wanted, success_text)
    UIManager:scheduleIn(2, function()
        local data, err = self:_status()
        if not data then
            self:_show("Result uncertain: " .. tostring(err), 5)
        elseif data.daemon_running == wanted then
            self:_show(success_text)
        else
            self:_show("Requested state was not verified; not retried", 5)
        end
    end)
end

function JoyConProbe:startExistingApi()
    local data, err = self:_status()
    if not data then
        self:_show("API unavailable; fresh creation is refused in KOReader", 6)
        return
    end
    if data.daemon_running then
        self:_show("HID daemon is already running")
        return
    end
    local _, request_err = self:_httpGet("/start")
    if request_err then
        self:_show("Start result uncertain; not retried", 5)
        return
    end
    self:_verifyLater(true, "Existing API daemon started")
end

function JoyConProbe:retryJoyConConnection()
    local data = self:_status()
    if not data or not data.daemon_running then
        self:_show("Running API required; nothing was changed", 5)
        return
    end
    local body, request_err = self:_httpGet("/retry-classic")
    if request_err or not body or not body:find('"ok"%s*:%s*true') then
        self:_show("Classic retry was not accepted; nothing else was reset", 5)
        return
    end
    self:_show("Joy-Con connection retry requested")
end

function JoyConProbe:stopExistingApi()
    local data = self:_status()
    if not data then
        self:_show("API unavailable; nothing was changed")
        return
    end
    if not data.daemon_running then
        self:_show("HID daemon is already parked")
        return
    end
    local _, request_err = self:_httpGet("/stop")
    if request_err then
        self:_show("Stop result uncertain; not retried", 5)
        return
    end
    self:_verifyLater(false, "Existing API daemon parked")
end

function JoyConProbe:restartExistingApi()
    local data = self:_status()
    if not data then
        self:_show("API unavailable; fresh creation is refused in KOReader", 6)
        return
    end
    if data.daemon_running then
        local _, request_err = self:_httpGet("/stop")
        if request_err then
            self:_show("Stop result uncertain; start refused", 5)
            return
        end
    end
    UIManager:scheduleIn(2, function()
        local parked = self:_status()
        if not parked or parked.daemon_running then
            self:_show("Parked state not verified; start refused", 5)
            return
        end
        local _, start_err = self:_httpGet("/start")
        if start_err then
            self:_show("Start result uncertain; not retried", 5)
            return
        end
        self:_verifyLater(true, "Existing API daemon restarted")
    end)
end

function JoyConProbe:saveIncidentSnapshot()
    local command = "/bin/sh /mnt/us/Kindle_Tools/scripts/hid-incident-snapshot.sh >/dev/null 2>&1"
    self:_show("Saving incident snapshot…", 3)
    UIManager:scheduleIn(0.1, function()
        local result = os.execute(command)
        if result == 0 or result == true then
            self:_show("Incident snapshot saved")
        else
            self:_show("Snapshot failed; no Bluetooth action was run", 5)
        end
    end)
end

function JoyConProbe:onEvdevInputInsert(path)
    UIManager:scheduleIn(1, function() self:_attach(path) end)
end

function JoyConProbe:onEvdevInputRemove(path)
    UIManager:scheduleIn(1, function() self:_detach(path) end)
end

function JoyConProbe:onJoyConButton(name)
    logger.info("JoyConProbe: button", name)
    UIManager:show(InfoMessage:new{ text = "Joy-Con: " .. name, timeout = 2 })
    return true
end

function JoyConProbe:onJoyConPrevious()
    logger.info("JoyConProbe: previous page")
    self.ui:handleEvent(Event:new("GotoViewRel", -1))
    return true
end

function JoyConProbe:onJoyConNext()
    logger.info("JoyConProbe: next page")
    self.ui:handleEvent(Event:new("GotoViewRel", 1))
    return true
end

function JoyConProbe:init()
    self.key_events = {}
    for _, name in ipairs(button_names) do
        self.key_events["Probe" .. name] = {
            { name }, event = "JoyConButton", args = name,
        }
    end
    -- Verified on this Joy-Con/HID descriptor:
    -- Verified descriptor: physical A -> BtnA, X -> BtnB,
    -- B -> BtnC, Y -> BtnX. Pair both sides for button wear sharing.
    self.key_events.ProbeBtnA = { { "BtnA" }, event = "JoyConPrevious" }
    self.key_events.ProbeBtnB = { { "BtnB" }, event = "JoyConNext" }
    self.key_events.ProbeBtnC = { { "BtnC" }, event = "JoyConPrevious" }
    self.key_events.ProbeBtnX = { { "BtnX" }, event = "JoyConNext" }
    self.key_events.ProbeBtnThumbR = { { "BtnThumbR" }, event = "JoyConNext" }
    self.key_events.ProbeBtnY = { { "BtnY" }, event = "JoyConNext" }
    self.key_events.ProbeBtnZ = { { "BtnZ" }, event = "JoyConNext" }
    self.key_events.ProbeBtnTL2 = { { "BtnTL2" }, event = "JoyConNext" }
    self.key_events.ProbeBtnThumbL = { { "BtnThumbL" }, event = "JoyConNext" }
    self.key_events.ProbeBtnMode = { { "BtnMode" }, event = "JoyConNext" }
    if self.ui.active_widgets then table.insert(self.ui.active_widgets, self) end
    logger.info("JoyConProbe: init")
    extendEventMap()
    self:_scan()
end

function JoyConProbe:addToMainMenu(menu_items)
    menu_items.joycon_kindle_tools = {
        text = "Joy-Con & Kindle Tools",
        sorting_hint = "network",
        sub_item_table = {
            {
                text = "Show HID status",
                keep_menu_open = true,
                callback = function() self:showKindleToolsStatus() end,
            },
            {
                text = "Start existing API daemon",
                keep_menu_open = true,
                callback = function() self:startExistingApi() end,
            },
            {
                text = "Retry Joy-Con connection",
                keep_menu_open = true,
                callback = function() self:retryJoyConConnection() end,
            },
            {
                text = "Stop existing API daemon…",
                keep_menu_open = true,
                callback = function()
                    UIManager:show(ConfirmBox:new{
                        text = "Park the current HID daemon and disconnect page turners?",
                        ok_text = "Stop",
                        ok_callback = function() self:stopExistingApi() end,
                    })
                end,
            },
            {
                text = "Restart existing API daemon…",
                keep_menu_open = true,
                callback = function()
                    UIManager:show(ConfirmBox:new{
                        text = "Restart only the already-resident API daemon? This will briefly disconnect page turners.",
                        ok_text = "Restart",
                        ok_callback = function() self:restartExistingApi() end,
                    })
                end,
            },
            {
                text = "Save incident snapshot",
                keep_menu_open = true,
                separator = true,
                callback = function() self:saveIncidentSnapshot() end,
            },
            {
                text = "Safety limits",
                keep_menu_open = true,
                callback = function()
                    self:_show("KOReader never creates the HID API, pairs devices, runs Full Stop, or changes native Bluetooth.", 7)
                end,
            },
        },
    }
end

function JoyConProbe:onCloseWidget()
    for path in pairs(input_fds) do self:_detach(path) end
end

return JoyConProbe
