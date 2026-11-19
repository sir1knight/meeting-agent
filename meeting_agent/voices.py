"""Слепки голоса: запоминание собеседников по голосу между встречами.

Хранение (только на этом Mac):
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


def match(speakers: dict[str, dict], cfg: dict, meeting: str) -> dict[str, dict]:
    """{метка: {voice_id, name, score, uncertain}} — жадно по убыванию сходства,
    один слепок — не больше одного собеседника встречи."""
    v = cfg["voices"]
    hi, lo = float(v["match_threshold"]), float(v["uncertain_threshold"])
    min_speech = float(v.get("min_speech_seconds", 10))
    bonus = float(v.get("participant_bonus", 0.10))
    from .participants import of
    listed = {_norm(n) for n in of(cfg)}
    profiles = [p for p in load_profiles(cfg) if p.get("embeddings")]
    pairs = []
    for label, sp in speakers.items():
        if not sp.get("embedding"):
            continue
        for p in profiles:
            s = score(p, sp["embedding"], v.get("method", "max"), exclude_meeting=meeting)
            # Человек есть в списке участников — ему порог ниже на bonus.
            b = bonus if _norm(p.get("name", "")) in listed else 0.0
            if s + b >= lo:
                pairs.append((s + b, s, label, p))
    pairs.sort(key=lambda x: x[0], reverse=True)
    used_labels, used_voices, out = set(), set(), {}
    for eff, s, label, p in pairs:
        if label in used_labels or p["id"] in used_voices:
            continue
        used_labels.add(label)
        used_voices.add(p["id"])
        # Мало речи — эмбеддинг ненадёжен: даже высокое сходство считаем «серой зоной».
        sure = eff >= hi and speakers[label].get("duration", 0) >= min_speech
        out[label] = {"voice_id": p["id"], "name": p.get("name", ""), "score": round(s, 3),
                      "uncertain": not sure}
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
        for label, m in found.items():
            sp = data["speakers"][label]
            sp.update(voice_id=m["voice_id"], name=m["name"], score=m["score"],
                      uncertain=m["uncertain"], auto=True)
            if not m["uncertain"]:  # уверенно узнали — слепок пополняется этой встречей
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
    note.write_text(text, encoding="utf-8")


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
    (work / SEGMENTS_FILE).write_text(json.dumps(seg_data, ensure_ascii=False), encoding="utf-8")
    rewrite_transcript(work, note)


def rewrite_transcript(work: Path, note: Path | None) -> None:
    """Обновить в заметке расшифровку и участников по текущей разметке (протокол не трогаем)."""
    from . import merge

    segs = apply_names(_read_segments(work)["segments"], work)
    md = merge.to_markdown(segs)
    (work / "transcript.md").write_text(md, encoding="utf-8")
    if not note or not note.exists():
        return
    text = note.read_text(encoding="utf-8")
    head, sep, _ = text.partition("\n## Расшифровка\n")
    if sep:
        text = head + sep + "\n" + md + "\n"
    participants = ", ".join(dict.fromkeys(s["speaker"] for s in segs))
    text = re.sub(r"^participants: .*$", lambda _: f"participants: {participants}", text, count=1, flags=re.M)
    note.write_text(text, encoding="utf-8")


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
