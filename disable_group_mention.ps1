$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$projectRoot = $PSScriptRoot
Set-Location -LiteralPath $projectRoot

function Set-EnvValue {
    param(
        [string]$Content,
        [string]$Name,
        [string]$Value
    )

    $pattern = "(?m)^$([regex]::Escape($Name))=.*$"
    $replacement = "$Name=$Value"
    if ($Content -match $pattern) {
        return [regex]::Replace($Content, $pattern, $replacement)
    }
    return $Content.TrimEnd() + "`n$replacement`n"
}

function Test-LocalPort {
    param([int]$Port)

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $result = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $result.AsyncWaitHandle.WaitOne(400)) {
            return $false
        }
        $client.EndConnect($result)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

$envPath = Join-Path $projectRoot ".env"
if (-not (Test-Path -LiteralPath $envPath)) {
    throw ".env was not found. Start the bot once first."
}

$strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
$content = [System.IO.File]::ReadAllText($envPath, $strictUtf8)
$content = Set-EnvValue -Content $content -Name "ENABLE_GROUP" -Value "true"
$content = Set-EnvValue -Content $content -Name "GROUP_REQUIRE_MENTION" -Value "false"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($envPath, $content, $utf8NoBom)
Write-Host "Group mention requirement disabled."

$botProcesses = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            ($_.Name -in @("uv.exe", "qq-smart-bot.exe", "python.exe")) -and
            $_.CommandLine -match "qq-smart-bot\.exe" -and
            $_.CommandLine -notmatch "qq-control"
        }
)

if ($botProcesses.Count -gt 0) {
    Write-Host "Restarting the bot..."
    Stop-Process -Id @($botProcesses | Select-Object -ExpandProperty ProcessId) -Force
    Start-Sleep -Seconds 2
}

if (Test-LocalPort -Port 8080) {
    throw "Port 8080 is still occupied."
}

$uv = (Get-Command uv.exe -ErrorAction Stop).Source
$stdoutLog = Join-Path $projectRoot ".runtime\bot.stdout.log"
$stderrLog = Join-Path $projectRoot ".runtime\bot.stderr.log"
Start-Process `
    -FilePath $uv `
    -ArgumentList @("run", "qq-smart-bot") `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog

$deadline = (Get-Date).AddSeconds(20)
$health = $null
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 1
    try {
        $health = Invoke-RestMethod `
            -Uri "http://127.0.0.1:8080/healthz" `
            -TimeoutSec 3
        break
    }
    catch {
        $health = $null
    }
}

if ($null -eq $health) {
    throw "The bot did not start. Check .runtime\bot.stderr.log."
}

$groupSettingsBody = @{
    enable_group = $true
    group_require_mention = $false
    group_smart_participation = $true
    group_trigger_prefixes = ""
} | ConvertTo-Json -Compress
Invoke-RestMethod `
    -Uri "http://127.0.0.1:8080/api/admin/group-settings" `
    -Method Patch `
    -ContentType "application/json; charset=utf-8" `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($groupSettingsBody)) `
    -TimeoutSec 10 | Out-Null

$profiles = Invoke-RestMethod `
    -Uri "http://127.0.0.1:8080/api/admin/group-profiles" `
    -TimeoutSec 10
foreach ($group in @($profiles.groups)) {
    $profileBody = @{
        require_mention = $false
        smart_participation = $true
        trigger_prefixes = ""
    } | ConvertTo-Json -Compress
    Invoke-RestMethod `
        -Uri "http://127.0.0.1:8080/api/admin/group-profiles/$($group.group_id)" `
        -Method Patch `
        -ContentType "application/json; charset=utf-8" `
        -Body ([System.Text.Encoding]::UTF8.GetBytes($profileBody)) `
        -TimeoutSec 10 | Out-Null
}

$health = Invoke-RestMethod -Uri "http://127.0.0.1:8080/healthz" -TimeoutSec 5
$profiles = Invoke-RestMethod `
    -Uri "http://127.0.0.1:8080/api/admin/group-profiles" `
    -TimeoutSec 10
if (
    -not $health.group_enabled -or
    $health.group_require_mention -or
    @($profiles.groups | Where-Object { $_.require_mention }).Count -gt 0
) {
    throw "The new group configuration did not take effect for every group."
}

Write-Host ""
Write-Host "Group chat now replies without mentions."
Write-Host "Group enabled: yes; mention required: no."
Write-Host "Applied to global defaults and $(@($profiles.groups).Count) existing group(s)."
