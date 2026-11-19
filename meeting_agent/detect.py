"""Определяет, идёт ли сейчас встреча в Zoom, по заголовкам его окон."""

import logging

from .osutil import IS_WIN

log = logging.getLogger(__name__)
_warned_no_titles = False


def zoom_windows() -> list[tuple[str, str]]:
    """(владелец, заголовок) всех окон Zoom."""
    return _windows_win() if IS_WIN else _windows_mac()


def _windows_mac() -> list[tuple[str, str]]:
    from Quartz import CGWindowListCopyWindowInfo, kCGNullWindowID, kCGWindowListOptionAll

    windows = CGWindowListCopyWindowInfo(kCGWindowListOptionAll, kCGNullWindowID) or []
    out = []
    for w in windows:
        owner = str(w.get("kCGWindowOwnerName") or "")
        if "zoom" in owner.lower():
            out.append((owner, str(w.get("kCGWindowName") or "")))
    return out


def _windows_win() -> list[tuple[str, str]]:
    """Видимые окна процессов Zoom (Zoom.exe). Свёрнутое окно встречи тоже считается видимым."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    names: dict[int, str] = {}

    def process_name(pid: int) -> str:
        if pid not in names:
            names[pid] = ""
            h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(len(buf))
                if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    names[pid] = buf.value.rsplit("\\", 1)[-1]
                kernel32.CloseHandle(h)
        return names[pid]

    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            owner = process_name(pid.value)
            if "zoom" in owner.lower():
                n = user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                out.append((owner, buf.value))
        return True

    user32.EnumWindows(visit, 0)
    return out


def in_meeting(titles: list[str]) -> bool:
    global _warned_no_titles
    wins = zoom_windows()
    wanted = [t.lower() for t in titles]
    if wins and not any(name for _, name in wins) and not _warned_no_titles and not IS_WIN:
        # macOS отдаёт заголовки окон только при разрешении «Запись экрана».
        log.warning("Окна Zoom видны, но без заголовков: дайте Терминалу разрешение "
                    "«Запись экрана и системного звука» и перезапустите агент.")
        _warned_no_titles = True
    return any(name and any(t in name.lower() for t in wanted) for _, name in wins)
