param(
    [switch]$Background
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $RepoRoot "api_keys.env"
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$LogDir = Join-Path $env:USERPROFILE ".openjarvis\logs"
$ServerLog = Join-Path $LogDir "mamed_server.log"
$ServerOutLog = Join-Path $LogDir "mamed_server_out.log"
$ServerErrLog = Join-Path $LogDir "mamed_server_err.log"
$TgOutLog = Join-Path $LogDir "mamed_telegram.log"
$TgErrLog = Join-Path $LogDir "mamed_telegram_err.log"

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

function Write-ServerLog {
    param([string]$Message)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] $Message"
    Add-Content -Path $ServerLog -Value $line -Encoding UTF8
}

if (-not (Test-Path $PythonExe)) {
    Write-ServerLog "HATA: Python sanal ortami bulunamadi: $PythonExe"
    throw "Python sanal ortamı bulunamadı: $PythonExe"
}

Set-Location $RepoRoot

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

$TgToken = [Environment]::GetEnvironmentVariable("TELEGRAM_BOT_TOKEN", "Process")
$TgJob = $null

if ($Background) {
    Write-ServerLog "Mamed sunucusu arka plan modunda baslatiliyor."
    if ($TgToken) {
        $TgScript = Join-Path $RepoRoot "scripts\mamed_telegram_bot.py"
        if (Test-Path $TgScript) {
            $TgJob = Start-Process -FilePath $PythonExe -ArgumentList "`"$TgScript`"" -WorkingDirectory $RepoRoot -WindowStyle Hidden -RedirectStandardOutput $TgOutLog -RedirectStandardError $TgErrLog -PassThru
            Write-ServerLog "Telegram botu arka planda baslatildi (PID=$($TgJob.Id))."
        }
    } else {
        Write-ServerLog "Bilgi: TELEGRAM_BOT_TOKEN bos oldugu icin Telegram botu atlandi."
    }

    $ServerProc = $null
    try {
        $ServerArgs = @("-m", "openjarvis.cli", "serve", "--host", "0.0.0.0", "--port", "8000", "--engine", "cloud", "--model", "gemini-3.5-flash")
        $ServerProc = Start-Process -FilePath $PythonExe -ArgumentList $ServerArgs -WorkingDirectory $RepoRoot -WindowStyle Hidden -RedirectStandardOutput $ServerOutLog -RedirectStandardError $ServerErrLog -PassThru
        Write-ServerLog "Mamed API sunucusu baslatildi (PID=$($ServerProc.Id))."
        $ServerProc.WaitForExit()
        Write-ServerLog "Mamed API sunucusu sonlandi (ExitCode=$($ServerProc.ExitCode))."
    } catch {
        Write-ServerLog "HATA: Mamed sunucusu calistirilamadi: $($_.Exception.Message)"
        throw
    } finally {
        if ($TgJob -and -not $TgJob.HasExited) {
            Stop-Process -Id $TgJob.Id -Force -ErrorAction SilentlyContinue
            Write-ServerLog "Telegram botu durduruldu (PID=$($TgJob.Id))."
        }
    }
    exit 0
}

# Etkilesimli baslatma modu (MAMED_BASLAT.ps1)
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

Write-ServerLog "Mamed sunucusu etkilesimli modda baslatiliyor (LAN=$LanIp)."

if ($TgToken) {
    Write-Host "Telegram botu arka planda baslatiliyor..." -ForegroundColor Green
    $TgScript = Join-Path $RepoRoot "scripts\mamed_telegram_bot.py"
    $TgJob = Start-Process -FilePath $PythonExe -ArgumentList "`"$TgScript`"" -WorkingDirectory $RepoRoot -PassThru -NoNewWindow
    Write-ServerLog "Telegram botu baslatildi (PID=$($TgJob.Id))."
} else {
    Write-Host "Bilgi: TELEGRAM_BOT_TOKEN bos oldugu icin Telegram botu atlandi." -ForegroundColor DarkGray
    Write-ServerLog "Bilgi: TELEGRAM_BOT_TOKEN bos oldugu icin Telegram botu atlandi."
}

try {
    & $PythonExe -m openjarvis.cli serve --host 0.0.0.0 --port 8000 --engine cloud --model gemini-3.5-flash
    Write-ServerLog "Mamed sunucusu cikis kodu: $LASTEXITCODE"
} catch {
    Write-ServerLog "HATA: Mamed sunucusu baslatilirken hata olustu: $($_.Exception.Message)"
    throw
} finally {
    if ($TgJob -and -not $TgJob.HasExited) {
        Stop-Process -Id $TgJob.Id -Force -ErrorAction SilentlyContinue
        Write-ServerLog "Telegram botu durduruldu (PID=$($TgJob.Id))."
    }
}
