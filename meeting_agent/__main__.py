"""Точка входа: python -m meeting_agent …

Если по кнопке «Обновить» скачана более новая версия кода (<данные>/app-code/<версия>), запускается она,
а не встроенная в приложение: так обновляются код и интерфейс без переустановки. Скачанная версия ждёт
в pending.txt и становится текущей (current.txt) только при запуске сервера или приложения — тогда, когда
удобно пользователю; процессы, которые запускает уже работающий сервер, остаются на его версии. Новая версия берётся,
только если она новее встроенной и подходит к установленной оболочке (Swift-приложение на Mac,
Meeting Agent.exe и Python на Windows). Отключить: MEETING_AGENT_NO_OVERLAY=1.
"""

import json
import os
import sys
from pathlib import Path


def _home() -> Path:
    if os.environ.get("MEETING_AGENT_HOME"):
        return Path(os.environ["MEETING_AGENT_HOME"])
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Meeting Agent"
    return Path.home() / "Library/Application Support/Meeting Agent"


def _manifest(root: Path) -> dict:
    try:
        return json.loads((root / "version.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _vt(v: str) -> tuple:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return (0,)


def _shell() -> int:
    exe = Path(sys.executable).resolve()
    for p in (exe.parents[2] / "shell.json", exe.parents[1] / "shell.json"):  # Mac: Resources, Windows: корень
        try:
            return int(json.loads(p.read_text(encoding="utf-8")).get("shell", 1))
        except (OSError, ValueError, IndexError):
            continue
    return 1


def _promote(base: Path) -> None:
    """Перезапуск: скачанная версия (pending.txt) или откат (pending.txt = «bundled») становится текущей."""
    pending = base / "pending.txt"
    try:
        want = pending.read_text(encoding="utf-8").strip()
    except OSError:
        return
    try:
        if want == "bundled":
            (base / "current.txt").unlink(missing_ok=True)
        elif (base / want / "meeting_agent" / "__init__.py").exists():
            (base / "current.txt").write_text(want, encoding="utf-8")
        pending.unlink()
    except OSError:
        pass


def _overlay(here: Path) -> Path | None:
    if os.environ.get("MEETING_AGENT_NO_OVERLAY"):
        return None
    base = _home() / "app-code"
    if len(sys.argv) > 1 and sys.argv[1] in ("serve", "winapp", "web"):
        _promote(base)
    try:
        d = base / (base / "current.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not (d / "meeting_agent" / "__init__.py").exists() or d.resolve() == here.resolve():
        return None
    m = _manifest(d)
    if _vt(m.get("app", "0")) <= _vt(_manifest(here).get("app", "0")):
        return None  # встроенная версия новее (поставили свежее приложение целиком)
    need = (m.get("shell") or {}).get("windows" if sys.platform == "win32" else "mac", 1)
    return d if need <= _shell() else None


_here = Path(__file__).resolve().parent.parent
_d = _overlay(_here)
os.environ.setdefault("MEETING_AGENT_BUNDLED_ROOT", str(_here))
if _d:
    for _k in [k for k in sys.modules if k == "meeting_agent" or k.startswith("meeting_agent.")]:
        del sys.modules[_k]
    sys.path.insert(0, str(_d))
    from meeting_agent.cli import main  # noqa: E402  — из скачанной версии
else:
    from .cli import main  # noqa: E402

main()
