"""Windows: установка и обновление пакетов Python (PyTorch, faster-whisper, llama.cpp, pyannote…).

Маленький установщик приходит без пакетов: при первом запуске (и после обновления кода, если поменялся
requirements-windows.lock) Meeting Agent.exe открывает окно консоли и ставит их через uv — с PyPI
(и llama-cpp-python со страницы его автора), строго версии из списка. Запуск: python -m meeting_agent bootstrap
"""

import os
import subprocess
import sys
import time
from pathlib import Path

from . import config
from .updates import lock_hash, site_packages

LLAMA_INDEX = "https://abetlen.github.io/llama-cpp-python/whl/cpu"
# uv не входит в маленький установщик (GitVerse не принимает файлы больше ~12 МБ): берём его с PyPI,
# как и остальные пакеты, и сверяем контрольную сумму.
UV_WHEEL = ("https://files.pythonhosted.org/packages/dd/ef/ceeabf3631117f7354787d9fb1ffe943899e59ed4b887a696ed547e47e76/"
            "uv-0.13.9-py3-none-win_amd64.whl")
UV_SHA256 = "6a44b2ed8111d2f60d2b92627c5dccda3f7ed7570b18ce115e569cd68c28246b"
UV_MEMBER = "uv-0.13.9.data/scripts/uv.exe"
NATIVE = ("llama-cpp-python", "torch", "ctranslate2")  # только готовые сборки для Windows


def uv_exe() -> Path | None:
    exe = Path(sys.executable).resolve()
    for p in (exe.parents[1] / "tools" / "uv.exe", exe.parent / "Scripts" / "uv.exe", config.HOME / "tools" / "uv.exe"):
        if p.exists():
            return p
    return _download_uv()


def _download_uv() -> Path | None:
    """uv.exe из колеса на PyPI (~15 МБ) в <данные>/tools — только стандартная библиотека Python."""
    import hashlib
    import urllib.request
    import zipfile

    dest = config.HOME / "tools" / "uv.exe"
    dest.parent.mkdir(parents=True, exist_ok=True)
    wheel = dest.with_name("uv.whl")
    print("Скачиваю установщик пакетов uv (~15 МБ)…")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(UV_WHEEL, timeout=120) as r, open(wheel, "wb") as out:
                while chunk := r.read(1 << 20):
                    out.write(chunk)
            if hashlib.sha256(wheel.read_bytes()).hexdigest() != UV_SHA256:
                raise IOError("контрольная сумма не совпала")
            with zipfile.ZipFile(wheel) as z, z.open(UV_MEMBER) as src, open(dest, "wb") as out:
                out.write(src.read())
            wheel.unlink(missing_ok=True)
            return dest
        except Exception as e:
            print(f"  не получилось ({e}), повтор…")
            time.sleep(5 * (attempt + 1))
    wheel.unlink(missing_ok=True)
    return None


def run() -> int:
    lock = config.ROOT / "requirements-windows.lock"
    print("Meeting Agent — установка компонентов\n")
    print("Скачиваются пакеты Python (около 350 МБ): распознавание речи, разделение собеседников,")
    print("локальная модель протоколов. Это нужно один раз, потом — только при обновлениях.\n")
    uv = uv_exe()
    if not uv or not lock.exists():
        print("Не удалось получить uv.exe (нужен доступ к files.pythonhosted.org) или нет списка пакетов.")
        input("\nНажмите Enter, чтобы закрыть окно…")
        return 2
    python = Path(sys.executable).resolve().with_name("python.exe")
    cmd = [str(uv), "pip", "install", "--python", str(python), "--no-deps", "-r", str(lock),
           "--extra-index-url", LLAMA_INDEX, "--index-strategy", "unsafe-best-match"]
    for pkg in NATIVE:
        cmd += ["--only-binary", pkg]
    env = dict(os.environ, UV_CACHE_DIR=str(config.HOME / "uv-cache"), UV_NO_PROGRESS="0",
               UV_HTTP_TIMEOUT="120", UV_CONCURRENT_DOWNLOADS="4")
    for attempt in range(1, 4):
        started = time.time()
        r = subprocess.run(cmd, env=env)
        if r.returncode == 0:
            (site_packages() / ".meeting-agent-lock").write_text(lock_hash(lock), encoding="utf-8")
            print(f"\nГотово за {int(time.time() - started)} с. Запускаю Meeting Agent…")
            time.sleep(2)
            return 0
        if attempt < 3:
            print(f"\nНе получилось (попытка {attempt} из 3). Повтор через 10 секунд — загрузка продолжится.")
            time.sleep(10)
    print("\nНе удалось скачать пакеты. Проверьте интернет (доступ к pypi.org и files.pythonhosted.org)")
    print("и запустите Meeting Agent снова. Если интернет закрыт прокси — попросите полный архив приложения.")
    input("\nНажмите Enter, чтобы закрыть окно…")
    return 1
