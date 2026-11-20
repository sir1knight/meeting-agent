"""Обновления по компонентам — с GitVerse, без переустановки приложения целиком.

Компоненты:
  • код и интерфейс (meeting_agent, ui, templates, config.toml) — архив ветки main с GitVerse, распаковывается
    в <данные>/app-code/<версия>; при запуске __main__.py берёт его вместо встроенного;
  • пакеты Python (только Windows) — список requirements-windows.lock из новой версии кода; ставятся через uv
    при следующем запуске приложения (bootstrap.py), пока ничего из них не загружено;
  • оболочка (Swift-приложение на Mac, Meeting Agent.exe и Python на Windows) — так не обновляется:
    если новой версии кода нужна новая оболочка, приложение предложит скачать его целиком;
  • модели — отдельно, в карточке «Модели».

Что нового и какой оболочке подходит — version.json в корне репозитория.
"""

import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from . import config, osutil  # requests — внутри функций: в установщике пакетов ещё нет

log = logging.getLogger(__name__)

REPO = "sir1knight/Meeting_agent"
API = f"https://gitverse.ru/api/repos/{REPO}"
PAGE = f"https://gitverse.ru/{REPO}"
BRANCH = "main"
KEEP = ("meeting_agent", "ui", "templates", "config.toml", "version.json", "requirements-windows.lock")


def _vt(v: str) -> tuple:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return (0,)


