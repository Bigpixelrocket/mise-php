local releases = require("releases")

-- List each installable patch once, as its plain version, newest first. Rebuild
-- revisions such as 8.5.10-1 fold into 8.5.10; PreInstall resolves a plain
-- version to its newest revision. mise keeps this order instead of sorting, so
-- the list must be numeric: GitHub's publish order would let a late rebuild of
-- an old patch become the branch's newest version.
function PLUGIN:Available(_)
    local seen = {}
    local versions = {}

    for _, release in ipairs(releases.list()) do
        local version = release.tag_name

        if releases.is_installable(release)
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
