"""Обработка одной встречи: аудио → расшифровка → спикеры → саммари → заметка."""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from . import audio, merge, osutil, participants, voices
from .summarize import summarize
from .transcribe import transcribe

log = logging.getLogger(__name__)
DONE_MARK = ".done"
PROTOCOL_ERROR = "protocol_error.txt"   # протокол не составлен (например, нет интернета) — причина
AUDIO_EXT = {".wav", ".m4a", ".mp3", ".mp4", ".aac", ".ogg", ".opus", ".flac", ".webm", ".mov"}


def notify(cfg: dict, title: str, message: str) -> None:
    if cfg["output"].get("notify"):
        osutil.notify(title, message)


def safe_name(text: str) -> str:
    text = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", text).strip()
    return re.sub(r"\s+", " ", text)[:80] or "Встреча"


def stage(name: str) -> None:
    """Метка этапа для окна приложения (сервер читает её из лога процесса)."""
    log.info("@stage %s", name)


def find_track(folder: Path, name: str) -> Path | None:
    """mic.wav / others.m4a / … — дорожка в папке встречи в любом поддерживаемом формате."""
    for f in sorted(folder.glob(f"{name}.*")):
        if f.suffix.lower() in AUDIO_EXT:
            return f
    return None


def process(path: Path, cfg: dict, template: str | None = None, retranscribe: bool = False) -> Path:
    """Обрабатывает папку встречи (mic.* и/или others.*) или отдельный аудиофайл.

    Если встреча уже расшифрована (segments.json), по умолчанию пересоздаётся только протокол:
    так смена шаблона или модели занимает минуту, а не всю обработку заново.
    """
    path = path.expanduser().resolve()
    template = template or cfg["output"]["template"]
    if path.is_dir():
        work, mic, others = path, find_track(path, "mic"), find_track(path, "others")
        meta = json.loads((path / "meta.json").read_text()) if (path / "meta.json").exists() else {}
        if meta.get("started"):
            started = datetime.fromtimestamp(meta["started"])
        elif any(k.endswith("_start") for k in meta):
            started = datetime.fromtimestamp(min(v for k, v in meta.items() if k.endswith("_start")))
        else:
            started = _date_from_name(path)
    elif path.suffix.lower() in AUDIO_EXT:  # любой аудиофайл, например диктофонная запись
        work = path.parent / f"{path.stem}_meeting"
        work.mkdir(exist_ok=True)
        mic, others, meta = None, path, {}
        started = datetime.fromtimestamp(path.stat().st_mtime)
    else:
        raise ValueError(f"Не понимаю, что обрабатывать: {path}")

    cfg["meeting"] = {"participants": participants.read(work), "fresh": retranscribe,
                      "calendar": participants.read_calendar(work)}
    for track in (mic, others):
        if track and track.suffix.lower() == ".wav" and audio.repair_wav(track):
            log.info("Починил заголовок %s: запись не была закрыта", track.name)
    if cfg["meeting"]["participants"]:
        log.info("Участники по списку: %s", ", ".join(cfg["meeting"]["participants"]))

    cache = work / "segments.json"
    if cache.exists() and not retranscribe:
        log.info("Расшифровка уже есть — пересоздаю только протокол")
        cached = json.loads(cache.read_text(encoding="utf-8"))
        segments, total = cached["segments"], cached["duration"]
        if voices.read_speakers(work).get("dirty") and others and others.exists():
            # Собеседников разделяли вручную — пересчитать их слепки голоса и образцы.
            stage("voices")
            t0 = min([v for k, v in meta.items() if k.endswith("_start")] or [0])
            oth16 = audio.to_16k_mono(others, work / "others_16k.wav")
            try:
                voices.refresh_dirty(work, oth16, meta.get("others_start", t0) - t0, cfg)
            except Exception as e:
                log.warning("Не удалось пересчитать слепки голоса: %s", e)
            finally:
                oth16.unlink(missing_ok=True)
        segments = voices.apply_names(segments, work)
    else:
        segments, total = _transcribe_all(work, mic, others, meta, cfg)
        cache.write_text(json.dumps({"segments": segments, "duration": total,
                                     "asr": cfg["whisper"].get("engine", "whisper-turbo")}, ensure_ascii=False),
                         encoding="utf-8")
    return _write_note(path, work, segments, total, started, cfg, template)


