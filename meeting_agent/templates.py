"""Шаблоны протоколов: встроенные (папка templates рядом с кодом, только чтение) и свои — в папке данных
приложения (<данные>/templates). Свой шаблон с тем же именем, что встроенный, заменяет его.

Файл своего шаблона — обычная инструкция для модели; первая строка — служебная:
    <!-- title: Оценка кандидата -->
"""

import re
import uuid
from pathlib import Path

from . import config

BUILTIN_TITLES = {"meeting": "Рабочая встреча", "standup": "Стендап",
                  "client": "Встреча с партнёром", "interview": "Custdev-интервью"}
USER_DIR = config.HOME / "templates"
_TITLE_RE = re.compile(r"^<!--\s*title:\s*(.*?)\s*-->\s*\n?", re.S)


def _builtin(name: str) -> Path:
    return config.TEMPLATES_DIR / f"{name}.md"


def _user(name: str) -> Path:
    return USER_DIR / f"{name}.md"


def _valid(name: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9_-]{1,60}", name or ""))


def path(name: str) -> Path | None:
    if not _valid(name):
        return None
    for p in (_user(name), _builtin(name)):
        if p.exists():
            return p
    return None


def _split(text: str) -> tuple[str, str]:
    m = _TITLE_RE.match(text)
    return (m.group(1), text[m.end():]) if m else ("", text)


def text(name: str) -> str:
    p = path(name)
    if not p:
        raise ValueError(f"Нет шаблона «{name}». Доступны: {', '.join(t['id'] for t in list_all())}")
    return _split(p.read_text(encoding="utf-8"))[1]


def list_all() -> list[dict]:
    out = {}
    for p in sorted(config.TEMPLATES_DIR.glob("*.md")):
        out[p.stem] = {"id": p.stem, "title": BUILTIN_TITLES.get(p.stem, p.stem), "custom": False}
    if USER_DIR.exists():
        for p in sorted(USER_DIR.glob("*.md")):
            title, _ = _split(p.read_text(encoding="utf-8"))
            out[p.stem] = {"id": p.stem, "title": title or p.stem, "custom": True,
                           "overrides": p.stem in BUILTIN_TITLES}
    return sorted(out.values(), key=lambda t: (t["custom"], t["title"].lower()))


def get(name: str) -> dict:
    p = path(name)
    if not p:
        raise LookupError("Шаблон не найден")
    title, body = _split(p.read_text(encoding="utf-8"))
    return {"id": name, "title": title or BUILTIN_TITLES.get(name, name), "text": body.strip(),
            "custom": p.parent == USER_DIR}


def save(name: str | None, title: str, body: str) -> dict:
    title, body = title.strip()[:80], body.strip()
    if not title:
        raise ValueError("Укажите название шаблона")
    if len(body) < 20:
        raise ValueError("Инструкция слишком короткая")
    if "TITLE:" not in body:  # из этой строки берётся заголовок заметки
        body = "Первой строкой ответа напиши: TITLE: <короткое название встречи, 3–7 слов>\n\n" + body
    name = name if _valid(name or "") else "my-" + uuid.uuid4().hex[:8]
    USER_DIR.mkdir(parents=True, exist_ok=True)
    _user(name).write_text(f"<!-- title: {title.replace('-->', '')} -->\n{body}\n", encoding="utf-8")
    return get(name)


def delete(name: str) -> None:
    p = _user(name) if _valid(name) else None
    if not p or not p.exists():
        raise LookupError("Можно удалить только свой шаблон")
    p.unlink()


def from_points(title: str, points: str, notes: str = "") -> str:
    """Инструкция из списка пунктов или чек-листа: по каждому пункту — что прозвучало на встрече."""
    items = [re.sub(r"^\s*(?:[-*•]|\d+[.)]|\[[ xX]?\])\s*", "", line).strip() for line in points.splitlines()]
    items = [i for i in items if i]
    if not items:
        raise ValueError("Добавьте хотя бы один пункт")
    sections = "\n\n".join(f"### {i}\n<что прозвучало по этому пункту: факты, цифры, мнения; "
                           f"если не обсуждалось — «не обсуждалось»>" for i in items)
    return (f"Это встреча: {title.strip()}. Составь протокол строго в таком формате:\n\n"
            "TITLE: <короткое название встречи, 3–7 слов>\n\n"
            "## Кратко\n2–4 предложения: о чём встреча и к чему пришли.\n\n"
            f"## Разбор по пунктам\n\n{sections}\n\n"
            "## Задачи\n| Задача | Ответственный | Срок |\n|---|---|---|\n| <что сделать> | <кто> | <когда> |\n\n"
            "## Открытые вопросы\n- <что осталось без ответа>\n\n"
            "Правила:\n- Ничего не додумывай: только то, что прозвучало в расшифровке.\n"
            "- Ответственных называй так, как они подписаны в расшифровке, или по прозвучавшему имени.\n"
            "- Нет ответственного или срока — «не указан»; пустой раздел — «—».\n"
            + (f"- {notes.strip()}\n" if notes.strip() else "")
            + "- Пиши по-русски, коротко и по делу.")
