"""Звук встречи для плеера в окне: микрофон и собеседники, сведённые в один файл по общей шкале времени
(та же, что у реплик расшифровки: ноль — начало самой ранней дорожки).

На Mac файл сохраняется в компактном playback.m4a (~28 МБ на час, AAC 64 кбит/с через встроенный afconvert);
если сжатие недоступно (например, Windows без ffmpeg) — в playback.wav (16 кГц моно, ~115 МБ на час).
Создаётся при первом воспроизведении; отсутствует, если аудио после обработки удаляется.
"""

import json
import logging
import threading
from pathlib import Path

from . import audio

log = logging.getLogger(__name__)

NAME_M4A = "playback.m4a"
NAME_WAV = "playback.wav"
RATE = 16000
_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def _tracks(work: Path) -> list[tuple[Path, float]]:
    """(дорожка, сдвиг в секундах) для mic и others."""
    from .pipeline import find_track

    meta = {}
    if (work / "meta.json").exists():
        try:
            meta = json.loads((work / "meta.json").read_text())
        except ValueError:
            pass
    t0 = min([v for k, v in meta.items() if k.endswith("_start")] or [0])
    out = []
    for name in ("mic", "others"):
        track = find_track(work, name)
        if track:
            out.append((track, max(0.0, meta.get(f"{name}_start", t0) - t0)))
    return out


def _existing(work: Path) -> Path | None:
    for name in (NAME_M4A, NAME_WAV):
        p = work / name
        if p.exists():
            return p
    return None


def available(work: Path) -> bool:
    return _existing(work) is not None or bool(_tracks(work))


def ensure(work: Path) -> Path:
    """Готовый playback.m4a / playback.wav (собирает при первом обращении; повторные запросы ждут сборку)."""
    with _guard:
        lock = _locks.setdefault(str(work), threading.Lock())
    with lock:
        tracks = _tracks(work)
        ex = _existing(work)
        if ex and all(ex.stat().st_mtime >= t.stat().st_mtime for t, _ in tracks):
            return ex
        if not tracks:
            raise LookupError("Аудио этой встречи не сохранено")
        return _build(work, tracks)


def _build(work: Path, tracks: list[tuple[Path, float]]) -> Path:
    import numpy as np
    import soundfile as sf

    log.info("Готовлю звук для плеера: %s", work.name)
    temps, inputs = [], []
    wav_out = work / NAME_WAV
    m4a_out = work / NAME_M4A
    part = work / ".playback.part.wav"
    try:
        for track, offset in tracks:
            tmp = work / f".play_{track.stem}.wav"
            audio.to_16k_mono(track, tmp)
            temps.append(tmp)
            inputs.append((sf.SoundFile(str(tmp)), int(round(offset * RATE))))
        total = max(f.frames + off for f, off in inputs)
        block = RATE * 30
        with sf.SoundFile(str(part), "w", RATE, 1, "PCM_16") as dst:
            for pos in range(0, total, block):
                n = min(block, total - pos)
                mix = np.zeros(n, dtype=np.float32)
                for f, off in inputs:
                    a, b = max(pos, off), min(pos + n, off + f.frames)  # пересечение блока с дорожкой
                    if b <= a:
                        continue
                    f.seek(a - off)
                    data = f.read(b - a, dtype="float32", always_2d=True).mean(axis=1)
                    mix[a - pos:a - pos + len(data)] += data
                dst.write(np.clip(mix, -1, 1))
        if audio.compress_to_m4a(part, m4a_out, bitrate=64000):
            part.unlink(missing_ok=True)
            wav_out.unlink(missing_ok=True)
            return m4a_out
        part.replace(wav_out)
        m4a_out.unlink(missing_ok=True)
        return wav_out
    finally:
        for f, _ in inputs:
            f.close()
        for t in temps:
            t.unlink(missing_ok=True)
        part.unlink(missing_ok=True)


def remove(work: Path) -> None:
    (work / NAME_M4A).unlink(missing_ok=True)
    (work / NAME_WAV).unlink(missing_ok=True)
