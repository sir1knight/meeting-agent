"""Распознавание во время записи: чтобы после долгой встречи ждать протокол минуты, а не четверть часа.

Отдельный процесс с низким приоритетом (сервер запускает его при начале записи; на macOS — через taskpolicy).
Каждые ~20 секунд дочитывает новый звук из mic.wav / others.wav, которые ещё пишутся,
режет по паузам и распознаёт. Результат — <встреча>/live/<дорожка>.json:
  {"until": секунды дорожки, до которых всё распознано, "segments": [{start, end, text}, …]}
Покрытие всегда — начало дорожки: всё, что после until, распознается после встречи как обычно.

Разделение собеседников по ходу встречи не делаем: pyannote надёжен только на всей записи.
Поэтому реплики собеседников режутся коротко (до 8 с, по паузам) — после встречи их раскладывают
по людям, а куски, где внутри сменился говорящий, распознают заново.
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
TICK = 20.0          # как часто дочитывать звук, секунд
TAIL_GUARD = 3.0     # последние секунды не трогаем: файл дописывается, фраза может продолжаться
TRACKS = {"mic": {"gap": 0.4, "max": 20.0}, "others": {"gap": 0.3, "max": 8.0}}
# Тяжёлые модели во время звонка не запускаем. На Windows Whisper идёт на процессоре — только GigaAM.
LIVE_ENGINES = ("gigaam",) if osutil.IS_WIN else ("gigaam", "whisper-turbo")
STOP_FILE = ".stop"  # на Windows нет SIGTERM «по-хорошему»: сервер просит остановиться этим файлом
_stop = False


def live_dir(work: Path) -> Path:
    return work / "live"


def read_result(work: Path, track: str) -> dict | None:
    try:
        return json.loads((live_dir(work) / f"{track}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


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
    def __init__(self, work: Path, name: str):
        self.name, self.path = name, work / f"{name}.wav"
        prev = read_result(work, name) or {}
        self.segments: list[dict] = prev.get("segments", [])
        self.until: float = prev.get("until", 0.0)      # секунды дорожки, распознано до
        self.frame = 0                                   # кадров исходного файла прочитано
        self.buf = None                                  # звук с self.until, 16 кГц
        self.out = live_dir(work) / f"{name}.json"
        self.asr = None

    def save(self) -> None:
        self.out.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.out.with_suffix(".tmp")
        tmp.write_text(json.dumps({"until": round(self.until, 2), "asr": self.asr, "segments": self.segments},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.out)


def _step(track: Track, cfg: dict, final: bool = False) -> None:
    import numpy as np

    from .transcribe import _chunks, speech_regions, transcribe_array

    if track.frame == 0 and track.until > 0:  # продолжение после перезапуска — пропускаем распознанное
        layout = _wav_layout(track.path)
        if layout:
            track.frame = int(track.until * layout[2])
    new, track.frame = read_new(track.path, track.frame)
    if new is not None:
        track.buf = new if track.buf is None else np.concatenate([track.buf, new])
    if track.buf is None or len(track.buf) < SR * 5:
        return
    window = len(track.buf) / SR - (0 if final else TAIL_GUARD)
    regions = [(s, e) for s, e in speech_regions(track.buf[: int(window * SR)], cfg)]
    if not final:  # фраза у края окна может продолжаться — её оставим на следующий раз
        regions = [(s, e) for s, e in regions if e < window - 0.5]
    if not regions:
        if window > 30:  # долгая тишина — сдвигаем начало, чтобы буфер не рос
            cut = window - 2
            track.buf, track.until = track.buf[int(cut * SR):], track.until + cut
            track.save()
        return
    p = TRACKS[track.name]
    chunks = [(s, e, None) for s, e in _chunks(regions, p["gap"], p["max"]) if e - s >= 0.3]
    segs = transcribe_array(track.buf, cfg, chunks, f"{track.name} (по ходу встречи)")
    for s in segs:
        s.pop("spk", None)
        s["start"] = round(s["start"] + track.until, 2)
        s["end"] = round(s["end"] + track.until, 2)
    track.segments += segs
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

    tracks = [Track(work, n) for n in TRACKS]
    for t in tracks:
        t.asr = asr_engine(cfg)
    log.info("Распознаю по ходу встречи: %s", work.name)
    paused = False
    while not _stop:
        t0 = time.time()
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
                        _step(tr, cfg)
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
