local ConfirmBox = require("ui/widget/confirmbox")
local Dispatcher = require("dispatcher")
local Event = require("ui/event")
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
local SCAN_POLL_SECONDS = 2
local PAIR_POLL_SECONDS = 2

local function urlEncode(value)
    return tostring(value):gsub("([^%w%-_%.~])", function(char)
        return string.format("%%%02X", string.byte(char))
    end)
end

local function connectionName(conn)
    if type(conn) ~= "table" then return "unknown device" end
    if type(conn.name) == "string" and conn.name ~= "" then return conn.name end
    if type(conn.address) == "string" and conn.address ~= "" then return conn.address end
    return "unknown device"
end

function BluetoothControl:_refreshFooter()
    UIManager:broadcastEvent(Event:new("RefreshAdditionalContent"))
end

function BluetoothControl:_cacheStatus(data)
    if not data then
        self._footer_text = "PT: ?"
        self:_refreshFooter()
        return
    end
    if not data.daemon_running then
        self._footer_text = "PT: off"
        self:_refreshFooter()
        return
    end
    local sides = {}
    local other = 0
    local connections = type(data.connections) == "table" and data.connections or {}
    for _, conn in ipairs(connections) do
        local name = connectionName(conn)
        if name:find("Joy%-Con %(R%)") then
            sides.R = true
        elseif name:find("Joy%-Con %(L%)") then
            sides.L = true
        else
            other = other + 1
        end
    end
    if sides.R and sides.L then
        self._footer_text = "PT: R+L"
    elseif sides.R then
        self._footer_text = "PT: R"
    elseif sides.L then
        self._footer_text = "PT: L"
    elseif other > 0 then
        self._footer_text = "PT: connected"
    else
        self._footer_text = "PT: --"
    end
    self:_refreshFooter()
end

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
    if not body then
        self:_cacheStatus(nil)
        return nil, err
    end
    local ok, data = pcall(rapidjson.decode, body)
    if not ok or type(data) ~= "table" then return nil, "invalid status response" end
    local trusted = data.version == "3.15.2-a4175a9"
    local capabilities = type(data.capabilities) == "table" and data.capabilities or {}
    for _, capability in ipairs(capabilities) do
        if capability == "retry_classic" then trusted = true end
    end
    if not trusted then
        self:_cacheStatus(nil)
        return nil, "untrusted API version"
    end
    self:_cacheStatus(data)
    return data
end

function BluetoothControl:showStatus()
    local data, err = self:_status()
    if not data then
        self:_show("HID API unavailable: " .. tostring(err), 5)
        return
    end
    local names = {}
    local connections = type(data.connections) == "table" and data.connections or {}
    for _, conn in ipairs(connections) do
        table.insert(names, connectionName(conn))
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
    UIManager:scheduleIn(3, function() self:_status() end)
end

function BluetoothControl:onShowHIDStatus()
    self:showStatus()
    return true
end

function BluetoothControl:onRetryPageTurnerConnection()
    self:retryConnection()
    return true
end

function BluetoothControl:onDispatcherRegisterActions()
    Dispatcher:registerAction("kindle_hid_status", {
        category = "none", event = "ShowHIDStatus",
        title = "Show HID status", general = true,
    })
    Dispatcher:registerAction("kindle_hid_retry_page_turner", {
        category = "none", event = "RetryPageTurnerConnection",
        title = "Retry page-turner connection", general = true,
    })
end

function BluetoothControl:onEvdevInputInsert()
    UIManager:scheduleIn(1, function() self:_status() end)
end

function BluetoothControl:onEvdevInputRemove()
    UIManager:scheduleIn(1, function() self:_status() end)
end

function BluetoothControl:_json(path)
    local body, err = self:_httpGet(path)
    if not body then return nil, err end
    local ok, data = pcall(rapidjson.decode, body)
    if not ok or type(data) ~= "table" then return nil, "invalid JSON response" end
    return data
end

