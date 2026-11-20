"""Обновления по компонентам — с GitVerse или GitLab (какой доступен), без переустановки приложения целиком.

Компоненты:
  • код и интерфейс (meeting_agent, ui, templates, config.toml) — архив ветки main, распаковывается
    в <данные>/app-code/<версия>; при запуске __main__.py берёт его вместо встроенного;
  • пакеты Python (только Windows) — список requirements-windows.lock из новой версии кода; ставятся через uv
    при следующем запуске приложения (bootstrap.py), пока ничего из них не загружено;
  • оболочка (Swift-приложение на Mac, Meeting Agent.exe и Python на Windows) — так не обновляется:
    если новой версии кода нужна новая оболочка, приложение предложит скачать его целиком;
  • модели — отдельно, в карточке «Модели».

Что нового и какой оболочке подходит — version.json в корне репозитория. Репозиторий один и тот же на двух
площадках: оба опрашиваются одновременно, берётся более новая версия (при равных — та, что ответила быстрее);
код скачивается оттуда же, а если не вышло — с другой площадки.
"""

import base64
import concurrent.futures as cf
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
BRANCH = "main"
_GL = "https://gitlab.com/api/v4/projects/" + REPO.replace("/", "%2F")
SOURCES = {
    "GitVerse": {"manifest": f"https://gitverse.ru/api/repos/{REPO}/contents/version.json?ref={BRANCH}",
                 "archive": f"https://gitverse.ru/api/repos/{REPO}/archive/{BRANCH}.zip",
                 "page": f"https://gitverse.ru/{REPO}"},
    "GitLab": {"manifest": f"{_GL}/repository/files/version.json/raw?ref={BRANCH}",
               "archive": f"{_GL}/repository/archive.zip?sha={BRANCH}",
               "page": f"https://gitlab.com/{REPO}"},
}
PAGE = SOURCES["GitVerse"]["page"]
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


# ---------- запросы к GitVerse и GitLab ----------

def _get(url: str, quick: bool = False, name: str = "GitVerse", **kw):
    """Площадки ограничивают частоту анонимных запросов (ответ 429) — ждём и повторяем
    (quick — одна попытка: для тихой проверки при запуске и когда есть вторая площадка)."""
    import requests

    last = None
    for wait in ((0,) if quick else (0, 15, 30, 45)):
        if wait:
            log.info("%s просит подождать, повтор через %d с…", name, wait)
            time.sleep(wait)
        try:
            r = requests.get(url, timeout=20 if quick else 60, **kw)
        except requests.RequestException as e:
            last = e
            continue
        if r.status_code == 200:
            return r
        if r.status_code in (400, 404) and "not found" in r.text.lower():
            raise RuntimeError(f"На {name} пока нет сведений об обновлениях (version.json) — "
                               "их опубликует автор вместе с новой версией")
        last = r.status_code
        if r.status_code not in (429, 500, 502, 503, 504):
            break
    if last == 429:
        raise RuntimeError(f"{name} временно ограничил запросы — попробуйте через пару минут")
    if isinstance(last, Exception):
        raise RuntimeError(f"Нет связи с {name}: проверьте интернет") from last
    raise RuntimeError(f"{name} ответил ошибкой {last}")


def _fetch_manifest(name: str, quick: bool) -> dict:
    r = _get(SOURCES[name]["manifest"], quick=quick, name=name)
    if name == "GitVerse":
        return json.loads(base64.b64decode(r.json()["content"]).decode("utf-8"))
    return json.loads(r.content.decode("utf-8"))


def _all_failed(errors: dict[str, Exception]) -> RuntimeError:
    text = "; ".join(f"{e}" for e in errors.values())
    if all("Нет связи" in str(e) for e in errors.values()):
        text = "Нет связи ни с GitVerse, ни с GitLab: проверьте интернет"
    return RuntimeError(text)


