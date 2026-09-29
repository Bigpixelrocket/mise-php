local cmd = require("cmd")
local ini = require("ini")
local json = require("json")
local releases = require("releases")

-- PostInstall gives every install its own bin/php.ini. PHP reads php.ini from
-- the real binary's folder before any compiled-in path, so the file applies
-- however this install's php starts: the mise shim, an absolute path, or a
-- symlink. Nothing here may raise for a missing optional file: an error makes
-- mise delete the install.

local MARKER = "@PHP_BIN_PREFIX@"

-- Notes mise-php writes directly above an extension line it changed. Each one
-- names the extension of that line, so the next carry-forward pairs a note
-- only with a line for the same extension and never takes over a line of the
-- user's that ends up below a note whose own line was removed. The next
-- carry-forward recognises each note by its exact wording, so a published
-- note never changes: installs in the field hold it.
local MISSING_NOTE = " is not bundled with this build: rebuild it with PIE (pie install <package>)"
local BUILTIN_NOTE = " is built into this PHP binary, so it needs no extension line here"
local DUPLICATE_NOTE = " is already enabled by another line in this file: PHP warns when it loads one twice"
-- Written above a commented line whose extension another line already enables.
local SHADOW_NOTE = " is enabled by another line in this file: keep this one commented"

-- Each note's kind: "off" means mise-php commented out the active line below
-- it, "shadow" that the commented line below it must stay commented.
local NOTE_KINDS = {
    { suffix = MISSING_NOTE, kind = "off" },
    { suffix = BUILTIN_NOTE, kind = "off" },
    { suffix = DUPLICATE_NOTE, kind = "off" },
    { suffix = SHADOW_NOTE, kind = "shadow" },
}

-- Notes published before they named their extension, by exact text.
local UNNAMED_NOTES = {
    ["; not bundled with this build: rebuild it with PIE (pie install <package>)"] = "off",
    ["; built into this PHP binary, so it needs no extension line here"] = "off",
    ["; already enabled by another line in this file: PHP warns when it loads one twice"] = "off",
    ["; enabled by another line in this file: keep this one commented"] = "shadow",
}

-- Sits above the extension_dir line mise-php rewrites on every carry-forward.
local MANAGED_COMMENT = "; Managed by mise-php: rewritten to this install's own folder."
-- Heads the default lines for the extensions bundled with a release.
local BUNDLED_HEADER = "; Bundled shared extensions"
-- Headed the defaults a carry-forward added before new defaults joined
-- BUNDLED_HEADER instead. Each named the install it carried from, which went
-- stale on the next upgrade.
local CARRIED_HEADER_PREFIX = "; Bundled shared extensions not in the settings carried from "