function BluetoothControl:pairLeftJoyCon()
    local data = self:_status()
    if not data or not data.daemon_running then
        self:_show("Running API required; pairing was not started", 5)
        return
    end
    UIManager:show(ConfirmBox:new{
        text = "This pauses current page turners for one 10-second scan. Put only Joy-Con (L) in pairing mode, then continue.",
        ok_text = "Scan once",
        ok_callback = function() self:_beginLeftScan() end,
    })
end

function BluetoothControl:_beginLeftScan()
    if self._scan_poll_cb or self._pair_poll_cb then
        self:_show("A pairing operation is already active", 4)
        return
    end
    local _, err = self:_httpGet("/scan")
    if err then
        self:_show("Scan failed to start: " .. tostring(err), 5)
        return
    end
    self:_show("Scanning once for Joy-Con (L)…", 4)
    self:_pollLeftScan(0)
end

function BluetoothControl:_pollLeftScan(tick)
    self._scan_poll_cb = function()
        self._scan_poll_cb = nil
        local data, err = self:_json("/scan-status")
        if not data then
            self:_show("Scan result unavailable: " .. tostring(err), 5)
            return
        end
        if data.scanning then
            if tick >= 10 then
                self:_show("Scan did not finish; pairing was not attempted", 5)
                return
            end
            self:_pollLeftScan(tick + 1)
            return
        end
        local matches = {}
        local devices = type(data.devices) == "table" and data.devices or {}
        for _, dev in ipairs(devices) do
            if type(dev) == "table" and dev.name == "Joy-Con (L)"
                    and type(dev.address) == "string"
                    and (dev.protocol == nil or dev.protocol == "classic") then
                table.insert(matches, dev)
            end
        end
        if #matches ~= 1 then
            self:_show("Expected exactly one Joy-Con (L); found " .. tostring(#matches)
                .. ". Pairing was not attempted.", 6)
            return
        end
        self:_beginLeftPair(matches[1])
    end
    UIManager:scheduleIn(SCAN_POLL_SECONDS, self._scan_poll_cb)
end

function BluetoothControl:_beginLeftPair(device)
    local url = "/pair?addr=" .. urlEncode(device.address)
        .. "&protocol=classic&name=" .. urlEncode("Joy-Con (L)")
    local _, err = self:_httpGet(url)
    if err then
        self:_show("Pairing failed to start: " .. tostring(err), 5)
        return
    end
    self:_show("Pairing Joy-Con (L)…", 4)
    self:_pollLeftPair(0)
end

function BluetoothControl:_pollLeftPair(tick)
    self._pair_poll_cb = function()
        self._pair_poll_cb = nil
        local data, err = self:_json("/pair-status")
        if not data then
            self:_show("Pair result unavailable: " .. tostring(err), 5)
            return
        end
        if data.pairing then
            if tick >= 30 then
                self:_show("Pairing timed out; it was not retried", 5)
                return
            end
            self:_pollLeftPair(tick + 1)
            return
        end
        if data.ok then
            self:_show("Joy-Con (L) paired: " .. tostring(data.address or ""), 6)
            UIManager:scheduleIn(2, function() self:_status() end)
        else
            self:_show("Joy-Con (L) pairing failed; it was not retried", 6)
        end
    end
    UIManager:scheduleIn(PAIR_POLL_SECONDS, self._pair_poll_cb)
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
    self:onDispatcherRegisterActions()
    self._footer_text = "PT: ?"
    self._footer_content_func = function() return self._footer_text end
    if self.ui.view and self.ui.view.footer then
        self.ui.view.footer:addAdditionalFooterContent(self._footer_content_func)
        UIManager:scheduleIn(1, function() self:_status() end)
    end
end

function BluetoothControl:onCloseWidget()
    if self._scan_poll_cb then UIManager:unschedule(self._scan_poll_cb) end
    if self._pair_poll_cb then UIManager:unschedule(self._pair_poll_cb) end
    self._scan_poll_cb = nil
    self._pair_poll_cb = nil
    if self.ui.view and self.ui.view.footer and self._footer_content_func then
        self.ui.view.footer:removeAdditionalFooterContent(self._footer_content_func)
    end
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
                text = "Pair Joy-Con (L)…", keep_menu_open = true,
                callback = function() self:pairLeftJoyCon() end,
            },
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
