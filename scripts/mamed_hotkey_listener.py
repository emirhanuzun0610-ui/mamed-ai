"""Mamed sistem geneli klavye kisayolu (Ctrl + Alt + M) arka plan dinleyicisi.

Windows API RegisterHotKey kullanarak sessiz ve tek ornek (single-instance)
calisir. Kisayol tetiklendiginde acik bir Mamed tarayici penceresini one
getirir; pencere kapaliysa launch_mamed_hud.ps1 ile sunucuyu ve pencereyi acar.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import time
import winreg

HOTKEY_ID = 0x4D41  # 'MA'
RETRY_TIMER_ID = 0x4D42
WM_HOTKEY = 0x0312
WM_TIMER = 0x0113

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

ERROR_ALREADY_EXISTS = 183
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

CREATE_NO_WINDOW = 0x08000000
SW_SHOW = 5
SW_RESTORE = 9
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

MUTEX_NAME = "Local\\MamedHotkeyListener_SingleInstance"

BROWSER_PROCESS_NAMES = {
    "msedge",
    "msedge.exe",
    "chrome",
    "chrome.exe",
    "brave",
    "brave.exe",
    "vivaldi",
    "vivaldi.exe",
    "opera",
    "opera.exe",
}

MODIFIER_MAP = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "windows": MOD_WIN,
    "super": MOD_WIN,
}

SPECIAL_VK_MAP = {
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
}

NV_SHADOWPLAY_KEY = r"Software\NVIDIA Corporation\Global\ShadowPlay\NVSPCAPS"

NV_VK_NAMES = {
    16: "Shift",
    17: "Ctrl",
    18: "Alt",
    192: "`",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def get_openjarvis_dir() -> Path:
    return Path.home() / ".openjarvis"


def get_log_path() -> Path:
    return get_openjarvis_dir() / "logs" / "mamed_hotkey.log"


def get_status_path() -> Path:
    return get_openjarvis_dir() / "hotkey_status.json"


def get_launcher_script_path() -> Path:
    return Path(__file__).resolve().parent / "launch_mamed_hud.ps1"


def mask_sensitive_text(text: str) -> str:
    """Log ciktilari icindeki olasi token/anahtar degerlerini maskeler."""
    masked = re.sub(r"(#token=)[^\s\"'&]+", r"\1***", text)
    masked = re.sub(
        r"((?:OPENJARVIS_API_KEY|MAMED_API_TOKEN|TELEGRAM_BOT_TOKEN)\s*=\s*)[^\s\"']+",
        r"\1***",
        masked,
        flags=re.IGNORECASE,
    )
    masked = re.sub(r"(Bearer\s+)[A-Za-z0-9._\-]+", r"\1***", masked, flags=re.IGNORECASE)
    return masked


def parse_hotkey(hotkey_str: str) -> tuple[int, int]:
    """Ornegin 'ctrl+alt+m' ifadesini (fsModifiers, vk) ikilisine cevirir."""
    if not hotkey_str or not isinstance(hotkey_str, str):
        raise ValueError("Kisayol metni bos olamaz.")

    parts = [p.strip().lower() for p in hotkey_str.split("+") if p.strip()]
    if len(parts) < 2:
        raise ValueError(f"Gecersiz kisayol bicimi (en az bir mod + tus gerekli): {hotkey_str}")

    modifiers = 0
    key_part = None
    for part in parts[:-1]:
        if part not in MODIFIER_MAP:
            raise ValueError(f"Desteklenmeyen degistirici tus: {part}")
        modifiers |= MODIFIER_MAP[part]

    key_part = parts[-1]
    if modifiers == 0:
        raise ValueError(f"En az bir degistirici tus (Ctrl/Alt/Shift/Win) gerekli: {hotkey_str}")

    if key_part in SPECIAL_VK_MAP:
        vk = SPECIAL_VK_MAP[key_part]
    elif len(key_part) == 1 and ("a" <= key_part <= "z" or "0" <= key_part <= "9"):
        vk = ord(key_part.upper())
    elif key_part.startswith("f") and key_part[1:].isdigit():
        fnum = int(key_part[1:])
        if not (1 <= fnum <= 24):
            raise ValueError(f"Gecersiz F tusu: {key_part}")
        vk = 0x70 + (fnum - 1)
    else:
        raise ValueError(f"Desteklenmeyen tus: {key_part}")

    return (modifiers | MOD_NOREPEAT, vk)


def is_mamed_browser_window(process_name: str, window_title: str) -> bool:
    """Pencerenin acik bir Mamed tarayici penceresi olup olmadigini belirler."""
    proc = (process_name or "").strip().lower()
    if proc not in BROWSER_PROCESS_NAMES:
        return False
    return "mamed" in (window_title or "").lower()


def _nv_dword_from_bytes(val: object) -> int | None:
    if isinstance(val, int):
        return val
    if isinstance(val, (bytes, bytearray)) and len(val) >= 4:
        return int.from_bytes(val[:4], "little", signed=False)
    return None


def _nv_vk_to_mod_and_key(vks: list[int]) -> tuple[int, int, str]:
    mods = 0
    key_vk = 0
    names: list[str] = []
    for vk in vks:
        if vk == 17:
            mods |= MOD_CONTROL
            names.append("Ctrl")
        elif vk == 18:
            mods |= MOD_ALT
            names.append("Alt")
        elif vk == 16:
            mods |= MOD_SHIFT
            names.append("Shift")
        elif vk in (91, 92):
            mods |= MOD_WIN
            names.append("Win")
        elif vk != 0:
            key_vk = vk
            if 0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39:
                names.append(chr(vk))
            elif 0x70 <= vk <= 0x87:
                names.append(f"F{vk - 0x70 + 1}")
            else:
                names.append(NV_VK_NAMES.get(vk, f"VK_{vk:#x}"))
    return mods, key_vk, "+".join(names)


def detect_nvidia_shadowplay_conflicts(
    target_mods: int,
    target_vk: int,
    reg_values: dict[str, object] | None = None,
) -> list[str]:
    """NVIDIA ShadowPlay / NVIDIA Overlay kisayol cakismalarini tespit eder."""
    values = reg_values
    if values is None:
        values = {}
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, NV_SHADOWPLAY_KEY) as key:
                idx = 0
                while True:
                    try:
                        name, val, _ = winreg.EnumValue(key, idx)
                        values[name] = val
                        idx += 1
                    except OSError:
                        break
        except OSError:
            return []

    enabled = _nv_dword_from_bytes(values.get("IsShadowPlayEnabled"))
    if enabled == 0:
        return []

    clean_target_mods = target_mods & ~MOD_NOREPEAT
    conflicts: list[str] = []
    for name, val in values.items():
        if not name.endswith("HKeyCount") and not name.endswith("HKeyV2Count"):
            continue
        count = _nv_dword_from_bytes(val)
        if not count or count <= 0 or count > 6:
            continue
        prefix = name[:-5]  # Strip trailing "Count"
        vks: list[int] = []
        for i in range(count):
            item_val = _nv_dword_from_bytes(values.get(f"{prefix}{i}"))
            if item_val is not None:
                vks.append(item_val)
        if not vks:
            continue
        mods, key_vk, combo_label = _nv_vk_to_mod_and_key(vks)
        if mods == clean_target_mods and key_vk == target_vk:
            conflicts.append(
                f"NVIDIA Overlay (ShadowPlay) etkin ve '{prefix}' kisayolu {combo_label} olarak kayitli."
            )
    return conflicts


def _read_lnk_hotkey(lnk_path: Path) -> tuple[int, int] | None:
    """Windows .lnk dosya basligindaki (0x40) wHotKey alanini okur."""
    try:
        data = lnk_path.read_bytes()
    except OSError:
        return None
    if len(data) < 0x42:
        return None
    if struct.unpack_from("<I", data, 0)[0] != 0x4C:
        return None
    raw_hotkey = struct.unpack_from("<H", data, 0x40)[0]
    if raw_hotkey == 0:
        return None
    vk = raw_hotkey & 0xFF
    flags = (raw_hotkey >> 8) & 0xFF
    mods = 0
    if flags & 0x01:
        mods |= MOD_SHIFT
    if flags & 0x02:
        mods |= MOD_CONTROL
    if flags & 0x04:
        mods |= MOD_ALT
    return (mods, vk)


def detect_lnk_conflicts(
    target_mods: int,
    target_vk: int,
    lnk_paths: list[Path] | None = None,
) -> list[str]:
    """Baslat Menusu / Masaustu .lnk kisayollarinda ayni tus kombinasyonunu arar."""
    if lnk_paths is None:
        search_dirs = [
            Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu",
            Path.home() / "Desktop",
            Path.home() / "OneDrive" / "Desktop",
        ]
        lnk_paths = []
        for d in search_dirs:
            if d.exists():
                lnk_paths.extend(d.rglob("*.lnk"))

    clean_target_mods = target_mods & ~MOD_NOREPEAT
    conflicts: list[str] = []
    for p in lnk_paths:
        parsed = _read_lnk_hotkey(p)
        if parsed is None:
            continue
        mods, vk = parsed
        if mods == clean_target_mods and vk == target_vk:
            conflicts.append(f"Windows kisayol dosyasi ayni tusu tutuyor: {p}")
    return conflicts


def detect_hotkey_conflicts(target_mods: int, target_vk: int) -> list[str]:
    conflicts: list[str] = []
    conflicts.extend(detect_nvidia_shadowplay_conflicts(target_mods, target_vk))
    conflicts.extend(detect_lnk_conflicts(target_mods, target_vk))
    return conflicts


def get_desktop_info() -> dict[str, object]:
    """Mevcut surecin Windows istasyonu, masaustu ve oturum kimligini dondurur."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    pid = os.getpid()
    session_id = wintypes.DWORD(0)
    kernel32.ProcessIdToSessionId(pid, ctypes.byref(session_id))

    def _uoi_name(handle: int) -> str:
        if not handle:
            return ""
        buf = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD(0)
        ok = user32.GetUserObjectInformationW(handle, 2, buf, ctypes.sizeof(buf), ctypes.byref(needed))
        return buf.value if ok else ""

    winsta = _uoi_name(user32.GetProcessWindowStation())
    desktop = _uoi_name(user32.GetThreadDesktop(kernel32.GetCurrentThreadId()))
    return {
        "pid": pid,
        "session_id": int(session_id.value),
        "window_station": winsta,
        "desktop": desktop,
    }


