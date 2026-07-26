local releases = require("releases")

function PLUGIN:Available(_)
    local result = {}

    for _, release in ipairs(releases.list()) do
        local version = release.tag_name
        local archive = version and releases.archive_name(version) or nil
        local publishable = not release.draft and not release.prerelease

        if publishable
            and version ~= nil
            and releases.is_supported_version(version)
            and releases.find_asset(release, archive) ~= nil
            and releases.find_asset(release, "SHA256SUMS") ~= nil
        then
            table.insert(result, { version = version })
        end
    end

    return result
end

