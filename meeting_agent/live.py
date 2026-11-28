"""Распознавание и определение голосов во время записи: чтобы видеть живую расшифровку прямо на созвоне,
а после долгой встречи ждать протокол минуты, а не четверть часа.

Отдельный процесс с низким приоритетом (сервер запускает его при начале записи; на macOS — через taskpolicy).
Каждые ~12 секунд дочитывает новый звук из mic.wav / others.wav, которые ещё пишутся,
режет по паузам, распознаёт речь, а для реплик собеседников (если включён pyannote) извлекает
голосовые эмбеддинги, раскладывает по говорящим и сразу узнаёт коллег из базы «Голоса».
Результат — <встреча>/live/<дорожка>.json и <встреча>/live/speakers.json.
Покрытие всегда — начало дорожки: всё, что после until, досчитается после встречи как обычно.
"""

import json
import logging
import signal
import struct
import time
from pathlib import Path

from . import osutil

log = logging.getLogger(__name__)

SR = 16000
TICK = 12.0          # как часто дочитывать звук, секунд
TAIL_GUARD = 2.0     # последние секунды не трогаем: файл дописывается, фраза может продолжаться
TRACKS = {"mic": {"gap": 0.4, "max": 18.0}, "others": {"gap": 0.3, "max": 8.0}}
# Тяжёлые модели во время звонка не запускаем. На Windows Whisper идёт на процессоре — только GigaAM.
LIVE_ENGINES = ("gigaam",) if osutil.IS_WIN else ("gigaam", "whisper-turbo", "apple-speech")
REFINE_MIN_AGE = 15.0  # переслушиваем реплику через 15–35 секунд с полным контекстом фразы
REFINE_MAX_AGE = 35.0
HISTORY_MAX_SEC = 45.0
STOP_FILE = ".stop"  # на Windows нет SIGTERM «по-хорошему»: сервер просит остановиться этим файлом
LIVE_SPEAKERS_FILE = "speakers.json"
_stop = False


def live_dir(work: Path) -> Path:
    return work / "live"


