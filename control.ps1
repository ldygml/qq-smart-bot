$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$projectRoot = $PSScriptRoot
Set-Location -LiteralPath $projectRoot

function Test-LocalPort {
    param([int]$Port)
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $result = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $result.AsyncWaitHandle.WaitOne(400)) { return $false }
        $client.EndConnect($result)
        return $true
    }
    catch { return $false }
    finally { $client.Dispose() }
}

$envPath = Join-Path $projectRoot ".env"
$port = 8090
if (Test-Path -LiteralPath $envPath) {
    $line = Get-Content -LiteralPath $envPath -Encoding UTF8 |
        Where-Object { $_ -match "^CONTROL_PORT=" } |
        Select-Object -First 1
    if ($line -and $line -match "^CONTROL_PORT=(\d+)$") {
        $port = [int]$Matches[1]
    }
}

if (-not (Test-LocalPort -Port $port)) {
    $uv = (Get-Command uv.exe -ErrorAction Stop).Source
    & $uv sync --dev
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }
    $stdout = Join-Path $projectRoot ".runtime\control.stdout.log"
    $stderr = Join-Path $projectRoot ".runtime\control.stderr.log"
    Start-Process `
        -FilePath $uv `
        -ArgumentList @("run", "qq-control") `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline -and -not (Test-LocalPort -Port $port)) {
        Start-Sleep -Seconds 1
    }
}

if (-not (Test-LocalPort -Port $port)) {
    throw "Control plane did not start. Check .runtime\control.stderr.log."
}

$publicUrl = ""
$autoTunnel = $env:CLOUDFLARE_AUTO_START
if ($autoTunnel -ne "false") {
    $cloudflareScript = Join-Path $projectRoot "cloudflare.ps1"
    if (Test-Path -LiteralPath $cloudflareScript) {
        try {
            & powershell.exe `
                -NoProfile `
                -ExecutionPolicy Bypass `
                -File $cloudflareScript `
                start
            if ($LASTEXITCODE -eq 0) {
                $cloudflareUrlFile = Join-Path $projectRoot ".runtime\cloudflared.url"
                if (Test-Path -LiteralPath $cloudflareUrlFile) {
                    $publicUrl = (Get-Content `
                        -LiteralPath $cloudflareUrlFile `
                        -Encoding UTF8 | Out-String).Trim()
                }
            }
        }
        catch {
            Write-Warning "Cloudflare tunnel was not started: $($_.Exception.Message)"
        }
    }
}

$url = "http://127.0.0.1:$port/"
Start-Process $url
Write-Host "Control plane is running: $url"
if ($publicUrl) {
    try { Set-Clipboard -Value $publicUrl } catch {}
    Write-Host "Mobile URL: $publicUrl"
    Write-Host "The current mobile URL has been copied to the clipboard."
}
