param(
    [ValidateSet("start", "stop", "status")]
    [string]$Action = "start",
    [string]$Token = "",
    [string]$PublicUrl = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$port = 8090
$envPath = Join-Path $projectRoot ".env"
if (Test-Path -LiteralPath $envPath) {
    $line = Get-Content -LiteralPath $envPath -Encoding UTF8 |
        Where-Object { $_ -match "^CONTROL_PORT=(\d+)$" } |
        Select-Object -First 1
    if ($line -and $line -match "^CONTROL_PORT=(\d+)$") {
        $port = [int]$Matches[1]
    }
}

$cloudflared = Get-Command cloudflared.exe -ErrorAction SilentlyContinue
if ($cloudflared) {
    $cloudflaredPath = $cloudflared.Source
} else {
    $fallback = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
    if (Test-Path -LiteralPath $fallback) {
        $cloudflaredPath = $fallback
    } else {
        throw "cloudflared is not installed. Install Cloudflare Tunnel first."
    }
}

$runtime = Join-Path $projectRoot ".runtime"
$log = Join-Path $runtime "cloudflared.log"
$stderrLog = Join-Path $runtime "cloudflared.stderr.log"
$pidFile = Join-Path $runtime "cloudflared.pid"
$urlFile = Join-Path $runtime "cloudflared.url"
New-Item -ItemType Directory -Path $runtime -Force | Out-Null

function Get-TunnelProcess {
    if (-not (Test-Path -LiteralPath $pidFile)) { return $null }
    try {
        $pidValue = [int](Get-Content -LiteralPath $pidFile -Raw).Trim()
        $details = Get-CimInstance Win32_Process -Filter "ProcessId=$pidValue" -ErrorAction Stop
        if (-not $details -or $details.CommandLine -notmatch "cloudflared") {
            return $null
        }
        return Get-Process -Id $pidValue -ErrorAction Stop
    } catch {
        return $null
    }
}

function Get-TunnelUrl {
    $text = ""
    if (Test-Path -LiteralPath $log) {
        $text += Get-Content -LiteralPath $log -Raw -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $stderrLog) {
        $text += "`n" + (Get-Content -LiteralPath $stderrLog -Raw -ErrorAction SilentlyContinue)
    }
    $match = [regex]::Match($text, "https://[A-Za-z0-9.-]+\.trycloudflare\.com")
    if ($match.Success) { return $match.Value }
    if ($PublicUrl) { return $PublicUrl.Trim() }
    if (Test-Path -LiteralPath $urlFile) {
        return (Get-Content -LiteralPath $urlFile -Raw).Trim()
    }
    return ""
}

function Show-Status {
    $process = Get-TunnelProcess
    $url = Get-TunnelUrl
    if ($process) {
        Write-Host "Cloudflare tunnel is running (PID $($process.Id))."
    } else {
        Write-Host "Cloudflare tunnel is stopped."
    }
    if ($url) { Write-Host "Mobile URL: $url" }
    Write-Host "Log: $log"
}

if ($Action -eq "status") {
    Show-Status
    exit 0
}

if ($Action -eq "stop") {
    $process = Get-TunnelProcess
    if ($process) {
        Stop-Process -Id $process.Id -Force
        Write-Host "Cloudflare tunnel stopped (PID $($process.Id))."
    } else {
        Write-Host "Cloudflare tunnel was not running."
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $urlFile -Force -ErrorAction SilentlyContinue
    exit 0
}

$running = Get-TunnelProcess
if ($running) {
    Show-Status
    exit 0
}

Remove-Item -LiteralPath $urlFile -Force -ErrorAction SilentlyContinue
if (-not $Token) {
    $Token = $env:CLOUDFLARE_TUNNEL_TOKEN
}

if ($Token) {
    $arguments = @("tunnel", "--no-autoupdate", "run", "--token", $Token)
} else {
    $arguments = @("tunnel", "--url", "http://127.0.0.1:$port", "--no-autoupdate")
}

$process = Start-Process `
    -FilePath $cloudflaredPath `
    -ArgumentList $arguments `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -PassThru `
    -RedirectStandardOutput $log `
    -RedirectStandardError $stderrLog
$process.Id | Set-Content -LiteralPath $pidFile -Encoding ASCII
if ($PublicUrl) {
    $PublicUrl.Trim() | Set-Content -LiteralPath $urlFile -Encoding ASCII
}

$deadline = (Get-Date).AddSeconds(20)
do {
    Start-Sleep -Milliseconds 500
    $url = Get-TunnelUrl
    if ($Token -or $url) { break }
} while ((Get-Date) -lt $deadline)

if ($url -and -not $PublicUrl) {
    $url | Set-Content -LiteralPath $urlFile -Encoding ASCII
}

Show-Status
if (-not (Get-TunnelProcess)) {
    Write-Host "Tunnel exited unexpectedly. Check $stderrLog" -ForegroundColor Red
    exit 1
}
