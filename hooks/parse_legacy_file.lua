local file = require("file")

function PLUGIN:ParseLegacyFile(ctx)
    local content = file.read(ctx.filepath)
    if content == nil then
        error("failed to read " .. ctx.filepath)
    end

    local version = content:match("(%d+%.%d+[^%s]*)")
    return { version = version }
end