def _transcribe_all(work: Path, mic: Path | None, others: Path | None, meta: dict,
                    cfg: dict) -> tuple[list[dict], float]:
    spk = cfg["speakers"]
    t0 = min([v for k, v in meta.items() if k.endswith("_start")] or [0])
    mine: list[dict] = []
    theirs: list[dict] = []
    total = 0.0
    stage("transcribe")

    mic16 = audio.to_16k_mono(mic, work / "mic_16k.wav") if mic and mic.exists() else None
    oth16 = audio.to_16k_mono(others, work / "others_16k.wav") if others and others.exists() else None

    # Офлайн-подавление эха (WebRTC AEC3 + GCC-PHAT): если при записи не работало Voice Processing macOS.
    mic_clean: Path | None = None
    if (mic16 and oth16 and cfg.get("recording", {}).get("echo_cancel", True)
            and not meta.get("echo_cancel") and not audio.is_silent(mic16) and not audio.is_silent(oth16)):
        from . import echo
        if echo.available():
            try:
                mic_clean = echo.cancel(
                    mic16, oth16,
                    mic_offset=meta.get("mic_start", t0) - t0,
                    others_offset=meta.get("others_start", t0) - t0,
                    out=work / "mic_clean_16k.wav",
                    muted_intervals=meta.get("speaker_muted"),
                )
            except Exception as e:  # noqa: BLE001 — улучшение, не повод ронять обработку
                log.warning("Подавление эха после встречи не удалось: %s", e)

    if mic16:
        mic_use = mic_clean or mic16
        total = max(total, audio.duration(mic_use))
        if audio.is_silent(mic_use):
            log.info("Микрофон молчал — пропускаю")
        else:
            raw_mine = transcribe(mic_use, cfg) if mic_clean else _with_live(work, "mic", mic_use, cfg)
            mine = [dict(s, speaker=spk["me"]) for s in raw_mine]
            _recheck_names(mine, mic_use, cfg, work)

    if oth16:
        total = max(total, audio.duration(oth16))
        if not audio.is_silent(oth16):
            # Сначала — кто когда говорил, потом распознаём каждую реплику отдельно:
            # так в один кусок не попадают два человека.
            speakers = {}
            from .models import is_ready
            if cfg["diarization"].get("enabled") and not is_ready("diarization", cfg):
                log.info("Модель разделения собеседников не скачана — все собеседники без номеров")
            elif cfg["diarization"].get("enabled"):
                stage("diarize")
                try:
                    from .diarize import diarize
                    free_memory()  # модель распознавания микрофона больше не нужна
                    _, speakers = diarize(oth16, cfg, samples_dir=work / voices.SAMPLES_DIR)
                except ImportError:
                    log.info("pyannote не установлен — собеседники без разделения по голосам")
                except Exception as e:  # не роняем всю обработку из-за спикеров
                    log.warning("Не удалось разделить собеседников: %s", e)
            stage("transcribe")
            if speakers:
                from .transcribe import regions_from_speakers
                theirs = _others_by_turns(work, oth16, speakers, regions_from_speakers(speakers), cfg)
                if cfg["diarization"].get("refine", True):
                    _refine(oth16, theirs, speakers, cfg, work)
                base = base_names(theirs, speakers, spk["others_prefix"])
                for s in theirs:
                    s["speaker"] = base.get(s["spk"], spk["others_prefix"])
                stage("voices")
                voices.identify(work, speakers, base, cfg)
                voices.apply_names(theirs, work)
            else:
                theirs = _with_live(work, "others", oth16, cfg)
                for s in theirs:
                    s["speaker"] = spk["others_prefix"]

    if theirs and others and (work / "others_16k.wav").exists():
        _recheck_names(theirs, work / "others_16k.wav", cfg, work)
    segments = merge.merge_tracks(
        mine, theirs,
        me_offset=meta.get("mic_start", t0) - t0,
        others_offset=meta.get("others_start", t0) - t0,
    )
    for f in ("mic_16k.wav", "mic_clean_16k.wav", "others_16k.wav"):
        (work / f).unlink(missing_ok=True)
    return segments, total


