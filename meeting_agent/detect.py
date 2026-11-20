"""Определяет, идёт ли сейчас встреча: Zoom — по заголовкам его окон; на Windows ещё Teams, Яндекс Телемост
и звонки в браузере — по тому, какая программа сейчас пишет микрофон (Windows отмечает это в реестре)."""

import logging

from .osutil import IS_WIN

log = logging.getLogger(__name__)
_warned_no_titles = False


def zoom_windows() -> list[tuple[str, str]]:
    """(владелец, заголовок) всех окон Zoom."""
    return _windows_win(lambda owner: "zoom" in owner) if IS_WIN else _windows_mac()


BROWSERS = ("chrome.exe", "msedge.exe", "browser.exe", "firefox.exe", "opera.exe", "opera_gx.exe", "brave.exe",
            "vivaldi.exe", "yandex.exe", "arc.exe", "dia.exe")
# Программы звонков (Windows, по имени exe). corp — российские и корпоративные площадки; включаются одной галочкой.
CALL_APPS = {
    "teams": ("ms-teams.exe", "teams.exe", "msteams"),
    "telemost": ("telemost.exe", "yandextelemost.exe", "telemost"),
    "jazz": ("jazz.exe", "sberjazz.exe", "salutejazz"),
    "trueconf": ("trueconf.exe",),
    "ktalk": ("ktalk.exe",),
    "vkcalls": ("vkcalls.exe",),
    "dion": ("dion.exe",),
    "webex": ("webex.exe", "ciscocollabhost.exe", "atmgr.exe"),
}
CORP = ("jazz", "trueconf", "ktalk", "vkcalls", "dion", "webex")
WEB_CALL_TITLES = ("meet.google.com", "google meet", "meet –", "meet -", "телемост", "telemost", "microsoft teams",
                   "teams.microsoft", "jitsi", "jazz", "контур.толк", "webex")


def mic_users_win() -> list[str]:
    """Программы, которые сейчас пишут микрофон: аудиосессии Windows (точно и сразу), плюс журнал
    доступа к микрофону в реестре (на случай, если сессии не прочитались). Имена — в нижнем регистре."""
    users = []
    try:
        users = mic_sessions_win()
    except Exception as e:  # noqa: BLE001 — нет COM или WASAPI: остаётся реестр
        log.debug("Аудиосессии не прочитались: %s", e)
    return users + [u for u in _mic_users_registry() if u not in users]


# --- аудиосессии WASAPI через ctypes (без pycaw/comtypes) ---
_CLSID_ENUM = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
_IID_ENUM = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
_IID_ASM2 = "{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}"
_IID_ASC2 = "{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}"


def _guid(text: str):
    import ctypes

    class GUID(ctypes.Structure):
        _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16), ("d3", ctypes.c_uint16), ("d4", ctypes.c_ubyte * 8)]

    g = GUID()
    ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
    return g


def _call(obj, index: int, *args, restype=None):
    """Метод COM-объекта по номеру в таблице (vtable)."""
    import ctypes

    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    types = [ctypes.c_void_p if type(a).__name__ == "CArgObject" else type(a) for a in args]
    fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *types)(vtbl[index])
    hr = fn(obj, *args)
    if hr < 0:
        raise OSError(f"COM 0x{hr & 0xFFFFFFFF:08X}")
    return hr


def _release(obj) -> None:
    if obj:
        _call(obj, 2)


def mic_sessions_win() -> list[str]:
    """exe программ с активной сессией записи на любом микрофоне (как детектор mymeet).
    Своя запись не считается. Поток должен быть с инициализированным COM (CoInitializeEx)."""
    import ctypes
    import os

    from .osutil import process_image

    CLSCTX_ALL, E_CAPTURE, ACTIVE = 23, 1, 1
    enum = ctypes.c_void_p()
    ctypes.oledll.ole32.CoCreateInstance(ctypes.byref(_guid(_CLSID_ENUM)), None, CLSCTX_ALL,
                                         ctypes.byref(_guid(_IID_ENUM)), ctypes.byref(enum))
    pids: set[int] = set()
    coll = ctypes.c_void_p()
    try:
        _call(enum, 3, ctypes.c_uint(E_CAPTURE), ctypes.c_uint(ACTIVE), ctypes.byref(coll))
        n = ctypes.c_uint()
        _call(coll, 3, ctypes.byref(n))
        for i in range(n.value):
            dev, mgr, sess_enum = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
            try:
                _call(coll, 4, ctypes.c_uint(i), ctypes.byref(dev))
                _call(dev, 3, ctypes.byref(_guid(_IID_ASM2)), ctypes.c_uint(CLSCTX_ALL), ctypes.c_void_p(), ctypes.byref(mgr))
                _call(mgr, 5, ctypes.byref(sess_enum))
                count = ctypes.c_int()
                _call(sess_enum, 3, ctypes.byref(count))
                for j in range(count.value):
                    ctl, ctl2 = ctypes.c_void_p(), ctypes.c_void_p()
                    try:
                        _call(sess_enum, 4, ctypes.c_int(j), ctypes.byref(ctl))
                        state = ctypes.c_int()
                        _call(ctl, 3, ctypes.byref(state))
                        if state.value != 1:  # AudioSessionStateActive
                            continue
                        _call(ctl, 0, ctypes.byref(_guid(_IID_ASC2)), ctypes.byref(ctl2))
                        pid = ctypes.c_uint32()
                        _call(ctl2, 14, ctypes.byref(pid))
                        if pid.value and pid.value != os.getpid():
                            pids.add(pid.value)
                    except OSError:
                        continue
                    finally:
                        _release(ctl2)
                        _release(ctl)
            except OSError:
                continue
            finally:
                _release(sess_enum)
                _release(mgr)
                _release(dev)
    finally:
        _release(coll)
        _release(enum)
    names = [process_image(p) for p in pids]
    return [n.rsplit("\\", 1)[-1].lower() for n in names if n and "meeting agent" not in n.lower()]


