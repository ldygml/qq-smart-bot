param(
    [switch]$ResetConfig
)

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

function Wait-LocalPort {
    param(
        [int]$Port,
        [int]$TimeoutSeconds
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-LocalPort -Port $Port) {
            return $true
        }
        Start-Sleep -Seconds 1
    }
    return $false
}

$templatePath = (Resolve-Path -LiteralPath ".env.example").Path
$envPath = Join-Path $projectRoot ".env"
$createConfig = $ResetConfig -or -not (Test-Path -LiteralPath $envPath)

if (-not $createConfig) {
    try {
        $strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
        [void][System.IO.File]::ReadAllText($envPath, $strictUtf8)
    }
    catch {
        Write-Warning ".env is not valid UTF-8; recreating it from .env.example."
        $createConfig = $true
    }
}

if ($createConfig) {
    $content = [System.IO.File]::ReadAllText(
        $templatePath,
        [System.Text.Encoding]::UTF8
    )
    $token = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 48 | ForEach-Object { [char]$_ })
    $content = $content.Replace("replace-with-a-long-random-token", $token)
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($envPath, $content, $utf8NoBom)
    Write-Host "Created .env with a random ONEBOT_TOKEN."
    Write-Host "Copy this token into NapCat's HTTP Server and HTTP Client settings:"
    Write-Host "  $token"
}

$runtimeRoot = Join-Path $projectRoot ".runtime\napcat-onekey"
$installations = @(
    Get-ChildItem -LiteralPath $runtimeRoot -Directory -Filter "NapCat.*.Shell" -ErrorAction SilentlyContinue
)
if ($installations.Count -ne 1) {
    throw "NapCat portable installation was not found under $runtimeRoot."
}
$napcatDirectory = $installations[0].FullName
$napcatLauncher = Join-Path $napcatDirectory "napcat.bat"
if (-not (Test-Path -LiteralPath $napcatLauncher)) {
    throw "NapCat launcher was not found: $napcatLauncher"
}

$configDirectories = @(
    Get-ChildItem -LiteralPath $napcatDirectory -Recurse -Directory |
        Where-Object { $_.FullName -match "\\resources\\app\\napcat\\config$" }
)
if ($configDirectories.Count -ne 1) {
    throw "Could not find the NapCat config directory."
}
$qqAccount = $null
$accountConfig = Get-ChildItem `
    -LiteralPath $configDirectories[0].FullName `
    -File `
    -Filter "onebot11_*.json" |
    Select-Object -First 1
if ($accountConfig -and $accountConfig.BaseName -match "^onebot11_(\d+)$") {
    $qqAccount = $Matches[1]
}

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
if ($napcatProcesses.Count -eq 0) {
    Write-Host "Starting NapCat..."
    if ($qqAccount) {
        Write-Host "Quick login account: $qqAccount"
        Start-Process `
            -FilePath (Join-Path $napcatDirectory "NapCatWinBootMain.exe") `
            -ArgumentList @($qqAccount) `
            -WorkingDirectory $napcatDirectory `
            -WindowStyle Hidden
    }
    else {
        Start-Process `
            -FilePath $napcatLauncher `
            -WorkingDirectory $napcatDirectory `
            -WindowStyle Hidden
    }
}
else {
    Write-Host "NapCat is already running."
}

Write-Host "Waiting for the QQ OneBot connection..."
if (-not (Wait-LocalPort -Port 3000 -TimeoutSeconds 20)) {
    Write-Host ""
    Write-Host "QQ login confirmation is required."
    Write-Host "A NapCat WebUI tab will be opened. Scan the QR code with the small account."

    $webUiConfigPath = Join-Path $configDirectories[0].FullName "webui.json"
    if (Test-Path -LiteralPath $webUiConfigPath) {
        $webUiConfig = [System.IO.File]::ReadAllText(
            $webUiConfigPath,
            [System.Text.Encoding]::UTF8
        ) | ConvertFrom-Json
        $webUiUrl = "http://127.0.0.1:6099/webui"
        if (-not [string]::IsNullOrWhiteSpace($webUiConfig.token)) {
            $webUiUrl += "?token=" + [Uri]::EscapeDataString($webUiConfig.token)
        }
        Set-Clipboard -Value $webUiUrl
        Start-Process $webUiUrl
    }

    if (-not (Wait-LocalPort -Port 3000 -TimeoutSeconds 180)) {
        throw "QQ login was not completed. Run the script again after scanning."
    }
}

$botProcesses = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            ($_.Name -in @("uv.exe", "qq-smart-bot.exe", "python.exe")) -and
            $_.CommandLine -match "qq-smart-bot"
        }
)

if ($botProcesses.Count -eq 0) {
    if (Test-LocalPort -Port 8080) {
        throw "Port 8080 is already used by another process."
    }

    Write-Host "Updating Python dependencies..."
    $uv = (Get-Command uv.exe -ErrorAction Stop).Source
    & $uv sync --dev
    if ($LASTEXITCODE -ne 0) {
        throw "uv sync failed."
    }

    Write-Host "Starting QQ smart bot..."
    $stdoutLog = Join-Path $projectRoot ".runtime\bot.stdout.log"
    $stderrLog = Join-Path $projectRoot ".runtime\bot.stderr.log"
    Start-Process `
        -FilePath $uv `
        -ArgumentList @("run", "qq-smart-bot") `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog
}
else {
    Write-Host "QQ smart bot is already running."
}

if (-not (Wait-LocalPort -Port 8080 -TimeoutSeconds 20)) {
    throw "The bot did not start on port 8080. Check .runtime\bot.stderr.log."
}

$health = Invoke-RestMethod -Uri "http://127.0.0.1:8080/healthz" -TimeoutSec 5
Write-Host ""
Write-Host "QQ smart bot is ready."
Write-Host "Text model:   $($health.model)"
Write-Host "Vision model: $($health.vision_model)"
Write-Host "WebUI:        http://127.0.0.1:6099/webui"
Write-Host "Bot logs:     $projectRoot\.runtime\bot.stderr.log"
