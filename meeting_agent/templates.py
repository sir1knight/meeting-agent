"""Шаблоны протоколов: встроенные (папка templates рядом с кодом, только чтение) и свои — в папке данных
приложения (<данные>/templates). Свой шаблон с тем же именем, что встроенный, заменяет его.

Файл своего шаблона — обычная инструкция для модели; первая строка — служебная:
    <!-- title: Оценка кандидата -->
"""

import re
import uuid
from pathlib import Path

from . import config, osutil

BUILTIN_TITLES = {"meeting": "Рабочая встреча", "standup": "Стендап",
                  "client": "Встреча с партнёром", "interview": "Custdev-интервью",
                  "demo": "Демо продукта", "brainstorm": "Мозговой штурм", "sales": "Продажа (MEDDIC)",
                  "hiring": "Собеседование кандидата", "prd": "Требования к продукту (PRD)",
                  "lecture": "Конспект лекции или вебинара"}
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
    osutil.atomic_write_text(_user(name), f"<!-- title: {title.replace('-->', '')} -->\n{body}\n")
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


GEN_SYSTEM = ("Ты помогаешь настроить приложение, которое пишет протоколы рабочих встреч на русском языке. "
              "Твоя задача — по примерам встреч одного типа составить инструкцию-шаблон, по которой модель потом "
              "будет писать протоколы таких встреч. Пиши шаблон по-русски.")

GEN_PROMPT = """Ниже {n} прошлых встреч одного типа — протоколы и расшифровки (сокращённые).

{meetings}

=== ЗАДАНИЕ ===
Ты видел {n} встреч одного типа. НЕ пересказывай их и не пиши протокол какой-то из них.
Составь ШАБЛОН — пустую форму-инструкцию, по которой модель потом будет писать протоколы любых встреч такого типа.
Изучи, что на таких встречах обычно обсуждают, какие решения, цифры, артефакты и роли важны, какие термины
используют участники, — и сделай под это разделы.

Требования:
- Формат — как в примере ниже: вступительная фраза, строка «TITLE: <…>», разделы «##» с подсказками
  в угловых скобках <что сюда писать>, где нужно — таблицы, в конце «Правила:».
- Внутри разделов — только подсказки в угловых скобках, а не содержание конкретных встреч. Никаких имён,
  дат, сумм и фактов из встреч выше.
- Обязательно оставь задачи (кто, что, срок) и открытые вопросы, если такие встречи их предполагают.
{hint}
Пример формата (шаблон обычной рабочей встречи):
---
{example}
---

Ответ — строго:
NAME: <название шаблона, 2–5 слов, например «Еженедельный синк проекта»>
<текст шаблона>"""


def cloud_backend(cfg: dict) -> str | None:
    """Облачная модель для шаблонов: та, что пишет протоколы, иначе та, что в умном поиске, иначе любая с ключом.
    Локальному Qwen такая задача не по силам (пересказывает встречу вместо шаблона)."""
    keys = {"gemini": bool(config.read_secret("gemini_key")), "openai": bool(config.openai_key(cfg)),
            "claude": bool(config.anthropic_key(cfg))}
    for b in (cfg["llm"].get("backend"), cfg.get("ask", {}).get("backend")):
        if b == "claude-code" or keys.get(b):
            return b
    return next((b for b, ok in keys.items() if ok), None)


def generate(meetings: list[dict], hint: str, cfg: dict) -> dict:
    """meetings: [{"title", "date", "summary", "transcript"}] → {"title", "text"} — черновик шаблона."""
    from .ask import _fit_transcript, _llm

    if len(meetings) < 2:
        raise ValueError("Нужно хотя бы две встречи одного типа")
    backend = cloud_backend(cfg)
    if not backend:
        raise ValueError("Шаблоны по прошлым встречам составляет облачная модель — добавьте ключ Gemini, "
                         "ChatGPT или Claude в Настройках")
    budget = 300000
    per = budget // len(meetings)
    blocks = []
    for i, m in enumerate(meetings, 1):
        summary = re.sub(r"\s*\[(?:\d{1,2}:)?\d{1,2}:\d{2}\]", "", m.get("summary", ""))[: per // 2]
        tr = _fit_transcript(m.get("transcript", ""), m.get("title", ""), max(0, per - len(summary)))
        blocks.append(f"### Встреча {i}: {m.get('title', '')} ({m.get('date', '')})\n"
                      f"Протокол:\n{summary}\n\nРасшифровка:\n{tr[:max(0, per - len(summary))]}")
    prompt = GEN_PROMPT.format(n=len(meetings), example=text("meeting"),
                               hint=f"- Пожелание пользователя: {hint.strip()}\n" if hint.strip() else "",
                               meetings="\n\n".join(blocks))
    run_cfg = dict(cfg, ask=dict(cfg.get("ask", {}), backend=backend))  # та же модель, что пишет протоколы
    for attempt in range(2):
        answer = _llm(run_cfg, prompt, GEN_SYSTEM).strip()
        m = re.match(r"\s*\**NAME\**\s*:\s*(.+)", answer)
        title = m.group(1).strip(" «»\"*") if m else "Шаблон по прошлым встречам"
        body = answer[m.end():].strip() if m else answer
        body = re.sub(r"^-{3,}\s*|\s*-{3,}$", "", body).strip()
        # Шаблон — это форма с подсказками <…>, а не пересказ встречи.
        if "TITLE:" in body and len(body) >= 80 and len(re.findall(r"<[^<>\n]{3,}>", body)) >= 3:
            return {"title": title[:80], "text": body}
        prompt += ("\n\nВНИМАНИЕ: в прошлый раз получился пересказ встречи. Нужна пустая форма: разделы с "
                   "подсказками в угловых скобках <…>, без фактов из встреч.")
    raise ValueError("Модель не составила шаблон (получился пересказ встречи). Попробуйте ещё раз, "
                     "выберите другие встречи или облачную модель по ключу API")