def write_status(status_path: Path, payload: dict[str, object]) -> None:
    status_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = status_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(status_path)


def _get_process_basename(pid: int) -> str:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name
        return ""
    finally:
        kernel32.CloseHandle(handle)


def focus_existing_mamed_window() -> str | None:
    """Acik bir Mamed tarayici penceresi bulursa one getirir ve basligini dondurur."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    matched: list[tuple[int, str]] = []

    def _cb(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if "mamed" not in title.lower():
            return True

        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return True
        proc_name = _get_process_basename(int(pid.value))
        if is_mamed_browser_window(proc_name, title):
            matched.append((hwnd, f"{proc_name} | {title}"))
            return False
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)
    if not matched:
        return None

    hwnd, label = matched[0]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    else:
        user32.ShowWindow(hwnd, SW_SHOW)

    fg = user32.GetForegroundWindow()
    fg_pid = wintypes.DWORD(0)
    fg_thread = user32.GetWindowThreadProcessId(fg, ctypes.byref(fg_pid)) if fg else 0
    target_pid = wintypes.DWORD(0)
    target_thread = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(target_pid))
    cur_thread = kernel32.GetCurrentThreadId()

    attached_fg = False
    attached_target = False
    try:
        if fg_thread and fg_thread != cur_thread:
            attached_fg = bool(user32.AttachThreadInput(cur_thread, fg_thread, True))
        if target_thread and target_thread != cur_thread:
            attached_target = bool(user32.AttachThreadInput(cur_thread, target_thread, True))
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
    finally:
        if attached_target:
            user32.AttachThreadInput(cur_thread, target_thread, False)
        if attached_fg:
            user32.AttachThreadInput(cur_thread, fg_thread, False)

    return label


def launch_mamed_async(launcher_script: Path, logger: logging.Logger) -> None:
    """launch_mamed_hud.ps1 betigini bloklamadan arka planda calistirir."""
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-File",
        str(launcher_script),
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(launcher_script.parent.parent),
        creationflags=CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    logger.info("Mamed baslatma betigi tetiklendi (PID=%s).", proc.pid)


def setup_logger(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("mamed_hotkey_listener")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = RotatingFileHandler(
        log_path,
        maxBytes=1_048_576,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    return logger


def run_listener(hotkey_str: str = "ctrl+alt+m", retry_seconds: int = 5, once_check: bool = False) -> int:
    log_path = get_log_path()
    status_path = get_status_path()
    logger = setup_logger(log_path)
    mods, vk = parse_hotkey(hotkey_str)
    desk = get_desktop_info()

    if once_check:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        ctypes.set_last_error(0)
        ok = bool(user32.RegisterHotKey(None, HOTKEY_ID, mods, vk))
        err = ctypes.get_last_error() if not ok else 0
        if ok:
            user32.UnregisterHotKey(None, HOTKEY_ID)
        conflicts = [] if ok else detect_hotkey_conflicts(mods, vk)
        payload = {
            **desk,
            "hotkey": hotkey_str,
            "registered": ok,
            "win32_error": err,
            "conflict_details": conflicts,
            "checked_at": _now_iso(),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if ok else 1

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ctypes.set_last_error(0)
    mutex_handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
    if not mutex_handle:
        err = ctypes.get_last_error()
        logger.error("Tek-ornek mutex olusturulamadi (win32=%s).", err)
        return 1
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        logger.info("Mamed kisayol dinleyicisi zaten calisiyor; ikinci ornek sonlandirildi.")
        kernel32.CloseHandle(mutex_handle)
        return 0

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    started_at = _now_iso()
    last_triggered_at: str | None = None
    last_trigger_monotonic = 0.0
    registered = False
    last_err = 0
    conflicts: list[str] = []

    def _try_register(log_conflict: bool = True) -> bool:
        nonlocal registered, last_err, conflicts
        ctypes.set_last_error(0)
        ok = bool(user32.RegisterHotKey(None, HOTKEY_ID, mods, vk))
        if ok:
            registered = True
            last_err = 0
            conflicts = []
            logger.info("Sistem geneli kisayol kaydedildi: %s (desktop=%s)", hotkey_str, desk.get("desktop"))
        else:
            registered = False
            last_err = ctypes.get_last_error()
            conflicts = detect_hotkey_conflicts(mods, vk) if last_err == ERROR_HOTKEY_ALREADY_REGISTERED else []
            if log_conflict:
                detail = "; ".join(conflicts) if conflicts else "Bilinmeyen kaynak"
                logger.warning(
                    "Kisayol kaydedilemedi (%s, win32=%s). Cakisma: %s",
                    hotkey_str,
                    last_err,
                    mask_sensitive_text(detail),
                )
        write_status(
            status_path,
            {
                **desk,
                "hotkey": hotkey_str,
                "registered": registered,
                "win32_error": last_err,
                "conflict_details": conflicts,
                "started_at": started_at,
                "updated_at": _now_iso(),
                "last_triggered_at": last_triggered_at,
            },
        )
        return ok

    logger.info(
        "Mamed kisayol dinleyicisi baslatildi (PID=%s, Session=%s, Desktop=%s, Hotkey=%s).",
        desk["pid"],
        desk["session_id"],
        desk["desktop"],
        hotkey_str,
    )

    if not _try_register(log_conflict=True):
        retry_ms = max(1, retry_seconds) * 1000
        user32.SetTimer(None, RETRY_TIMER_ID, retry_ms, None)
        logger.info("Kisayol bosaldiginda otomatik kaydetmek icin %s sn aralikla yeniden denenecek.", retry_seconds)

    msg = wintypes.MSG()
    launcher_script = get_launcher_script_path()

    try:
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret == 0 or ret == -1:
                break
            if msg.message == WM_TIMER and msg.wParam == RETRY_TIMER_ID:
                if not registered:
                    if _try_register(log_conflict=False):
                        user32.KillTimer(None, RETRY_TIMER_ID)
            elif msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                now_m = time.monotonic()
                if now_m - last_trigger_monotonic < 0.8:
                    continue
                last_trigger_monotonic = now_m
                last_triggered_at = _now_iso()
                logger.info("Kisayol tetiklendi: %s", hotkey_str)
                try:
                    focused = focus_existing_mamed_window()
                    if focused:
                        logger.info("Mevcut Mamed penceresi one getirildi: %s", focused)
                    else:
                        launch_mamed_async(launcher_script, logger)
                except Exception as exc:
                    logger.exception("Kisayol islenirken hata olustu: %s", mask_sensitive_text(str(exc)))
                write_status(
                    status_path,
                    {
                        **desk,
                        "hotkey": hotkey_str,
                        "registered": registered,
                        "win32_error": last_err,
                        "conflict_details": conflicts,
                        "started_at": started_at,
                        "updated_at": _now_iso(),
                        "last_triggered_at": last_triggered_at,
                    },
                )
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        if registered:
            user32.UnregisterHotKey(None, HOTKEY_ID)
        user32.KillTimer(None, RETRY_TIMER_ID)
        kernel32.ReleaseMutex(mutex_handle)
        kernel32.CloseHandle(mutex_handle)
        logger.info("Mamed kisayol dinleyicisi durduruldu.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Mamed Windows sistem geneli kisayol dinleyicisi")
    parser.add_argument("--hotkey", default="ctrl+alt+m", help="Dinlenecek tus kombinasyonu (varsayilan: ctrl+alt+m)")
    parser.add_argument("--retry-seconds", type=int, default=5, help="Cakisma durumunda yeniden deneme suresi (sn)")
    parser.add_argument("--once-check", action="store_true", help="Tek seferlik kisayol uygunluk kontrolu yap ve cik")
    args = parser.parse_args()
    return run_listener(hotkey_str=args.hotkey, retry_seconds=args.retry_seconds, once_check=args.once_check)


if __name__ == "__main__":
    sys.exit(main())
