$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $RepoRoot "api_keys.env"
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $PythonExe)) {
    throw "Python sanal ortamı bulunamadı: $PythonExe"
}

if (Test-Path $EnvFile) {
    Get-Content $EnvFile -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }
        $idx = $line.IndexOf("=")
        if ($idx -gt 0) {
            $k = $line.Substring(0, $idx).Trim()
            $v = $line.Substring($idx + 1).Trim()
            if ($v -ne "") {
                [Environment]::SetEnvironmentVariable($k, $v, "Process")
            }
        }
    }
}

$Token = [Environment]::GetEnvironmentVariable("OPENJARVIS_API_KEY", "Process")
if (-not $Token) {
    $Token = [Environment]::GetEnvironmentVariable("MAMED_API_TOKEN", "Process")
}

# Yerel Wi-Fi IPv4 adresini bul
$LanIp = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and $_.PrefixOrigin -ne "WellKnown" } |
    Select-Object -First 1 -ExpandProperty IPAddress
if (-not $LanIp) { $LanIp = "127.0.0.1" }

Write-Host ""
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host "  MAMED - Kisisel Turkce Yapay Zeka Asistani" -ForegroundColor Cyan
Write-Host "=====================================================" -ForegroundColor Cyan
if ($Token) {
    Write-Host "PC Adresi      : http://127.0.0.1:8000/#token=$Token" -ForegroundColor Green
    Write-Host "Telefon (Wi-Fi): http://${LanIp}:8000/#token=$Token" -ForegroundColor Yellow
} else {
    Write-Host "PC Adresi      : http://127.0.0.1:8000" -ForegroundColor Green
    Write-Host "Telefon (Wi-Fi): http://${LanIp}:8000" -ForegroundColor Yellow
}
Write-Host "-----------------------------------------------------"
Write-Host "Durdurmak icin Ctrl+C tusuna basin."
Write-Host ""

$TgToken = [Environment]::GetEnvironmentVariable("TELEGRAM_BOT_TOKEN", "Process")
$TgJob = $null
if ($TgToken) {
    Write-Host "Telegram botu arka planda baslatiliyor..." -ForegroundColor Green
    $TgScript = Join-Path $RepoRoot "scripts\mamed_telegram_bot.py"
    $TgJob = Start-Process -FilePath $PythonExe -ArgumentList "`"$TgScript`"" -PassThru -NoNewWindow
} else {
    Write-Host "Bilgi: TELEGRAM_BOT_TOKEN bos oldugu icin Telegram botu atlandi." -ForegroundColor DarkGray
}

try {
    & $PythonExe -m openjarvis.cli serve --host 0.0.0.0 --port 8000 --engine cloud --model gemini-3.5-flash
} finally {
    if ($TgJob -and -not $TgJob.HasExited) {
        Stop-Process -Id $TgJob.Id -Force -ErrorAction SilentlyContinue
    }
}
