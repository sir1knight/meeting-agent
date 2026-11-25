"""Предполагаемые имена собеседников по разговору: «Олег, что у тебя?» → отвечает Собеседник 2 → «Олег*».

Угадывает та же модель, что пишет протокол (llm.backend). Догадка хранится в speakers.json
отдельно от настоящего имени (поле guess) и показывается со звёздочкой, пока пользователь её не подтвердит.
"""

import json
import logging
import re
from collections import Counter, defaultdict
from pathlib import Path

from . import merge, participants, voices
from .summarize import chat, chunk_size, split_chunks

log = logging.getLogger(__name__)

SYSTEM = ("Ты помогаешь подписать участников расшифровки рабочей встречи на русском языке. "
          "Отвечаешь только JSON, без пояснений.")

PROMPT = """Ниже {part}расшифровка встречи. Участники подписаны метками.
Определи настоящие имена участников с метками: {labels}.

Имя можно считать установленным, только если в тексте есть прямое указание:
- к участнику обращаются по имени, и он сразу отвечает («Олег, что у тебя?» → следующая реплика — его);
- участник представляется («Это Саша», «меня зовут Ирина»);
- ему передают слово по имени («Дима, давай ты»), и следующая реплика — его.
Не угадывай по теме разговора, должности или косвенным признакам. Имена, которые упоминаются
в третьем лице («Настя в отпуске»), не относятся к говорящим.

Ответ — только JSON такого вида (метка → имя и короткая цитата-доказательство):
{{"Собеседник 2": {{"name": "Олег", "evidence": "Олег, что у тебя? — Да, по MGM…"}}}}
Пиши имя так, как к человеку обращаются (Олег, Саша, Ирина Петровна). Если ни для кого
имя не установить — ответь {{}}.{listed}

Расшифровка:
{text}"""


def _parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _clean_name(name) -> str | None:
    if not isinstance(name, str):
        return None
    name = re.sub(r"[\s*?()«»\"]+", " ", name).strip(" .")
    words = name.split()
    if not 1 <= len(words) <= 3 or any(not re.fullmatch(r"[А-ЯЁA-Z][а-яёa-z\-]*", w) for w in words):
        return None
    return name


def _heard(name: str, chunk: str) -> bool:
    """Имя действительно звучит в текущем фрагменте расшифровки (защита от выдуманных моделью имён).
    Сравниваем от начала слова («Олег» ↔ «Олега», но не внутри «более» или «максимально»)."""
    low = chunk.lower().replace("ё", "е")
    for w in name.split():
        w = w.lower().replace("ё", "е").strip(".")
        if len(w) <= 1:
            continue
        stem = w[:max(3, len(w) - 1)]
        if not re.search(r"(?<![а-яеa-z])" + re.escape(stem) + r"[а-яе]{0,3}(?![а-яеa-z])", low):
            return False
    return True


def _evidence_in_chunk(ev: str, chunk: str) -> bool:
    """Цитата-доказательство действительно взята из этого фрагмента, а не скопирована из примера в промпте."""
    low_ev = (ev or "").lower().replace("ё", "е")
    if not low_ev or "по mgm" in low_ev:
        return False
    low_chunk = chunk.lower().replace("ё", "е")
    words = [w for w in re.findall(r"[а-яеa-z]{4,}", low_ev) if w not in ("тебя", "меня", "зовут", "давай")]
    return not words or any(w in low_chunk for w in words)


def _match_listed(name: str, listed: list[str]) -> str | None:
    """Если угаданное имя совпадает с участником из списка («Вова» → «Владимир», «Кирилл» → «Кирилл В»),
    возвращает запись из списка (только если совпадение однозначное)."""
    exact = [ln for ln in listed if voices.same_person_name(name, ln) == 2]
    if len(exact) == 1:
        return exact[0]
    partial = [ln for ln in listed if voices.same_person_name(name, ln) >= 1]
    if len(partial) == 1:
        return partial[0]
    return None


def guess(work: Path, segments: list[dict], cfg: dict) -> dict[str, dict]:
    """Угадывает имена безымянных собеседников, сохраняет в speakers.json. Возвращает {метка: догадка}."""
    data = voices.read_speakers(work)
    sps = data.get("speakers", {})
    targets = {voices.display_name(sp): label for label, sp in sps.items()
               if not sp.get("name") and not sp.get("guess") and not sp.get("guess_rejected")}
    if not targets:
        return {}

    transcript = merge.to_text(segments)
    listed = participants.of(cfg)
    listed_keys = {voices._norm(n): n for n in listed}
    listed_hint = ("\n\nУчастники встречи по списку: " + ", ".join(listed) + ". Если обращение — форма одного "
                   "из этих имён (Дарья → Даша, Владимир → Вова) или явно искажённое распознаванием («наша, спасибо» "
                   "сразу после отчёта — скорее «Даша»), пиши имя из списка. Доказательство всё равно нужно.") if listed else ""
    # Облачные модели читают встречу целиком; локальная — кусками с голосованием.
    size = chunk_size(cfg)
    chunks = split_chunks(transcript, size)
    votes: dict[str, Counter] = defaultdict(Counter)
    evidence: dict[tuple[str, str], str] = {}
    for i, chunk in enumerate(chunks, 1):
        present = [d for d in targets if d in chunk]
        if not present:
            continue
        part = f"фрагмент {i} из {len(chunks)}: " if len(chunks) > 1 else ""
        try:
            answer = chat(cfg, PROMPT.format(part=part, labels=", ".join(f"«{d}»" for d in present),
                                             text=chunk, listed=listed_hint), system=SYSTEM)
        except Exception as e:  # имена — подсказка, не повод ронять обработку
            log.warning("Не удалось угадать имена: %s", e)
            return {}
        for disp, item in _parse(answer).items():
            if disp not in targets or not isinstance(item, dict):
                continue
            raw_name = _clean_name(item.get("name"))
            ev = str(item.get("evidence", "")).strip()[:200]
            if not raw_name or not _evidence_in_chunk(ev, chunk):
                continue
            name = listed_keys.get(voices._norm(raw_name)) or _match_listed(raw_name, listed) or raw_name
            if _heard(raw_name, chunk) or (name in listed and _heard(name, chunk)):
                votes[disp][name] += 1
                evidence.setdefault((disp, name), ev)

    # Одно имя — одному собеседнику, и не то, что уже носит другой участник встречи.
    taken_names = [sp["name"] for sp in sps.values() if sp.get("name")]
    ranked = sorted(((c.most_common(1)[0][1], disp, c.most_common(1)[0][0]) for disp, c in votes.items()),
                    reverse=True)
    found = {}
    for _, disp, name in ranked:
        if any(voices.same_person_name(name, t) == 2 or voices._norm(name) == voices._norm(t) for t in taken_names):
            continue
        taken_names.append(name)
        label = targets[disp]
        found[label] = {"guess": name, "guess_evidence": evidence.get((disp, name), "")}
        log.info("Предполагаемое имя: %s → %s* («%s»)", disp, name, found[label]["guess_evidence"][:60])
    for label, g in found.items():
        sps[label].update(g)
    data["names_guessed"] = True
    voices.write_speakers(work, data)
    return found
