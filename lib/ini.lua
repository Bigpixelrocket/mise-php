-- Where each php.ini statement ends, following PHP's own ini scanner
-- (Zend/zend_ini_scanner.l in normal mode, the mode PHP reads php.ini in).
--
-- A statement runs on over later lines only inside a double-quoted string, a
-- single-quoted raw string, a ${...:-...} fallback, after a "$" or "\" that
-- escapes the newline where PHP allows one, or inside a section or offset
-- name. PHP opens a raw string at a single quote only inside a value, a
-- section, or an offset, and only when another single quote follows
-- somewhere later in the file: an apostrophe in a comment or an option name
-- never opens one. Where PHP stops with a syntax error instead, such as a
-- double-quoted string that never closes or a single quote with no partner,
-- the statement ends on its own line, so a malformed value never hides the
-- lines after it.

local M = {}

-- Characters PHP's ini scanner does not allow in an option or variable name.
local NOT_LABEL = "[=\n\r\t;&|^$~(){}!\"%[%]]"

local function char_at(lines, li, col)
    local line = lines[li]
    if line == nil then
        return nil
    end
    if col > #line then
        return "\n"
    end
    return line:sub(col, col)
end

local function step(lines, li, col)
    if col > #lines[li] then
        return li + 1, 1
    end
    return li, col + 1
end

local skip_double_quoted, skip_variable

-- From just after an opening single quote, the position after the closing
-- one. nil when no quote follows at all: PHP then ignores the rest of the
-- file. A quote directly after the opening one is PHP's empty case and is
-- handled by the caller.
local function skip_raw(lines, li, col)
    while true do
        local c = char_at(lines, li, col)
        if c == nil then
            return nil
        end
        li, col = step(lines, li, col)
        if c == "'" then
            return li, col
        end
    end
end

-- "$" followed by any character but "{", or "$\" followed by any character,
-- is literal text, and that character may be the newline.
local function skip_literal_dollar(lines, li, col)
    li, col = step(lines, li, col)
    local c = char_at(lines, li, col)
    if c == nil then
        return nil
    end
    if c == "\\" then
        li, col = step(lines, li, col)
        if char_at(lines, li, col) == nil then
            return nil
        end
    end
    return step(lines, li, col)
end

-- From just after "${", the position after the closing "}". A name never
-- spans lines; a ":-" fallback may, inside a string or after a backslash.
skip_variable = function(lines, li, col)
    local length = 0
    while true do
        local c = char_at(lines, li, col)
        if c == ":" and char_at(lines, li, col + 1) == "-" then
            -- PHP reads a fallback after a name of fewer than two characters
            -- before the name itself, which is a syntax error.
            if length < 2 then
                return nil
            end
            col = col + 2
            break
        elseif c == "}" then
            return step(lines, li, col)
        elseif c == nil or c:match(NOT_LABEL) then
            return nil
        end
        length = length + 1
        col = col + 1
    end

    while true do
        local c = char_at(lines, li, col)
        if c == nil or c == "\n" or c == ";" or c == "'" then
            return nil
        elseif c == "}" then
            return step(lines, li, col)
        elseif c == '"' then
            li, col = skip_double_quoted(lines, step(lines, li, col))
        elseif c == "\\" then
            li, col = step(lines, li, col)
            if char_at(lines, li, col) == nil then
                return nil
            end
            li, col = step(lines, li, col)
        elseif c == "$" and char_at(lines, li, col + 1) == "{" then
            li, col = skip_variable(lines, li, col + 2)
        elseif c == "$" then
            li, col = skip_literal_dollar(lines, li, col)
        else
            li, col = step(lines, li, col)
        end
        if li == nil then
            return nil
        end
    end
end

-- From just after an opening double quote, the position after the closing
-- one, or nil when the string never closes.
skip_double_quoted = function(lines, li, col)
    while true do
        local c = char_at(lines, li, col)
        if c == nil then
            return nil
        elseif c == '"' then
            return step(lines, li, col)
        elseif c == "\\" then
            li, col = step(lines, li, col)
            local escaped = char_at(lines, li, col)
            if escaped == nil then
                return nil
            end
            li, col = step(lines, li, col)
            -- An escaped quote that ends the line closes the string instead,
            -- as in key = "C:\path\". A backslash that is itself escaped
            -- escapes nothing after it, so "a\\" closes at its last quote,
            -- as PHP 8.5 reads it.
            local after = char_at(lines, li, col)
            if escaped == '"' and (after == nil or after == "\n") then
                return li, col
            end
        elseif c == "$" and char_at(lines, li, col + 1) == "{" then
            li, col = skip_variable(lines, li, col + 2)
            if li == nil then
                return nil
            end
        else
            li, col = step(lines, li, col)
        end
    end
