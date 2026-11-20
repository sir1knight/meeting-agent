"""Обновления по компонентам — с GitVerse или GitLab (какой доступен), без переустановки приложения целиком.

Компоненты:
  • код и интерфейс (meeting_agent, ui, templates, config.toml) — архив ветки main, распаковывается
    в <данные>/app-code/<версия>; при запуске __main__.py берёт его вместо встроенного;
  • пакеты Python (Windows и Mac с Python вне приложения) — список requirements-<платформа>.lock из новой
    версии кода; ставятся через uv
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
KEEP = ("meeting_agent", "ui", "templates", "config.toml", "version.json", "requirements-windows.lock",
        "requirements-mac.lock")


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


def app_bundle() -> Path | None:
    """Mac: путь к Meeting Agent.app (оболочка передаёт его серверу; у старых сборок Python внутри .app)."""
    if osutil.IS_WIN:
        return None
    if os.environ.get("MEETING_AGENT_APP"):
        return Path(os.environ["MEETING_AGENT_APP"])
    for p in Path(sys.executable).resolve().parents:
        if p.suffix == ".app":
            return p
    return None


def python_bundled() -> bool:
    """Mac: Python и пакеты внутри .app (сборки до 1.5) — пакеты обновляются только вместе с приложением."""
    return not osutil.IS_WIN and ".app/Contents/Resources/python" in str(Path(sys.executable).resolve())


def lock_file(root: Path | None = None) -> Path:
    return (root or config.ROOT) / ("requirements-windows.lock" if osutil.IS_WIN else "requirements-mac.lock")


def shell_version() -> int:
    exe = Path(sys.executable).resolve()
    app = app_bundle()
    cands = [app / "Contents/Resources/shell.json"] if app else []
    for p in cands + [exe.parents[2] / "shell.json", exe.parents[1] / "shell.json"]:
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
    exe = Path(sys.executable).resolve()
    if osutil.IS_WIN:
        return exe.parent / "Lib" / "site-packages"
    return exe.parents[1] / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"


def installed_packages_hash() -> str:
    try:
        return (site_packages() / ".meeting-agent-lock").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def packages_needed(root: Path | None = None) -> bool:
    """Пакеты не совпадают со списком этой версии кода (или ещё не ставились). На Mac со встроенным
    в приложение Python — никогда: там пакеты меняются только вместе с приложением."""
    if python_bundled():
        return False
    want = lock_hash(lock_file(root))
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


SEEN = "seen-version.txt"  # версия, о которой пользователю уже сказали


def just_updated() -> dict | None:
    """Версия кода выросла с прошлого запуска — что показать в окне «Обновление установлено».
    Первый запуск (ещё нечего сравнивать) и откат к старой версии — молча запоминаем."""
    running = _manifest(config.ROOT)
    cur = running.get("app", "1.0.0")
    path = config.HOME / SEEN
    try:
        seen = path.read_text(encoding="utf-8").strip()
    except OSError:
        seen = ""
    if not seen and config.ROOT.resolve() != bundled_root().resolve():
        seen = _manifest(bundled_root()).get("app", "")  # обновились с версии, которая ещё не вела учёт
    if seen == cur:
        return None
    if not seen or _vt(cur) < _vt(seen):
        mark_seen()
        return None
    return {"from": seen, "to": cur, "date": running.get("date", ""), "notes": running.get("notes", ""),
            "changes": running.get("changes") or {}}


def mark_seen() -> None:
    try:
        config.HOME.mkdir(parents=True, exist_ok=True)
        (config.HOME / SEEN).write_text(_manifest(config.ROOT).get("app", "1.0.0"), encoding="utf-8")
    except OSError:
        pass


def status() -> dict:
    """Без обращения к сети: что установлено сейчас."""
    running = _manifest(config.ROOT)
    bundled = _manifest(bundled_root())
    p = pending()
    root_next = overlay_dir() / p if p and p != "bundled" else None
    return {"platform": platform(), "version": running.get("app", "1.0.0"),
            "bundled": bundled.get("app", "1.0.0"), "shell": shell_version(),
            "overlay": config.ROOT.resolve() != bundled_root().resolve(), "pending": p,
            "packages_pending": packages_needed(root_next) if root_next else packages_needed(), "page": PAGE,
            "python_bundled": python_bundled(),
            "just_updated": just_updated()}


def check(max_age_hours: float = 0) -> dict:
    st = status()
    m = remote_manifest(max_age_hours)
    latest = m.get("app", "0")
    need_shell = (m.get("shell") or {}).get(platform(), 1)
    st.update(source=m.get("_source", ""), latest=latest, notes=m.get("notes", ""), changes=m.get("changes") or {}, date=m.get("date", ""),
              code_available=_vt(latest) > _vt(st["version"]) and latest != st["pending"],
              shell_needed=need_shell > st["shell"], shell_latest=need_shell)
    if not python_bundled():
        want = (m.get("packages") or {}).get(f"{platform()}_hash", "")
        st["packages_changed"] = bool(want) and want != installed_packages_hash()
    # Новая оболочка (Mac): маленький Meeting-Agent-Mac.zip — ставится этой же кнопкой (install_shell).
    dl = (m.get("shell_download") or {}).get(platform()) or {}
    st["shell_download"] = dl if dl.get("url") and int(dl.get("version", 0)) > st["shell"] and app_bundle() else None
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
    if probe.returncode != 0 and not (not python_bundled() and "ModuleNotFoundError" in probe.stderr):
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


# ---------- новая оболочка (Mac): маленький Meeting-Agent-Mac.zip этой же кнопкой ----------

SHELL_STAGE = "shell-update"

_HELPER = r"""#!/bin/bash
# Замена Meeting Agent.app новой версией: ждём, пока приложение закроется, меняем и открываем снова.
APP_PID="$1"; OLD="$2"; NEW="$3"; RUNTIME="$4"; LOCK_HASH="$5"; LOG="$6"
exec >>"$LOG" 2>&1
echo "$(date '+%F %T') замена оболочки: $OLD"
for i in $(seq 1 120); do kill -0 "$APP_PID" 2>/dev/null || break; sleep 0.5; done
# Переход со сборок, где Python был внутри приложения: забираем его, чтобы не скачивать 1,5 ГБ заново.
if [ -d "$OLD/Contents/Resources/python" ] && [ ! -x "$RUNTIME/python/bin/python3" ]; then
  mkdir -p "$RUNTIME" && mv "$OLD/Contents/Resources/python" "$RUNTIME/python" && \
    SP=$(ls -d "$RUNTIME"/python/lib/python3.*/site-packages | head -1) && echo -n "$LOCK_HASH" > "$SP/.meeting-agent-lock" && \
    echo "Python перенесён в $RUNTIME/python"
