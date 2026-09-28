param(
    [ValidateRange(1, 604800)][int]$DurationSeconds = 7200,
    [ValidateRange(1, 3600)][int]$IntervalSeconds = 30,
    [string]$ProcessName = 'XianyuAssistant',
    [string]$OutputPath = ''
)

$dataDirectory = Join-Path $env:LOCALAPPDATA 'XianyuAssistant'
if (-not $OutputPath) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $OutputPath = Join-Path $dataDirectory "diagnostics\resource-$stamp.csv"
}
$outputFile = [System.IO.Path]::GetFullPath($OutputPath)
$outputDirectory = [System.IO.Path]::GetDirectoryName($outputFile)
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
$database = Join-Path $dataDirectory 'xianyu_assistant.db'
$driveName = [System.IO.Path]::GetPathRoot($outputFile)
$deadline = (Get-Date).AddSeconds($DurationSeconds)

while ((Get-Date) -lt $deadline) {
    $now = (Get-Date).ToString('o')
    $dbBytes = if (Test-Path -LiteralPath $database) {
        (Get-Item -LiteralPath $database).Length
    } else { 0 }
    $disk = Get-PSDrive -Name $driveName.TrimEnd('\').TrimEnd(':') -ErrorAction SilentlyContinue
    $freeBytes = if ($disk) { $disk.Free } else { $null }
    $processes = @(Get-Process -Name $ProcessName -ErrorAction SilentlyContinue)
    if ($processes.Count -eq 0) {
        $rows = @([pscustomobject]@{
            Time = $now; ProcessId = ''; Alive = 0; CpuSeconds = ''
            WorkingSetBytes = ''; Handles = ''; Threads = ''
            DatabaseBytes = $dbBytes; DiskFreeBytes = $freeBytes
        })
    } else {
        $rows = @($processes | ForEach-Object {
            [pscustomobject]@{
                Time = $now; ProcessId = $_.Id; Alive = 1
                CpuSeconds = $_.CPU; WorkingSetBytes = $_.WorkingSet64
                Handles = $_.Handles; Threads = $_.Threads.Count
                DatabaseBytes = $dbBytes; DiskFreeBytes = $freeBytes
            }
        })
    }
    if (Test-Path -LiteralPath $outputFile) {
        $rows | Export-Csv -LiteralPath $outputFile -NoTypeInformation -Append -Encoding UTF8
    } else {
        $rows | Export-Csv -LiteralPath $outputFile -NoTypeInformation -Encoding UTF8
    }
    if ((Get-Date).AddSeconds($IntervalSeconds) -ge $deadline) { break }
    Start-Sleep -Seconds $IntervalSeconds
}

Write-Output $outputFile
