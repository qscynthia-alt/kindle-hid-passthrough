local ConfirmBox = require("ui/widget/confirmbox")
local InfoMessage = require("ui/widget/infomessage")
local InputContainer = require("ui/widget/container/inputcontainer")
local UIManager = require("ui/uimanager")
local http = require("socket.http")
local ltn12 = require("ltn12")
local rapidjson = require("rapidjson")
local socket = require("socket")

local BluetoothControl = InputContainer:extend{
    name = "kindlebtcontrol",
    is_doc_only = false,
}

local API_URL = "http://127.0.0.1:8321"
local API_TIMEOUT = 2

function BluetoothControl:_show(text, timeout)
    UIManager:show(InfoMessage:new{ text = text, timeout = timeout or 4 })
end

function BluetoothControl:_httpGet(path)
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

function BluetoothControl:_status()
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

function BluetoothControl:showStatus()
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

function BluetoothControl:_verifyLater(wanted, success_text)
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

function BluetoothControl:startExistingApi()
    local data = self:_status()
    if not data then
        self:_show("API unavailable; fresh creation is refused in KOReader", 6)
        return
    end
    if data.daemon_running then
        self:_show("HID daemon is already running")
        return
    end
    local _, err = self:_httpGet("/start")
    if err then
        self:_show("Start result uncertain; not retried", 5)
        return
    end
    self:_verifyLater(true, "Existing API daemon started")
end

function BluetoothControl:retryConnection()
    local data = self:_status()
    if not data or not data.daemon_running then
        self:_show("Running API required; nothing was changed", 5)
        return
    end
    local body, err = self:_httpGet("/retry-classic")
    if err or not body or not body:find('"ok"%s*:%s*true') then
        self:_show("Connection retry was not accepted; nothing else was reset", 5)
        return
    end
    self:_show("Bluetooth page-turner connection retry requested")
end

function BluetoothControl:stopExistingApi()
    local data = self:_status()
    if not data then
        self:_show("API unavailable; nothing was changed")
        return
    end
    if not data.daemon_running then
        self:_show("HID daemon is already parked")
        return
    end
    local _, err = self:_httpGet("/stop")
    if err then
        self:_show("Stop result uncertain; not retried", 5)
        return
    end
    self:_verifyLater(false, "Existing API daemon parked")
end

function BluetoothControl:restartExistingApi()
    local data = self:_status()
    if not data then
        self:_show("API unavailable; fresh creation is refused in KOReader", 6)
        return
    end
    if data.daemon_running then
        local _, err = self:_httpGet("/stop")
        if err then
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
        local _, err = self:_httpGet("/start")
        if err then
            self:_show("Start result uncertain; not retried", 5)
            return
        end
        self:_verifyLater(true, "Existing API daemon restarted")
    end)
end

function BluetoothControl:saveSnapshot()
    self:_show("Saving incident snapshot…", 3)
    UIManager:scheduleIn(0.1, function()
        local result = os.execute(
            "/bin/sh /mnt/us/Kindle_Tools/scripts/hid-incident-snapshot.sh >/dev/null 2>&1")
        if result == 0 or result == true then
            self:_show("Incident snapshot saved")
        else
            self:_show("Snapshot failed; no Bluetooth action was run", 5)
        end
    end)
end

function BluetoothControl:init()
    if self.ui.menu then self.ui.menu:registerToMainMenu(self) end
end

function BluetoothControl:addToMainMenu(menu_items)
    menu_items.kindle_bt_control = {
        text = "Bluetooth / HID Control",
        sorting_hint = "network",
        sub_item_table = {
            { text = "Show HID status", keep_menu_open = true,
              callback = function() self:showStatus() end },
            { text = "Start existing API daemon", keep_menu_open = true,
              callback = function() self:startExistingApi() end },
            { text = "Retry page-turner connection", keep_menu_open = true,
              callback = function() self:retryConnection() end },
            {
                text = "Stop existing API daemon…", keep_menu_open = true,
                callback = function()
                    UIManager:show(ConfirmBox:new{
                        text = "Park the current HID daemon and disconnect page turners?",
                        ok_text = "Stop",
                        ok_callback = function() self:stopExistingApi() end,
                    })
                end,
            },
            {
                text = "Restart existing API daemon…", keep_menu_open = true,
                callback = function()
                    UIManager:show(ConfirmBox:new{
                        text = "Restart only the already-resident API daemon? This briefly disconnects page turners.",
                        ok_text = "Restart",
                        ok_callback = function() self:restartExistingApi() end,
                    })
                end,
            },
            { text = "Save incident snapshot", keep_menu_open = true, separator = true,
              callback = function() self:saveSnapshot() end },
            { text = "Safety limits", keep_menu_open = true,
              callback = function()
                  self:_show("This menu never creates the HID API, pairs devices, runs Full Stop, changes native Bluetooth, or reboots.", 8)
              end },
        },
    }
end

return BluetoothControl
