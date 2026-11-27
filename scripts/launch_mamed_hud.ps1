$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $RepoRoot "api_keys.env"
$StartScript = Join-Path $PSScriptRoot "start_mamed.ps1"

$Token = ""
if (Test-Path $EnvFile) {
    Get-Content $EnvFile -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }
        $idx = $line.IndexOf("=")
        if ($idx -gt 0) {
            $k = $line.Substring(0, $idx).Trim()
            $v = $line.Substring($idx + 1).Trim()
            if (($k -eq "OPENJARVIS_API_KEY" -or $k -eq "MAMED_API_TOKEN") -and $v -and -not $Token) {
                $Token = $v
            }
        }
    }
}

function Test-MamedHealth {
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2 -UseBasicParsing
        return ($resp.StatusCode -eq 200)
    } catch {
        return $false
    }
}

if (-not (Test-MamedHealth)) {
    Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$StartScript`"" -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 400
        if (Test-MamedHealth) { break }
    }
}

$Url = if ($Token) { "http://127.0.0.1:8000/#token=$Token" } else { "http://127.0.0.1:8000" }

$EdgePath = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
$ChromePath = "C:\Program Files\Google\Chrome\Application\chrome.exe"

if (Test-Path $EdgePath) {
    Start-Process -FilePath $EdgePath -ArgumentList "--app=`"$Url`""
} elseif (Test-Path $ChromePath) {
    Start-Process -FilePath $ChromePath -ArgumentList "--app=`"$Url`""
} else {
    Start-Process $Url
}
