local cmd = require("cmd")
local json = require("json")
local releases = require("releases")

-- PostInstall gives every install its own bin/php.ini. PHP reads php.ini from
-- the real binary's folder before any compiled-in path, so the file applies
-- however this install's php starts: the mise shim, an absolute path, or a
-- symlink. Nothing here may raise for a missing optional file: an error makes
-- mise delete the install.

local MARKER = "@PHP_BIN_PREFIX@"
local MISSING_NOTE = "; not bundled with this build: rebuild it with PIE (pie install <package>)"

local HEADER = [[
; php.ini for this PHP install, written by mise-php.
;
; PHP reads this file whenever this install's bin/php starts, through the mise
; shim, an absolute path, or a symlink. Edit it freely: installing a newer
; patch of the same PHP branch copies these settings forward and rewrites only
; the extension_dir line.
;
; Turn a bundled extension on or off by removing or adding the leading ";" on
; its line below. Build other extensions with PIE, for example:
;   pie install apcu/apcu
]]

local COMMON_SETTINGS = [[
; Common settings, shown at PHP's command-line defaults. Remove the leading ";"
; and change the value to override one.
;memory_limit = 128M
;upload_max_filesize = 2M
;post_max_size = 8M
;max_execution_time = 0
;max_input_vars = 1000
;date.timezone = UTC
;display_errors = On
;error_reporting = E_ALL
;opcache.enable_cli = 0
]]

local function read_file(path)
    local handle = io.open(path, "r")
    if handle == nil then
        return nil
    end

    local content = handle:read("*a")
    handle:close()
    return content
end

local function write_file(path, content)
    local handle, err = io.open(path, "w")
    if handle == nil then
        error("cannot write " .. path .. ": " .. tostring(err))
    end

    handle:write(content)
    handle:close()
end

local function file_exists(path)
    local handle = io.open(path, "r")
    if handle == nil then
        return false
    end

    handle:close()
    return true
end

local function shell_quote(value)
    return "'" .. value:gsub("'", "'\\''") .. "'"
end

