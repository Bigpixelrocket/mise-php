local platform = require("platform")
local releases = require("releases")

function PLUGIN:PreInstall(ctx)
    platform.assert_supported()

    local version = ctx.version
    if not releases.is_supported_version(version) then
        error("unsupported php release version: " .. tostring(version))
    end

    local release = releases.get(version)
    if release.draft or release.prerelease then
        error("php release is not published: " .. version)
    end

    local filename = releases.archive_name(version)
    local archive = releases.find_asset(release, filename)
    local checksums = releases.find_asset(release, "SHA256SUMS")

    if archive == nil then
        error("php release is missing archive: " .. filename)
    end

    if checksums == nil then
        error("php release is missing SHA256SUMS")
    end

    local checksum_body = releases.download_text(checksums.browser_download_url)
    local sha256 = releases.checksum_for(checksum_body, filename)
    if sha256 == nil then
        error("SHA256SUMS has no valid entry for " .. filename)
    end

    return {
        version = version,
        url = archive.browser_download_url,
        sha256 = sha256,
        note = "Installing PHP " .. version .. " for macOS arm64",
    }
end

