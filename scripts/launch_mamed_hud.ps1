$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $RepoRoot "api_keys.env"
$StartScript = Join-Path $PSScriptRoot "start_mamed.ps1"
$LogDir = Join-Path $env:USERPROFILE ".openjarvis\logs"
$LogFile = Join-Path $LogDir "mamed_launcher.log"

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

function Write-LauncherLog {
    param([string]$Message)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $safe = $Message -replace '(#token=)[^\s"&]+', '$1***'
    Add-Content -Path $LogFile -Value "[$ts] $safe" -Encoding UTF8
}

function Test-MamedHealth {
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2 -UseBasicParsing
        return ($resp.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Focus-MamedWindow {
    if (-not ("MamedWinHelper" -as [type])) {
        Add-Type @"
using System;
using System.Text;
using System.Diagnostics;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public class MamedWinHelper {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();

    private static readonly HashSet<string> BrowserNames = new HashSet<string>(StringComparer.OrdinalIgnoreCase) {
        "msedge", "chrome", "brave", "vivaldi", "opera"
    };

    public static string FocusExistingMamed() {
        IntPtr foundHwnd = IntPtr.Zero;
        string foundTitle = "";
        EnumWindows((hWnd, lParam) => {
            if (!IsWindowVisible(hWnd)) return true;
            var sb = new StringBuilder(512);
            if (GetWindowText(hWnd, sb, sb.Capacity) <= 0) return true;
            string title = sb.ToString();
            if (title.IndexOf("Mamed", StringComparison.OrdinalIgnoreCase) < 0) return true;

            uint pid = 0;
            GetWindowThreadProcessId(hWnd, out pid);
            if (pid == 0) return true;

            try {
                string procName = Process.GetProcessById((int)pid).ProcessName;
                if (!BrowserNames.Contains(procName)) return true;
                foundHwnd = hWnd;
                foundTitle = procName + " | " + title;
                return false;
            } catch {
                return true;
            }
        }, IntPtr.Zero);

        if (foundHwnd == IntPtr.Zero) return null;

        if (IsIconic(foundHwnd)) {
            ShowWindow(foundHwnd, 9);
        } else {
            ShowWindow(foundHwnd, 5);
        }

        IntPtr fg = GetForegroundWindow();
        uint fgPid = 0;
        uint fgThread = fg != IntPtr.Zero ? GetWindowThreadProcessId(fg, out fgPid) : 0;
        uint targetPid = 0;
        uint targetThread = GetWindowThreadProcessId(foundHwnd, out targetPid);
        uint curThread = GetCurrentThreadId();

        bool attachedFg = false;
        bool attachedTarget = false;
        try {
            if (fgThread != 0 && fgThread != curThread) {
                attachedFg = AttachThreadInput(curThread, fgThread, true);
            }
            if (targetThread != 0 && targetThread != curThread) {
                attachedTarget = AttachThreadInput(curThread, targetThread, true);
            }
            BringWindowToTop(foundHwnd);
            SetForegroundWindow(foundHwnd);
            SetWindowPos(foundHwnd, new IntPtr(-1), 0, 0, 0, 0, 0x0001 | 0x0002);
            SetWindowPos(foundHwnd, new IntPtr(-2), 0, 0, 0, 0, 0x0001 | 0x0002);
        } finally {
            if (attachedTarget) AttachThreadInput(curThread, targetThread, false);
            if (attachedFg) AttachThreadInput(curThread, fgThread, false);
        }

        return foundTitle;
    }
}
"@
    }
    return [MamedWinHelper]::FocusExistingMamed()
}

try {
    Write-LauncherLog "launch_mamed_hud.ps1 baslatildi."

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

    $healthy = Test-MamedHealth
    if ($healthy) {
        Write-LauncherLog "Sunucu zaten saglikli (/health=200)."
        $focused = Focus-MamedWindow
        if ($focused) {
            Write-LauncherLog "Mevcut Mamed penceresi one getirildi: $focused"
            exit 0
        }
    } else {
        Write-LauncherLog "Sunucu yanit vermiyor; arka plan baslatma kontrol ediliyor."
        $existingServer = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match 'openjarvis\.cli\s+serve' }
        if (-not $existingServer) {
            Start-Process -FilePath "powershell.exe" `
                -ArgumentList "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$StartScript`" -Background" `
                -WorkingDirectory $RepoRoot `
                -WindowStyle Hidden | Out-Null
            Write-LauncherLog "start_mamed.ps1 -Background baslatildi."
        } else {
            Write-LauncherLog "Baslamakta olan sunucu sureci tespit edildi; /health bekleniyor."
        }

        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $deadline = (Get-Date).AddSeconds(30)
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 400
            if (Test-MamedHealth) {
                $healthy = $true
                break
            }
        }
        $sw.Stop()
        Write-LauncherLog ("Sunucu bekleme sonucu: healthy={0}, sure={1}ms" -f $healthy, $sw.ElapsedMilliseconds)

        if (-not $healthy) {
            Write-LauncherLog "HATA: Sunucu 30 saniye icinde ayaga kalkmadi; pencere acilmadi."
            exit 1
        }
    }

    $focusedAfter = Focus-MamedWindow
    if ($focusedAfter) {
        Write-LauncherLog "Mevcut Mamed penceresi one getirildi: $focusedAfter"
        exit 0
    }

    $Url = if ($Token) { "http://127.0.0.1:8000/#token=$Token" } else { "http://127.0.0.1:8000" }
    $EdgePath = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    $ChromePath = "C:\Program Files\Google\Chrome\Application\chrome.exe"

    if (Test-Path $EdgePath) {
        Start-Process -FilePath $EdgePath -ArgumentList "--app=`"$Url`""
        Write-LauncherLog "Yeni Mamed penceresi Edge ile acildi: $Url"
    } elseif (Test-Path $ChromePath) {
        Start-Process -FilePath $ChromePath -ArgumentList "--app=`"$Url`""
        Write-LauncherLog "Yeni Mamed penceresi Chrome ile acildi: $Url"
    } else {
        Start-Process $Url
        Write-LauncherLog "Yeni Mamed penceresi varsayilan tarayici ile acildi: $Url"
    }
    exit 0
} catch {
    Write-LauncherLog "HATA: $($_.Exception.Message)"
    exit 1
}
