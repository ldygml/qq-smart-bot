$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
Set-Location -LiteralPath $projectRoot

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

$botProcesses = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            ($_.Name -in @("uv.exe", "qq-smart-bot.exe", "python.exe")) -and
            $_.CommandLine -match "qq-smart-bot\.exe" -and
            $_.CommandLine -notmatch "qq-control"
        }
)
if ($botProcesses.Count -gt 0) {
    Write-Host "Stopping QQ smart bot..."
    Stop-Process -Id @($botProcesses | Select-Object -ExpandProperty ProcessId) -Force
}
else {
    Write-Host "QQ smart bot is not running."
}

$runtimeRoot = Join-Path $projectRoot ".runtime\napcat-onekey"
$installations = @(
    Get-ChildItem -LiteralPath $runtimeRoot -Directory -Filter "NapCat.*.Shell" -ErrorAction SilentlyContinue
)

if ($installations.Count -eq 1 -and (Test-LocalPort -Port 3000)) {
    $envPath = Join-Path $projectRoot ".env"
    if (Test-Path -LiteralPath $envPath) {
        $envText = [System.IO.File]::ReadAllText(
            $envPath,
            [System.Text.Encoding]::UTF8
        )
        $tokenMatch = [regex]::Match($envText, "(?m)^ONEBOT_TOKEN=(.+)$")
        if ($tokenMatch.Success) {
            $token = $tokenMatch.Groups[1].Value.Trim()
            try {
                Invoke-RestMethod `
                    -Uri "http://127.0.0.1:3000/bot_exit" `
                    -Method Post `
                    -Headers @{ Authorization = "Bearer $token" } `
                    -ContentType "application/json" `
                    -Body "{}" `
                    -TimeoutSec 10 | Out-Null
                Start-Sleep -Seconds 3
            }
            catch {
                Write-Warning "NapCat did not exit through OneBot; cleaning up its processes."
            }
        }
    }
}

if ($installations.Count -eq 1) {
    $napcatDirectory = $installations[0].FullName
    $napcatProcesses = @(
        Get-CimInstance Win32_Process |
            Where-Object {
                $_.ExecutablePath -and
                $_.ExecutablePath.StartsWith(
                    $napcatDirectory + [IO.Path]::DirectorySeparatorChar,
                    [StringComparison]::OrdinalIgnoreCase
                )
            }
    )
    $napcatWrappers = @(
        Get-CimInstance Win32_Process |
            Where-Object {
                $_.Name -eq "cmd.exe" -and
                $_.CommandLine -and
                $_.CommandLine.IndexOf(
                    $napcatDirectory,
                    [StringComparison]::OrdinalIgnoreCase
                ) -ge 0
            }
    )
    $napcatProcessIds = @(
        @($napcatProcesses) + @($napcatWrappers) |
            Select-Object -ExpandProperty ProcessId -Unique
    )
    if ($napcatProcessIds.Count -gt 0) {
        Write-Host "Stopping portable NapCat..."
        Stop-Process -Id $napcatProcessIds -Force
        Start-Sleep -Seconds 2
    }
}

$remainingPorts = @(
    3000, 6099, 8080 | Where-Object { Test-LocalPort -Port $_ }
)
if ($remainingPorts.Count -gt 0) {
    throw "Some bot ports are still open: $($remainingPorts -join ', ')"
}

Write-Host ""
Write-Host "QQ smart bot has been stopped."
Write-Host "Your regular QQ client was not affected."
