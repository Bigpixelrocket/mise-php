local http = require("http")
local json = require("json")

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
    return version:match("^8%.[2-5]%.%d+$") ~= nil
        or version:match("^8%.[2-5]%.%d+%-[1-9]%d*$") ~= nil
end


function M.is_exact_stable_version(version)
    return version:match("^%d+%.%d+%.%d+$") ~= nil
        or version:match("^%d+%.%d+%.%d+%-[1-9]%d*$") ~= nil
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
