"""Главы и темы встречи: встреча делится на тематические разделы со временем начала, плюс 3–6 тем-меток
для фильтра в списке встреч. Делает модель протоколов (или чата — для старых встреч, по кнопке).
Хранится в <запись>/chapters.json: {"chapters": [{"start": сек, "title": "…"}], "topics": ["…"]}.
"""

import json
import logging
import re
from pathlib import Path

from . import merge, osutil

log = logging.getLogger(__name__)

FILE = "chapters.json"
SYSTEM = ("Ты помогаешь разбирать расшифровки рабочих встреч на русском языке. "
          "Опирайся только на расшифровку. Отвечай строго JSON.")

PROMPT = """Ниже — расшифровка встречи по минутам: [чч:мм:сс] и что говорили.
1) Раздели встречу на главы — крупные части разговора по темам, в порядке времени. Глав {lo}–{hi},
   у каждой — время начала из расшифровки и короткое понятное название (2–6 слов, без кавычек).
   Первая глава начинается с начала встречи.
2) Назови 3–6 тем встречи — коротких меток (1–3 слова), по которым её потом удобно искать
   (проект, продукт, система, процесс). Без общих слов вроде «встреча», «обсуждение».

Названия глав — по сути разговора (о чём договаривались, какую проблему решали), а не «Обсуждение …».
{context}
Ответь строго JSON без пояснений:
{{"chapters": [{{"time": "чч:мм:сс", "title": "…"}}], "topics": ["…"]}}

Расшифровка:
{text}"""


def _condense(segments: list[dict], budget: int) -> str:
    """Расшифровка блоками примерно по минуте; если не влезает — каждый блок короче."""
    blocks: list[tuple[float, str]] = []
    start, parts = None, []
    for s in segments:
        if start is None:
            start = s["start"]
        parts.append(f"{s.get('speaker', '')}: {s.get('text', '')}")
        if s["end"] - start >= 60:
            blocks.append((start, " ".join(parts)))
            start, parts = None, []
    if parts:
        blocks.append((start or 0.0, " ".join(parts)))
    per = max(80, budget // max(1, len(blocks)) - 14)
    return "\n".join(f"[{merge.ts(t)}] {txt[:per]}" for t, txt in blocks)


def _seconds(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    m = re.match(r"\s*(?:(\d+):)?(\d{1,2}):(\d{2})", str(value or ""))
    if not m:
        return None
    h, mi, s = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
    return float(h * 3600 + mi * 60 + s)


def parse(answer: str, total: float) -> dict:
    m = re.search(r"\{.*\}", answer, re.S)
    if not m:
        raise ValueError("Модель не вернула главы")
    data = json.loads(m.group(0))
    chapters = []
    for c in data.get("chapters") or []:
        t = _seconds(c.get("time", c.get("start")))
        title = str(c.get("title") or "").strip(" «»\"'")
        if t is None or not title or t > total + 5:
            continue
        if chapters and t <= chapters[-1]["start"]:
            continue
        chapters.append({"start": t, "title": title[:80]})
    if chapters:
        chapters[0]["start"] = 0.0
    topics = []
    for t in data.get("topics") or []:
        t = str(t).strip(" «»\"'#").strip()
        if t.lower() in GENERIC:
            continue
        if t and t.lower() not in (x.lower() for x in topics):
            topics.append(t[:40])
    return {"chapters": chapters, "topics": topics[:6]}


GENERIC = {"встреча", "встречи", "обсуждение", "совещание", "созвон", "разговор", "звонок", "планёрка",
           "планерка", "работа", "вопросы", "задачи", "проект", "команда"}


def build(segments: list[dict], chat, budget: int = 12000, summary: str = "") -> dict:
    """chat(prompt) -> ответ модели. summary — протокол встречи: подсказка, о чём она."""
    if not segments:
        return {"chapters": [], "topics": []}
    total = max(s["end"] for s in segments)
    minutes = total / 60
    lo, hi = (2, 4) if minutes < 20 else (3, 7) if minutes < 60 else (5, 12)
    summary = re.sub(r"\s*\[(?:\d{1,2}:)?\d{1,2}:\d{2}\]", "", summary or "").strip()[:2500]
    context = f"\nПротокол этой встречи (для понимания сути; время бери из расшифровки):\n{summary}\n" if summary else ""
    answer = chat(PROMPT.format(lo=lo, hi=hi, context=context, text=_condense(segments, budget)))
    return parse(answer, total)


def save(work: Path, data: dict) -> None:
    osutil.atomic_write_text((work / FILE), json.dumps(data, ensure_ascii=False))


def read(work: Path | None) -> dict:
    try:
        data = json.loads((work / FILE).read_text(encoding="utf-8"))
        return {"chapters": data.get("chapters") or [], "topics": data.get("topics") or []}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"chapters": [], "topics": []}
