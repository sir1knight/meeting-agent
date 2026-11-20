"""Имена и термины: чтобы распознавание «слышало» их правильно.

GigaAM хорошо распознаёт русскую речь, но подсказок не принимает и пишет привычные слова: «Илья» вместо
«Ильяс». Whisper подсказки принимает. Поэтому после GigaAM фразы, где прозвучало слово, похожее на имя
известного человека (вы, участники встречи, календарь, «Голоса», словарь), распознаются ещё раз Whisper —
по тому же звуку и с подсказкой «Ильяс, Надя, Нина…». Если Whisper слышит известное имя, берём его.
Слово, которое само является именем кого-то из участников (на встрече правда есть Илья), не трогаем.

Ещё здесь: какие имена подсказывать распознаванию и модели протоколов, и пополнение словаря
распознавания из ручных правок (новое написание имени или термина запоминается).
"""

import logging
import re
from difflib import SequenceMatcher
from pathlib import Path

log = logging.getLogger(__name__)

SR = 16000
WORD = re.compile(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\-]*")
MAX_RECHECK = 40
GENERIC = re.compile(r"^(я|собеседник(\s*\d+)?|speaker\s*\d+)$", re.I)


def my_name(cfg: dict) -> str:
    return str(cfg.get("me", {}).get("name") or "").strip()


def known_names(cfg: dict, work: Path | None = None) -> list[str]:
    """Имена и термины, которые распознавание должно писать именно так."""
    from . import participants, voices

    out = [my_name(cfg)] + participants.of(cfg)
    if work:
        out += participants.read(work)
        out += [a for a in participants.read_calendar(work).get("attendees", []) if isinstance(a, str)]
    try:
        out += [p.get("name", "") for p in voices.load_profiles(cfg)]
    except Exception:  # noqa: BLE001 — без голосов тоже работаем
        pass
    out += list(cfg["whisper"].get("glossary") or [])
    seen, names = set(), []
    for n in out:
        n = re.sub(r"[*?]|\(\?\)", "", str(n or "")).strip()
        if n and not GENERIC.match(n) and n.lower() not in seen:
            seen.add(n.lower())
            names.append(n)
    return names


def name_words(names: list[str]) -> list[str]:
    """Отдельные слова-имена: «Надя В.» → «Надя»; инициалы и короткие слова не берём."""
    words = []
    for n in names:
        for w in WORD.findall(n):
            if len(w) >= 3 and w.lower() not in (x.lower() for x in words):
                words.append(w)
    return words


def _same_name(word: str, name: str) -> bool:
    """«Ильяса», «Ильясу» — формы «Ильяс»; «Надю» — форма «Надя»."""
    w, n = word.lower(), name.lower()
    if w == n:
        return True
    stem = n[:-1] if len(n) >= 4 and n[-1] in "аяьйоеиу" else n
    return w.startswith(stem) and len(w) - len(stem) <= 3


def _close(word: str, name: str) -> bool:
    w, n = word.lower(), name.lower()
    if _same_name(word, name) or len(w) < 3:
        return False
    return w[:2] == n[:2] and SequenceMatcher(None, w, n).ratio() >= 0.72


def suspicious(text: str, words: list[str], protect: list[str] = ()) -> list[tuple[str, str]]:
    """(слово в тексте, на какое известное имя похоже) — только слова с большой буквы или латиницей.
    protect — имена участников этой встречи: их формы не трогаем (на встрече правда есть Илья).
    Слово, совпадающее с другим знакомым именем («Илья» из «Голосов»), всё равно перепроверяем, если оно
    похоже на имя участника: какое из двух прозвучало — решает Whisper по звуку."""
    out = []
    for w in WORD.findall(text):
        if not (w[0].isupper() or re.search(r"[A-Za-z]", w)):
            continue
        if any(_same_name(w, n) for n in protect):
            continue
        for n in words:
            if _close(w, n):
                out.append((w, n))
                break
    return out


def meeting_people(cfg: dict, work: Path | None) -> list[str]:
    """Кто точно на этой встрече: вы, список участников, календарь, узнанные по голосу собеседники."""
    from . import participants, voices

    names = [my_name(cfg)] + participants.of(cfg)
    if work:
        names += participants.read(work)
        names += [a for a in participants.read_calendar(work).get("attendees", []) if isinstance(a, str)]
        try:
            names += [sp.get("name", "") for sp in voices.read_speakers(work).get("speakers", {}).values()]
        except Exception:  # noqa: BLE001
            pass
    return name_words([n for n in names if n])


