[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$sourceDatabase = Join-Path $env:LOCALAPPDATA "XianyuAssistant\xianyu_assistant.db"
$assetDirectory = Join-Path $projectRoot "build\installer-assets"
$stagedDatabase = Join-Path $assetDirectory "xianyu_assistant.db"
$vcRedist = Join-Path $assetDirectory "vc_redist.x64.exe"
$chromeInstaller = Join-Path $assetDirectory "googlechromestandaloneenterprise64.msi"
$chineseLanguage = Join-Path $assetDirectory "ChineseSimplified.isl"
$installerDefinition = Join-Path $projectRoot "installer\XianyuAssistant.iss"
$applicationPath = Join-Path $projectRoot "dist\XianyuAssistant\XianyuAssistant.exe"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "未找到项目虚拟环境：$python"
}

$runningApplication = Get-CimInstance Win32_Process |
    Where-Object { $_.ExecutablePath -eq $applicationPath }
if ($runningApplication) {
    throw "正式程序仍在运行，请关闭后再构建安装包。"
}

New-Item -ItemType Directory -Path $assetDirectory -Force | Out-Null

& (Join-Path $PSScriptRoot "build_windows.ps1")

& $python (Join-Path $PSScriptRoot "stage_installer_data.py") `
    --source $sourceDatabase `
    --destination $stagedDatabase
if ($LASTEXITCODE -ne 0) {
    throw "业务数据库快照创建失败，退出代码：$LASTEXITCODE"
}

if (-not (Test-Path -LiteralPath $vcRedist -PathType Leaf)) {
    Invoke-WebRequest -UseBasicParsing `
        -Uri "https://aka.ms/vc14/vc_redist.x64.exe" `
        -OutFile $vcRedist
}
if (-not (Test-Path -LiteralPath $chromeInstaller -PathType Leaf)) {
    Invoke-WebRequest -UseBasicParsing `
        -Uri "https://dl.google.com/dl/chrome/install/googlechromestandaloneenterprise64.msi" `
        -OutFile $chromeInstaller
}
if (-not (Test-Path -LiteralPath $chineseLanguage -PathType Leaf)) {
    Invoke-WebRequest -UseBasicParsing `
        -Uri "https://raw.githubusercontent.com/kira-96/Inno-Setup-Chinese-Simplified-Translation/main/ChineseSimplified.isl" `
        -OutFile $chineseLanguage
}

if ((Get-Item -LiteralPath $vcRedist).Length -lt 10MB) {
    throw "VC++ 运行库安装程序大小异常。"
}
if ((Get-Item -LiteralPath $chromeInstaller).Length -lt 50MB) {
    throw "Chrome 离线安装程序大小异常。"
}
if ((Get-Item -LiteralPath $chineseLanguage).Length -lt 10KB) {
    throw "Inno Setup 简体中文语言文件大小异常。"
}

$vcSignature = Get-AuthenticodeSignature -LiteralPath $vcRedist
if ($vcSignature.Status -ne "Valid" -or $vcSignature.SignerCertificate.Subject -notmatch "Microsoft") {
    throw "VC++ 运行库安装程序的 Microsoft 数字签名无效。"
}
$chromeSignature = Get-AuthenticodeSignature -LiteralPath $chromeInstaller
if ($chromeSignature.Status -ne "Valid" -or $chromeSignature.SignerCertificate.Subject -notmatch "Google") {
    throw "Chrome 离线安装程序的 Google 数字签名无效。"
}

$isccCandidates = @(
    (Get-Command iscc.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) }
$iscc = $isccCandidates | Select-Object -First 1
if (-not $iscc) {
    throw "未找到 Inno Setup 6 编译器（ISCC.exe）。"
}

& $iscc $installerDefinition
if ($LASTEXITCODE -ne 0) {
    throw "安装包构建失败，退出代码：$LASTEXITCODE"
}

$installerPath = Join-Path $projectRoot "dist\XianyuAssistant-Setup-win64.exe"
if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
    throw "安装包输出不存在：$installerPath"
}

# Keep the adjacent checksum coupled to the installer build.  A stale checksum
# is worse than no checksum because operators may reject a valid upgrade or,
# conversely, trust an older package with the same filename.
$installerHash = Get-FileHash -LiteralPath $installerPath -Algorithm SHA256
$installerHashPath = Join-Path $projectRoot "dist\XianyuAssistant-Setup-win64.sha256.txt"
$installerHashLine = "{0} *{1}{2}" -f `
    $installerHash.Hash, `
    (Split-Path -Leaf $installerPath), `
    [Environment]::NewLine
[System.IO.File]::WriteAllText(
    $installerHashPath,
    $installerHashLine,
    [System.Text.UTF8Encoding]::new($false)
)

$artifacts = @(
    $installerPath,
    $installerHashPath,
    $applicationPath,
    $stagedDatabase,
    $vcRedist,
    $chromeInstaller
)
foreach ($artifact in $artifacts) {
    $item = Get-Item -LiteralPath $artifact
    $hash = Get-FileHash -LiteralPath $artifact -Algorithm SHA256
    Write-Host ("{0} | {1} bytes | SHA-256 {2}" -f $item.FullName, $item.Length, $hash.Hash)
}
