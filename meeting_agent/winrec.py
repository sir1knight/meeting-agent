"""Запись встречи на Windows: микрофон и звук компьютера (собеседники в Zoom) — в два WAV, как на Mac:
mic.wav, others.wav и meta.json со временем начала каждой дорожки.

Звук собеседников берётся «петлёй» WASAPI с устройства вывода по умолчанию: это весь звук компьютера,
не только Zoom (уведомления и видео в браузере тоже попадут в запись). Пока в системе тишина, Windows
не присылает петле ничего, поэтому параллельно в то же устройство играет беззвучный поток, а пропуски
дополнительно заполняются нулями по часам — чтобы время в файле совпадало с реальным и с микрофоном.
"""

import json
import logging
import struct
import threading
import time
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

RATE = 48000
BLOCK = RATE // 10   # по 0,1 с


class WavWriter:
    """WAV 16 бит моно. Заголовок с нулевой длиной дописывается при закрытии — как у записи на Mac,
    поэтому распознавание по ходу встречи читает растущий файл тем же кодом (live.py)."""

    def __init__(self, path: Path, rate: int):
        self.f = open(path, "wb")
        self.rate = rate
        self.frames = 0
        self.lock = threading.Lock()
        fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
        self.f.write(b"RIFF" + struct.pack("<I", 0) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt
                     + b"data" + struct.pack("<I", 0))

    def write(self, mono: np.ndarray) -> None:
        pcm = (np.clip(mono, -1, 1) * 32767).astype("<i2").tobytes()
        with self.lock:
            if self.f.closed:
                return
            self.f.write(pcm)
            self.f.flush()
            self.frames += len(mono)

    def silence(self, frames: int) -> None:
        while frames > 0:
            n = min(frames, RATE)
            self.write(np.zeros(n, dtype=np.float32))
            frames -= n

    def close(self) -> None:
        with self.lock:
            if self.f.closed:
                return
            data = self.frames * 2
            self.f.seek(4)
            self.f.write(struct.pack("<I", 36 + data))
            self.f.seek(40)
            self.f.write(struct.pack("<I", data))
            self.f.close()


def _com_init() -> None:
    """soundcard работает через COM: в каждом новом потоке его нужно инициализировать."""
    import ctypes

    ctypes.windll.ole32.CoInitializeEx(None, 0)


class Track(threading.Thread):
    def __init__(self, name: str, writer: WavWriter, open_device, pad_by_clock: bool):
        super().__init__(daemon=True, name=f"rec-{name}")
        self.track = name
        self.writer = writer
        self.open_device = open_device
        self.pad_by_clock = pad_by_clock
        self.stop_event = threading.Event()
        self.first_time: float | None = None
        self.error: Exception | None = None
        self.ready = threading.Event()

    def run(self) -> None:
        try:
            _com_init()
            with self.open_device().recorder(samplerate=RATE, blocksize=BLOCK) as rec:
                self.ready.set()
                while not self.stop_event.is_set():
                    data = rec.record(numframes=BLOCK)
                    now = time.time()
                    if not len(data):
                        continue
                    mono = data.mean(axis=1) if data.ndim > 1 else data
                    if self.first_time is None:
                        self.first_time = now - len(mono) / RATE
                    elif self.pad_by_clock:
                        # Система ничего не присылала (тишина) — дописываем нули до текущего момента.
                        gap = int((now - self.first_time) * RATE) - len(mono) - self.writer.frames
                        if gap > RATE // 5:
                            self.writer.silence(gap)
                    self.writer.write(mono.astype(np.float32))
        except Exception as e:  # покажем пользователю через MeetingRecorder.error
            self.error = e
            log.exception("Запись дорожки %s оборвалась", self.track)
        finally:
            self.ready.set()


class SilenceKeeper(threading.Thread):
    """Беззвучный поток в устройство вывода: без него петля WASAPI молчит, пока никто не говорит."""

    def __init__(self):
        super().__init__(daemon=True, name="rec-keepalive")
        self.stop_event = threading.Event()

    def run(self) -> None:
        try:
            import soundcard as sc

            _com_init()
            zeros = np.zeros((BLOCK, 1), dtype=np.float32)
            with sc.default_speaker().player(samplerate=RATE, channels=1, blocksize=BLOCK) as p:
                while not self.stop_event.is_set():
                    p.play(zeros)
        except Exception as e:
            log.warning("Беззвучный поток не запустился (%s) — пропуски заполню по часам", e)


class MeetingRecorder:
    def __init__(self, folder: Path):
        self.folder = folder
        self.tracks: list[Track] = []
        self.keeper: SilenceKeeper | None = None

    def start(self) -> None:
        import soundcard as sc

        _com_init()  # запись начинают из потока слежения за Zoom или из меню трея
        self.folder.mkdir(parents=True, exist_ok=True)
        speaker = sc.default_speaker()
        mic = Track("mic", WavWriter(self.folder / "mic.wav", RATE), sc.default_microphone, pad_by_clock=False)
        others = Track("others", WavWriter(self.folder / "others.wav", RATE),
                       lambda: sc.get_microphone(speaker.id, include_loopback=True), pad_by_clock=True)
        self.keeper = SilenceKeeper()
        self.keeper.start()
        self.tracks = [mic, others]
        for t in self.tracks:
            t.start()
        for t in self.tracks:
            t.ready.wait(10)
        if mic.error:
            self.stop()
            raise RuntimeError(f"Микрофон недоступен: {mic.error}. Проверьте Параметры Windows → "
                               "Конфиденциальность → Микрофон: доступ для классических приложений")
        if others.error:
            self.stop()
            raise RuntimeError(f"Не удалось записывать звук компьютера: {others.error}")
        log.info("Запись: микрофон «%s», звук компьютера с «%s»", sc.default_microphone().name, speaker.name)

    def alive(self) -> bool:
        return any(t.is_alive() for t in self.tracks)

    def stop(self) -> None:
        stopped = time.time()
        if self.keeper:
            self.keeper.stop_event.set()
        for t in self.tracks:
            t.stop_event.set()
        for t in self.tracks:
            t.join(5)
        for t in self.tracks:
            # Хвост тишины после последнего звука собеседников — до момента остановки.
            if t.pad_by_clock and t.first_time:
                gap = int((stopped - t.first_time) * RATE) - t.writer.frames
                if gap > RATE // 5:
                    t.writer.silence(gap)
            t.writer.close()
        meta = {"target": "all"}
        for t in self.tracks:
            if t.first_time:
                meta[f"{t.track}_start"] = t.first_time
        (self.folder / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