local function split_lines(content)
    local lines = {}
    for line in (content:gsub("\r\n", "\n") .. "\n"):gmatch("(.-)\n") do
        table.insert(lines, line)
    end
    if lines[#lines] == "" then
        table.remove(lines)
    end

    return lines
end

-- Read share/php-bin/manifest.json. Archives published before the manifest
-- existed have none and load every module statically; they get an empty list.
local function read_manifest(root)
    local content = read_file(root .. "/share/php-bin/manifest.json")
    if content == nil then
        return { extensions = {} }
    end

    local ok, manifest = pcall(json.decode, content)
    if not ok or type(manifest) ~= "table" or type(manifest.extensions or {}) ~= "table" then
        error("php archive has an unreadable share/php-bin/manifest.json")
    end

    manifest.extensions = manifest.extensions or {}
    return manifest
end

local function directive_for(extension)
    if extension.zend then
        return "zend_extension"
    end

    return "extension"
end

-- The default php.ini line for one bundled extension, active when the release
-- turns it on by default, commented out otherwise.
local function default_lines(extension)
    local lines = {}
    local requires = extension.requires or {}
    if not extension.default and #requires > 0 then
        table.insert(lines, "; " .. extension.name .. " also needs: " .. table.concat(requires, ", "))
    end

    local prefix = extension.default and "" or ";"
    table.insert(lines, prefix .. directive_for(extension) .. "=" .. extension.name)
    return lines
end

-- Recognise extension= and zend_extension= lines, active or commented out, and
-- return the extension each one names. The value may be a bare name, a file
-- name, or a path, optionally quoted and followed by an inline comment.
local function parse_extension_line(line)
    local body = line:match("^%s*(.-)%s*$")
    local commented = false
    local uncommented = body:match("^;+%s*(.*)$")
    if uncommented ~= nil then
        commented = true
        body = uncommented
    end

    local directive, value = body:match("^([%a_]+)%s*=%s*(.-)%s*$")
    if directive ~= "extension" and directive ~= "zend_extension" then
        return nil
    end

    local quoted = value:match('^"([^"]*)"') or value:match("^'([^']*)'")
    if quoted ~= nil then
        value = quoted
    else
        value = value:gsub("%s*;.*$", "")
    end
    if value == "" then
        return nil
    end

    -- A value ending in "/" names no file; leave that line untouched.
    local base = value:match("([^/]+)$")
    if base == nil then
        return nil
    end

    local name = base:gsub("%.so$", ""):lower()
    return { commented = commented, value = value, name = name }
end

-- True when the extension a line names belongs to this install: an absolute
-- path must exist inside this install's own extension_dir, and a name must
-- resolve there. A path into another install stays unavailable, so a binary
-- built against a sibling is never loaded from here.
local function extension_available(value, extension_dir)
    if value:sub(1, 1) == "/" then
        return value:sub(1, #extension_dir + 1) == extension_dir .. "/"
            and file_exists(value)
    end

    return file_exists(extension_dir .. "/" .. value)
        or file_exists(extension_dir .. "/" .. value .. ".so")
end

-- Find the newest other install of the same PHP branch that has a php.ini.
-- mise alias links such as 8, 8.5 and latest are skipped: find -type d does
-- not follow symbolic links.
local function carry_source(root)
    local parent, own_name = root:match("^(.*)/([^/]+)$")
    local own_key = releases.parse_version(own_name)
    if parent == nil or own_key == nil then
        return nil
    end

    local ok, listing = pcall(
        cmd.exec,
        "find " .. shell_quote(parent) .. " -mindepth 1 -maxdepth 1 -type d"
    )
    if not ok or type(listing) ~= "string" then
        return nil
    end

    local best_dir, best_name, best_key = nil, nil, nil
    for dir in listing:gmatch("[^\n]+") do
        local name = dir:match("([^/]+)$")
        local key = releases.parse_version(name)
        if key ~= nil
            and name ~= own_name
            and key.major == own_key.major
            and key.minor == own_key.minor
            and file_exists(dir .. "/bin/php.ini")
            and (best_key == nil or releases.is_newer(key, best_key))
        then
            best_dir, best_name, best_key = dir, name, key
        end
    end

    if best_dir == nil then
        return nil
    end

    return { dir = best_dir, name = best_name }
end

local function fresh_ini(extension_dir, manifest)
    local lines = split_lines(HEADER)
    table.insert(lines, "")
    table.insert(lines, "; Managed by mise-php: rewritten to this install's own folder.")
    table.insert(lines, 'extension_dir = "' .. extension_dir .. '"')

    if #manifest.extensions > 0 then
        table.insert(lines, "")
        table.insert(lines, "; Bundled shared extensions")
        for _, extension in ipairs(manifest.extensions) do
            for _, line in ipairs(default_lines(extension)) do
                table.insert(lines, line)
            end
        end
    end

    table.insert(lines, "")
    for _, line in ipairs(split_lines(COMMON_SETTINGS)) do
        table.insert(lines, line)
    end

    return table.concat(lines, "\n") .. "\n"
end

-- Copy the source install's settings, point extension_dir at this install,
-- comment out any active extension this install does not have, and append the
-- defaults for bundled extensions the source never mentioned. Extension
-- binaries are never copied: one built against another install stays there.
local function carried_ini(previous, source_name, extension_dir, manifest)
    local lines = {}
    local mentioned = {}
    local managed_line = 'extension_dir = "' .. extension_dir .. '"'
    local has_extension_dir = false

    for _, line in ipairs(split_lines(previous)) do
        local extension = parse_extension_line(line)
        if line:match("^%s*extension_dir%s*=") then
            table.insert(lines, managed_line)
            has_extension_dir = true
        elseif extension ~= nil then
            mentioned[extension.name] = true
            if not extension.commented and not extension_available(extension.value, extension_dir) then
                table.insert(lines, MISSING_NOTE)
                table.insert(lines, ";" .. line:match("^%s*(.-)%s*$"))
            else
                table.insert(lines, line)
            end
        else
            table.insert(lines, line)
        end
    end

    if not has_extension_dir then
        table.insert(lines, 1, managed_line)
        table.insert(lines, 1, "; Managed by mise-php: rewritten to this install's own folder.")
    end

    local added = {}
    for _, extension in ipairs(manifest.extensions) do
        if not mentioned[extension.name:lower()] then
            for _, line in ipairs(default_lines(extension)) do
                table.insert(added, line)
            end
        end
    end
    if #added > 0 then
        table.insert(lines, "")
        table.insert(lines, "; Bundled shared extensions not in the settings carried from " .. source_name)
        for _, line in ipairs(added) do
            table.insert(lines, line)
        end
    end

    return table.concat(lines, "\n") .. "\n"
end

-- php-config and phpize ship with a placeholder where the install prefix
-- belongs, so extensions built with PIE or phpize find this install.
local function relocate_build_kit(root)
    for _, tool in ipairs({ "php-config", "phpize" }) do
        local path = root .. "/bin/" .. tool
        local content = read_file(path)
        if content ~= nil and content:find(MARKER, 1, true) ~= nil then
            local escaped = MARKER:gsub("%p", "%%%0")
            write_file(path, (content:gsub(escaped, function()
                return root
            end)))
        end
    end
end

function PLUGIN:PostInstall(ctx)
    local root = ctx.rootPath
    local ini_path = root .. "/bin/php.ini"
    local extension_dir = root .. "/lib/php/extensions"
    local manifest = read_manifest(root)

    relocate_build_kit(root)

    if file_exists(ini_path) then
        return
    end

    local source = carry_source(root)
    local ini = nil
    if source ~= nil then
        local previous = read_file(source.dir .. "/bin/php.ini")
        if previous ~= nil then
            ini = carried_ini(previous, source.name, extension_dir, manifest)
            print("php.ini settings carried forward from PHP " .. source.name)
        end
    end

    write_file(ini_path, ini or fresh_ini(extension_dir, manifest))
end
