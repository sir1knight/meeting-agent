"""Слепки голоса: запоминание собеседников по голосу между встречами.

Хранение (только на компьютере, где установлено приложение):
  <paths.voices>/<id>/profile.json — имя, контакты, эмбеддинги голоса (по одному на встречу);
  <paths.voices>/<id>/sample.wav   — образец голоса на 10–15 секунд.
В папке встречи:
  speakers.json  — собеседники встречи: эмбеддинг, длительность речи, кому сопоставлен;
  voices/*.wav   — образцы голоса каждого собеседника этой встречи.
"""

import json
import logging
import re
import shutil
import time
import uuid
from pathlib import Path

from . import osutil

log = logging.getLogger(__name__)

SPEAKERS_FILE = "speakers.json"
SAMPLES_DIR = "voices"
TG_RE = re.compile(r"^[A-Za-z0-9_]{4,32}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------- профили ----------

def voices_dir(cfg: dict) -> Path:
    return Path(cfg["paths"]["voices"])


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip()).casefold().replace("ё", "е")


# Уменьшительные и полные формы русских имён — чтобы «Саша» в списке участников находил слепок «Александр Нестеров»,
# а «Надя В» (без точки) — слепок «Надя В.». Имена «Илья» и «Ильяс» намеренно разные.
_CANON_NAMES = {
    "александр": "александр", "саша": "александр", "саня": "александр", "шура": "александр", "алекс": "александр",
    "алексей": "алексей", "леша": "алексей",
    "дмитрий": "дмитрий", "дима": "дмитрий", "митя": "дмитрий",
    "надежда": "надежда", "надя": "надежда",
    "ирина": "ирина", "ира": "ирина",
    "ольга": "ольга", "оля": "ольга",
    "николай": "николай", "коля": "николай",
    "татьяна": "татьяна", "таня": "татьяна",
    "юлия": "юлия", "юля": "юля",
    "антонина": "антонина", "тоня": "антонина",
    "анатолий": "анатолий", "толя": "анатолий",
    "владимир": "владимир", "вова": "владимир", "володя": "владимир",
    "владислав": "владислав", "влад": "владислав",
    "максим": "максим", "макс": "максим",
    "анна": "анна", "аня": "анна", "анюта": "анна",
    "сергей": "сергей", "сережа": "сергей",
    "михаил": "михаил", "миша": "михаил",
    "маргарита": "маргарита", "рита": "маргарита",
    "лидия": "лидия", "лида": "лидия",
    "елена": "елена", "лена": "елена",
    "наталья": "наталья", "наталия": "наталья", "наташа": "наталья",
    "мария": "мария", "маша": "мария",
    "екатерина": "екатерина", "катя": "екатерина",
    "евгений": "евгений", "евгения": "евгений", "женя": "евгений",
    "павел": "павел", "паша": "павел",
    "валентин": "валентин", "валентина": "валентин", "валя": "валентин",
    "виктория": "виктория", "вика": "виктория",
    "анастасия": "анастасия", "настя": "анастасия",
    "артем": "артем", "тема": "артем",
    "иван": "иван", "ваня": "иван",
    "петр": "петр", "петя": "петр",
    "константин": "константин", "костя": "константин",
    "станислав": "станислав", "стас": "станислав",
    "вячеслав": "вячеслав", "слава": "вячеслав",
    "роман": "роман", "рома": "роман",
    "дарья": "дарья", "даша": "дарья",
    "софия": "софия", "софья": "софия", "соня": "софия",
    "ксения": "ксения", "ксенья": "ксения", "ксюша": "ксения",
    "светлана": "светлана", "света": "светлана",
    "людмила": "людмила", "люда": "людмила",
    "галина": "галина", "галя": "галина",
    "любовь": "любовь", "люба": "любовь",
    "валерий": "валерий", "валерия": "валерий", "валера": "валерий",
}


def _name_tokens(name: str) -> list[str]:
    raw = re.findall(r"[a-zа-я0-9]+", name.casefold().replace("ё", "е"))
    return [_CANON_NAMES.get(w, w) for w in raw]


