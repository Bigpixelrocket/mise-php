local platform = require("platform")
local releases = require("releases")

-- List each patch installable on this platform once, as its plain version,
-- newest first. A patch whose releases carry no archive for this platform, such
-- as one published before php-bin built for Linux, is left out there. Rebuild
-- revisions such as 8.5.10-1 fold into 8.5.10; PreInstall resolves a plain
-- version to its newest revision. mise keeps this order instead of sorting, so
-- the list must be numeric: GitHub's publish order would let a late rebuild of
-- an old patch become the branch's newest version.
function PLUGIN:Available(_)
    -- Listings run on this machine, so a platform is always resolved.
    local target = assert(platform.current(), "php binaries are not available for this platform")
    local seen = {}
    local versions = {}

    for _, release in ipairs(releases.list()) do
        local version = release.tag_name

        if releases.is_installable(release, target)
            and releases.is_supported_version(version)
            and releases.parse_version(version) ~= nil
        then
            local plain = releases.plain_version(version)
            if not seen[plain] then
                seen[plain] = true
                table.insert(versions, { plain = plain, key = releases.parse_version(plain) })
            end
        end
    end

    table.sort(versions, function(a, b)
        return releases.is_newer(a.key, b.key)
    end)

    local result = {}
    for _, entry in ipairs(versions) do
        table.insert(result, { version = entry.plain })
    end

    return result
end
