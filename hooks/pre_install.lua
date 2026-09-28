local platform = require("platform")
local releases = require("releases")

function PLUGIN:PreInstall(ctx)
    platform.assert_supported()

    local version = ctx.version
    if not releases.is_exact_stable_version(version) then
        error("unsupported php release version: " .. tostring(version))
    end

    -- A plain patch installs its newest published rebuild revision; an explicit
    -- revision such as 8.5.10-1 stays an exact pin. Resolving a plain patch
    -- reads the release listing, which already holds the chosen release, so
    -- only an explicit pin, or a plain patch the listing cannot install, reads
    -- its release by tag.
    local tag, release = version, nil
    if releases.plain_version(version) == version then
        tag, release = releases.resolve_tag(version, releases.list())
    end

    release = release or releases.get(tag)
    if release.draft or release.prerelease then
        error("php release is not published: " .. tag)
    end

    local filename = releases.archive_name(tag)
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

    local label = version
    if tag ~= version then
        label = version .. " (build " .. tag .. ")"
    end

    return {
        version = version,
        url = archive.browser_download_url,
        sha256 = sha256,
        note = "Installing PHP " .. label .. " for macOS arm64",
    }
end
