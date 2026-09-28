local http = require("http")
local json = require("json")
local policy = require("policy")

local M = {}

local GITHUB_API_URL = "https://api.github.com"
local API_BASE_URL = (os.getenv("MISE_PHP_API_BASE_URL") or GITHUB_API_URL):gsub("/+$", "")
local REPOSITORY_PATH = "/repos/bigpixelrocket/php-bin"
local TOKEN_VARIABLES = { "MISE_PHP_GITHUB_TOKEN", "GITHUB_TOKEN" }
local PAGE_SIZE = 100
local MAX_PAGES = 100

-- The GitHub token for API calls and the variable it came from, or nil.
-- MISE_PHP_GITHUB_TOKEN wins over GITHUB_TOKEN, and an empty value counts as
-- unset. The token is sent only to api.github.com: a custom
-- MISE_PHP_API_BASE_URL never receives it, and neither does any download, since
-- release assets redirect to other hosts.
local function api_token()
    if API_BASE_URL ~= GITHUB_API_URL then
        return nil, nil
    end

    for _, name in ipairs(TOKEN_VARIABLES) do
        local value = (os.getenv(name) or ""):match("^%s*(.-)%s*$")
        if value ~= "" then
            return value, name
        end
    end

    return nil, nil
end

-- Explain a refused API call. Anonymous GitHub API calls share a small hourly
-- limit per address, which shared CI runners exhaust. Names the variable in
-- use, never its value.
local function refusal_message(status, token_variable)
    local text = "php release server returned HTTP " .. tostring(status)
    if token_variable ~= nil then
        return text .. ": GitHub rate limited or refused the token from " .. token_variable
    end

    return text .. ": GitHub rate limited or refused this anonymous request;"
        .. " set MISE_PHP_GITHUB_TOKEN (or GITHUB_TOKEN) to a GitHub token to raise the limit"
end

-- GET one URL. Only API metadata calls pass api = true, so only they may carry
-- the token; downloads always go out anonymously.
local function request(url, api)
    local headers = {
        Accept = "application/vnd.github+json",
        ["X-GitHub-Api-Version"] = "2022-11-28",
    }
    local token_variable = nil
    if api then
        local token, variable = api_token()
        if token ~= nil then
            headers.Authorization = "Bearer " .. token
            token_variable = variable
        end
    end

    local response, err = http.get({ url = url, headers = headers })

    if err ~= nil then
        error("failed to fetch php release metadata: " .. tostring(err))
    end

    local status = response.status_code
    if api and API_BASE_URL == GITHUB_API_URL and (status == 401 or status == 403 or status == 429) then
        error(refusal_message(status, token_variable))
    end

    if status ~= 200 then
        error("php release server returned HTTP " .. tostring(status))
    end

    if api then
        return json.decode(response.body)
    end

    return response.body
end


-- Every release, newest published first, read page by page until a short
-- page: one page holds at most 100 releases.
function M.list()
    local releases = {}
    for page = 1, MAX_PAGES do
        local batch = request(
            API_BASE_URL .. REPOSITORY_PATH .. "/releases?per_page=" .. PAGE_SIZE .. "&page=" .. page,
            true
        )
        if type(batch) ~= "table" then
            error("php release listing page " .. page .. " is not a list")
        end

        for _, release in ipairs(batch) do
            table.insert(releases, release)
        end

        if #batch < PAGE_SIZE then
            return releases
        end
    end

    error("php release listing is longer than " .. MAX_PAGES .. " pages")
end


-- Read one release by its exact tag.
function M.get(version)
    return request(API_BASE_URL .. REPOSITORY_PATH .. "/releases/tags/" .. version, true)
end


-- Download a release asset as text, always without the token.
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


-- Resolve a plain patch version to its newest installable rebuild revision and
-- return that tag with its release from the listing. With no installable match
-- it returns the plain tag and nil, so the caller reads that release directly
-- and reports what exactly is missing.
function M.resolve_tag(version, releases)
    local best_tag, best_key, best_release = nil, nil, nil

    for _, release in ipairs(releases) do
        local tag = release.tag_name
        local key = tag and M.parse_version(tag) or nil
        if key ~= nil
            and M.plain_version(tag) == version
            and M.is_installable(release)
            and (best_key == nil or M.is_newer(key, best_key))
        then
            best_tag, best_key, best_release = tag, key, release
        end
    end

    return best_tag or version, best_release
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
