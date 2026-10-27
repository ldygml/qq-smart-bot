$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$envPath = Join-Path $projectRoot ".env"
if (-not (Test-Path -LiteralPath $envPath)) {
    throw ".env does not exist. Run start.ps1 once first."
}

Write-Host "Paste your Alibaba Cloud Model Studio (DashScope) API Key."
Write-Host "Input is hidden and will only be stored in the local .env file."
$secureKey = Read-Host "API Key" -AsSecureString

$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
try {
    $apiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer).Trim()
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
}

if ($apiKey.Length -lt 20) {
    throw "The API Key looks too short. Nothing was changed."
}
if ($apiKey -match "[\r\n]") {
    throw "The API Key contains a line break. Nothing was changed."
}

$strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
$content = [System.IO.File]::ReadAllText($envPath, $strictUtf8)
if ($content -notmatch "(?m)^LLM_API_KEY=") {
    throw "LLM_API_KEY is missing from .env. Nothing was changed."
}

$content = [regex]::Replace(
    $content,
    "(?m)^LLM_API_KEY=.*$",
    "LLM_API_KEY=$apiKey"
)
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($envPath, $content, $utf8NoBom)
Write-Host "Qwen API Key saved locally to .env."
