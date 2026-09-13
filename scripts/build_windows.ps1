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
        --hidden-import keyring.backends.Windows `
        --hidden-import win32ctypes.pywin32.win32cred `
        --hidden-import win32ctypes.pywin32.pywintypes `
        $entryPoint
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller 打包失败，退出代码：$LASTEXITCODE"
    }

    # Some developer shells put Poppler ahead of System32 on PATH. PyInstaller
    # may then copy Poppler's ICU shim beside the executable, where it shadows
    # the Windows ICU used by Qt6 and makes QtGui fail to load. These files are
    # not application dependencies and must not ship in the release root.
    $releaseInternal = (Resolve-Path (Join-Path $projectRoot "dist\XianyuAssistant\_internal")).Path
    $expectedInternal = Join-Path $projectRoot "dist\XianyuAssistant\_internal"
    if ($releaseInternal -ne $expectedInternal) {
        throw "发布目录校验失败：$releaseInternal"
    }
    $shadowingIcuFiles = Get-ChildItem -LiteralPath $releaseInternal -File |
        Where-Object { $_.Name -eq "icuuc.dll" -or $_.Name -like "icudt*.dll" }
    foreach ($shadowingIcuFile in $shadowingIcuFiles) {
        Remove-Item -LiteralPath $shadowingIcuFile.FullName -Force
    }
}
finally {
    Pop-Location
}

$releaseDirectory = Join-Path $projectRoot "dist\XianyuAssistant"
$releaseArchive = Join-Path $projectRoot "dist\XianyuAssistant-win64.zip"

# Playwright bundles a console-subsystem node.exe. Even when the parent asks
# Windows to hide it, conhost can briefly flash before the Qt window is painted.
# Change only the bundled release copy to the GUI subsystem; its redirected
# stdin/stdout pipes continue to work, but Windows no longer creates a console.
$playwrightNode = Join-Path $releaseDirectory "_internal\playwright\driver\node.exe"
if (-not (Test-Path -LiteralPath $playwrightNode -PathType Leaf)) {
    throw "未找到 Playwright 驱动：$playwrightNode"
}
$nodeStream = [System.IO.File]::Open(
    $playwrightNode,
    [System.IO.FileMode]::Open,
    [System.IO.FileAccess]::ReadWrite,
    [System.IO.FileShare]::None
)
$nodeReader = [System.IO.BinaryReader]::new($nodeStream)
$nodeWriter = [System.IO.BinaryWriter]::new($nodeStream)
try {
    $nodeStream.Position = 0x3c
    $peOffset = $nodeReader.ReadInt32()
    $nodeStream.Position = $peOffset
    if ($nodeReader.ReadUInt32() -ne 0x00004550) {
        throw "Playwright 驱动不是有效的 PE 文件。"
    }
    $optionalHeaderOffset = $peOffset + 24
    $nodeStream.Position = $optionalHeaderOffset
    $optionalHeaderMagic = $nodeReader.ReadUInt16()
    if ($optionalHeaderMagic -notin @(0x010b, 0x020b)) {
        throw "Playwright 驱动的 PE 可选头格式不受支持。"
    }
    $subsystemOffset = $optionalHeaderOffset + 68
    $nodeStream.Position = $subsystemOffset
    $subsystem = $nodeReader.ReadUInt16()
    if ($subsystem -notin @(2, 3)) {
        throw "Playwright 驱动使用了非预期的 Windows 子系统：$subsystem"
    }
    if ($subsystem -eq 3) {
        $nodeStream.Position = $subsystemOffset
        $nodeWriter.Write([UInt16]2)
        $nodeWriter.Flush()
    }
}
finally {
    $nodeWriter.Dispose()
    $nodeReader.Dispose()
    $nodeStream.Dispose()
}

try {
    Compress-Archive -LiteralPath $releaseDirectory -DestinationPath $releaseArchive -Force
}
catch [System.IO.IOException] {
    # Explorer, antivirus, or a previous handoff can temporarily hold the
    # stable archive open. Keep the successful EXE build and emit a distinct
    # archive instead of failing the whole release at the final step.
    $archiveStamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $releaseArchive = Join-Path $projectRoot "dist\XianyuAssistant-win64-$archiveStamp.zip"
    Compress-Archive -LiteralPath $releaseDirectory -DestinationPath $releaseArchive
}
Write-Host "Release directory: $releaseDirectory"
Write-Host "Release archive: $releaseArchive"
