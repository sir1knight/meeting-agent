"""Звук встречи для плеера в окне: микрофон и собеседники, сведённые в один файл по общей шкале времени
(та же, что у реплик расшифровки: ноль — начало самой ранней дорожки).

Файл playback.wav (16 кГц моно, ~115 МБ на час) создаётся при первом воспроизведении и лежит в папке
записи. Его нет, если аудио после обработки удаляется (настройка «Хранить аудио»).
"""

import json
import logging
import threading
from pathlib import Path

from . import audio

log = logging.getLogger(__name__)

NAME = "playback.wav"
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


def available(work: Path) -> bool:
    return (work / NAME).exists() or bool(_tracks(work))


def ensure(work: Path) -> Path:
    """Готовый playback.wav (собирает при первом обращении; повторные запросы ждут сборку)."""
    out = work / NAME
    with _guard:
        lock = _locks.setdefault(str(work), threading.Lock())
    with lock:
        tracks = _tracks(work)
        if out.exists() and all(out.stat().st_mtime >= t.stat().st_mtime for t, _ in tracks):
            return out
        if not tracks:
            raise LookupError("Аудио этой встречи не сохранено")
        _build(work, tracks, out)
        return out


def _build(work: Path, tracks: list[tuple[Path, float]], out: Path) -> None:
    import numpy as np
    import soundfile as sf

    log.info("Готовлю звук для плеера: %s", work.name)
    temps, inputs = [], []
    try:
        for track, offset in tracks:
            tmp = work / f".play_{track.stem}.wav"
            audio.to_16k_mono(track, tmp)
            temps.append(tmp)
            inputs.append((sf.SoundFile(str(tmp)), int(round(offset * RATE))))
        total = max(f.frames + off for f, off in inputs)
        part = out.with_suffix(".part.wav")
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
        part.replace(out)
    finally:
        for f, _ in inputs:
            f.close()
        for t in temps:
            t.unlink(missing_ok=True)


def remove(work: Path) -> None:
    (work / NAME).unlink(missing_ok=True)