def same_person_name(a: str, b: str) -> int:
    """Степень совпадения двух записей имени: 2 — полное (с учётом точек и инициала фамилии),
    1 — одно является частью другого («Иван» ↔ «Иван Митрохин», «Нина» ↔ «Нина Р.»), 0 — разные люди."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return 0
    sa, sb = set(ta), set(tb)
    if sa == sb:
        return 2
    if len(ta) == 2 and len(tb) == 2 and ta[0] == tb[0]:
        if (len(ta[1]) == 1 and tb[1].startswith(ta[1])) or (len(tb[1]) == 1 and ta[1].startswith(tb[1])):
            return 2
    if sa < sb or sb < sa:
        return 1
    return 0


def load_profiles(cfg: dict) -> list[dict]:
    root = voices_dir(cfg)
    out = []
    if root.exists():
        for f in sorted(root.glob("*/profile.json")):
            try:
                p = json.loads(f.read_text(encoding="utf-8"))
                p["id"] = f.parent.name
                out.append(p)
            except (OSError, ValueError):
                log.warning("Повреждён слепок голоса %s", f)
    return out


def get_profile(cfg: dict, vid: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{12}", vid or ""):
        raise LookupError("Слепок голоса не найден")
    f = voices_dir(cfg) / vid / "profile.json"
    if not f.exists():
        raise LookupError("Слепок голоса не найден")
    p = json.loads(f.read_text(encoding="utf-8"))
    p["id"] = vid
    return p


def save_profile(cfg: dict, p: dict) -> None:
    d = voices_dir(cfg) / p["id"]
    d.mkdir(parents=True, exist_ok=True)
    p["updated"] = _now()
    p["meetings"] = len({e["meeting"] for e in p.get("embeddings", [])})
    tmp = d / "profile.tmp"
    tmp.write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(d / "profile.json")


def find_by_name(cfg: dict, name: str) -> dict | None:
    key = _norm(name)
    return next((p for p in load_profiles(cfg) if _norm(p.get("name", "")) == key), None)


def create_profile(cfg: dict, name: str) -> dict:
    p = {"id": uuid.uuid4().hex[:12], "name": name.strip(), "email": "", "telegram": "",
         "embeddings": [], "created": _now()}
    save_profile(cfg, p)
    return p


def add_embedding(cfg: dict, p: dict, meeting: str, speaker: str, vector: list[float],
                  duration: float, auto: bool) -> None:
    """Один эмбеддинг на встречу. Лишние сверх лимита — сначала автоматические, потом самые старые."""
    embs = [e for e in p.get("embeddings", []) if e["meeting"] != meeting]
    embs.append({"meeting": meeting, "speaker": speaker, "vector": vector,
                 "duration": round(duration, 1), "auto": auto, "added": _now()})
    limit = int(cfg["voices"].get("max_embeddings", 20))
    while len(embs) > limit:
        victim = next((e for e in embs if e.get("auto")), embs[0])
        embs.remove(victim)
    p["embeddings"] = embs
    save_profile(cfg, p)


def remove_embedding(cfg: dict, vid: str, meeting: str) -> None:
    try:
        p = get_profile(cfg, vid)
    except LookupError:
        return
    before = len(p.get("embeddings", []))
    p["embeddings"] = [e for e in p.get("embeddings", []) if e["meeting"] != meeting]
    if len(p["embeddings"]) != before:
        save_profile(cfg, p)


def public(cfg: dict, p: dict) -> dict:
    """Профиль для окна приложения — без самих векторов."""
    d = voices_dir(cfg) / p["id"]
    return {"id": p["id"], "name": p.get("name", ""), "email": p.get("email", ""),
            "telegram": p.get("telegram", ""), "meetings": p.get("meetings", 0),
            "created": p.get("created"), "updated": p.get("updated"),
            "has_sample": (d / "sample.wav").exists()}


# ---------- сопоставление ----------

def _cos(a, b) -> float:
    import numpy as np

    a, b = np.asarray(a, dtype="float32"), np.asarray(b, dtype="float32")
    return float(a @ b / ((np.linalg.norm(a) * np.linalg.norm(b)) or 1.0))


def score(profile: dict, vector: list[float], method: str = "max", exclude_meeting: str | None = None) -> float:
    import numpy as np

    vecs = [e["vector"] for e in profile.get("embeddings", []) if e["meeting"] != exclude_meeting]
    if not vecs:
        return -1.0
    if method == "centroid":
        c = np.mean(np.asarray(vecs, dtype="float32"), axis=0)
        return _cos(c, vector)
    return max(_cos(v, vector) for v in vecs)


def match(speakers: dict[str, dict], cfg: dict, meeting: str,
          taken_voices: set[str] | None = None) -> dict[str, dict]:
    """{метка: {voice_id, name, score, uncertain}} — жадно по убыванию сходства.
    Если задан список участников встречи — максимально ищет их голоса по слепкам."""
    v = cfg["voices"]
    hi, lo = float(v["match_threshold"]), float(v["uncertain_threshold"])
    min_speech = float(v.get("min_speech_seconds", 10))
    bonus = max(float(v.get("participant_bonus", 0.15)), 0.15)
    listed_floor = float(v.get("listed_min_threshold", 0.34))
    from .participants import of
    listed_raw = [n for n in of(cfg) if _norm(n)]
    has_list = bool(listed_raw)

    def in_listed(name: str) -> bool:
        return any(same_person_name(name, ln) > 0 for ln in listed_raw)

    profiles = [p for p in load_profiles(cfg) if p.get("embeddings")]
    on_list_ids = {p["id"] for p in profiles if in_listed(p.get("name", ""))}
    all_scores: dict[tuple[str, str], float] = {}
    top_by_label: dict[str, list[tuple[float, str]]] = {}

    for label, sp in speakers.items():
        if not sp.get("embedding"):
            continue
        row = []
        for p in profiles:
            s = score(p, sp["embedding"], v.get("method", "max"), exclude_meeting=meeting)
            all_scores[(label, p["id"])] = s
            row.append((s, p["id"]))
        row.sort(reverse=True)
        top_by_label[label] = row

    pairs = []
    fallback_listed = []
    for label, sp in speakers.items():
        if not sp.get("embedding"):
            continue
        dur = sp.get("duration", 0)
        row = top_by_label.get(label, [])
        best_any = row[0][0] if row else -1.0
        top5_ids = {pid for _, pid in row[:5]}
        for p in profiles:
            s = all_scores[(label, p["id"])]
            on_list = p["id"] in on_list_ids
            # Если задан список участников: посторонних не подставляем в «серой зоне» (с «?»),
            # чтобы на встречу не попадали имена отсутствующих коллег; для них требуется уверенное совпадение.
            if has_list and not on_list:
                if s >= hi and dur >= min_speech:
                    pairs.append((s, s, label, p))
                continue
            # В серой зоне (s < lo) требуем, чтобы слепок входил в топ-5 ближайших голосов этого спикера
            # и не отставал от лучшего голоса более чем на 0.15 (защита от ложного совпадения чужого пола).
            if s < lo and (p["id"] not in top5_ids or best_any - s > 0.15):
                continue
            b = bonus if on_list else 0.0
            if s + b >= lo:
                pairs.append((s + b, s, label, p))
            elif on_list and s >= listed_floor:
                fallback_listed.append((s, label, p))

    pairs.sort(key=lambda x: x[0], reverse=True)
    used_labels: set[str] = set()
    used_voices: set[str] = set(taken_voices or ())
    out: dict[str, dict] = {}
    for eff, s, label, p in pairs:
        if label in used_labels or p["id"] in used_voices:
            continue
        used_labels.add(label)
        used_voices.add(p["id"])
        # Мало речи — эмбеддинг ненадёжен: даже высокое сходство считаем «серой зоной».
        sure = eff >= hi and speakers[label].get("duration", 0) >= min_speech
        out[label] = {"voice_id": p["id"], "name": p.get("name", ""), "score": round(s, 3),
                      "uncertain": not sure}

    # Проход 2: для участников из списка, кому ещё не достался голос, ищем лучший свободный кластер до listed_floor.
    fallback_listed.sort(key=lambda x: x[0], reverse=True)
    for s, label, p in fallback_listed:
        if label in used_labels or p["id"] in used_voices:
            continue
        used_labels.add(label)
        used_voices.add(p["id"])
        out[label] = {"voice_id": p["id"], "name": p.get("name", ""), "score": round(s, 3),
                      "uncertain": True}

    # Проход 3: если pyannote разбил одного человека на несколько кластеров (SPEAKER_XX),
    # и у оставшегося безымянного кластера явно тот же голос, что уже найден на встрече.
    for label, sp in speakers.items():
        if label in used_labels or not sp.get("embedding"):
            continue
        ranked = sorted(((all_scores.get((label, p["id"]), -1.0), p) for p in profiles),
                        key=lambda x: x[0], reverse=True)
        if not ranked:
            continue
        best_s, best_p = ranked[0]
        second_s = ranked[1][0] if len(ranked) > 1 else -1.0
        if best_p["id"] not in used_voices:
            continue
        on_list = best_p["id"] in on_list_ids
        dur = sp.get("duration", 0)
        if has_list and not on_list and dur < min_speech:
            continue
        if best_s >= hi or (on_list and best_s >= 0.68 and best_s - second_s >= 0.15):
            sure = (best_s + (bonus if on_list else 0.0)) >= hi and dur >= min_speech
            out[label] = {"voice_id": best_p["id"], "name": best_p.get("name", ""), "score": round(best_s, 3),
                          "uncertain": not sure}
            used_labels.add(label)
    return out


# ---------- собеседники встречи ----------

def read_speakers(work: Path) -> dict:
    try:
        return json.loads((work / SPEAKERS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_speakers(work: Path, data: dict) -> None:
    tmp = work / (SPEAKERS_FILE + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(work / SPEAKERS_FILE)


def display_name(sp: dict) -> str:
    """Как подписать собеседника: имя → имя по голосу «(?)» → предполагаемое по разговору «*» → «Собеседник N»."""
    if sp.get("name"):
        return sp["name"] + (" (?)" if sp.get("uncertain") else "")
    if sp.get("guess"):
        return sp["guess"] + "*"
    return sp.get("base", "Собеседник")


def identify(work: Path, speakers, base_names: dict[str, str], cfg: dict) -> dict:
    """После разделения собеседников: записать speakers.json и подставить известные голоса."""
    data = {"model": cfg["diarization"]["model"], "speakers": {}}
    prev = read_speakers(work).get("speakers", {})
    for label, sp in speakers.items():
        data["speakers"][label] = {"base": base_names.get(label, "Собеседник"),
                                   "duration": sp.duration, "embedding": sp.embedding,
                                   "sample": sp.sample}
    if cfg["voices"].get("enabled", True) and cfg["diarization"].get("enabled"):
        # Убрать из слепков эмбеддинги этой встречи от прошлой обработки — метки могли поменяться.
        for old in prev.values():
            if old.get("voice_id") and old.get("auto"):
                remove_embedding(cfg, old["voice_id"], work.name)
        found = match(data["speakers"], cfg, work.name)
        added_voices: set[str] = set()
        for label, m in sorted(found.items(), key=lambda kv: data["speakers"][kv[0]].get("duration", 0), reverse=True):
            sp = data["speakers"][label]
            sp.update(voice_id=m["voice_id"], name=m["name"], score=m["score"],
                      uncertain=m["uncertain"], auto=True)
            if not m["uncertain"] and m["voice_id"] not in added_voices:  # уверенно узнали — слепок пополняется этой встречей
                added_voices.add(m["voice_id"])
                p = get_profile(cfg, m["voice_id"])
                add_embedding(cfg, p, work.name, label, sp["embedding"], sp["duration"], auto=True)
            log.info("Узнал голос: %s → %s (%.2f%s)", sp["base"], m["name"], m["score"],
                     ", не уверен" if m["uncertain"] else "")
        # Имена, которые пользователь задал вручную без слепка, сохраняем при повторной обработке.
        for label, old in prev.items():
            if label in data["speakers"] and old.get("manual") and label not in found:
                data["speakers"][label].update(name=old.get("name"), manual=True)
    write_speakers(work, data)
    return data


def rematch(cfg: dict, work: Path, note: Path | None = None) -> bool:
    """Пересопоставить сохранённые в speakers.json эмбеддинги со слепками голосов (например, когда
    пользователь добавил или изменил список участников уже обработанной встречи) без повторной расшифровки."""
    if not cfg["voices"].get("enabled", True):
        return False
    data = read_speakers(work)
    sps = data.get("speakers", {})
    if not sps:
        return False
    from . import participants
    cfg.setdefault("meeting", {})["participants"] = participants.read(work)

    before = {label: display_name(sp) for label, sp in sps.items()}
    taken_voices: set[str] = set()
    candidates: dict[str, dict] = {}
    raw_text = ""
    if (work / SEGMENTS_FILE).exists():
        try:
            raw_text = "\n".join(s.get("text", "") for s in _read_segments(work).get("segments", []))
        except LookupError:
            pass
    from . import names as _names
    for label, sp in sps.items():
        # Ручные переименования пользователя (с запоминанием голоса или без) не перезаписываем.
        if sp.get("manual") or (sp.get("voice_id") and not sp.get("auto")):
            if sp.get("voice_id"):
                taken_voices.add(sp["voice_id"])
            continue
        if sp.get("voice_id") and sp.get("auto"):
            remove_embedding(cfg, sp["voice_id"], work.name)
        for k in ("name", "voice_id", "score", "uncertain", "auto"):
            sp.pop(k, None)
        if sp.get("guess") and raw_text:
            if (not _names._evidence_in_chunk(sp.get("guess_evidence", ""), raw_text)
                    or not _names._heard(sp["guess"], raw_text)):
                sp.pop("guess", None)
                sp.pop("guess_evidence", None)
        if sp.get("embedding"):
            candidates[label] = sp

    if candidates:
        found = match(candidates, cfg, work.name, taken_voices=taken_voices)
        added_voices = set(taken_voices)
        for label, m in sorted(found.items(), key=lambda kv: sps[kv[0]].get("duration", 0), reverse=True):
            sp = sps[label]
            sp.update(voice_id=m["voice_id"], name=m["name"], score=m["score"],
                      uncertain=m["uncertain"], auto=True)
            if not m["uncertain"] and m["voice_id"] not in added_voices:
                added_voices.add(m["voice_id"])
                try:
                    p = get_profile(cfg, m["voice_id"])
                    add_embedding(cfg, p, work.name, label, sp["embedding"], sp.get("duration", 0), auto=True)
                except LookupError:
                    pass
            log.info("Узнал голос (по списку): %s → %s (%.2f%s)", sp.get("base", label), m["name"], m["score"],
                     ", не уверен" if m["uncertain"] else "")

    changed = any(display_name(sps[k]) != before[k] for k in sps)
    if any(not sp.get("name") for sp in sps.values()):
        data["names_guessed"] = False
    if changed and note and note.exists():
        data["protocol_stale"] = True
    write_speakers(work, data)
    if changed:
        for label, sp in sps.items():
            old_d, new_d = before[label], display_name(sp)
            if old_d != new_d:
                replace_in_note(note, old_d, new_d)
        if (work / SEGMENTS_FILE).exists():
            rewrite_transcript(work, note)
    return changed


def apply_names(segments: list[dict], work: Path) -> list[dict]:
    """Подставляет имена из speakers.json в реплики (по метке pyannote)."""
    sps = read_speakers(work).get("speakers", {})
    for s in segments:
        sp = sps.get(s.get("spk") or "")
        if sp:
            s["speaker"] = display_name(sp)
    return segments


# ---------- переименование и правка заметки ----------

def replace_in_note(note: Path, old: str, new: str) -> None:
    """«Собеседник 2» → «Иван Петров» во всей заметке (не задевая «Собеседник 21»)."""
    if not note or not note.exists() or old == new:
        return
    text = note.read_text(encoding="utf-8")
    text = re.sub(re.escape(old) + r"(?![\w\d])", new.replace("\\", r"\\"), text)
    osutil.atomic_write_text(note, text)


def rename_speaker(cfg: dict, work: Path, label: str, name: str, remember: bool,
                   note: Path | None) -> dict:
    data = read_speakers(work)
    sp = data.get("speakers", {}).get(label)
    if not sp:
        raise LookupError("Собеседник не найден")
    name = re.sub(r"\s+", " ", name).strip()
    old_display = display_name(sp)

    if not name:  # вернуть «Собеседник N» (и не предлагать больше угаданное имя)
        if sp.get("voice_id"):
            remove_embedding(cfg, sp["voice_id"], work.name)
        for k in ("name", "voice_id", "score", "uncertain", "auto", "manual", "guess", "guess_evidence"):
            sp.pop(k, None)
        sp["guess_rejected"] = True
    else:
        prev_voice = sp.get("voice_id")
        target = find_by_name(cfg, name) if remember else None
        # Имя исправлено: эмбеддинг этой встречи больше не должен учить неверный слепок.
        if prev_voice and (not target or target["id"] != prev_voice):
            remove_embedding(cfg, prev_voice, work.name)
        if remember:
            if not sp.get("embedding"):
                raise ValueError("Для этого собеседника нет слепка голоса: "
                                 "включите разделение собеседников и обработайте встречу заново")
            if not target:
                target = create_profile(cfg, name)
            add_embedding(cfg, target, work.name, label, sp["embedding"], sp.get("duration", 0), auto=False)
            vdir = voices_dir(cfg) / target["id"]
            if not (vdir / "sample.wav").exists() and sp.get("sample"):
                src = work / SAMPLES_DIR / sp["sample"]
                if src.exists():
                    shutil.copyfile(src, vdir / "sample.wav")
            sp.update(voice_id=target["id"], name=target["name"], uncertain=False, auto=False)
            sp.pop("manual", None)
        else:
            sp.update(name=name, uncertain=False, auto=False, manual=True)
            sp.pop("voice_id", None)
    write_speakers(work, data)
    replace_in_note(note, old_display, display_name(sp))
    return sp


def meetings_with_voice(cfg: dict, vid: str) -> list[tuple[Path, str]]:
    """(папка встречи, метка) — где этот слепок сопоставлен собеседнику."""
    root = Path(cfg["paths"]["recordings"])
    out = []
    if root.exists():
        for f in root.glob(f"*/{SPEAKERS_FILE}"):
            for label, sp in read_speakers(f.parent).get("speakers", {}).items():
                if sp.get("voice_id") == vid:
                    out.append((f.parent, label))
    return out


def note_of(work: Path) -> Path | None:
    try:
        p = Path((work / ".done").read_text().strip())
    except OSError:
        return None
    return p if p.suffix == ".md" and p.exists() else None


def update_profile(cfg: dict, vid: str, name: str | None, email: str | None, telegram: str | None) -> dict:
    p = get_profile(cfg, vid)
    if email is not None:
        email = email.strip()
        if email and not EMAIL_RE.match(email):
            raise ValueError("Похоже, в адресе почты ошибка")
        p["email"] = email
    if telegram is not None:
        telegram = telegram.strip().removeprefix("https://t.me/").removeprefix("t.me/").lstrip("@")
        if telegram and not TG_RE.match(telegram):
            raise ValueError("Ник в Telegram — латиница, цифры и _, от 4 до 32 символов")
        p["telegram"] = telegram
    if name is not None and name.strip() and name.strip() != p.get("name"):
        name = re.sub(r"\s+", " ", name).strip()
        other = find_by_name(cfg, name)
        if other and other["id"] != vid:
            raise ValueError(f"Слепок «{name}» уже есть — объедините их кнопкой «Объединить с…»")
        old = p.get("name", "")
        p["name"] = name
        _relabel_meetings(cfg, vid, old, name)
    save_profile(cfg, p)
    return p


def _relabel_meetings(cfg: dict, vid: str, old_name: str, new_name: str, new_vid: str | None = None) -> None:
    for work, label in meetings_with_voice(cfg, vid):
        data = read_speakers(work)
        sp = data["speakers"][label]
        before = display_name(sp)
        sp["name"] = new_name
        if new_vid:
            sp["voice_id"] = new_vid
        write_speakers(work, data)
        replace_in_note(note_of(work), before, display_name(sp))


def merge_profiles(cfg: dict, src_id: str, dst_id: str) -> dict:
    if src_id == dst_id:
        raise ValueError("Нельзя объединить слепок сам с собой")
    src, dst = get_profile(cfg, src_id), get_profile(cfg, dst_id)
    for e in src.get("embeddings", []):
        if not any(x["meeting"] == e["meeting"] for x in dst.get("embeddings", [])):
            dst.setdefault("embeddings", []).append(e)
    dst["embeddings"].sort(key=lambda e: e.get("added", ""))
    limit = int(cfg["voices"].get("max_embeddings", 20))
    dst["embeddings"] = dst["embeddings"][-limit:]
    for key in ("email", "telegram"):
        if not dst.get(key) and src.get(key):
            dst[key] = src[key]
    sdir, ddir = voices_dir(cfg) / src_id, voices_dir(cfg) / dst_id
    if not (ddir / "sample.wav").exists() and (sdir / "sample.wav").exists():
        shutil.copyfile(sdir / "sample.wav", ddir / "sample.wav")
    save_profile(cfg, dst)
    _relabel_meetings(cfg, src_id, src.get("name", ""), dst.get("name", ""), new_vid=dst_id)
    shutil.rmtree(sdir, ignore_errors=True)
    return dst


def delete_profile(cfg: dict, vid: str) -> None:
    get_profile(cfg, vid)
    for work, label in meetings_with_voice(cfg, vid):
        data = read_speakers(work)
        sp = data["speakers"][label]
        sp.pop("voice_id", None)
        sp.pop("auto", None)
        sp["manual"] = True  # имя во встрече остаётся, но больше не связано со слепком
        write_speakers(work, data)
    shutil.rmtree(voices_dir(cfg) / vid, ignore_errors=True)


def fragments(cfg: dict) -> list[dict]:
    """Все собеседники всех встреч, у которых есть образец голоса, — для «Заменить образец»."""
    root = Path(cfg["paths"]["recordings"])
    out = []
    if root.exists():
        for f in sorted(root.glob(f"*/{SPEAKERS_FILE}"), reverse=True):
            work = f.parent
            note = note_of(work)
            for label, sp in read_speakers(work).get("speakers", {}).items():
                if sp.get("sample") and (work / SAMPLES_DIR / sp["sample"]).exists():
                    out.append({"meeting": work.name, "note": note.name if note else None,
                                "speaker": label, "display": display_name(sp),
                                "voice_id": sp.get("voice_id"), "duration": sp.get("duration", 0)})
    return out


def set_sample(cfg: dict, vid: str, meeting: str, label: str) -> None:
    get_profile(cfg, vid)
    src = sample_path(cfg, meeting, label)
    shutil.copyfile(src, voices_dir(cfg) / vid / "sample.wav")


def sample_path(cfg: dict, meeting: str, label: str) -> Path:
    root = Path(cfg["paths"]["recordings"]).resolve()
    work = (root / meeting).resolve()
    if work.parent != root:
        raise LookupError("Встреча не найдена")
    sp = read_speakers(work).get("speakers", {}).get(label)
    if not sp or not sp.get("sample"):
        raise LookupError("У этого собеседника нет образца голоса")
    src = work / SAMPLES_DIR / Path(sp["sample"]).name
    if not src.exists():
        raise LookupError("Образец голоса удалён")
    return src


# ---------- ручное разделение собеседников ----------

SEGMENTS_FILE = "segments.json"


def _read_segments(work: Path) -> dict:
    try:
        return json.loads((work / SEGMENTS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise LookupError("У этой встречи нет сохранённой расшифровки — обработайте её заново")


def reassign(cfg: dict, work: Path, index: int, to: str, note: Path | None) -> None:
    """Реплика index сказана другим собеседником (to — метка или "new"). Слепки голоса изменённых
    собеседников пересчитаются при следующем пересоздании протокола (флаг dirty)."""
    seg_data = _read_segments(work)
    segs = seg_data["segments"]
    if not 0 <= index < len(segs):
        raise LookupError("Реплика не найдена")
    seg = segs[index]
    if not seg.get("spk"):
        raise ValueError("Это реплика с вашего микрофона — её нельзя отдать собеседнику")
    data = read_speakers(work)
    sps = data.setdefault("speakers", {})
    old = seg["spk"]
    prefix = cfg["speakers"]["others_prefix"]

    if to == "new":
        # Был один собеседник без номера — теперь их двое: «Собеседник» → «Собеседник 1».
        for sp in sps.values():
            if sp.get("base") == prefix:
                sp["base"] = f"{prefix} 1"
        nums = [int(m.group(1)) for sp in sps.values() if (m := re.search(r"(\d+)$", sp.get("base", "")))]
        n = 1
        while f"MANUAL_{n:02d}" in sps:
            n += 1
        to = f"MANUAL_{n:02d}"
        sps[to] = {"base": f"{prefix} {max(nums, default=0) + 1}", "duration": 0.0, "embedding": None,
                   "sample": None, "manual_split": True}
    elif to not in sps:
        raise LookupError("Собеседник не найден")
    if to == old:
        return

    seg["spk"] = to
    for label in (old, to):
        if label in sps:
            sps[label]["duration"] = round(sum(s["end"] - s["start"] for s in segs if s.get("spk") == label), 1)
    removed = set()
    if not any(s.get("spk") == old for s in segs):  # у собеседника не осталось реплик
        if sps.get(old, {}).get("voice_id"):
            remove_embedding(cfg, sps[old]["voice_id"], work.name)
        sps.pop(old, None)
        removed.add(old)
    data["dirty"] = sorted((set(data.get("dirty", [])) | {old, to}) - removed)
    data["protocol_stale"] = True
    data["names_guessed"] = False  # у нового собеседника имя ещё не угадывали
    write_speakers(work, data)
    osutil.atomic_write_text((work / SEGMENTS_FILE), json.dumps(seg_data, ensure_ascii=False))
    rewrite_transcript(work, note)


def edit_text(work: Path, index: int, text: str, note: Path | None) -> list[str]:
    """Исправить текст реплики (ошибка распознавания). Пустой текст — реплика удаляется."""
    seg_data = _read_segments(work)
    segs = seg_data["segments"]
    if not 0 <= index < len(segs):
        raise LookupError("Реплика не найдена")
    text = " ".join(text.split())
    old = segs[index].get("text", "")
    if text:
        segs[index]["text"] = text
        segs[index]["edited"] = True
    else:
        del segs[index]
    osutil.atomic_write_text((work / SEGMENTS_FILE), json.dumps(seg_data, ensure_ascii=False))
    rewrite_transcript(work, note)
    # Новое написание имени или термина — в словарь распознавания: в следующих встречах его услышат верно.
    from .namefix import learn_terms, remember
    try:
        return remember(learn_terms(old, text))
    except Exception:  # noqa: BLE001
        return []


def insert_segment(work: Path, index: int, seg: dict, note: Path | None) -> None:
    """Вернуть удалённую реплику на место (отмена удаления)."""
    seg_data = _read_segments(work)
    segs = seg_data["segments"]
    keep = {k: seg[k] for k in ("start", "end", "text", "speaker", "spk", "edited") if k in seg}
    segs.insert(max(0, min(index, len(segs))), keep)
    osutil.atomic_write_text((work / SEGMENTS_FILE), json.dumps(seg_data, ensure_ascii=False))
    rewrite_transcript(work, note)


def raw_segment(work: Path, index: int) -> dict:
    segs = _read_segments(work)["segments"]
    if not 0 <= index < len(segs):
        raise LookupError("Реплика не найдена")
    return dict(segs[index])


def rewrite_transcript(work: Path, note: Path | None) -> None:
    """Обновить в заметке расшифровку и участников по текущей разметке (протокол не трогаем)."""
    from . import merge

    segs = apply_names(_read_segments(work)["segments"], work)
    md = merge.to_markdown(segs)
    osutil.atomic_write_text((work / "transcript.md"), md)
    if not note or not note.exists():
        return
    text = note.read_text(encoding="utf-8")
    head, sep, _ = text.partition("\n## Расшифровка\n")
    if sep:
        text = head + sep + "\n" + md + "\n"
    participants = ", ".join(dict.fromkeys(s["speaker"] for s in segs))
    text = re.sub(r"^participants: .*$", lambda _: f"participants: {participants}", text, count=1, flags=re.M)
    osutil.atomic_write_text(note, text)


def refresh_dirty(work: Path, others16: Path, others_offset: float, cfg: dict) -> None:
    """Пересчитать слепки голоса и образцы собеседников, изменённых вручную; безымянных — попробовать узнать."""
    data = read_speakers(work)
    dirty = [label for label in data.get("dirty", []) if label in data.get("speakers", {})]
    if not dirty:
        return
    segs = _read_segments(work)["segments"]
    intervals = {label: [(s["start"] - others_offset, s["end"] - others_offset)
                         for s in segs if s.get("spk") == label] for label in dirty}
    from .diarize import refresh_speakers

    fresh = refresh_speakers(others16, intervals, cfg, work / SAMPLES_DIR)
    for label, sp in fresh.items():
        entry = data["speakers"][label]
        entry.update(duration=sp.duration, embedding=sp.embedding, sample=sp.sample or entry.get("sample"))
        if entry.get("voice_id") and entry.get("auto"):  # голос изменился — прежнее авто-совпадение недействительно
            remove_embedding(cfg, entry["voice_id"], work.name)
            for k in ("name", "voice_id", "score", "uncertain", "auto"):
                entry.pop(k, None)
        elif entry.get("voice_id") and sp.embedding:  # подтверждённое имя — обновить вклад встречи в слепок
            try:
                add_embedding(cfg, get_profile(cfg, entry["voice_id"]), work.name, label, sp.embedding,
                              sp.duration, auto=False)
            except LookupError:
                pass
    unnamed = {label: data["speakers"][label] for label in fresh
               if not data["speakers"][label].get("name") and data["speakers"][label].get("embedding")}
    if unnamed and cfg["voices"].get("enabled", True):
        taken = {sp.get("voice_id") for sp in data["speakers"].values() if sp.get("voice_id")}
        for label, m in match(unnamed, cfg, work.name).items():
            if m["voice_id"] in taken:
                continue
            data["speakers"][label].update(voice_id=m["voice_id"], name=m["name"], score=m["score"],
                                           uncertain=True, auto=True)  # после ручной правки — только с «?»
    data["dirty"] = []
    write_speakers(work, data)
