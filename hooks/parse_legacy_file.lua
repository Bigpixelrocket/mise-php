local file = require("file")

function PLUGIN:ParseLegacyFile(ctx)
    local content = file.read(ctx.filepath)
    if content == nil then
        error("failed to read " .. ctx.filepath)
    end

    local version = content:match("(8%.[2-5][^%s]*)")
    return { version = version }
end
