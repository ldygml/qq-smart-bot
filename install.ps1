[CmdletBinding()]
param(
    [string]$NapCatZip = "",
    [string]$ApiKey = "",
    [switch]$SkipQQInstall,
    [switch]$SkipStart,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$projectRoot = $PSScriptRoot
Set-Location -LiteralPath $projectRoot

function Write-Step {
    param([string]$Text)
    Write-Host ""
    Write-Host "== $Text ==" -ForegroundColor Cyan
}

function Test-LocalPort {
    param([int]$Port)
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $result = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $result.AsyncWaitHandle.WaitOne(500)) {
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

function Refresh-ProcessPath {
    $parts = @(
        $env:Path,
        (Join-Path $env:USERPROFILE ".local\bin"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links")
    )
    $env:Path = ($parts | Where-Object { $_ }) -join ";"
}

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

function Find-NapCatDirectory {
    $runtimeRoot = Join-Path $projectRoot ".runtime\napcat-onekey"
    if (-not (Test-Path -LiteralPath $runtimeRoot)) {
        return $null
    }
    return Get-ChildItem `
        -LiteralPath $runtimeRoot `
        -Directory `
        -Filter "NapCat.*.Shell" |
        Where-Object {
            Test-Path -LiteralPath (Join-Path $_.FullName "NapCatWinBootMain.exe")
        } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
}

function Get-NapCatArchive {
    $localCandidates = @(
        $NapCatZip,
        (Join-Path $projectRoot "NapCat.Shell.zip"),
        (Join-Path $projectRoot "downloads\NapCat.Shell.zip")
    ) | Where-Object { $_ }
    foreach ($candidate in $localCandidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    $downloadPath = Join-Path $projectRoot ".runtime\napcat-download.zip"
    $downloadUrls = @()
    if ($env:NAPCAT_ZIP_URL) {
        $downloadUrls += $env:NAPCAT_ZIP_URL
    }
    $downloadUrls += (
        "https://github.com/NapNeko/NapCatQQ/releases/latest/download/NapCat.Shell.zip"
    )
    foreach ($url in $downloadUrls) {
        try {
            Write-Host "Trying NapCat download: $url"
            Invoke-WebRequest `
                -Uri $url `
                -OutFile $downloadPath `
                -UseBasicParsing `
                -MaximumRedirection 10 `
                -TimeoutSec 120
            if ((Get-Item -LiteralPath $downloadPath).Length -gt 1000000) {
                return $downloadPath
            }
        }
        catch {
            Write-Warning "Download failed: $url"
        }
    }

    if (Get-Command gh.exe -ErrorAction SilentlyContinue) {
        try {
            gh auth status *> $null
            if ($LASTEXITCODE -eq 0) {
                $artifacts = gh api `
                    "repos/NapNeko/NapCatQQ/actions/artifacts?per_page=100" |
                    ConvertFrom-Json
                $artifact = $artifacts.artifacts |
                    Where-Object {
                        $_.name -eq "NapCat.Shell" -and -not $_.expired
                    } |
                    Sort-Object created_at -Descending |
                    Select-Object -First 1
                if ($artifact) {
                    $token = (gh auth token).Trim()
                    Invoke-WebRequest `
                        -Uri $artifact.archive_download_url `
                        -Headers @{
                            Authorization = "Bearer $token"
                            Accept = "application/vnd.github+json"
                        } `
                        -OutFile $downloadPath `
                        -MaximumRedirection 10 `
                        -TimeoutSec 300
                    return $downloadPath
                }
            }
        }
        catch {
            Write-Warning "GitHub Actions artifact download failed."
        }
    }

    Write-Host ""
    Write-Host "NapCat 官方仓库目前没有公开 Release，无法无条件自动下载。" `
        -ForegroundColor Yellow
    Write-Host "请从官方渠道下载 NapCat.Shell.zip，放到：" `
        -ForegroundColor Yellow
    Write-Host "  $(Join-Path $projectRoot 'NapCat.Shell.zip')"
    Write-Host "然后按回车继续。也可以直接输入 zip 的完整路径。"
    Start-Process "https://github.com/NapNeko/NapCatQQ"
    $entered = Read-Host "NapCat zip 路径（留空表示使用项目根目录）"
    if ($entered.Trim()) {
        $resolved = (Resolve-Path -LiteralPath $entered.Trim()).Path
        return $resolved
    }
    $defaultPath = Join-Path $projectRoot "NapCat.Shell.zip"
    if (Test-Path -LiteralPath $defaultPath -PathType Leaf) {
        return $defaultPath
    }
    throw "未找到 NapCat.Shell.zip。"
}

