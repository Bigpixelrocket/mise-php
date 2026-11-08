-- The platform a hook works for, and the php-bin archive suffix that serves it.
--
-- RUNTIME describes the machine mise runs on, except while mise writes a
-- lockfile entry for another platform: then mise calls PreInstall with
-- RUNTIME.osType and RUNTIME.archType set to that platform and RUNTIME.envType
-- set to nil. On a Linux host mise sets envType to "gnu" or "musl", from its
-- libc setting (MISE_LIBC) or its own detection, so a nil envType on Linux
-- means a lockfile entry for another machine, and the host checks below never
-- run for it.

local cmd = require("cmd")

local M = {}

local OS_NAMES = {
    darwin = "macos",
    macos = "macos",
    linux = "linux",
}

local ARCH_NAMES = {
    arm64 = "aarch64",
    aarch64 = "aarch64",
    amd64 = "x86_64",
    x86_64 = "x86_64",
    x64 = "x86_64",
}

-- Platforms php-bin publishes, keyed by "<os>-<arch>" with the names above.
local TARGETS = {
    ["macos-aarch64"] = { suffix = "macos-aarch64", label = "macOS arm64" },
    ["linux-x86_64"] = { suffix = "linux-x86_64", label = "Linux x86_64" },
    ["linux-aarch64"] = { suffix = "linux-aarch64", label = "Linux aarch64" },
}

local SUPPORTED = "macOS arm64, Linux x86_64, and Linux aarch64 with glibc"

local function file_exists(path)
    local handle = io.open(path, "r")
    if handle == nil then
        return false
    end

    handle:close()
    return true
end

-- True on NixOS, which keeps no dynamic loader at /lib64 or /lib, so a
-- glibc binary built for other distributions cannot start there.
local function is_nixos()
    if file_exists("/etc/NIXOS") then
        return true
    end

    local handle = io.open("/etc/os-release", "r")
    if handle == nil then
        return false
    end

    local content = handle:read("*a") or ""
    handle:close()
    for line in content:gmatch("[^\r\n]+") do
        local id = line:match("^ID=[\"']?([%w._-]+)[\"']?%s*$")
        if id ~= nil then
            return id == "nixos"
        end
    end

    return false
end

-- Refuse a Linux host the published binaries cannot run on.
local function assert_host_runs_glibc_binaries()
    local env_type = RUNTIME.envType
    if env_type == nil then
        return
    end

    if env_type == "musl" then
        error("php binaries need glibc 2.17 or newer; this Linux system uses musl libc"
            .. " (for example Alpine Linux), which is not supported. Supported targets: " .. SUPPORTED)
    end

    if is_nixos() then
        error("php binaries are built for Linux distributions with a standard glibc loader;"
            .. " NixOS is not supported. Supported targets: " .. SUPPORTED)
    end
end

-- The machine mise runs on as "<os>-<arch>", in the names RUNTIME uses, or
-- nil when uname cannot tell.
local function host()
    local ok, output = pcall(cmd.exec, "uname -sm")
    if not ok or type(output) ~= "string" then
        return nil
    end

    local os_type, arch_type = output:match("^%s*(%S+)%s+(%S+)")
    if os_type == nil then
        return nil
    end

    return os_type:lower() .. "-" .. arch_type
end

-- True when RUNTIME names a platform other than the machine mise runs on,
-- which happens only while mise writes a lockfile entry for that platform.
local function is_lockfile_target(os_type, arch_type)
    local machine = host()
    if machine == nil then
        return false
    end

    local machine_os, machine_arch = machine:match("^(.-)%-(.+)$")
    return OS_NAMES[machine_os] ~= OS_NAMES[os_type]
        or ARCH_NAMES[machine_arch] ~= ARCH_NAMES[arch_type]
end

-- The platform this hook works for: { suffix, label }. Raises an
-- unsupported-target error for any other operating system or architecture,
-- and for a Linux host whose libc the binaries cannot use.
--
-- Returns nil instead for a platform php-bin does not build, such as Intel
-- macOS or Windows, when it is not this machine. mise writes lockfile entries
-- for those by default, and a plugin error would abort the whole lockfile, so
-- PreInstall returns no archive for them and mise leaves them out.
function M.current()
    local os_type = tostring(RUNTIME.osType)
    local arch_type = tostring(RUNTIME.archType)
    local os_name = OS_NAMES[os_type]
    local arch_name = ARCH_NAMES[arch_type]
    local target = os_name and arch_name and TARGETS[os_name .. "-" .. arch_name]

    if target == nil then
        if is_lockfile_target(os_type, arch_type) then
            return nil
        end
        error("php binaries are available only for " .. SUPPORTED
            .. "; detected OS " .. os_type .. " and architecture " .. arch_type)
    end

    if os_name == "linux" then
        assert_host_runs_glibc_binaries()
    end

    return target
end

return M