def _recheck_names(segs: list[dict], wav16: Path, cfg: dict, work: Path) -> None:
    """GigaAM путает похожие имена («Илья» вместо «Ильяс») — фразы с ними перепроверяет Whisper с подсказкой."""
    from . import namefix

    try:
        n = namefix.recheck(segs, wav16, cfg, work)
        if n:
            log.info("Имена: исправлено %d", n)
    except Exception as e:  # noqa: BLE001 — улучшение, не повод ронять обработку
        log.warning("Перепроверка имён не удалась: %s", e)


# ---------- распознанное по ходу встречи ----------

def _live(work: Path, track: str, cfg: dict) -> dict | None:
    """Результат распознавания по ходу встречи — если он тем же движком и не просили расшифровать заново."""
    from .live import read_result
    from .models import asr_engine

    if cfg.get("meeting", {}).get("fresh"):
        return None
    r = read_result(work, track)
    if not r or r.get("until", 0) < 1 or r.get("asr") != asr_engine(cfg):
        return None
    return r


def _with_live(work: Path, track: str, wav16: Path, cfg: dict) -> list[dict]:
    """Готовое из распознавания по ходу встречи + только хвост, который оно не успело."""
    from .transcribe import transcribe_array

    live = _live(work, track, cfg)
    if not live:
        return transcribe(wav16, cfg)
    wav = audio.load(wav16)
    t = float(live["until"])
    log.info("%s: по ходу встречи распознано %d из %d мин", track, t // 60, len(wav) / 16000 // 60)
    tail = []
    if len(wav) - int(t * 16000) > 8000:
        tail = transcribe_array(wav[int(t * 16000):], cfg, None, f"{track} (хвост)")
        for s in tail:
            s["start"], s["end"] = round(s["start"] + t, 2), round(s["end"] + t, 2)
    return [dict(s) for s in live["segments"]] + tail


def _others_by_turns(work: Path, oth16: Path, speakers: dict, regions: list, cfg: dict) -> list[dict]:
    """Реплики собеседников по людям. Распознанное по ходу встречи раскладываем по разметке pyannote;
    заново распознаём только куски, где внутри сменился говорящий, и то, что после live.until."""
    live = _live(work, "others", cfg)
    if not live:
        return transcribe(oth16, cfg, regions)
    out, redo = [], []
    for s in live["segments"]:
        over: dict[str, float] = {}
        for label, sp in speakers.items():
            o = sum(max(0.0, min(s["end"], b) - max(s["start"], a)) for a, b in sp.segments)
            if o > 0:
                over[label] = o
        if not over:
            continue  # pyannote не считает это речью — скорее всего шум
        ranked = sorted(over.items(), key=lambda x: x[1], reverse=True)
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        if second > 0.6 and second > 0.25 * (s["end"] - s["start"]):
            redo.append((s["start"], s["end"]))  # два человека в одном куске — распознаем по репликам
        else:
            out.append(dict(s, spk=ranked[0][0]))
    spans = redo + [(float(live["until"]), float("inf"))]
    todo = [(max(s, a), min(e, b), label) for s, e, label in regions for a, b in spans
            if min(e, b) - max(s, a) >= 0.3]
    log.info("Собеседники: по ходу встречи — %d реплик, заново — %d кусков", len(out), len(todo))
    if todo:
        out += transcribe(oth16, cfg, todo)
    return sorted(out, key=lambda s: s["start"])


def _refine(oth16: Path, theirs: list[dict], speakers: dict, cfg: dict, work: Path) -> None:
    """Реплики, явно похожие по голосу на другого собеседника, — к нему; затронутым — новые слепки и образцы."""
    stage("refine")
    try:
        from .diarize import refine, refresh_speakers
        changed = refine(oth16, theirs, speakers, cfg)
        if not changed:
            return
        intervals = {k: [(s["start"], s["end"]) for s in theirs if s.get("spk") == k] for k in changed}
        for k, segs in intervals.items():
            if not segs and k in speakers and speakers[k].duration < 60:
                speakers.pop(k)  # все реплики разошлись по другим собеседникам
        intervals = {k: v for k, v in intervals.items() if v and k in speakers}
        for k, sp in refresh_speakers(oth16, intervals, cfg, work / voices.SAMPLES_DIR).items():
            speakers[k].embedding, speakers[k].duration = sp.embedding or speakers[k].embedding, sp.duration
            speakers[k].sample = sp.sample or speakers[k].sample
    except Exception as e:  # уточнение — улучшение, не повод ронять обработку
        log.warning("Не удалось уточнить разметку собеседников: %s", e)


def base_names(theirs: list[dict], speakers: dict, prefix: str) -> dict[str, str]:
    """«Собеседник N» по порядку первой реплики; кто говорил, но без слов, — в конце.
    Один собеседник — без номера."""
    first: dict[str, float] = {}
    for s in theirs:
        first.setdefault(s["spk"], s["start"])
    labels = sorted(first, key=first.get) + [k for k in speakers if k not in first]
    if len(labels) == 1:
        return {labels[0]: prefix}
    return {label: f"{prefix} {i}" for i, label in enumerate(labels, 1)}


def make_chapters(work: Path, segments: list[dict], cfg: dict, summary: str = "") -> None:
    """Главы и темы встречи — той же моделью, что писала протокол; не вышло — не беда, встреча и так готова."""
    from . import chapters
    from .summarize import chat, chunk_size

    stage("chapters")
    log.info("Главы и темы…")
    try:
        data = chapters.build(segments, lambda p: chat(cfg, p, chapters.SYSTEM), min(chunk_size(cfg), 60000), summary)
        chapters.save(work, data)
    except Exception as e:  # noqa: BLE001
        log.warning("Главы не составлены: %s", e)


def _write_note(path: Path, work: Path, segments: list[dict], total: float, started: datetime,
                cfg: dict, template: str) -> Path:
    osutil.atomic_write_text((work / "transcript.md"), merge.to_markdown(segments))
    if not segments:
        log.warning("Речь не распознана: %s", path)
        notify(cfg, "Meeting Agent", "В записи не найдено речи")
        (work / DONE_MARK).write_text("empty")
        return work / "transcript.md"

    if (cfg["names"].get("guess", True) and (work / voices.SPEAKERS_FILE).exists()
            and not voices.read_speakers(work).get("names_guessed")):
        stage("names")
        from . import names
        if names.guess(work, segments, cfg):
            segments = voices.apply_names(segments, work)
            osutil.atomic_write_text((work / "transcript.md"), merge.to_markdown(segments))

    free_memory()
    stage("summarize")
    error_file = work / PROTOCOL_ERROR
    previous = _previous_note(work)
    try:
        title, summary = summarize(merge.to_text(segments), cfg, template)
        error_file.unlink(missing_ok=True)
    except Exception as e:
        # Расшифровка уже готова и сохранена (segments.json) — повтор составит только протокол.
        error_file.write_text(str(e), encoding="utf-8")
        if previous:  # пересоздание не удалось — прежний протокол оставляем как есть
            raise
        log.warning("Протокол не составлен: %s", e)
        notify(cfg, "Протокол не составлен", "Расшифровка сохранена — составьте протокол позже")
        title = f"Встреча {started:%d.%m %H:%M}"
        summary = f"> Протокол не составлен: {e}"
    if not error_file.exists():
        make_chapters(work, segments, cfg, summary)
    from .gpu import stop_server

    stop_server()  # Qwen на видеокарте (Windows) больше не нужен — освобождаем видеопамять
    failed = error_file.exists()
    participants = list(dict.fromkeys(s["speaker"] for s in segments))
    note = "\n".join([
        "---",
        f"date: {started:%Y-%m-%d %H:%M}",
        f"duration: {round(total / 60)} мин",
        f"participants: {', '.join(participants)}",
        f"template: {template}",
        f"llm: {cfg['llm'].get('backend', 'local')}",
        f"asr: {asr_used(work) or cfg['whisper'].get('engine', 'whisper-turbo')}",
        f"source: {work}",
        "---",
        "",
        f"# {title}",
        "",
        summary,
        "",
        "---",
        "",
        "## Расшифровка",
        "",
        merge.to_markdown(segments),
        "",
    ])

    notes_dir: Path = cfg["paths"]["notes"]
    notes_dir.mkdir(parents=True, exist_ok=True)
    out = notes_dir / f"{started:%Y-%m-%d %H-%M} — {safe_name(title)}.md"
    n = 2
    while out.exists() and out != previous:  # две разные встречи с одинаковым названием
        out = notes_dir / f"{started:%Y-%m-%d %H-%M} — {safe_name(title)} ({n}).md"
        n += 1
    osutil.atomic_write_text(out, note)
    sp_data = voices.read_speakers(work)
    if sp_data.get("protocol_stale"):  # протокол снова соответствует разметке собеседников
        sp_data.pop("protocol_stale")
        voices.write_speakers(work, sp_data)
    if previous and previous != out:  # протокол пересоздан — старый больше не нужен
        previous.unlink(missing_ok=True)
    (work / DONE_MARK).write_text(str(out))

    if failed:
        log.info("Заметка с расшифровкой сохранена, протокол — позже: %s", out)
        return out

    if not cfg["output"].get("keep_audio") and path.is_dir():
        for name in ("mic", "others"):
            track = find_track(work, name)
            if track:
                track.unlink()
        (work / "playback.wav").unlink(missing_ok=True)
        (work / "playback.m4a").unlink(missing_ok=True)
    elif cfg["output"].get("keep_audio") and cfg["output"].get("compress_audio", True) and path.is_dir():
        for name in ("mic", "others"):
            wav = work / f"{name}.wav"
            m4a = work / f"{name}.m4a"
            if wav.exists() and not m4a.exists():
                if audio.compress_to_m4a(wav, m4a, bitrate=48000):
                    wav.unlink(missing_ok=True)
                    log.info("Архив аудио: %s → %s (%.1f МБ)", wav.name, m4a.name, m4a.stat().st_size / 1e6)

    log.info("Заметка готова: %s", out)
    notify(cfg, "Заметка по встрече готова", title)
    return out


def asr_used(work: Path) -> str | None:
    """Каким движком расшифрована встреча (из segments.json)."""
    try:
        return json.loads((work / "segments.json").read_text(encoding="utf-8")).get("asr")
    except (OSError, ValueError):
        return None


def _previous_note(work: Path) -> Path | None:
    try:
        prev = Path((work / DONE_MARK).read_text().strip())
    except OSError:
        return None
    return prev if prev.suffix == ".md" and prev.exists() else None


def free_memory() -> None:
    """Выгружает модель распознавания перед следующими этапами: на 16 ГБ всё сразу держать незачем."""
    from .transcribe import free_gigaam, free_whisper

    free_gigaam()
    free_whisper()


def _date_from_name(path: Path) -> datetime:
    try:
        return datetime.strptime(path.name[:19], "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return datetime.fromtimestamp(path.stat().st_mtime)


def pending(cfg: dict) -> list[Path]:
    root: Path = cfg["paths"]["recordings"]
    if not root.exists():
        return []
    from . import recovery

    out = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or (d / DONE_MARK).exists():
            continue
        if recovery.is_live(d):
            continue  # запись идёт прямо сейчас (сервер перезапустился посреди встречи) — не трогаем
        if recovery.is_interrupted(d):
            if recovery.recover(d):
                notify(cfg, "Meeting Agent", f"Восстановлена запись {d.name}, прерванная сбоем — обрабатываю")
        if find_track(d, "mic") or find_track(d, "others"):
            out.append(d)
    return out
