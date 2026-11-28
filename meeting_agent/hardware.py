"""Что за компьютер и какой вариант работы ему подходит — для экрана первого запуска на Windows.

Одна сборка на все Windows-компьютеры: при первом запуске приложение смотрит на процессор, память
и видеокарту и предлагает модель распознавания, кто пишет протоколы и нужно ли скачивать ускорение NVIDIA.
На Mac (только Apple Silicon) подбор не нужен — подходят настройки по умолчанию.
"""

import os
import platform
import subprocess

from . import osutil

_info: dict | None = None
MIN_LOCAL_LLM_RAM_GB = 12


def local_llm_supported(hw: dict | None = None) -> bool:
    """Локальный Qwen 2.5 7B требует от 12 ГБ ОЗУ: на машинах с 8 ГБ его загрузка заблокирована."""
    hw = hw or info()
    ram = hw.get("ram_gb") or 0
    return ram == 0 or ram >= MIN_LOCAL_LLM_RAM_GB


def info() -> dict:
    global _info
    if _info is None:
        cpu, ram = _cpu_ram()
        gpu = None
        if osutil.IS_WIN:
            from . import gpu as gpu_mod

            nv = gpu_mod.nvidia()
            if nv:
                gpu = {"kind": "nvidia", "name": nv["name"], "vram_gb": round(nv["vram_mb"] / 1024, 1)}
        elif osutil.IS_MAC:
            gpu = {"kind": "apple", "name": cpu}  # видеоядра в том же чипе, память общая
        _info = {"os": _os_name(), "cpu": cpu, "cores": os.cpu_count() or 0, "ram_gb": ram, "gpu": gpu}
    return _info


def _os_name() -> str:
    if osutil.IS_WIN:
        build = int(platform.version().split(".")[-1] or 0)
        return f"Windows {'11' if build >= 22000 else '10'}"
    if osutil.IS_MAC:
        return f"macOS {platform.mac_ver()[0]}"
    return platform.system()


def _cpu_ram() -> tuple[str, float]:
    if osutil.IS_WIN:
        import ctypes
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                cpu = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        except OSError:
            cpu = platform.processor()

        class MemStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MemStatus()
        m.dwLength = ctypes.sizeof(MemStatus)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return cpu, round(m.ullTotalPhys / 2**30)
    try:
        cpu = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
        ram = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip())
        return cpu or platform.processor(), round(ram / 2**30)
    except (OSError, ValueError):
        return platform.processor(), 0


def recommend(hw: dict | None = None) -> dict:
    """{asr, llm, gpu_accel, notes: {asr, llm, gpu}} — лучший вариант для этого компьютера."""
    from .gpu import MIN_LLM_VRAM_MB

    hw = hw or info()
    ram, gpu = hw["ram_gb"], hw["gpu"] or {}
    nvidia = gpu.get("kind") == "nvidia"
    big_gpu = nvidia and gpu["vram_gb"] * 1024 >= MIN_LLM_VRAM_MB

    # Встречи на русском: GigaAM точнее Whisper на русской речи и быстрее всех на обычном процессоре.
    asr = "gigaam"
    asr_note = ("GigaAM обучен на русской речи и созвонах: точнее Whisper на русском и быстро работает "
                "даже на процессоре. Для встреч на других языках выберите Whisper.")

    if not local_llm_supported(hw):
        llm, llm_note = "gemini", (f"На компьютере {ram:.0f} ГБ оперативной памяти — для локального Qwen 2.5 7B "
                                   "нужно от 12 ГБ. Локальная модель отключена, используйте облачную "
                                   "(например, GigaChat или Gemini Flash).")
    elif big_gpu and ram >= 16:
        llm, llm_note = "local", (f"{gpu['name']} ({gpu['vram_gb']:.0f} ГБ) потянет Qwen целиком: протоколы "
                                  "пишутся на этом компьютере за пару минут, ничего не уходит в интернет. "
                                  "Нужно скачать ускорение NVIDIA (шаг ниже).")
    else:
        why = (f"видеокарта {gpu['name']} мала для Qwen ({gpu['vram_gb']:.0f} ГБ)" if nvidia
               else "видеокарты NVIDIA нет")
        llm, llm_note = "gemini", (f"Здесь {why}, а на процессоре Qwen пишет протокол часовой встречи 20–50 минут. "
                                   "Gemini Flash бесплатен с ключом и пишет протокол за минуту. "
                                   "Если расшифровки нельзя отправлять в облако — выберите Qwen, он просто медленнее.")

    gpu_accel = bool(big_gpu)
    if big_gpu:
        gpu_note = "Скачайте библиотеки CUDA: Qwen и Whisper будут считать на видеокарте в разы быстрее."
    elif nvidia:
        gpu_note = ("Видеопамяти мало для Qwen; ускорение поможет только Whisper. С GigaAM оно не нужно.")
    else:
        gpu_note = ""
    return {"asr": asr, "llm": llm, "gpu_accel": gpu_accel,
            "notes": {"asr": asr_note, "llm": llm_note, "gpu": gpu_note}}