function Install-NapCatArchive {
    param([string]$ArchivePath)

    $runtimeRoot = Join-Path $projectRoot ".runtime\napcat-onekey"
    New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
    $stage = Join-Path $runtimeRoot (
        ".install-" + [guid]::NewGuid().ToString("N")
    )
    New-Item -ItemType Directory -Path $stage -Force | Out-Null
    Expand-Archive -LiteralPath $ArchivePath -DestinationPath $stage -Force

    $bootMain = Get-ChildItem `
        -LiteralPath $stage `
        -Filter "NapCatWinBootMain.exe" `
        -File `
        -Recurse |
        Select-Object -First 1
    if (-not $bootMain) {
        throw "NapCat package does not contain NapCatWinBootMain.exe."
    }
    $packageRoot = $bootMain.Directory.FullName
    $stamp = Get-Date -Format "yyyyMMddHHmmss"
    $target = Join-Path $runtimeRoot "NapCat.$stamp.Shell"
    Move-Item -LiteralPath $packageRoot -Destination $target

    $launcher = Join-Path $target "napcat.bat"
    if (-not (Test-Path -LiteralPath $launcher)) {
        $userLauncher = if ([Environment]::OSVersion.Version.Build -ge 22000) {
            "launcher-user.bat"
        }
        else {
            "launcher-win10-user.bat"
        }
        if (-not (Test-Path -LiteralPath (Join-Path $target $userLauncher))) {
            $userLauncher = "launcher.bat"
        }
        $launcherText = (
            "@echo off`r`n" +
            "chcp 65001 >nul`r`n" +
            "call `"%~dp0$userLauncher`" %*`r`n"
        )
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText(
            $launcher,
            $launcherText,
            $utf8NoBom
        )
    }
    return Get-Item -LiteralPath $target
}

function Ensure-QQNT {
    param([string]$NapCatDirectory)
    if (Test-Path -LiteralPath (Join-Path $NapCatDirectory "QQ.exe")) {
        Write-Host "NapCat package includes its own QQ runtime."
        return
    }
    $registryPaths = @(
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
        "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ"
    )
    foreach ($path in $registryPaths) {
        if (Test-Path $path) {
            Write-Host "QQNT is already installed."
            return
        }
    }
    if ($SkipQQInstall) {
        Write-Warning "QQNT is not installed. NapCat may fail to launch."
        return
    }
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        throw "QQNT is required. Install QQ from https://im.qq.com and rerun."
    }
    Write-Host "Installing QQNT with winget..."
    winget install `
        --id Tencent.QQ.NT `
        -e `
        --accept-package-agreements `
        --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "QQNT installation failed. Install QQ manually and rerun."
    }
}

function Ensure-Uv {
    Refresh-ProcessPath
    if (Get-Command uv.exe -ErrorAction SilentlyContinue) {
        return
    }
    if (Get-Command winget.exe -ErrorAction SilentlyContinue) {
        Write-Host "Installing Python package manager uv..."
        winget install `
            --id astral-sh.uv `
            -e `
            --accept-package-agreements `
            --accept-source-agreements
    }
    Refresh-ProcessPath
    if (-not (Get-Command uv.exe -ErrorAction SilentlyContinue)) {
        Write-Host "Installing uv from its official installer..."
        Invoke-Expression (
            Invoke-RestMethod "https://astral.sh/uv/install.ps1"
        )
        Refresh-ProcessPath
    }
    if (-not (Get-Command uv.exe -ErrorAction SilentlyContinue)) {
        throw "uv installation failed. Install uv and rerun this script."
    }
}

function Ensure-EnvironmentFile {
    $envPath = Join-Path $projectRoot ".env"
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $token = -join (
        (48..57) + (65..90) + (97..122) |
            Get-Random -Count 64 |
            ForEach-Object { [char]$_ }
    )
    if (-not (Test-Path -LiteralPath $envPath)) {
        $template = [System.IO.File]::ReadAllText(
            (Join-Path $projectRoot ".env.example"),
            [System.Text.Encoding]::UTF8
        )
        $template = $template.Replace(
            "replace-with-a-long-random-token",
            $token
        )
        [System.IO.File]::WriteAllText($envPath, $template, $utf8NoBom)
        Write-Host "Created .env with a random OneBot token."
        return
    }
    $existing = [System.IO.File]::ReadAllText(
        $envPath,
        [System.Text.Encoding]::UTF8
    )
    if ($existing.Contains("replace-with-a-long-random-token")) {
        $existing = $existing.Replace(
            "replace-with-a-long-random-token",
            $token
        )
        [System.IO.File]::WriteAllText($envPath, $existing, $utf8NoBom)
        Write-Host "Replaced the placeholder OneBot token in .env."
    }
}

function Configure-ApiKey {
    param([string]$NapCatDirectory)
    $envPath = Join-Path $projectRoot ".env"
    $strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
    $content = [System.IO.File]::ReadAllText($envPath, $strictUtf8)
    $existing = [regex]::Match(
        $content,
        "(?m)^LLM_API_KEY=(.*)$"
    )
    if (
        $existing.Success -and
        $existing.Groups[1].Value.Trim().Length -ge 20
    ) {
        Write-Host "Existing model API Key found; keeping it."
        return
    }

    $keyValue = $ApiKey.Trim()
    if (-not $keyValue) {
        Write-Host ""
        Write-Host "请输入模型 API Key。默认配置使用阿里云百炼（DashScope）。"
        Write-Host "输入内容会隐藏，只保存在本机 .env。"
        $secureKey = Read-Host "API Key" -AsSecureString
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
            $secureKey
        )
        try {
            $keyValue = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
                $pointer
            ).Trim()
        }
        finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        }
    }
    if ($keyValue.Length -lt 20) {
        Write-Warning "API Key 为空或过短。稍后可在控制台配置供应商和 Key。"
        return
    }
    if ($keyValue.Contains("`r") -or $keyValue.Contains("`n")) {
        throw "API Key contains a line break."
    }
    $content = Set-EnvValue `
        -Content $content `
        -Name "LLM_API_KEY" `
        -Value $keyValue
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($envPath, $content, $utf8NoBom)
    Write-Host "Model API Key saved locally."
}

Write-Step "检查运行环境"
if ($PSVersionTable.PSVersion.Major -lt 5) {
    throw "Windows PowerShell 5.1 or newer is required."
}

$napcatDirectory = Find-NapCatDirectory
if ($CheckOnly) {
    [pscustomobject]@{
        Windows = [Environment]::OSVersion.VersionString
        PowerShell = $PSVersionTable.PSVersion.ToString()
        NapCat = if ($napcatDirectory) { $napcatDirectory.FullName } else { "" }
        Uv = if (Get-Command uv.exe -ErrorAction SilentlyContinue) {
            (Get-Command uv.exe).Source
        }
        else {
            ""
        }
        EnvFileExists = Test-Path -LiteralPath (Join-Path $projectRoot ".env")
    } | ConvertTo-Json
    exit 0
}

Ensure-Uv

if (-not $napcatDirectory) {
    Write-Step "安装 NapCat"
    $archive = Get-NapCatArchive
    $napcatDirectory = Install-NapCatArchive -ArchivePath $archive
}
else {
    Write-Host "Existing NapCat found: $($napcatDirectory.FullName)"
}

Write-Step "准备 QQNT"
Ensure-QQNT -NapCatDirectory $napcatDirectory.FullName

Write-Step "生成配置"
Ensure-EnvironmentFile
Configure-ApiKey -NapCatDirectory $napcatDirectory.FullName

Write-Step "配置 OneBot"
& (Join-Path $projectRoot "scripts\configure_napcat.ps1") -Force

if ($SkipStart) {
    Write-Host ""
    Write-Host "安装与配置已完成。运行 start.ps1 启动机器人。"
    exit 0
}

Write-Step "启动并等待 QQ 扫码"
& (Join-Path $projectRoot "start.ps1")

Write-Step "启动本地控制台"
& (Join-Path $projectRoot "control.ps1")

Write-Host ""
Write-Host "安装完成。" -ForegroundColor Green
Write-Host "控制台：http://127.0.0.1:8090/"
