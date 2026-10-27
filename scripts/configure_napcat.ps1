param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$runtimeRoot = Join-Path $projectRoot ".runtime\napcat-onekey"
$envFile = Join-Path $projectRoot ".env"

if (-not (Test-Path -LiteralPath $envFile)) {
    throw ".env does not exist. Run start.ps1 once to create it."
}

$envText = [System.IO.File]::ReadAllText(
    $envFile,
    [System.Text.Encoding]::UTF8
)
$tokenMatch = [regex]::Match($envText, "(?m)^ONEBOT_TOKEN=(.+)$")
if (-not $tokenMatch.Success) {
    throw "ONEBOT_TOKEN was not found in .env."
}
$token = $tokenMatch.Groups[1].Value.Trim()
if ($token -eq "replace-with-a-long-random-token" -or $token.Length -lt 24) {
    throw "ONEBOT_TOKEN is a placeholder or too short."
}

$installations = @(
    Get-ChildItem -LiteralPath $runtimeRoot -Directory -Filter "NapCat.*.Shell" -ErrorAction Stop
)
if ($installations.Count -ne 1) {
    throw "Expected exactly one NapCat installation under $runtimeRoot."
}

$configDirectories = @(
    Get-ChildItem -LiteralPath $installations[0].FullName -Recurse -Directory |
        Where-Object { $_.FullName -match "\\resources\\app\\napcat\\config$" }
)
if ($configDirectories.Count -ne 1) {
    throw "Could not find the NapCat config directory."
}

$configDirectory = $configDirectories[0].FullName

$config = [ordered]@{
    network = [ordered]@{
        httpServers = @(
            [ordered]@{
                name = "ai-api"
                enable = $true
                port = 3000
                host = "127.0.0.1"
                enableCors = $false
                enableWebsocket = $false
                messagePostFormat = "array"
                token = $token
                debug = $false
            }
        )
        httpClients = @(
            [ordered]@{
                name = "ai-webhook"
                enable = $true
                url = "http://127.0.0.1:8080/onebot"
                messagePostFormat = "array"
                reportSelfMessage = $false
                token = $token
                debug = $false
            }
        )
        websocketServers = @()
        websocketClients = @()
    }
    musicSignUrl = ""
    enableLocalFile2Url = $false
    parseMultMsg = $false
}

$json = $config | ConvertTo-Json -Depth 10
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$configPaths = @(
    Join-Path $configDirectory "onebot11.json"
    Get-ChildItem -LiteralPath $configDirectory -File -Filter "onebot11_*.json" |
        Select-Object -ExpandProperty FullName
)

$written = 0
foreach ($configPath in $configPaths) {
    if ((Test-Path -LiteralPath $configPath) -and -not $Force) {
        Write-Output "NapCat OneBot config already exists: $configPath"
        continue
    }
    [System.IO.File]::WriteAllText($configPath, $json, $utf8NoBom)
    $written++
    Write-Output "NapCat OneBot config written to: $configPath"
}

if ($written -eq 0) {
    Write-Output "Use -Force to replace existing NapCat OneBot configs."
}

Write-Output "HTTP API: http://127.0.0.1:3000"
Write-Output "Webhook:  http://127.0.0.1:8080/onebot"
