"""Различия macOS и Windows в одном месте: уведомления, Finder/Проводник, приоритет процессов,
питание, блокировка второго экземпляра, устройство для PyTorch."""

import os
import subprocess
import sys
from pathlib import Path

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
PC = "этом компьютере" if IS_WIN else "этом Mac"  # «на этом Mac» в сообщениях

if IS_WIN:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Без объявленных типов ctypes обрезает 64-битные HANDLE до 32-битного int.
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.SetPriorityClass.restype = wintypes.BOOL
    CREATE_NO_WINDOW = 0x08000000


def popen_kwargs() -> dict:
    """Дочерний процесс без своего окна консоли (Windows) / в своей группе (macOS)."""
    return {"creationflags": CREATE_NO_WINDOW} if IS_WIN else {"start_new_session": True}


def notify(title: str, message: str) -> None:
    """Уведомление системы. На Windows его показывает значок в трее (winapp), здесь — ничего."""
    if IS_MAC:
        import json

        script = (f"display notification {json.dumps(message, ensure_ascii=False)} "
                  f"with title {json.dumps(title, ensure_ascii=False)}")
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def reveal(path: Path) -> None:
    """Файл — выделить в Finder/Проводнике, папку — открыть."""
    if IS_WIN:
        if path.is_file():
            # Строкой, а не списком: explorer разбирает командную строку по-своему и ждёт /select,"путь".
            subprocess.run(f'explorer /select,"{path}"', check=False)
        else:
            os.startfile(str(path))  # noqa: S606 — только наши папки
    else:
        subprocess.run(["open", "-R", str(path)] if path.is_file() else ["open", str(path)], check=False)


def open_path(path: Path) -> None:
    if IS_WIN:
        os.startfile(str(path))  # noqa: S606
    else:
        subprocess.run(["open", str(path)], check=False)


def low_priority_cmd(cmd: list[str]) -> list[str]:
    """Пониженный приоритет macOS (utility): процессор в первую очередь у Zoom. Не -b: тот душит и диск.
    На Windows процесс понижает приоритет сам (lower_priority)."""
    if Path("/usr/sbin/taskpolicy").exists():
        return ["/usr/sbin/taskpolicy", "-c", "utility", *cmd]
    return cmd


def lower_priority() -> None:
    if IS_WIN:
        BELOW_NORMAL = 0x00004000
        _k32.SetPriorityClass(_k32.GetCurrentProcess(), BELOW_NORMAL)
        return
    try:
        os.nice(10)
    except OSError:
        pass


def on_battery() -> bool:
    if IS_WIN:
        class Status(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                        ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                        ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]
        st = Status()
        return bool(_k32.GetSystemPowerStatus(ctypes.byref(st))) and st.ACLineStatus == 0
    try:
        out = subprocess.run(["/usr/bin/pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "Battery Power" in out


def lock_exclusive(f) -> bool:
    """Неблокирующая блокировка открытого файла. False — файл уже держит другой процесс."""
    try:
        if IS_WIN:
            import msvcrt

            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def process_alive(pid: int) -> bool:
    if IS_WIN:
        SYNCHRONIZE, WAIT_TIMEOUT = 0x00100000, 0x102
        h = _k32.OpenProcess(SYNCHRONIZE, False, pid)
        if not h:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: процесс есть, просто чужой
        try:
            return _k32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
        finally:
            _k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def torch_device(preferred: str | None = None):
    """mps (видеоядра Apple), cuda (видеокарта NVIDIA) или cpu — что есть на этом компьютере."""
    import torch

    if preferred != "cpu":
        if torch.backends.mps.is_available():
            return torch.device("mps")
        if torch.cuda.is_available():
            return torch.device("cuda")
    return torch.device("cpu")


def empty_cache(device) -> None:
    import torch

    kind = getattr(device, "type", str(device))
    if kind == "mps":
        torch.mps.empty_cache()
    elif kind == "cuda":
        torch.cuda.empty_cache()
