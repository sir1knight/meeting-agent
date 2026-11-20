"""Умный поиск: вопрос обычными словами → ответ по протоколам и расшифровкам со ссылками на источники.

1. Отбор фрагментов: даты из вопроса («вчера», «на прошлой неделе», «26 октября») сужают круг встреч,
   затем поиск по словам (BM25 с грубым учётом окончаний) по репликам расшифровок и пунктам протоколов.
2. Ответ: модель отвечает только по найденным фрагментам и ссылается на них номерами [n].
   Модель — на выбор (ask.backend): Claude по подписке, Claude API, Gemini API, ChatGPT (OpenAI API)
   или Qwen на этом компьютере.
"""

import json
import logging
import math
import re
import subprocess
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config
from .osutil import PC

log = logging.getLogger(__name__)

BACKENDS = {"claude-code": "Claude по подписке (Claude Code)", "claude": "Claude API по ключу",
            "gemini": "Gemini API по ключу", "openai": "ChatGPT (OpenAI API) по ключу",
            "local": f"Qwen на {PC}"}

SYSTEM = ("Ты отвечаешь на вопросы о рабочих встречах пользователя по фрагментам их протоколов и расшифровок. "
          "Пиши по-русски, коротко и по делу. Опирайся только на фрагменты; если ответа в них нет — так и скажи. "
          "После каждого утверждения ставь ссылку на источник в квадратных скобках: [3] или [2][5]. "
          "«Я» в расшифровках — это сам пользователь. Имя со звёздочкой (Олег*) угадано по разговору, "
          "со знаком (?) — неуверенно узнано по голосу. Расшифровка автоматическая, в ней бывают ошибки распознавания.")

STOP = set("""а без более бы был была были было быть в вам вас весь во вот все всё всего всех вы где да даже для до его
ее её если есть еще ещё же за и из или им их к как какие какой когда кто ли либо мне мы на над надо наш не него нее
неё нет ни них но ну о об однако он она они оно от очень по под при про с со так также такой там те тем то того тоже
той только том ту ты у уж уже хотя чего чей чем что чтобы чье чья эта эти это этот я
обсуждали обсуждал обсуждала обсуждение говорили сказал сказали решили решение решения встреча встрече встречи встречах
вчера сегодня позавчера неделе неделю прошлой этой прошлую было будет мой моя мои меня какая какое какие расскажи
напомни скажи найди""".split())

MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7, "август": 8,
          "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12}
WEEKDAYS = {"понедельник": 0, "вторник": 1, "сред": 2, "четверг": 3, "пятниц": 4, "суббот": 5, "воскресень": 6}


# ---------- тексты встреч ----------

def _stem(word: str) -> str:
    w = word.lower().replace("ё", "е")
    return w[:5] if len(w) > 5 and re.match(r"[а-я]", w) else w


