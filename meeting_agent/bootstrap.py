"""Установка и обновление пакетов Python (PyTorch, Whisper, GigaAM, pyannote…) через uv — с PyPI, строго
версии из списка requirements-<платформа>.lock.

Windows: маленький установщик приходит без пакетов — Meeting Agent.exe открывает окно консоли и ставит их.
Mac (с 1.5): Python и пакеты лежат не внутри приложения, а в папке данных (runtime/) — оболочка показывает
окно «Установка компонентов» и выводит в него ход установки. Ставится при первом запуске и после обновления
кода, если поменялся список. Запуск: python -m meeting_agent bootstrap [--check]
"""

import os
import subprocess
import sys
import time
from pathlib import Path

from . import config
from .osutil import IS_WIN
from .updates import KEEP, SOURCES, _manifest, _vt, lock_file, lock_hash, overlay_dir, platform, shell_version, site_packages

LLAMA_INDEX = "https://abetlen.github.io/llama-cpp-python/whl/cpu"
# uv не входит в маленький установщик (GitVerse не принимает файлы больше ~12 МБ): берём его с PyPI,
# как и остальные пакеты, и сверяем контрольную сумму.
if IS_WIN:
    UV_WHEEL = ("https://files.pythonhosted.org/packages/dd/ef/ceeabf3631117f7354787d9fb1ffe943899e59ed4b887a696ed547e47e76/"
                "uv-0.13.9-py3-none-win_amd64.whl")
    UV_SHA256 = "6a44b2ed8111d2f60d2b92627c5dccda3f7ed7570b18ce115e569cd68c28246b"
    UV_MEMBER = "uv-0.13.9.data/scripts/uv.exe"
    UV_NAME = "uv.exe"
    NATIVE = ("llama-cpp-python", "torch", "ctranslate2")  # только готовые сборки для Windows
else:
    UV_WHEEL = ("https://files.pythonhosted.org/packages/f3/a7/a8d7b074798c7fb3b26b37584b8fe2e29df2ccdc24ce7902caab2a53edbf/"
                "uv-0.13.9-py3-none-macosx_11_0_arm64.whl")
    UV_SHA256 = "66c512266d633bef48384a1f53003f41f2a8e7b36fe6e7fbfd3e0fd52d96fa63"
    UV_MEMBER = "uv-0.13.9.data/scripts/uv"
    UV_NAME = "uv"
    NATIVE = ("torch", "mlx", "mlx-metal")


def uv_exe() -> Path | None:
    exe = Path(sys.executable).resolve()
    for p in (exe.parents[1] / "tools" / UV_NAME, exe.parent / "Scripts" / UV_NAME, config.HOME / "tools" / UV_NAME):
        if p.exists():
            return p
    return _download_uv()


def _download_uv() -> Path | None:
    """uv.exe из колеса на PyPI (~15 МБ) в <данные>/tools — только стандартная библиотека Python."""
    import hashlib
    import urllib.request
    import zipfile

    dest = config.HOME / "tools" / UV_NAME
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
            if not IS_WIN:
                dest.chmod(0o755)
            wheel.unlink(missing_ok=True)
            return dest
        except Exception as e:
            print(f"  не получилось ({e}), повтор…")
            time.sleep(5 * (attempt + 1))
    wheel.unlink(missing_ok=True)
    return None


def _pause() -> None:
    if IS_WIN:  # окно консоли закрылось бы сразу; на Mac ход установки показывает окно оболочки
        input("\nНажмите Enter, чтобы закрыть окно…")