end

-- From just after the "[" of a section or an option offset, the position
-- after its "]", or nil on a syntax error.
local function skip_bracketed(lines, li, col)
    while true do
        local c = char_at(lines, li, col)
        if c == nil or c == "\n" or c == ";" then
            return nil
        elseif c == "]" then
            return step(lines, li, col)
        elseif c == '"' then
            li, col = skip_double_quoted(lines, step(lines, li, col))
        elseif c == "'" then
            if char_at(lines, li, col + 1) == "'" then
                return nil
            end
            li, col = skip_raw(lines, step(lines, li, col))
        elseif c == "\\" then
            li, col = step(lines, li, col)
            if char_at(lines, li, col) == nil then
                return nil
            end
            li, col = step(lines, li, col)
        elseif c == "$" and char_at(lines, li, col + 1) == "{" then
            li, col = skip_variable(lines, li, col + 2)
        elseif c == "$" then
            li, col = skip_literal_dollar(lines, li, col)
        else
            li, col = step(lines, li, col)
        end
        if li == nil then
            return nil
        end
    end
end

-- The line an option value ends on, from just after its "=". Returns false on
-- a syntax error, or the position PHP reads on from as the start of a new
-- statement when an empty '' ends the value mid-line.
local function scan_value(lines, li, col)
    while true do
        local c = char_at(lines, li, col)
        if c == nil then
            return li - 1
        elseif c == "\n" or c == ";" then
            return li
        elseif c == "=" then
            return false
        elseif c == "'" and char_at(lines, li, col + 1) == "'" then
            return nil, li, col + 1
        end

        if c == '"' then
            li, col = skip_double_quoted(lines, step(lines, li, col))
        elseif c == "'" then
            li, col = skip_raw(lines, step(lines, li, col))
        elseif c == "$" and char_at(lines, li, col + 1) == "{" then
            li, col = skip_variable(lines, li, col + 2)
        elseif c == "$" then
            li, col = skip_literal_dollar(lines, li, col)
        else
            li, col = step(lines, li, col)
        end
        if li == nil then
            return false
        end
    end
end

-- The last line of the statement that starts on line `first`, and whether
-- that statement opens a section. A statement PHP would reject with a syntax
-- error ends on its own line.
local function statement_end(lines, first)
    local li, col = first, 1
    if first == 1 and lines[1]:sub(1, 3) == "\239\187\191" then
        col = 4
    end
    local section = false
    local leading = true

    while true do
        local c = char_at(lines, li, col)
        while c == " " or c == "\t" do
            col = col + 1
            c = char_at(lines, li, col)
        end
        local first_token = leading
        leading = false

        if c == nil then
            return li - 1, section
        elseif c == "\n" or c == ";" then
            return li, section
        elseif c == "[" then
            section = first_token
            li, col = skip_bracketed(lines, li, col + 1)
            if li == nil then
                return first, section, true
            end
            while char_at(lines, li, col) == " " or char_at(lines, li, col) == "\t" do
                col = col + 1
            end
            if char_at(lines, li, col) == "\n" then
                return li, section
            end
        elseif c == "=" then
            local last, resume_li, resume_col = scan_value(lines, li, col + 1)
            if last == false then
                return first, section, true
            elseif last ~= nil then
                return last, section
            end
            li, col = resume_li, resume_col
        elseif c:match(NOT_LABEL) then
            return first, section, true
        else
            while not char_at(lines, li, col):match(NOT_LABEL) do
                col = col + 1
            end
            if char_at(lines, li, col) == "[" then
                li, col = skip_bracketed(lines, li, col + 1)
                if li == nil then
                    return first, section, true
                end
            end
        end
    end
end

-- Scan php.ini lines, as split without their newlines, into statements.
-- Returns tables keyed by line index: `continued` marks a line inside a
-- statement an earlier line opened, `opens` a line whose statement runs on to
-- the next line, and `section` a line that starts a section. `error` is true
-- when some statement fell back to ending on its own line.
function M.scan(lines)
    local result = { continued = {}, opens = {}, section = {} }
    local index = 1
    while index <= #lines do
        local last, section, failed = statement_end(lines, index)
        result.error = result.error or failed
        if last < index then
            last = index
        end
        result.section[index] = section
        if last > index then
            result.opens[index] = true
        end
        for continued = index + 1, last do
            result.continued[continued] = true
        end
        index = last + 1
    end
    return result
end

return M
