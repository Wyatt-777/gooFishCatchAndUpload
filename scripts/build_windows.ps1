[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$entryPoint = Join-Path $projectRoot "src\xianyu_assistant\main.py"

if (-not (Test-Path -LiteralPath $python)) {
    throw "未找到项目虚拟环境：$python"
}

Push-Location $projectRoot
try {
    & $python -m PyInstaller `
        --noconfirm `
        --clean `
        --onedir `
        --windowed `
        --name "XianyuAssistant" `
        --paths (Join-Path $projectRoot "src") `
        --collect-all playwright `
        --hidden-import playwright.sync_api `
        $entryPoint
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller 打包失败，退出代码：$LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

Write-Host "Release package: $(Join-Path $projectRoot 'dist\XianyuAssistant')"
