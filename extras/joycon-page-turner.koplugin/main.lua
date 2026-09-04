local Device = require("device")
local Event = require("ui/event")
local InfoMessage = require("ui/widget/infomessage")
local InputContainer = require("ui/widget/container/inputcontainer")
local UIManager = require("ui/uimanager")
local logger = require("logger")
local ffi = require("ffi")
local lfs = require("libs/libkoreader-lfs")
local C = ffi.C

pcall(require, "ffi/posix_h")
pcall(require, "ffi/fbink_input_h")

local JoyConProbe = InputContainer:extend{
    name = "joyconprobe",
    is_doc_only = true,
}

local input_fds = {}

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
    logger.info("JoyConProbe: physical A (BtnA) -> previous page")
    self.ui:handleEvent(Event:new("GotoViewRel", -1))
    return true
end

function JoyConProbe:onJoyConNext()
    logger.info("JoyConProbe: physical X (BtnB) -> next page")
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
    -- physical A -> BtnA, X -> BtnB, B -> BtnC, Y -> BtnX.
    self.key_events.ProbeBtnA = { { "BtnA" }, event = "JoyConPrevious" }
    self.key_events.ProbeBtnB = { { "BtnB" }, event = "JoyConNext" }
    if self.ui.active_widgets then table.insert(self.ui.active_widgets, self) end
    logger.info("JoyConProbe: init")
    extendEventMap()
    self:_scan()
end

function JoyConProbe:onCloseWidget()
    for path in pairs(input_fds) do self:_detach(path) end
end

return JoyConProbe