def remote_manifest(max_age_hours: float = 0) -> dict:
    """version.json с обеих площадок сразу; в ответе _source — откуда взят. max_age_hours > 0 — можно взять
    сохранённый ответ не старше этого (автопроверка при запуске не опрашивает площадки чаще раза в сутки)."""
    cache = config.HOME / "update-check.json"
    if max_age_hours and cache.exists() and time.time() - cache.stat().st_mtime < max_age_hours * 3600:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except ValueError:
            pass
    got: list[tuple[str, dict]] = []  # в порядке ответа
    errors: dict[str, Exception] = {}
    with cf.ThreadPoolExecutor(len(SOURCES)) as pool:
        futures = {pool.submit(_fetch_manifest, n, True): n for n in SOURCES}
        for f in cf.as_completed(futures):
            try:
                got.append((futures[f], f.result()))
            except Exception as e:  # noqa: BLE001 — недоступна одна площадка, есть другая
                errors[futures[f]] = e
    if not got and not max_age_hours:  # обе не ответили с первого раза — ждём, как раньше, по очереди
        for n in SOURCES:
            try:
                got.append((n, _fetch_manifest(n, False)))
                break
            except Exception as e:  # noqa: BLE001
                errors[n] = e
    if not got:
        raise _all_failed(errors)
    for n, e in errors.items():
        log.info("Обновления: %s недоступен (%s)", n, e)
    name, m = max(got, key=lambda x: _vt(x[1].get("app", "0")))  # max берёт первый из равных — быстрейший
    m = dict(m, _source=name)
    cache.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    return m


# ---------- что можно обновить ----------

def pending() -> str | None:
    """Скачанная версия, которая станет текущей при перезапуске («bundled» — откат к встроенной)."""
    try:
        return (overlay_dir() / "pending.txt").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def status() -> dict:
    """Без обращения к сети: что установлено сейчас."""
    running = _manifest(config.ROOT)
    bundled = _manifest(bundled_root())
    p = pending()
    root_next = overlay_dir() / p if p and p != "bundled" else None
    return {"platform": platform(), "version": running.get("app", "1.0.0"),
            "bundled": bundled.get("app", "1.0.0"), "shell": shell_version(),
            "overlay": config.ROOT.resolve() != bundled_root().resolve(), "pending": p,
            "packages_pending": packages_needed(root_next) if root_next else packages_needed(), "page": PAGE}


def check(max_age_hours: float = 0) -> dict:
    st = status()
    m = remote_manifest(max_age_hours)
    latest = m.get("app", "0")
    need_shell = (m.get("shell") or {}).get(platform(), 1)
    st.update(source=m.get("_source", ""), latest=latest, notes=m.get("notes", ""), changes=m.get("changes") or {}, date=m.get("date", ""),
              code_available=_vt(latest) > _vt(st["version"]) and latest != st["pending"],
              shell_needed=need_shell > st["shell"], shell_latest=need_shell)
    if osutil.IS_WIN:
        want = (m.get("packages") or {}).get("windows_hash", "")
        st["packages_changed"] = bool(want) and want != installed_packages_hash()
    return st


def apply_code() -> dict:
    """Скачивает код (GitVerse или GitLab), проверяет, что он запускается, и делает его текущим."""
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
    first = m.get("_source") if m.get("_source") in SOURCES else next(iter(SOURCES))
    errors: dict[str, Exception] = {}
    order = [first] + [n for n in SOURCES if n != first]
    # Сначала по одной попытке на каждой площадке (занята одна — сразу берём другую), ждём — только если отказали обе.
    for quick, name in [(True, n) for n in order] + [(False, n) for n in order]:
        try:
            archive.write_bytes(_get(SOURCES[name]["archive"], quick=quick, name=name).content)
            log.info("Код версии %s скачан с %s", version, name)
            break
        except Exception as e:  # noqa: BLE001
            errors[name] = e
    else:
        shutil.rmtree(tmp, ignore_errors=True)
        raise _all_failed(errors)
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
    # Не сразу: станет текущей при следующем запуске сервера/приложения (__main__._promote).
    (base / "pending.txt").write_text(got, encoding="utf-8")
    try:
        current = (base / "current.txt").read_text(encoding="utf-8").strip()
    except OSError:
        current = ""
    for old in base.iterdir():  # прошлые версии больше не нужны (кроме работающей сейчас)
        if old.is_dir() and old.name not in (got, current) and not old.name.startswith("."):
            shutil.rmtree(old, ignore_errors=True)
    log.info("Скачана версия %s — применится при перезапуске", got)
    return {"version": got, "packages_pending": packages_needed(final)}


def reset_code() -> None:
    """Вернуться к версии, встроенной в приложение — при следующем перезапуске."""
    overlay_dir().mkdir(parents=True, exist_ok=True)
    (overlay_dir() / "pending.txt").write_text("bundled", encoding="utf-8")


def cancel_pending() -> None:
    (overlay_dir() / "pending.txt").unlink(missing_ok=True)
