local http = require("http")
local json = require("json")
local policy = require("policy")

local M = {}

local API_BASE_URL = os.getenv("MISE_PHP_API_BASE_URL") or "https://api.github.com"
local REPOSITORY_PATH = "/repos/bigpixelrocket/php-bin"

local function request(url, decode_json)
    local response, err = http.get({
        url = url,
        headers = {
            Accept = "application/vnd.github+json",
            ["X-GitHub-Api-Version"] = "2022-11-28",
        },
    })

    if err ~= nil then
        error("failed to fetch php release metadata: " .. tostring(err))
    end

    if response.status_code ~= 200 then
        error("php release server returned HTTP " .. tostring(response.status_code))
    end

    if decode_json then
        return json.decode(response.body)
    end

    return response.body
end


function M.list()
    return request(API_BASE_URL .. REPOSITORY_PATH .. "/releases?per_page=100", true)
end


function M.get(version)
    return request(API_BASE_URL .. REPOSITORY_PATH .. "/releases/tags/" .. version, true)
end


function M.download_text(url)
    return request(url, false)
end


function M.is_supported_version(version)
    for _, branch in ipairs(policy.maintained) do
        local prefix = "^" .. branch:gsub("%.", "%%.") .. "%.%d+"
        if version:match(prefix .. "$") ~= nil
            or version:match(prefix .. "%-[1-9]%d*$") ~= nil
        then
            return true
        end
    end

    return false
end


function M.is_exact_stable_version(version)
    return version:match("^%d+%.%d+%.%d+$") ~= nil
        or version:match("^%d+%.%d+%.%d+%-[1-9]%d*$") ~= nil
end


-- Split an exact release tag such as 8.5.10 or 8.5.10-1 into numbers. A plain
-- tag is revision 0, so every rebuild revision sorts after the patch it rebuilds.
function M.parse_version(version)
    local text = tostring(version)
    local revision = "0"
    local major, minor, patch = text:match("^(%d+)%.(%d+)%.(%d+)$")
    if major == nil then
        major, minor, patch, revision = text:match("^(%d+)%.(%d+)%.(%d+)%-([1-9]%d*)$")
    end
    if major == nil then
        return nil
    end

    return {
        major = tonumber(major),
        minor = tonumber(minor),
        patch = tonumber(patch),
        revision = tonumber(revision),
    }
end


-- Order two parsed versions numerically: true when a is newer than b.
function M.is_newer(a, b)
    for _, field in ipairs({ "major", "minor", "patch", "revision" }) do
        if a[field] ~= b[field] then
            return a[field] > b[field]
        end
    end

    return false
end


-- The user-facing version of a release tag: 8.5.10-1 lists as 8.5.10.
function M.plain_version(version)
    return (tostring(version):gsub("%-[1-9]%d*$", ""))
end


-- True when a release is published and carries both assets an install needs.
function M.is_installable(release)
    local version = release.tag_name
    return version ~= nil
        and not release.draft
        and not release.prerelease
        and M.find_asset(release, M.archive_name(version)) ~= nil
        and M.find_asset(release, "SHA256SUMS") ~= nil
end


-- Resolve a plain patch version to its newest installable rebuild revision,
-- falling back to the plain tag when the listing holds no revision of it.
function M.resolve_tag(version, listing)
    local best_tag, best_key = nil, nil

    for _, release in ipairs(listing) do
        local tag = release.tag_name
        local key = tag and M.parse_version(tag) or nil
        if key ~= nil
            and M.plain_version(tag) == version
            and M.is_installable(release)
            and (best_key == nil or M.is_newer(key, best_key))
        then
            best_tag, best_key = tag, key
        end
    end

    return best_tag or version
end


function M.archive_name(version)
    return "php-" .. version .. "-cli-macos-aarch64.tar.gz"
end


function M.find_asset(release, name)
    for _, asset in ipairs(release.assets or {}) do
        if asset.name == name then
            return asset
        end
    end

    return nil
end


function M.checksum_for(checksum_body, filename)
    for line in checksum_body:gmatch("[^\r\n]+") do
        local checksum, candidate = line:match("^(%x+)%s+%*?(.+)$")
        if candidate == filename and checksum ~= nil and #checksum == 64 then
            return checksum:lower()
        end
    end

    return nil
end


return M