def _tokens(text: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", text) if w.lower() not in STOP and len(w) > 1]


def _ts_seconds(ts: str) -> int:
    h, m, s = (int(x) for x in ts.split(":"))
    return h * 3600 + m * 60 + s


def load_chunks(cfg: dict) -> list[dict]:
    """Фрагменты всех встреч: пункты протоколов и реплики расшифровок."""
    from .server import _front_matter  # разбор заметки — тот же, что для окна приложения

    out = []
    notes_dir = Path(cfg["paths"]["notes"])
    for path in sorted(notes_dir.glob("*.md")):
        try:
            meta, body = _front_matter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        m = re.search(r"^# (.+)$", body, re.M)
        title = m.group(1).strip() if m else path.stem
        try:
            day = datetime.strptime(meta.get("date", "")[:10], "%Y-%m-%d").date()
        except ValueError:
            day = None
        base = {"note": path.name, "title": title, "date": meta.get("date", ""), "day": day}
        summary, _, transcript = body.partition("\n## Расшифровка\n")
        section = ""
        for line in summary.splitlines():
            line = line.strip()
            if line.startswith("## "):
                section = line[3:].strip()
            elif line and not line.startswith("# ") and not set(line) <= set("|-: "):
                out.append(dict(base, kind="summary", section=section, text=line.strip("- ").strip()))
        for block in transcript.split("\n\n"):
            m = re.match(r"\*\*\[(\d\d:\d\d:\d\d)\]\s*(.+?):\*\*\s*(.*)", block.strip(), re.S)
            if m:
                out.append(dict(base, kind="transcript", ts=m.group(1), seconds=_ts_seconds(m.group(1)),
                                speaker=m.group(2), text=m.group(3).strip()))
    return out


# ---------- даты в вопросе ----------

def date_filter(question: str, today: date | None = None) -> tuple[date, date] | None:
    q = question.lower().replace("ё", "е")
    today = today or date.today()
    if "позавчера" in q:
        d = today - timedelta(days=2)
        return d, d
    if "вчера" in q:
        d = today - timedelta(days=1)
        return d, d
    if "сегодня" in q:
        return today, today
    monday = today - timedelta(days=today.weekday())
    if re.search(r"прошл\w* недел", q):
        return monday - timedelta(days=7), monday - timedelta(days=1)
    if re.search(r"(эт\w*|текущ\w*) недел", q):
        return monday, today
    if re.search(r"прошл\w* месяц", q):
        first = today.replace(day=1)
        prev_last = first - timedelta(days=1)
        return prev_last.replace(day=1), prev_last
    m = re.search(r"\b(\d{1,2})[.\s]+(\d{1,2}|[а-я]+)", q)
    if m:
        day_n, mon = int(m.group(1)), m.group(2)
        month = int(mon) if mon.isdigit() else next((v for k, v in MONTHS.items() if mon.startswith(k)), None)
        if month and 1 <= month <= 12 and 1 <= day_n <= 31:
            try:
                d = date(today.year, month, day_n)
                if d > today:
                    d = d.replace(year=today.year - 1)
                return d, d
            except ValueError:
                pass
    for k, wd in WEEKDAYS.items():
        if re.search(rf"\b(в|во|в прошл\w*)\s+{k}", q):
            d = today - timedelta(days=(today.weekday() - wd) % 7 or 7)
            return d, d
    return None


# ---------- отбор фрагментов ----------

def retrieve(question: str, cfg: dict, history: list[dict], budget: int) -> list[dict]:
    chunks = load_chunks(cfg)
    rng = date_filter(question)
    if rng:
        chunks = [c for c in chunks if c["day"] and rng[0] <= c["day"] <= rng[1]]
    if not chunks:
        return []
    # Для уточняющих вопросов («а кто за это отвечает?») учитываем и предыдущий вопрос.
    prev = " ".join(h["q"] for h in history[-1:]) if history else ""
    q_terms = _tokens(question) + [t for t in _tokens(prev) if t not in _tokens(question)][:6]
    docs = [_tokens(c["text"] + " " + c.get("section", "")) for c in chunks]
    n = len(docs)
    avg = sum(map(len, docs)) / n or 1
    df = Counter(t for d in docs for t in set(d))
    scores = []
    for c, d in zip(chunks, docs):
        tf = Counter(d)
        s = 0.0
        for t in set(q_terms):
            if t in tf:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * len(d) / avg))
        if c["kind"] == "summary":
            s *= 1.3  # пункты протокола — уже выжимка
        scores.append(s)

    picked: list[int] = []
    ranked = sorted(range(n), key=lambda i: scores[i], reverse=True)
    if scores[ranked[0]] > 0:
        picked = [i for i in ranked[:24] if scores[i] > 0]
    # Вопрос про встречу целиком («что решили вчера?») — даём протоколы встреч за этот период.
    if rng or not picked:
        notes = list(dict.fromkeys(chunks[i]["note"] for i in picked)) or \
            list(dict.fromkeys(c["note"] for c in sorted(chunks, key=lambda c: c["date"], reverse=True)))
        for i, c in enumerate(chunks):
            if c["kind"] == "summary" and c["note"] in notes[:4] and i not in picked:
                picked.append(i)

    # Реплике расшифровки добавляем соседние — ответ без контекста часто непонятен.
    out, used, size = [], set(), 0
    for i in picked:
        c = dict(chunks[i])
        if c["kind"] == "transcript":
            near = [chunks[j] for j in (i - 1, i + 1) if 0 <= j < n and chunks[j]["note"] == c["note"]
                    and chunks[j]["kind"] == "transcript"]
            c["context"] = near
        key = (c["note"], c.get("ts") or c["text"][:40])
        if key in used:
            continue
        piece = len(c["text"]) + sum(len(x["text"]) for x in c.get("context", []))
        if size + piece > budget:
            break
        used.add(key)
        size += piece
        out.append(c)
    out.sort(key=lambda c: (c["date"], c.get("seconds", -1)))
    for k, c in enumerate(out, 1):
        c["n"] = k
    return out


def _context(sources: list[dict]) -> str:
    lines = []
    for c in sources:
        where = f"{c['date']} «{c['title']}»"
        if c["kind"] == "summary":
            lines.append(f"[{c['n']}] {where}, протокол, раздел «{c['section']}»: {c['text']}")
        else:
            ctx = " ".join(f"({x['speaker']}: {x['text'][:200]})" for x in c.get("context", []))
            lines.append(f"[{c['n']}] {where}, расшифровка {c['ts']}, {c['speaker']}: {c['text']}"
                         + (f"\n    рядом: {ctx}" if ctx else ""))
    return "\n".join(lines)


# ---------- модели ----------

def backend(cfg: dict) -> str:
    b = cfg.get("ask", {}).get("backend") or cfg["llm"].get("backend", "local")
    return b if b in BACKENDS else "local"


