"""Список участников встречи, указанный вручную (до, во время или после встречи).

Помогает всем этапам: имена уходят в подсказку Whisper, слепки голоса людей из списка
узнаются при более низком пороге, угадывание имён выбирает из списка, протокол пишет имена правильно.
  <папка встречи>/participants.json — список этой встречи;
  <MEETING_AGENT_HOME>/next_participants.json — список для следующей записи или импорта.
"""

import json
import re
from pathlib import Path

from . import config

FILE = "participants.json"


def clean(names) -> list[str]:
    if isinstance(names, str):
        names = re.split(r"[,;\n]", names)
    out, seen = [], set()
    for n in names or []:
        n = re.sub(r"\s+", " ", str(n)).strip(" .*?")
        if re.search(r"\((я|me)\)", n, re.I):  # «Иван (я)» — это вы, ваша речь и так подписана «Я»
            continue
        key = n.casefold().replace("ё", "е")
        if n and n.casefold() not in ("я", "me") and key not in seen and len(n) <= 60:
            seen.add(key)
            out.append(n)
    return out[:50]


def _read(path: Path) -> list[str]:
    try:
        return clean(json.loads(path.read_text(encoding="utf-8")).get("names", []))
    except (OSError, ValueError, AttributeError):
        return []


def _write(path: Path, names: list[str]) -> list[str]:
    names = clean(names)
    path.parent.mkdir(parents=True, exist_ok=True)
    if names:
        path.write_text(json.dumps({"names": names}, ensure_ascii=False), encoding="utf-8")
    else:
        path.unlink(missing_ok=True)
    return names


def read(work: Path) -> list[str]:
    return _read(work / FILE)


def write(work: Path, names) -> list[str]:
    return _write(work / FILE, names)


def _next_path() -> Path:
    return config.HOME / "next_participants.json"


def read_next() -> list[str]:
    return _read(_next_path())


def write_next(names) -> list[str]:
    return _write(_next_path(), names)


def take_next(work: Path) -> list[str]:
    """Новая запись или импорт забирает список «следующей встречи» себе."""
    names = read_next()
    if names and not read(work):
        write(work, names)
    _next_path().unlink(missing_ok=True)
    return names


CALENDAR_FILE = "calendar.json"


def save_calendar(work: Path, title: str, attendees: list[dict]) -> list[str]:
    """Событие календаря, совпавшее с записью: название и участники. Участники становятся списком
    встречи, если его не задали вручную. Возвращает итоговый список."""
    names = clean([a.get("name") or (a.get("email") or "").split("@")[0] for a in attendees])
    (work / CALENDAR_FILE).write_text(json.dumps({"title": title.strip()[:200], "attendees": attendees[:60]},
                                                 ensure_ascii=False, indent=1), encoding="utf-8")
    if names and not read(work):
        write(work, names)
    return read(work)


def read_calendar(work: Path) -> dict:
    try:
        return json.loads((work / CALENDAR_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def of(cfg: dict) -> list[str]:
    """Список участников обрабатываемой встречи (pipeline кладёт его в cfg)."""
    return cfg.get("meeting", {}).get("participants", [])
