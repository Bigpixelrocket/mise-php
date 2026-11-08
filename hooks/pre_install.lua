local platform = require("platform")
local releases = require("releases")

-- True when a release carries a php-bin archive for any platform.
local function has_any_archive(release)
    for _, asset in ipairs(release.assets or {}) do
        if tostring(asset.name):match("^php%-.+%-cli%-.+%.tar%.gz$") ~= nil then
            return true
        end
    end

    return false
end

-- Resolve the archive for the platform RUNTIME names. mise also calls this
-- hook for other platforms while it writes a lockfile, with RUNTIME set to
-- that platform, so each lockfile entry gets that platform's archive URL.
function PLUGIN:PreInstall(ctx)
    local target = platform.current()

    local version = ctx.version
    if not releases.is_exact_stable_version(version) then
        error("unsupported php release version: " .. tostring(version))
    end

    -- A lockfile entry for a platform php-bin does not build gets no archive.
    if target == nil then
        return {
            version = version,
            note = "php-bin publishes no build of PHP " .. version .. " for "
                .. tostring(RUNTIME.osType) .. " " .. tostring(RUNTIME.archType),
        }
    end

    -- A plain patch installs its newest rebuild revision published for this
    -- platform; an explicit revision such as 8.5.10-1 stays an exact pin, and
    -- fails when it has no archive for this platform. Resolving a plain patch
    -- reads the release listing, which already holds the chosen release, so
    -- only an explicit pin, or a plain patch the listing cannot install, reads
    -- its release by tag.
    local tag, release = version, nil
    if releases.plain_version(version) == version then
        tag, release = releases.resolve_tag(version, releases.list(), target)
    end

    release = release or releases.get(tag)
    if release.draft or release.prerelease then
        error("php release is not published: " .. tag)
    end

    local filename = releases.archive_name(tag, target)
    local archive = releases.find_asset(release, filename)
    local checksums = releases.find_asset(release, "SHA256SUMS")

    if archive == nil then
        -- Releases published before php-bin built for Linux carry only the
        -- macOS archive; they stay macOS-only forever.
        if has_any_archive(release) then
            error("no " .. target.label .. " build for " .. tag .. ": the release has no " .. filename)
        end
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
        note = "Installing PHP " .. label .. " for " .. target.label,
    }
end
