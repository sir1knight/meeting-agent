"""Версии протокола по текстовому запросу: «письмо коллегам», «для CEO — коротко, о рисках и деньгах»…
Модель пишет документ по встрече под адресата: его язык, длину и уровень подробностей. Основной протокол
не меняется; версии лежат в <запись>/versions/<id>.json и показываются вкладками над протоколом.
"""

import json
import re
import time
import uuid
from pathlib import Path

from . import osutil

DIR = "versions"

SYSTEM = (
    "Ты готовишь документы по рабочей встрече пользователя: протоколы, письма, сводки. У тебя есть протокол "
    "встречи и её расшифровка. Опирайся только на них — не выдумывай факты, имена, даты и суммы. "
    "Главное — адресат: пиши на языке и в объёме, которые нужны именно ему. Руководителю (CEO, директору) — "
    "коротко, начиная с вывода: решения, риски, деньги, сроки, что требуется от него; без технических деталей "
    "и жаргона. Коллегам и команде — конкретно: договорённости, задачи, ответственные, сроки, детали, нужные "
    "для работы. Клиенту или партнёру — вежливо, без внутренней кухни. Технической команде — точные термины "
    "и детали. Письма — деловым стилем от первого лица пользователя, с темой письма. «Я» в расшифровке — сам "
    "пользователь. Имя со звёздочкой (Олег*) угадано по разговору, со знаком (?) — неуверенно узнано по голосу. "
    "Оформляй в Markdown. Первая строка ответа — «TITLE: <название документа, 2–5 слов>».")

EXAMPLES = [
    "Письмо коллегам с итогами и задачами",
    "Для CEO: коротко — решения, риски, сроки",
    "Для клиента: вежливое резюме без внутренних деталей",
    "Для технической команды: детали и задачи",
]


def _dir(work: Path) -> Path:
    return work / DIR


def list_all(work: Path | None) -> list[dict]:
    if not work or not _dir(work).is_dir():
        return []
    out = []
    for p in _dir(work).glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda v: v.get("created", 0))


def create(work: Path, title: str, date_str: str, summary: str, transcript: str, request: str, cfg: dict) -> dict:
    from .ask import _fit_transcript, _llm, backend
    from .summarize import parse_title

    request = request.strip()
    if not request:
        raise ValueError("Напишите, какой документ нужен и для кого")
    budget = 20000 if backend(cfg) == "local" else 400000
    from .namefix import protocol_hint
    hint = protocol_hint(cfg)
    user = ((hint + "\n\n" if hint else "") + f"Встреча: {title} ({date_str}).\n\n## Протокол\n{summary}\n\n## Расшифровка\n"
            f"{_fit_transcript(transcript, request, budget)}\n\n"
            f"Запрос пользователя: {request}\n\n"
            "Подстрой язык, длину и подробность под адресата из запроса. Если запрос не называет адресата — "
            "пиши для коллег-участников встречи.")
    name, text = parse_title(_llm(cfg, user, SYSTEM))
    label = re.split(r"[:—\-–.,(]", request, maxsplit=1)[0].strip() or request
    v = {"id": uuid.uuid4().hex[:8], "request": request, "label": label[:32] + ("…" if len(label) > 32 else ""), "title": name if name != "Встреча" else request[:40],
         "text": text, "created": time.time()}
    _dir(work).mkdir(exist_ok=True)
    osutil.atomic_write_text(_dir(work) / f"{v['id']}.json", json.dumps(v, ensure_ascii=False))
    return v


def delete(work: Path, vid: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{8}", vid or ""):
        raise LookupError("Версия не найдена")
    (_dir(work) / f"{vid}.json").unlink(missing_ok=True)