def _mic_users_registry() -> list[str]:
    """Журнал доступа к микрофону (Параметры → Конфиденциальность → Микрофон); у идущей записи
    LastUsedTimeStop = 0.
    (Параметры → Конфиденциальность → Микрофон); у идущей записи LastUsedTimeStop = 0.
    Имена — путь к exe (с «#» вместо «\\») или имя пакета Магазина, в нижнем регистре."""
    import winreg

    base = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
    out: list[str] = []

    def scan(path: str) -> None:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            return
        with key:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                if name == "NonPackaged":
                    scan(path + "\\" + name)
                    continue
                try:
                    with winreg.OpenKey(key, name) as sub:
                        stop = winreg.QueryValueEx(sub, "LastUsedTimeStop")[0]
                        start = winreg.QueryValueEx(sub, "LastUsedTimeStart")[0]
                except OSError:
                    continue
                if stop == 0 and start > 0:
                    out.append(name.lower())

    scan(base)
    return [u for u in out if "meeting agent" not in u]  # своя запись не считается


def active_platform(titles: list[str], platforms: list[str], current: str | None = None) -> str | None:
    """Какая встреча идёт: zoom | teams | telemost | meet | None. current — платформа идущей автозаписи:
    для неё признак мягче (вкладку браузера могли переключить — пока браузер пишет микрофон, встреча идёт)."""
    if "zoom" in platforms and in_meeting(titles):
        return "zoom"
    if not IS_WIN:
        return None
    users = mic_users_win()

    def uses(names: tuple) -> bool:
        return any(n in u for u in users for n in names)

    for app in ("teams", "telemost") + CORP:
        enabled = app in platforms or (app in CORP and "corp" in platforms)
        if enabled and uses(CALL_APPS[app]):
            return app
    if "meet" in platforms and any(u.endswith(BROWSERS) for u in users):
        if current == "meet" or _windows_win(lambda o: o.endswith(BROWSERS), WEB_CALL_TITLES):
            return "meet"
    return None


def _windows_mac() -> list[tuple[str, str]]:
    from Quartz import CGWindowListCopyWindowInfo, kCGNullWindowID, kCGWindowListOptionAll

    windows = CGWindowListCopyWindowInfo(kCGWindowListOptionAll, kCGNullWindowID) or []
    out = []
    for w in windows:
        owner = str(w.get("kCGWindowOwnerName") or "")
        if "zoom" in owner.lower():
            out.append((owner, str(w.get("kCGWindowName") or "")))
    return out


def _windows_win(owner_ok, title_words: tuple = ()) -> list[tuple[str, str]]:
    """Видимые окна процессов, чьё имя exe (в нижнем регистре) подходит owner_ok; если заданы title_words —
    только окна, в заголовке которых есть одно из слов. Свёрнутое окно встречи тоже считается видимым."""
    import ctypes
    from ctypes import wintypes

    user32, kernel32 = _win_api()
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
            if owner_ok(owner.lower()):
                n = user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if not title_words or any(w in buf.value.lower() for w in title_words):
                    out.append((owner, buf.value))
        return True

    user32.EnumWindows(visit, 0)
    return out


_api = None


def _win_api():
    """user32/kernel32 с объявленными типами: без них ctypes считает HANDLE/HWND 32-битным int."""
    global _api
    if _api is None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                        ctypes.POINTER(wintypes.DWORD)]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.EnumWindows.restype = wintypes.BOOL
        _api = (user32, kernel32)
    return _api


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