def _llm(cfg: dict, user: str, system: str = SYSTEM) -> str:
    from .summarize import chat_claude, chat_claude_code, chat_gemini, chat_openai

    b = backend(cfg)
    if b == "claude-code":
        return chat_claude_code(cfg, user, system)
    if b == "claude":
        return chat_claude(cfg, user, system)
    if b == "gemini":
        # Для поиска — быстрая «flash», а не модель протоколов.
        return chat_gemini(cfg, user, system, cfg.get("ask", {}).get("gemini_model"))
    if b == "openai":
        return chat_openai(cfg, user, system)
    # Qwen — в отдельном процессе: сервер не держит 4,5 ГБ модели в памяти между вопросами.
    r = subprocess.run([sys.executable, "-m", "meeting_agent", "ask-llm"], input=json.dumps(
        {"system": system, "user": user}, ensure_ascii=False), capture_output=True, text=True,
        cwd=config.ROOT, timeout=900)
    if r.returncode != 0:
        err = (r.stderr.strip().splitlines() or ["неизвестная ошибка"])[-1]
        raise RuntimeError(f"Qwen не ответил: {err}")
    return r.stdout.strip()


# ---------- вопрос целиком ----------

def ask(question: str, history: list[dict], cfg: dict) -> dict:
    question = question.strip()
    if not question:
        raise ValueError("Задайте вопрос")
    budget = 9000 if backend(cfg) == "local" else 40000
    sources = retrieve(question, cfg, history, budget)
    if not sources:
        rng = date_filter(question)
        msg = (f"За {rng[0]:%d.%m}" + (f"–{rng[1]:%d.%m}" if rng[1] != rng[0] else "") + " встреч не нашёл."
               if rng else "Не нашёл во встречах ничего похожего. Попробуйте другие слова.")
        return {"answer": msg, "sources": []}
    past = "\n".join(f"Вопрос: {h['q']}\nОтвет: {h['a'][:600]}" for h in history[-3:])
    user = ((f"Предыдущий разговор:\n{past}\n\n" if past else "")
            + f"Сегодня {date.today():%d.%m.%Y}.\nФрагменты встреч:\n{_context(sources)}\n\nВопрос: {question}")
    answer = _llm(cfg, user)
    return {"answer": answer, "backend": backend(cfg),
            "sources": [{"n": c["n"], "note": c["note"], "title": c["title"], "date": c["date"],
                         "kind": c["kind"], "ts": c.get("ts"), "seconds": c.get("seconds"),
                         "section": c.get("section"), "speaker": c.get("speaker"),
                         "text": c["text"][:240]} for c in sources]}


# ---------- чат по одной встрече ----------

MEETING_SYSTEM = (
    "Ты помощник по одной рабочей встрече пользователя. У тебя есть её протокол и расшифровка. "
    "Пиши по-русски, по делу, без вступлений. Опирайся только на протокол и расшифровку; "
    "чего в них нет — не выдумывай, так и скажи. «Я» в расшифровке — сам пользователь. "
    "Имя со звёздочкой (Олег*) угадано по разговору, со знаком (?) — неуверенно узнано по голосу. "
    "Расшифровка автоматическая, в ней бывают ошибки распознавания. Таблицы и списки оформляй в Markdown. "
    "Письма пиши деловым стилем, от первого лица пользователя, без выдуманных имён, дат и сумм.")


def _fit_transcript(transcript: str, question: str, budget: int) -> str:
    """Расшифровка целиком, если влезает; иначе — реплики, ближе всего к вопросу, в исходном порядке."""
    if len(transcript) <= budget:
        return transcript
    blocks = [b for b in transcript.split("\n\n") if b.strip()]
    words = set(_tokens(question))
    scored = sorted(range(len(blocks)), key=lambda i: -len(words & set(_tokens(blocks[i]))))
    keep, size = set(), 0
    for i in scored:
        if size + len(blocks[i]) > budget:
            continue
        keep.add(i)
        size += len(blocks[i]) + 2
    return "\n\n".join(blocks[i] for i in sorted(keep)) + "\n\n(расшифровка сокращена: показаны реплики, ближе всего к вопросу)"


def meeting_ask(title: str, date_str: str, summary: str, transcript: str, question: str,
                history: list[dict], cfg: dict) -> dict:
    question = question.strip()
    if not question:
        raise ValueError("Задайте вопрос")
    local = backend(cfg) == "local"
    budget = 20000 if local else 400000  # Qwen: ~8 тыс. токенов на расшифровку; облачным — целиком
    past = "\n".join(f"Вопрос: {h['q']}\nОтвет: {h['a'][:1500]}" for h in history[-3:])
    user = (f"Встреча: {title} ({date_str}).\n\n## Протокол\n{summary}\n\n## Расшифровка\n"
            f"{_fit_transcript(transcript, question, budget)}\n\n"
            + (f"Предыдущий разговор:\n{past}\n\n" if past else "")
            + f"Сегодня {date.today():%d.%m.%Y}.\nЗадание: {question}")
    return {"answer": _llm(cfg, user, MEETING_SYSTEM), "backend": backend(cfg)}