local HEADER = [[
; php.ini for this PHP install, written by mise-php.
;
; PHP reads this file whenever this install's bin/php starts, through the mise
; shim, an absolute path, or a symlink. Edit it freely: installing a newer
; patch of the same PHP branch copies these settings forward, rewrites the
; extension_dir line, and comments out extensions the new install lacks.
;
; Turn a bundled extension on or off by removing or adding the leading ";" on
; its line below. Build other extensions with PIE, for example:
;   pie install apcu/apcu
; PIE adds its own extension line. Keep only one line per extension active:
; PHP warns when it loads an extension twice.
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
-- return the extension each one names. PHP matches both directive names in any
-- case. The value may be a bare name, a file name, or a path, optionally quoted
-- and followed by an inline comment.
local function parse_extension_line(line)
    local body = line:match("^%s*(.-)%s*$")
    local commented = false
    local uncommented = body:match("^;+%s*(.*)$")
    if uncommented ~= nil then
        commented = true
        body = uncommented
    end

    local directive, value = body:match("^([%a_]+)%s*=%s*(.-)%s*$")
    directive = directive and directive:lower()
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
-- built against a sibling is never loaded from here; a ".." segment could
-- climb out of extension_dir, so it never counts as available.
local function extension_available(value, extension_dir)
    if ("/" .. value .. "/"):find("/%.%./") ~= nil then
        return false
    end

    if value:sub(1, 1) == "/" then
        return value:sub(1, #extension_dir + 1) == extension_dir .. "/"
            and file_exists(value)
    end

    return file_exists(extension_dir .. "/" .. value)
        or file_exists(extension_dir .. "/" .. value .. ".so")
end

-- The modules compiled into this install's php, lower-cased, or an empty set
-- when php cannot report them. Archives without a manifest compile in every
-- module, so an extension that is shared elsewhere can be built in here.
local function builtin_modules(root)
    local modules = {}
    local ok, output = pcall(cmd.exec, shell_quote(root .. "/bin/php") .. " -n -m")
    if not ok or type(output) ~= "string" then
        return modules
    end

    for line in output:gmatch("[^\r\n]+") do
        local name = line:match("^%s*(.-)%s*$"):lower()
        if name ~= "" and name:sub(1, 1) ~= "[" then
            modules[name] = true
            -- php -m names the OPcache module "Zend OPcache".
            modules[(name:gsub("^zend ", ""))] = true
        end
    end

    return modules
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
    table.insert(lines, MANAGED_COMMENT)
    table.insert(lines, 'extension_dir = "' .. extension_dir .. '"')

    if #manifest.extensions > 0 then
        table.insert(lines, "")
        table.insert(lines, BUNDLED_HEADER)
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

-- The note a line holds, as { kind = "off" or "shadow", name = the extension
-- it names }, or nil. A note published before notes named their extension
-- has no name.
local function parse_note(line)
    local kind = UNNAMED_NOTES[line]
    if kind ~= nil then
        return { kind = kind }
    end
    if line:sub(1, 2) ~= "; " then
        return nil
    end

    for _, note in ipairs(NOTE_KINDS) do
        local name_length = #line - 2 - #note.suffix
        if name_length > 0 and line:sub(-#note.suffix) == note.suffix then
            return { kind = note.kind, name = line:sub(3, 2 + name_length) }
        end
    end

    return nil
end

local function note_for(extension, suffix)
    return "; " .. extension.name .. suffix
end

-- The extension of a line in exactly the form mise-php gives an extension
-- line it turns off: ";" directly followed by the text of the active line.
-- Any other commented line, such as "; extension=x" or ";;extension=x", was
-- commented by someone else.
local function turned_off_extension(line)
    local text = line:match("^;([^;%s].*)$")
    local extension = text and parse_extension_line(text)
    if extension == nil or extension.commented then
        return nil
    end

    return extension
end

-- Split the source php.ini into items. A note mise-php wrote forms one item
-- with the line below it when that line is still its own: for a note that
-- turned a line off, the exact commented form of an active line, and for a
-- note that names an extension, a line for that extension. Its state is then
-- decided again for the new install instead of stacking notes on every
-- upgrade. A note without its own line below, because that line was removed,
-- rewritten, or made active again, is dropped, and whatever line follows it
-- stays as it is. Lines inside a value that spans several lines are kept as
-- they are and never read as notes or settings.
local function parse_items(previous)
    local lines = split_lines(previous)
    local scan = ini.scan(lines)
    local function plain(index)
        return not scan.continued[index] and not scan.opens[index]
    end

    local items = {}
    local index = 1
    while index <= #lines do
        local line = lines[index]
        local note = plain(index) and parse_note(line)
        if note then
            local below = lines[index + 1]
            local own = nil
            if below ~= nil and plain(index + 1) then
                if note.kind == "off" then
                    own = turned_off_extension(below)
                else
                    own = parse_extension_line(below)
                    if own ~= nil and not own.commented then
                        own = nil
                    end
                end
            end
            if own ~= nil and note.name ~= nil and own.name ~= note.name then
                own = nil
            end

            if own ~= nil then
                table.insert(items, {
                    line = below,
                    extension = parse_extension_line(below),
                    off = note.kind == "off",
                })
                index = index + 2
            else
                index = index + 1
            end
        else
            table.insert(items, {
                line = line,
                extension = plain(index) and parse_extension_line(line) or nil,
                value = not plain(index),
                section = scan.section[index],
            })
            index = index + 1
        end
    end

    return items
end

-- The line text without mise-php's leading ";", as the user last had it active.
local function restored_line(line)
    return (line:match("^%s*(.-)%s*$"):gsub("^;%s*", "", 1))
end

-- PIE writes these two comments above each extension line it adds.
local function is_pie_comment(item)
    return item ~= nil
        and not item.value
        and (item.line:match("^; PIE automatically added this to enable the .* extension$") ~= nil
            or item.line:match("^; priority=%-?%d+$") ~= nil)
end

local function is_blank(item)
    return item ~= nil and not item.value and item.line:match("^%s*$") ~= nil
end

-- True when two extension values name the same file for an install: the same
-- text, or the same bare name with or without ".so", which PHP looks up in
-- extension_dir either way.
local function same_target(a, b)
    if a == b then
        return true
    end
    if a:find("/", 1, true) ~= nil or b:find("/", 1, true) ~= nil then
        return false
    end

    return (a:gsub("%.so$", ""):lower()) == (b:gsub("%.so$", ""):lower())
end

-- Drop an item mise-php turned off once an active line for the same extension
-- supersedes it: one that loads the extension in this install, or one that
-- names the same file. That happens after pie install rebuilds an extension an
-- upgrade had commented out: PIE never sees commented lines and adds a line of
-- its own. An active line that loads nothing here, such as a path into another
-- install, never supersedes a line that names another file: that line may
-- load in a later install, so both stay and each keeps its note. The comments
-- PIE wrote above a dropped line go with it, and so does one of the blank
-- lines around it, so dropped lines leave no gap behind.
local function drop_superseded(items, extension_dir)
    local active = {}
    for _, item in ipairs(items) do
        local extension = item.extension
        if extension ~= nil and not extension.commented and not item.off then
            active[extension.name] = active[extension.name] or {}
            table.insert(active[extension.name], extension.value)
        end
    end

    local function superseded(item)
        local restored = parse_extension_line(restored_line(item.line))
        for _, value in ipairs(active[item.extension.name] or {}) do
            if extension_available(value, extension_dir)
                or (restored ~= nil and same_target(value, restored.value))
            then
                return true
            end
        end
        return false
    end

    local kept = {}
    local dropped = false
    for _, item in ipairs(items) do
        if item.off and superseded(item) then
            local pie_comments = false
            while is_pie_comment(kept[#kept]) do
                table.remove(kept)
                pie_comments = true
            end
            if pie_comments and is_blank(kept[#kept]) then
                table.remove(kept)
            end
            dropped = true
        elseif dropped and is_blank(item) and (#kept == 0 or is_blank(kept[#kept])) then
            -- A blank line that would double the one before the dropped lines.
        else
            table.insert(kept, item)
            dropped = false
        end
    end
    if dropped and is_blank(kept[#kept]) then
        table.remove(kept)
    end

    return kept
end

-- Copy the source install's settings, point extension_dir at this install,
-- and keep exactly one active line per extension. A line whose extension this
-- install lacks is commented out with a note: built in, or rebuild it with
-- PIE. A line mise-php turned off earlier is active again once this install
-- has its extension. A second active line for an extension is commented out,
-- and a commented line for an extension another line enables is marked, so
-- removing its ";" does not make PHP load the extension twice. Bundled
-- extensions the source never mentions get their default lines. Extension
-- binaries are never copied: one built against another install stays there.
--
-- New lines go into the global scope, before any section, and never inside a
-- value that spans several lines: PHP loads extensions in any line order.
-- New default lines join the bundled extensions under BUNDLED_HEADER when the
-- global scope has that header, and otherwise start a block of their own with
-- it. The managed extension_dir goes before the first line that is neither
-- blank nor a comment, and before that header, unless the file already starts
-- with it, as every carried file does after its first upgrade.
local function carried_ini(previous, root, extension_dir, manifest)
    local items = drop_superseded(parse_items(previous), extension_dir)
    local managed_line = 'extension_dir = "' .. extension_dir .. '"'
    local mentioned = {}
    local builtin = nil

    -- Pick one winner per extension among the lines meant to be active: the
    -- first one this install can load, else the first one.
    local winners = {}
    for index, item in ipairs(items) do
        local extension = item.extension
        if extension ~= nil then
            mentioned[extension.name] = true
            local restored = item.off and parse_extension_line(restored_line(item.line))
            if restored then
                item.line = restored_line(item.line)
                item.extension = restored
                extension = restored
            end
            if not extension.commented then
                local available = extension_available(extension.value, extension_dir)
                local winner = winners[extension.name]
                if winner == nil or (available and not winner.available) then
                    winners[extension.name] = { index = index, available = available }
                end
            end
        end
    end

    local lines = {}
    local global = true
    local header = nil
    for index, item in ipairs(items) do
        local extension = item.extension
        local text = item.line:match("^%s*(.-)%s*$")
        if item.section then
            global = false
        end

        if extension == nil then
            local line = item.line
            if not item.value then
                if line:match("^%s*extension_dir%s*=") then
                    line = managed_line
                elseif line:sub(1, #CARRIED_HEADER_PREFIX) == CARRIED_HEADER_PREFIX then
                    line = BUNDLED_HEADER
                end
            end
            table.insert(lines, line)
            if not item.value and line == BUNDLED_HEADER and global and header == nil then
                header = #lines
            end
        elseif extension.commented then
            local winner = winners[extension.name]
            if winner ~= nil and winner.available then
                table.insert(lines, note_for(extension, SHADOW_NOTE))
            end
            table.insert(lines, item.line)
        else
            local winner = winners[extension.name]
            if winner.index == index and winner.available then
                table.insert(lines, item.line)
            else
                builtin = builtin or builtin_modules(root)
                if winner.index ~= index and (winner.available or builtin[extension.name]) then
                    table.insert(lines, note_for(extension, DUPLICATE_NOTE))
                elseif builtin[extension.name] then
                    table.insert(lines, note_for(extension, BUILTIN_NOTE))
                else
                    table.insert(lines, note_for(extension, MISSING_NOTE))
                end
                table.insert(lines, ";" .. text)
            end
        end
    end

    local added = {}
    for _, extension in ipairs(manifest.extensions) do
        if not mentioned[extension.name:lower()] then
            for _, line in ipairs(default_lines(extension)) do
                table.insert(added, line)
            end
        end
    end
    if #added > 0 and header ~= nil then
        for offset, line in ipairs(added) do
            table.insert(lines, header + offset, line)
        end
    end

    local at = #lines + 1
    for index, line in ipairs(lines) do
        if not line:match("^%s*$") and not line:match("^%s*;") then
            at = index
            break
        end
    end

    local groups = {}
    if lines[at] == managed_line then
        at = at + 1
    else
        table.insert(groups, { MANAGED_COMMENT, managed_line })
        if header ~= nil and header < at then
            at = header
        end
    end
    if #added > 0 and header == nil then
        table.insert(added, 1, BUNDLED_HEADER)
        table.insert(groups, added)
    end

    local block = {}
    for _, group in ipairs(groups) do
        if #block > 0 or (at > 1 and lines[at - 1] ~= "") then
            table.insert(block, "")
        end
        for _, line in ipairs(group) do
            table.insert(block, line)
        end
    end
    if #block > 0 and at <= #lines and not lines[at]:match("^%s*$") then
        table.insert(block, "")
    end
    for offset, line in ipairs(block) do
        table.insert(lines, at + offset - 1, line)
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
            ini = carried_ini(previous, root, extension_dir, manifest)
            print("php.ini settings carried forward from PHP " .. source.name)
        end
    end

    write_file(ini_path, ini or fresh_ini(extension_dir, manifest))
end
