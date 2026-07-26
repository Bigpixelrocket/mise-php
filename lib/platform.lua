local M = {}

function M.assert_supported()
    local os_type = RUNTIME.osType
    local arch_type = RUNTIME.archType

    if os_type ~= "darwin" then
        error("php binaries are available only for macOS arm64; detected OS: " .. tostring(os_type))
    end

    if arch_type ~= "arm64" and arch_type ~= "aarch64" then
        error("php binaries are available only for macOS arm64; detected architecture: " .. tostring(arch_type))
    end
end

return M