fi
BAK="$(dirname "$NEW")/previous.app"
rm -rf "$BAK"
if mv "$OLD" "$BAK" && mv "$NEW" "$OLD"; then
  echo "готово"; rm -rf "$BAK"
else
  echo "не удалось заменить — возвращаю прежнюю версию"; [ -d "$BAK" ] && [ ! -d "$OLD" ] && mv "$BAK" "$OLD"
fi
open "$OLD"
"""


def _signer(app: Path) -> str:
    """Чем подписано приложение: строки Authority/TeamIdentifier из codesign (пусто — подписи нет)."""
    r = subprocess.run(["codesign", "-dvv", str(app)], capture_output=True, text=True)
    return "\n".join(l for l in r.stderr.splitlines() if l.startswith(("Authority=", "TeamIdentifier=")))


def install_shell(recording: bool = False) -> dict:
    """Mac: скачать новую оболочку, проверить контрольную сумму и подпись (та же, что у установленной —
    тогда macOS сохранит разрешения на микрофон и запись экрана), закрыть приложение и заменить его."""
    if osutil.IS_WIN:
        raise RuntimeError("На Windows оболочка обновляется установщиком")
    if recording:
        raise RuntimeError("Идёт запись — обновите приложение после встречи")
    dl = (remote_manifest().get("shell_download") or {}).get("mac") or {}
    if not dl.get("url") or int(dl.get("version", 0)) <= shell_version():
        raise RuntimeError("Новой версии приложения нет")
    app = app_bundle()
    if not app or not app.is_dir():
        raise RuntimeError("Не нашёл, где установлено приложение")
    stage = config.HOME / SHELL_STAGE
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    archive = stage / "Meeting-Agent-Mac.zip"
    import requests

    with requests.get(dl["url"], stream=True, timeout=120) as r:
        if r.status_code != 200:
            raise RuntimeError(f"Не удалось скачать приложение (ответ {r.status_code})")
        with open(archive, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    if dl.get("sha256") and hashlib.sha256(archive.read_bytes()).hexdigest() != dl["sha256"]:
        raise RuntimeError("Скачанное приложение повреждено (не совпала контрольная сумма) — попробуйте ещё раз")
    subprocess.run(["ditto", "-x", "-k", str(archive), str(stage)], check=True)
    new = stage / "Meeting Agent.app"
    if not new.is_dir():
        raise RuntimeError("В архиве нет Meeting Agent.app")
    if subprocess.run(["codesign", "--verify", "--deep", str(new)], capture_output=True).returncode != 0:
        raise RuntimeError("Подпись нового приложения не прошла проверку — обновление отменено")
    if _signer(new) != _signer(app):
        raise RuntimeError("Новое приложение подписано не тем сертификатом — обновление отменено")
    subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(new)], capture_output=True)
    helper = stage / "replace.sh"
    helper.write_text(_HELPER, encoding="utf-8")
    helper.chmod(0o755)
    runtime = config.HOME / "runtime"
    subprocess.Popen(["/bin/bash", str(helper), str(os.getppid()), str(app), str(new), str(runtime),
                      lock_hash(lock_file()), str(config.HOME / "logs" / "shell-update.log")],
                     start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    log.info("Новая оболочка %s готова — приложение перезапустится", dl.get("version"))
    # Обычный выход приложения (запись, если идёт, дописывается) — помощник дождётся и заменит.
    subprocess.Popen(["osascript", "-e", 'quit app id "local.meeting-agent"'], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    return {"ok": True, "version": dl.get("version")}