def read_result(work: Path, track: str) -> dict | None:
    try:
        return json.loads((live_dir(work) / f"{track}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_live_speakers(work: Path) -> dict[str, dict]:
    try:
        return json.loads((live_dir(work) / LIVE_SPEAKERS_FILE).read_text(encoding="utf-8")).get("speakers", {})
    except (OSError, ValueError):
        return {}


def allowed(cfg: dict) -> tuple[bool, str]:
    from .models import asr_engine

    if not cfg.get("live", {}).get("enabled", True):
        return False, "выключено в настройках"
    if asr_engine(cfg) not in LIVE_ENGINES:
        return False, "выбрана тяжёлая модель распознавания — всё после встречи"
    return True, ""


def on_battery() -> bool:
    return osutil.on_battery()


def request_stop(work: Path) -> None:
    d = live_dir(work)
    d.mkdir(parents=True, exist_ok=True)
    (d / STOP_FILE).touch()


def count_segments(work: Path) -> int:
    """Быстрый подсчёт уже распознанных по ходу встречи реплик (для бейджа в сайдбаре)."""
    n = 0
    for tr in ("mic", "others"):
        r = read_result(work, tr)
        if r and isinstance(r.get("segments"), list):
            n += len(r["segments"])
    return n


def snapshot(work: Path, cfg: dict) -> dict:
    """Сводка живой расшифровки для окна приложения (/api/live)."""
    from . import models, participants, voices

    ok, reason = allowed(cfg)
    battery = on_battery()
    meta = {}
    try:
        if (work / "meta.json").exists():
            meta = json.loads((work / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    starts = [v for k, v in meta.items() if k.endswith("_start") and isinstance(v, (int, float))]
    t0 = min(starts) if starts else 0.0
    me_off = float(meta.get("mic_start", t0)) - t0 if starts else 0.0
    oth_off = float(meta.get("others_start", t0)) - t0 if starts else 0.0

    me_name = cfg["speakers"].get("me", "Я")
    oth_prefix = cfg["speakers"].get("others_prefix", "Собеседник")
    mic_res = read_result(work, "mic") or {}
    oth_res = read_result(work, "others") or {}
    live_sps = read_live_speakers(work)

    merged: list[dict] = []
    for s in mic_res.get("segments", []):
        if s.get("text", "").strip():
            merged.append({
                "start": round(max(0.0, float(s["start"]) + me_off), 2),
                "end": round(max(0.0, float(s["end"]) + me_off), 2),
                "speaker": me_name,
                "track": "mic",
                "text": s["text"].strip(),
                "refined": bool(s.get("refined")),
            })
    single_unnamed = len(live_sps) == 1 and not next(iter(live_sps.values())).get("name")
    for s in oth_res.get("segments", []):
        if not s.get("text", "").strip():
            continue
        cid = s.get("live_spk")
        sp_info = live_sps.get(cid) if cid else None
        if sp_info:
            disp = oth_prefix if single_unnamed else voices.display_name(sp_info)
        else:
            disp = s.get("speaker") or oth_prefix
        merged.append({
            "start": round(max(0.0, float(s["start"]) + oth_off), 2),
            "end": round(max(0.0, float(s["end"]) + oth_off), 2),
            "speaker": disp,
            "label": cid,
            "track": "others",
            "text": s["text"].strip(),
            "refined": bool(s.get("refined")),
        })
    merged.sort(key=lambda x: (x["start"], 0 if x["track"] == "mic" else 1))

    sp_list = []
    for cid, sp in live_sps.items():
        disp = oth_prefix if single_unnamed else voices.display_name(sp)
        sp_list.append({
            "label": cid,
            "display": disp,
            "name": sp.get("name", ""),
            "base": sp.get("base", oth_prefix),
            "uncertain": bool(sp.get("uncertain")),
            "score": sp.get("score"),
            "duration": round(float(sp.get("duration", 0)), 1),
        })

    diar_on = bool(cfg["diarization"].get("enabled", True)
                   and models.pyannote_installed() and models.is_ready("diarization", cfg))
    until = max(float(mic_res.get("until") or 0.0), float(oth_res.get("until") or 0.0))
    return {
        "active": True,
        "folder": str(work),
        "enabled": ok,
        "reason": reason,
        "on_battery": battery,
        "asr": models.asr_engine(cfg),
        "diarization": diar_on,
        "voices": bool(diar_on and cfg["voices"].get("enabled", True)),
        "until": round(until, 1),
        "expected": participants.read(work),
        "speakers": sp_list,
        "has_me": any(s["track"] == "mic" for s in merged),
        "segments": merged,
    }


# ---------- онлайн-разделение собеседников и узнавание по базе «Голоса» ----------

class LiveSpeakerTracker:
    """Извлекает голосовые эмбеддинги (WeSpeaker / pyannote) для реплик собеседников на лету,
    кластеризует голоса этой встречи (LIVE_01, LIVE_02…) и сопоставляет их с базой «Голоса»."""

    CLUSTER_SIM = 0.56     # порог отнесения новой реплики к уже звучавшему на встрече голосу
    MIN_SEC = 1.2          # минимальная длина реплики (с) для надёжного эмбеддинга

    def __init__(self, work: Path):
        self.work = work
        self.out = live_dir(work) / LIVE_SPEAKERS_FILE
        self.speakers: dict[str, dict] = read_live_speakers(work)
        self._infer = None
        self._failed = False

    def _ready(self, cfg: dict) -> bool:
        if self._failed or not cfg["diarization"].get("enabled", True):
            return False
        from . import models
        return models.pyannote_installed() and models.is_ready("diarization", cfg)

    def _embed(self, piece, cfg: dict):
        import numpy as np
        import torch

        if self._infer is None:
            from pyannote.audio import Inference, Model
            from . import models
            from .diarize import EMBEDDING_FALLBACK

            device = osutil.torch_device(cfg["diarization"].get("device"))
            model = Model.from_pretrained(models.diarization_local_path(EMBEDDING_FALLBACK), token=False)
            infer = Inference(model, window="whole")
            infer.to(device)
            self._infer = infer
            log.info("Живое распознавание голосов (WeSpeaker) подключено")
        v = np.asarray(self._infer({"waveform": torch.from_numpy(piece[None].copy()), "sample_rate": SR}),
                       dtype="float32")
        n = float(np.linalg.norm(v))
        if not np.all(np.isfinite(v)) or n <= 0:
            return None
        return v / n

    def save(self) -> None:
        self.out.parent.mkdir(parents=True, exist_ok=True)
        osutil.atomic_write_text(self.out, json.dumps({"speakers": self.speakers}, ensure_ascii=False))

    def assign(self, all_segments: list[dict], new_segs: list[dict], raw_spans: list[tuple[float, float]],
               buf, cfg: dict) -> None:
        import numpy as np
        from . import participants, voices

        prefix = cfg["speakers"].get("others_prefix", "Собеседник")
        if not self._ready(cfg):
            for s in new_segs:
                s["speaker"] = prefix
            return

        try:
            # Находим последнюю реплику с известным кластером (для коротких поддакиваний < 1.2 с).
            prev_cid = None
            prev_end = -999.0
            for old in reversed(all_segments):
                if old.get("live_spk"):
                    prev_cid, prev_end = old["live_spk"], float(old.get("end", 0.0))
                    break

            changed_clusters = False
            for seg, (s0, s1) in zip(new_segs, raw_spans):
                dur = s1 - s0
                piece = buf[int(max(0.0, s0) * SR): int(min(len(buf) / SR, s1) * SR)]
                rms = float(np.sqrt(np.mean(piece ** 2))) if len(piece) else 0.0
                v = self._embed(piece, cfg) if (dur >= self.MIN_SEC and rms >= 0.004) else None
                if v is None:
                    if prev_cid and float(seg["start"]) - prev_end <= 2.5:
                        seg["live_spk"] = prev_cid
                    prev_end = float(seg["end"])
                    continue

                best_cid, best_sim = None, -1.0
                for cid, sp in self.speakers.items():
                    emb = sp.get("embedding")
                    if emb:
                        sim = float(v @ np.asarray(emb, dtype="float32"))
                        if sim > best_sim:
                            best_cid, best_sim = cid, sim

                if best_cid and best_sim >= self.CLUSTER_SIM:
                    sp = self.speakers[best_cid]
                    old_dur = float(sp.get("duration", 1.0))
                    merged = np.asarray(sp["embedding"], dtype="float32") * min(old_dur, 60.0) + v * dur
                    norm = float(np.linalg.norm(merged)) or 1.0
                    sp["embedding"] = (merged / norm).round(6).tolist()
                    sp["duration"] = round(old_dur + dur, 1)
                    cid = best_cid
                else:
                    idx = len(self.speakers) + 1
                    cid = f"LIVE_{idx:02d}"
                    self.speakers[cid] = {
                        "base": f"{prefix} {idx}",
                        "duration": round(dur, 1),
                        "embedding": v.round(6).tolist(),
                    }
                seg["live_spk"] = cid
                prev_cid, prev_end = cid, float(seg["end"])
                changed_clusters = True

            # Сопоставляем кластеры с базой «Голоса» и текущим списком участников встречи.
            if self.speakers and cfg["voices"].get("enabled", True):
                local_cfg = dict(cfg, meeting={"participants": participants.read(self.work)})
                found = voices.match(self.speakers, local_cfg, self.work.name)
                for cid, sp in self.speakers.items():
                    for k in ("name", "voice_id", "score", "uncertain"):
                        sp.pop(k, None)
                    if cid in found:
                        sp.update(found[cid])
                changed_clusters = True

            single_unnamed = len(self.speakers) == 1 and not next(iter(self.speakers.values())).get("name")
            for s in all_segments + new_segs:
                cid = s.get("live_spk")
                sp = self.speakers.get(cid) if cid else None
                s["speaker"] = (prefix if single_unnamed else voices.display_name(sp)) if sp else prefix

            if changed_clusters:
                self.save()
        except Exception as e:  # noqa: BLE001 — сбой pyannote не должен мешать живому распознаванию текста
            log.warning("Живое разделение собеседников отключено до конца записи: %s", e)
            self._failed = True
            for s in new_segs:
                s.setdefault("speaker", prefix)


# ---------- чтение WAV, который ещё пишется ----------

def _wav_layout(path: Path) -> tuple[int, int, int, int] | None:
    """(смещение данных, каналы, частота, бит на отсчёт). Размер данных в заголовке до закрытия
    файла не заполнен, поэтому длину считаем по размеру файла."""
    try:
        with open(path, "rb") as f:
            head = f.read(65536)
    except OSError:
        return None
    if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return None
    pos, fmt = 12, None
    while pos + 8 <= len(head):
        cid, size = head[pos:pos + 4], struct.unpack("<I", head[pos + 4:pos + 8])[0]
        if cid == b"fmt ":
            _, ch, rate, _, _, bits = struct.unpack("<HHIIHH", head[pos + 8:pos + 24])
            fmt = (ch, rate, bits)
        elif cid == b"data":
            return (pos + 8, *fmt) if fmt else None
        pos += 8 + size + (size & 1)
    return None


def read_new(path: Path, from_frame: int):
    """Новые отсчёты с from_frame: (моно float32 16 кГц, новый номер кадра в исходной частоте)."""
    import numpy as np
    from scipy.signal import resample_poly

    layout = _wav_layout(path)
    if not layout:
        return None, from_frame
    offset, ch, rate, bits = layout
    width = bits // 8 * ch
    frames_now = (path.stat().st_size - offset) // width
    if frames_now <= from_frame:
        return None, from_frame
    with open(path, "rb") as f:
        f.seek(offset + from_frame * width)
        raw = f.read((frames_now - from_frame) * width)
    dtype = {16: "<i2", 32: "<f4"}.get(bits)
    if not dtype:
        return None, from_frame
    x = np.frombuffer(raw, dtype=dtype).astype("float32")
    if bits == 16:
        x /= 32768.0
    x = x.reshape(-1, ch).mean(axis=1)
    if rate != SR:
        from math import gcd
        g = gcd(SR, rate)
        x = resample_poly(x, SR // g, rate // g).astype("float32")
    return x, frames_now


# ---------- основной цикл ----------

class Track:
    def __init__(self, work: Path, name: str, tracker: LiveSpeakerTracker | None = None):
        self.work = work
        self.name, self.path = name, work / f"{name}.wav"
        prev = read_result(work, name) or {}
        self.segments: list[dict] = prev.get("segments", [])
        self.until: float = prev.get("until", 0.0)      # секунды дорожки, распознано до
        self.frame = 0                                   # кадров исходного файла прочитано
        self.buf = None                                  # звук с self.until, 16 кГц
        self.history_buf = None                          # кольцевой буфер последних ~45 с для переслушивания
        self.history_start: float = self.until
        self.out = live_dir(work) / f"{name}.json"
        self.asr = None
        self.tracker = tracker

    def append_history(self, new) -> None:
        import numpy as np

        if new is None or len(new) == 0:
            return
        if self.history_buf is None:
            self.history_buf = new.copy()
            self.history_start = self.until
        else:
            self.history_buf = np.concatenate([self.history_buf, new])
        max_samples = int(HISTORY_MAX_SEC * SR)
        if len(self.history_buf) > max_samples:
            drop = len(self.history_buf) - max_samples
            self.history_buf = self.history_buf[drop:]
            self.history_start += drop / SR

    def save(self) -> None:
        self.out.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.out.with_suffix(".tmp")
        tmp.write_text(json.dumps({"until": round(self.until, 2), "asr": self.asr, "segments": self.segments},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.out)


def _refine_delayed(track: Track, cfg: dict) -> bool:
    """Асинхронное переслушивание реплик, сказанных 15–35 секунд назад, с более широким контекстом:
    убирает обрывы краёв и опечатки первичного потокового распознавания."""
    from .transcribe import transcribe_array

    if track.history_buf is None or len(track.history_buf) < SR * 2:
        return False
    now_pos = track.history_start + len(track.history_buf) / SR
    changed = False
    refined_this_tick = 0
    for seg in track.segments:
        if refined_this_tick >= 2:
            break
        if seg.get("refined"):
            continue
        s0, s1 = float(seg.get("start", 0.0)), float(seg.get("end", 0.0))
        age = now_pos - s1
        if age < REFINE_MIN_AGE or age > REFINE_MAX_AGE:
            continue
        pad_s = max(track.history_start, s0 - 0.35)
        pad_e = min(now_pos, s1 + 0.35)
        if pad_e - pad_s < 0.8 or s0 < track.history_start:
            seg["refined"] = True
            continue
        i0 = int((pad_s - track.history_start) * SR)
        i1 = int((pad_e - track.history_start) * SR)
        piece = track.history_buf[i0:i1]
        if len(piece) < SR // 2:
            seg["refined"] = True
            continue
        try:
            res = transcribe_array(piece, cfg, [(0.0, len(piece) / SR, None)], f"{track.name} (уточнение)")
            new_text = " ".join(x.get("text", "").strip() for x in res).strip()
            old_text = (seg.get("text") or "").strip()
            if new_text and len(new_text.split()) >= max(1, int(len(old_text.split()) * 0.65)):
                if new_text != old_text:
                    seg["text"] = new_text
                seg["refined"] = True
                changed = True
                refined_this_tick += 1
        except Exception:  # noqa: BLE001
            seg["refined"] = True
    return changed


def _step(track: Track, cfg: dict, final: bool = False) -> None:
    import numpy as np

    from . import participants
    from .transcribe import _chunks, speech_regions, transcribe_array

    if track.frame == 0 and track.until > 0:  # продолжение после перезапуска — пропускаем распознанное
        layout = _wav_layout(track.path)
        if layout:
            track.frame = int(track.until * layout[2])
    new, track.frame = read_new(track.path, track.frame)
    if new is not None:
        track.buf = new if track.buf is None else np.concatenate([track.buf, new])
        track.append_history(new)
    if track.buf is None or len(track.buf) < int(SR * 3.5):
        return
    window = len(track.buf) / SR - (0 if final else TAIL_GUARD)
    if window <= 0.5:
        return
    regions = [(s, e) for s, e in speech_regions(track.buf[: int(window * SR)], cfg)]
    if not final:  # фраза у края окна может продолжаться — её оставим на следующий раз
        regions = [(s, e) for s, e in regions if e < window - 0.4]
    cfg = dict(cfg, meeting={"participants": participants.read(track.work)})
    if not regions:
        did_refine = _refine_delayed(track, cfg)
        if window > 25:  # долгая тишина — сдвигаем начало, чтобы буфер не рос
            cut = window - 2
            track.buf, track.until = track.buf[int(cut * SR):], track.until + cut
            track.save()
        elif did_refine:
            track.save()
        return
    p = TRACKS[track.name]
    chunks = [(s, e, None) for s, e in _chunks(regions, p["gap"], p["max"]) if e - s >= 0.3]
    segs = transcribe_array(track.buf, cfg, chunks, f"{track.name} (по ходу встречи)")
    raw_spans = [(float(s["start"]), float(s["end"])) for s in segs]
    me_label = cfg["speakers"].get("me", "Я")
    for s in segs:
        s.pop("spk", None)
        s["start"] = round(s["start"] + track.until, 2)
        s["end"] = round(s["end"] + track.until, 2)
        if track.name == "mic":
            s["speaker"] = me_label
    if track.name == "others" and track.tracker is not None and segs:
        track.tracker.assign(track.segments, segs, raw_spans, track.buf, cfg)
    track.segments += segs
    _refine_delayed(track, cfg)
    cut = regions[-1][1] if not final else window
    track.buf, track.until = track.buf[int(cut * SR):], track.until + cut
    track.save()


def _on_term(*_) -> None:
    global _stop
    _stop = True


def run(work: Path, cfg: dict) -> None:
    """Пока идёт запись: дочитывать и распознавать. SIGTERM — доделать текущий шаг и выйти."""
    ok, why = allowed(cfg)
    if not ok:
        log.info("Распознавание во время записи не запускаю: %s", why)
        return
    osutil.lower_priority()
    signal.signal(signal.SIGTERM, _on_term)
    stop_file = live_dir(work) / STOP_FILE
    stop_file.unlink(missing_ok=True)
    from .models import asr_engine

    tracker = LiveSpeakerTracker(work)
    tracks = [Track(work, n, tracker if n == "others" else None) for n in TRACKS]
    for t in tracks:
        t.asr = asr_engine(cfg)
    log.info("Распознаю по ходу встречи: %s", work.name)
    paused = False
    while not _stop:
        t0 = time.time()
        fresh_cfg = cfg
        try:
            from . import config as _cfg_mod
            fresh_cfg = _cfg_mod.load()
        except Exception:  # noqa: BLE001
            pass
        if on_battery():
            if not paused:
                log.info("Компьютер работает от батареи — распознавание по ходу встречи на паузе")
                paused = True
        else:
            if paused:
                log.info("Питание от сети — продолжаю распознавание по ходу встречи")
                paused = False
            for tr in tracks:
                if _stop:
                    break
                if tr.path.exists():
                    try:
                        _step(tr, fresh_cfg)
                    except Exception as e:  # не страшно: досчитается после встречи
                        log.warning("Не удалось распознать кусок %s: %s", tr.name, e)
        while not _stop and time.time() - t0 < TICK:
            time.sleep(0.5)
            if stop_file.exists():
                _on_term()
    for tr in tracks:
        tr.save()
    log.info("Распознавание по ходу встречи остановлено: %s",
             ", ".join(f"{t.name} до {int(t.until) // 60}:{int(t.until) % 60:02d}" for t in tracks))