def _manifest(root: Path) -> dict:
    try:
        return json.loads((root / "version.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def platform() -> str:
    return "windows" if osutil.IS_WIN else "mac"


def bundled_root() -> Path:
    return Path(os.environ.get("MEETING_AGENT_BUNDLED_ROOT") or config.ROOT)


def overlay_dir() -> Path:
    return config.HOME / "app-code"


def shell_version() -> int:
    exe = Path(sys.executable).resolve()
    for p in (exe.parents[2] / "shell.json", exe.parents[1] / "shell.json"):
        try:
            return int(json.loads(p.read_text(encoding="utf-8")).get("shell", 1))
        except (OSError, ValueError, IndexError):
            continue
    return 1


def lock_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def site_packages() -> Path:
    return Path(sys.executable).resolve().parent / "Lib" / "site-packages"


def installed_packages_hash() -> str:
    try:
        return (site_packages() / ".meeting-agent-lock").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def packages_needed(root: Path | None = None) -> bool:
    """Windows: пакеты не совпадают со списком этой версии кода (или ещё не ставились)."""
    if not osutil.IS_WIN:
        return False
    want = lock_hash((root or config.ROOT) / "requirements-windows.lock")
    return bool(want) and want != installed_packages_hash()


# ---------- запросы к GitVerse ----------

def _get(url: str, quick: bool = False, **kw):
    """GitVerse ограничивает частоту анонимных запросов (ответ 429) — ждём и повторяем
    (quick — одна попытка: для тихой проверки при запуске)."""
    import requests

    last = None
    for wait in ((0,) if quick else (0, 15, 30, 45)):
        if wait:
            log.info("GitVerse просит подождать, повтор через %d с…", wait)
            time.sleep(wait)
        try:
            r = requests.get(url, timeout=20 if quick else 60, **kw)
        except requests.RequestException as e:
            last = e
            continue
        if r.status_code == 200:
            return r
        last = r.status_code
        if r.status_code not in (429, 500, 502, 503, 504):
            break
    if last == 429:
        raise RuntimeError("GitVerse временно ограничил запросы — попробуйте через пару минут")
    if isinstance(last, Exception):
        raise RuntimeError("Нет связи с GitVerse: проверьте интернет") from last
    raise RuntimeError(f"GitVerse ответил ошибкой {last}")


def remote_manifest(max_age_hours: float = 0) -> dict:
    """version.json с GitVerse. max_age_hours > 0 — можно взять сохранённый ответ не старше этого
    (автопроверка при запуске не дёргает GitVerse чаще раза в сутки)."""
    cache = config.HOME / "update-check.json"
    if max_age_hours and cache.exists() and time.time() - cache.stat().st_mtime < max_age_hours * 3600:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except ValueError:
            pass
    r = _get(f"{API}/contents/version.json", quick=bool(max_age_hours), params={"ref": BRANCH})
    m = json.loads(base64.b64decode(r.json()["content"]).decode("utf-8"))
    cache.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    return m


# ---------- что можно обновить ----------

def status() -> dict:
    """Без обращения к сети: что установлено сейчас."""
    running = _manifest(config.ROOT)
    bundled = _manifest(bundled_root())
    return {"platform": platform(), "version": running.get("app", "1.0.0"),
            "bundled": bundled.get("app", "1.0.0"), "shell": shell_version(),
            "overlay": config.ROOT.resolve() != bundled_root().resolve(),
            "packages_pending": packages_needed(), "page": PAGE}


def check(max_age_hours: float = 0) -> dict:
    st = status()
    m = remote_manifest(max_age_hours)
    latest = m.get("app", "0")
    need_shell = (m.get("shell") or {}).get(platform(), 1)
    st.update(latest=latest, notes=m.get("notes", ""),
              code_available=_vt(latest) > _vt(st["version"]),
              shell_needed=need_shell > st["shell"], shell_latest=need_shell)
    if osutil.IS_WIN:
        want = (m.get("packages") or {}).get("windows_hash", "")
        st["packages_changed"] = bool(want) and want != installed_packages_hash()
    return st


def apply_code() -> dict:
    """Скачивает код с GitVerse, проверяет, что он запускается, и делает его текущим."""
    m = remote_manifest()
    version = m.get("app", "0")
    if (m.get("shell") or {}).get(platform(), 1) > shell_version():
        raise RuntimeError("Этой версии нужна новая версия приложения целиком — скачайте его со страницы проекта")
    base = overlay_dir()
    base.mkdir(parents=True, exist_ok=True)
    tmp = base / f".{version}.download"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    archive = base / ".code.zip"
    archive.write_bytes(_get(f"{API}/archive/{BRANCH}.zip").content)
    try:
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                parts = Path(info.filename).parts
                if len(parts) < 2 or parts[1] not in KEEP or info.is_dir():
                    continue  # первая папка архива — имя репозитория
                dest = tmp.joinpath(*parts[1:])
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
    finally:
        archive.unlink(missing_ok=True)
    got = _manifest(tmp).get("app")
    if not (tmp / "meeting_agent" / "__init__.py").exists() or not got:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("Скачанный архив неполный — попробуйте ещё раз")
    # Проба: новая версия импортируется на этом Python и с этими пакетами.
    probe = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                            "import meeting_agent.server, meeting_agent.pipeline", str(tmp)],
                           capture_output=True, text=True, timeout=180, cwd=str(tmp),
                           env=dict(os.environ, MEETING_AGENT_NO_OVERLAY="1"), **osutil.popen_kwargs())
    if probe.returncode != 0 and not (osutil.IS_WIN and "ModuleNotFoundError" in probe.stderr):
        shutil.rmtree(tmp, ignore_errors=True)
        err = (probe.stderr.strip().splitlines() or ["?"])[-1]
        raise RuntimeError(f"Новая версия не запускается на этом компьютере: {err}")
    final = base / got
    shutil.rmtree(final, ignore_errors=True)
    tmp.rename(final)
    (base / "current.txt").write_text(got, encoding="utf-8")
    for old in base.iterdir():  # прошлые версии больше не нужны
        if old.is_dir() and old.name not in (got,) and not old.name.startswith("."):
            shutil.rmtree(old, ignore_errors=True)
    log.info("Код обновлён до %s", got)
    return {"version": got, "packages_pending": packages_needed(final)}


def reset_code() -> None:
    """Вернуться к версии, встроенной в приложение."""
    (overlay_dir() / "current.txt").unlink(missing_ok=True)
    shutil.rmtree(overlay_dir(), ignore_errors=True)