def _fetch_latest_code() -> Path | None:
    """Универсальный установщик: на свежей установке (или при обновлении пакетов) тихо проверяет
    последнюю версию кода на GitHub / GitLab / GitVerse (только стандартная библиотека, без requests).
    Если вышла версия новее встроенной и совместимая с оболочкой — скачивает её сразу, чтобы установить
    актуальный список пакетов и сразу запустить свежую версию без повторного перезапуска."""
    import base64
    import json
    import shutil
    import urllib.request
    import zipfile

    cur_ver = _manifest(config.ROOT).get("app", "0")
    my_shell = shell_version()
    plat = platform()
    best: tuple[str, dict] | None = None
    for name in ("GitHub", "GitLab", "GitVerse"):
        src = SOURCES.get(name)
        if not src:
            continue
        try:
            with urllib.request.urlopen(src["manifest"], timeout=10) as r:
                raw = r.read()
            m = (json.loads(base64.b64decode(json.loads(raw)["content"]).decode("utf-8"))
                 if name == "GitVerse" else json.loads(raw.decode("utf-8")))
            if best is None or _vt(m.get("app", "0")) > _vt(best[1].get("app", "0")):
                best = (name, m)
        except Exception:  # noqa: BLE001 — нет доступа к площадке, пробуем следующую
            continue
    if not best:
        return None
    first, m = best
    latest = m.get("app", "0")
    need_shell = (m.get("shell") or {}).get(plat, 1)
    if _vt(latest) <= _vt(cur_ver) or need_shell > my_shell:
        return None

    base = overlay_dir()
    final = base / latest
    if (final / "meeting_agent" / "__init__.py").exists():
        (base / "current.txt").write_text(latest, encoding="utf-8")
        return final

    print(f"Найдена свежая версия {latest} (установщик {cur_ver}) — скачиваю обновление…", flush=True)
    base.mkdir(parents=True, exist_ok=True)
    tmp = base / f".{latest}.bootstrap"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    archive = base / ".bootstrap-code.zip"
    order = [first] + [n for n in ("GitHub", "GitLab", "GitVerse") if n != first and n in SOURCES]
    downloaded = False
    for name in order:
        try:
            with urllib.request.urlopen(SOURCES[name]["archive"], timeout=30) as r, open(archive, "wb") as out:
                while chunk := r.read(1 << 18):
                    out.write(chunk)
            downloaded = True
            break
        except Exception:  # noqa: BLE001
            continue
    if not downloaded:
        shutil.rmtree(tmp, ignore_errors=True)
        archive.unlink(missing_ok=True)
        return None
    try:
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                parts = Path(info.filename).parts
                if len(parts) < 2 or parts[1] not in KEEP or info.is_dir():
                    continue
                dest = tmp.joinpath(*parts[1:])
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as s_in, open(dest, "wb") as s_out:
                    shutil.copyfileobj(s_in, s_out)
    except Exception:  # noqa: BLE001
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    finally:
        archive.unlink(missing_ok=True)

    got = _manifest(tmp).get("app")
    if not (tmp / "meeting_agent" / "__init__.py").exists() or not got:
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    final = base / got
    shutil.rmtree(final, ignore_errors=True)
    tmp.rename(final)
    (base / "current.txt").write_text(got, encoding="utf-8")
    (base / "pending.txt").unlink(missing_ok=True)
    print(f"Код обновлён до {got}.\n", flush=True)
    return final


def run() -> int:
    print("Meeting Agent — установка компонентов\n", flush=True)
    fresh = _fetch_latest_code()
    lock = lock_file(fresh) if fresh else lock_file()
    print(f"Скачиваются пакеты Python (около {'350 МБ' if IS_WIN else '600 МБ'}): распознавание речи, разделение "
          "собеседников, локальная модель протоколов. Это нужно один раз, потом — только при обновлениях.\n", flush=True)
    uv = uv_exe()
    if not uv or not lock.exists():
        print("Не удалось получить установщик пакетов uv (нужен доступ к files.pythonhosted.org) или нет списка пакетов.")
        _pause()
        return 2
    python = Path(sys.executable).resolve().with_name("python.exe") if IS_WIN else Path(sys.executable)
    cmd = [str(uv), "pip", "install", "--python", str(python), "--no-deps", "-r", str(lock)]
    if IS_WIN:
        cmd += ["--extra-index-url", LLAMA_INDEX, "--index-strategy", "unsafe-best-match"]
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
    _pause()
    return 1