def _whisper_cfg(cfg: dict) -> dict | None:
    from . import models

    for engine in ("whisper-turbo", "whisper-large"):
        if models.engine_ready(cfg, engine):
            return dict(cfg, whisper=dict(cfg["whisper"], engine=engine))
    return None


def recheck(segments: list[dict], wav16: Path, cfg: dict, work: Path | None) -> int:
    """Перепроверить Whisper фразы с подозрительными именами (время сегментов — в шкале этой дорожки).
    Возвращает, сколько слов исправлено."""
    from . import models

    if models.asr_engine(cfg) != "gigaam" or not segments:
        return 0
    words = name_words(known_names(cfg, work))
    if not words:
        return 0
    protect = meeting_people(cfg, work)
    todo = [(s, sus) for s in segments if (sus := suspicious(s.get("text", ""), words, protect))][:MAX_RECHECK]
    if not todo:
        return 0
    wcfg = _whisper_cfg(cfg)
    if not wcfg:
        log.info("Имена: Whisper не скачан — перепроверка %d фраз пропущена (Настройки → Распознавание)", len(todo))
        return 0
    from . import audio
    from .transcribe import _whisper_raw

    wav = audio.load(wav16)
    prompt = ", ".join(words[:40]) + "."
    fixed = 0
    log.info("Имена: перепроверяю Whisper %d фраз…", len(todo))
    for seg, sus in todo:
        a, b = max(0, int((seg["start"] - 0.3) * SR)), min(len(wav), int((seg["end"] + 0.3) * SR))
        if b - a < SR // 3:
            continue
        try:
            heard = " ".join(x.get("text", "") for x in _whisper_raw(wav[a:b], wcfg, prompt))
        except Exception as e:  # noqa: BLE001 — перепроверка не должна ронять обработку
            log.warning("Имена: перепроверка не удалась: %s", e)
            return fixed
        heard_words = WORD.findall(heard)
        for wrong, name in sus:
            right = next((h for h in heard_words if _same_name(h, name)), None)
            if not right or wrong in heard_words:
                continue  # Whisper тоже слышит «Илья» — оставляем как есть
            seg["text"] = re.sub(rf"(?<![\w-]){re.escape(wrong)}(?![\w-])", right, seg["text"], count=1)
            log.info("Имена: «%s» → «%s» (%s)", wrong, right, f"{seg['start']:.0f} с")
            fixed += 1
    return fixed


# ---------- словарь из ручных правок ----------

def learn_terms(old: str, new: str) -> list[str]:
    """Слова, которые пользователь вписал при правке: имена и термины (с большой буквы или латиницей)."""
    before = {w.lower() for w in WORD.findall(old or "")}
    out = []
    for w in WORD.findall(new or ""):
        if len(w) < 3 or w.lower() in before or w.lower() in (x.lower() for x in out):
            continue
        if w[0].isupper() or re.search(r"[A-Za-z]", w):
            out.append(w)
    return out


def remember(terms: list[str]) -> list[str]:
    """Пополнить словарь распознавания (Настройки → Распознавание речи → Словарь). Возвращает добавленное."""
    from . import config

    if not terms:
        return []
    cur = list(config.load()["whisper"].get("glossary") or [])
    low = {w.lower() for w in cur}
    add = [t for t in terms if t.lower() not in low]
    if add:
        config.save_user_settings({"whisper": {"glossary": (cur + add)[-200:]}})
        log.info("Словарь распознавания: + %s", ", ".join(add))
    return add


def protocol_hint(cfg: dict, work: Path | None = None) -> str:
    """Строка для модели протоколов: кто «Я» и как правильно пишутся имена."""
    names = known_names(cfg, work)
    me = my_name(cfg)
    parts = []
    if me:
        parts.append(f"«Я» в расшифровке — это {me} (пользователь).")
    if names:
        parts.append("Распознавание речи могло исказить похожие имена и термины (например, «Илья» вместо «Ильяс»). "
                     f"Правильные написания: {', '.join(names[:40])} — используй их, если в расшифровке явно "
                     "искажённый вариант того же имени.")
    return " ".join(parts)
