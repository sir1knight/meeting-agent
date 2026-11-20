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


def _com_init() -> bool:
    """soundcard работает через COM: в каждом новом потоке его нужно инициализировать.
    True — инициализация прошла (S_OK или S_FALSE), и в конце потока нужен парный CoUninitialize."""
    import ctypes

    return ctypes.windll.ole32.CoInitializeEx(None, 0) in (0, 1)


def _com_done(inited: bool) -> None:
    if inited:
        import ctypes

        ctypes.windll.ole32.CoUninitialize()


class Track(threading.Thread):
    def __init__(self, name: str, writer: WavWriter, open_device, pad_by_clock: bool):
        super().__init__(daemon=True, name=f"rec-{name}")
        self.track = name
        self.writer = writer
        self.open_device = open_device
        self.pad_by_clock = pad_by_clock
        self.stop_event = threading.Event()
        self.first_time: float | None = None  # время начала по часам системы — для meta.json
        self.first_mono: float | None = None  # то же по монотонным часам — для расчёта пропусков
        self.error: Exception | None = None
        self.ready = threading.Event()
        self.peak = 0.0                       # пиковый уровень с прошлой проверки (0 — цифровая тишина)
        self.last_block: float | None = None  # монотонное время последнего блока — «жива ли дорожка»
        self.resync = False                   # перезапуск: первый блок — дописать пропуск по часам

    def revived(self) -> "Track":
        """Новая дорожка в тот же файл (устройство пропало или сменилось): время продолжается, пропуск — тишиной."""
        t = Track(self.track, self.writer, self.open_device, self.pad_by_clock)
        t.first_time, t.first_mono, t.resync = self.first_time, self.first_mono, self.first_mono is not None
        return t

    def run(self) -> None:
        inited = False
        try:
            inited = _com_init()
            with self.open_device().recorder(samplerate=RATE, blocksize=BLOCK) as rec:
                self.ready.set()
                while not self.stop_event.is_set():
                    data = rec.record(numframes=BLOCK)
                    now, now_mono = time.time(), time.monotonic()
                    if not len(data):
                        continue
                    mono = data.mean(axis=1) if data.ndim > 1 else data
                    self.last_block = now_mono
                    self.peak = max(self.peak, float(np.abs(mono).max()) if len(mono) else 0.0)
                    if self.first_time is None:
                        self.first_time = now - len(mono) / RATE
                        self.first_mono = now_mono - len(mono) / RATE
                    elif self.resync or self.pad_by_clock:
                        self.resync = False
                        # Система ничего не присылала (тишина) — дописываем нули до текущего момента.
                        # Монотонные часы: подводка системных часов (NTP) не превратится в «тишину».
                        gap = int((now_mono - self.first_mono) * RATE) - len(mono) - self.writer.frames
                        if gap > RATE // 5:
                            self.writer.silence(gap)
                    self.writer.write(mono.astype(np.float32))
        except Exception as e:  # покажем пользователю через MeetingRecorder.error
            self.error = e
            log.exception("Запись дорожки %s оборвалась", self.track)
        finally:
            self.ready.set()
            _com_done(inited)


class SilenceKeeper(threading.Thread):
    """Беззвучный поток в устройство вывода: без него петля WASAPI молчит, пока никто не говорит."""

    def __init__(self):
        super().__init__(daemon=True, name="rec-keepalive")
        self.stop_event = threading.Event()

    def run(self) -> None:
        inited = False
        try:
            import soundcard as sc

            inited = _com_init()
            zeros = np.zeros((BLOCK, 1), dtype=np.float32)
            with sc.default_speaker().player(samplerate=RATE, channels=1, blocksize=BLOCK) as p:
                while not self.stop_event.is_set():
                    p.play(zeros)
        except Exception as e:
            log.warning("Беззвучный поток не запустился (%s) — пропуски заполню по часам", e)
        finally:
            _com_done(inited)


class MeetingRecorder:
    def __init__(self, folder: Path):
        self.folder = folder
        self.tracks: list[Track] = []
        self.keeper: SilenceKeeper | None = None

    def start(self) -> None:
        import soundcard as sc

        _com_init()  # запись начинают из потока слежения за Zoom или из меню трея (поток живёт долго)
        self.folder.mkdir(parents=True, exist_ok=True)
        from .recovery import write_lock

        write_lock(self.folder, "all")  # если приложение упадёт, сервер восстановит запись при запуске
        try:
            speaker = sc.default_speaker()
        except Exception as e:
            raise RuntimeError(f"Нет устройства вывода звука ({e}): подключите динамики или наушники "
                               "и выберите их в Параметрах Windows → Система → Звук") from e
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
            raise RuntimeError(f"Микрофон недоступен: {mic.error}. {mic_access_hint()}")
        if others.error:
            self.stop()
            raise RuntimeError(f"Не удалось записывать звук компьютера: {others.error}")
        try:
            mic_name = sc.default_microphone().name
        except Exception:
            mic_name = "?"
        log.info("Запись: микрофон «%s», звук компьютера с «%s»", mic_name, speaker.name)

    def alive(self) -> bool:
        return any(t.is_alive() for t in self.tracks)

    def health(self) -> dict:
        """Состояние дорожек с прошлой проверки; упавшие дорожки перезапускаются (как «сторож» у mymeet)."""
        out = {}
        now = time.monotonic()
        for i, t in enumerate(list(self.tracks)):
            dead = not t.is_alive() or (t.last_block is not None and now - t.last_block > 10)
            if dead and not t.stop_event.is_set():
                log.warning("Дорожка %s не отвечает — перезапускаю", t.track)
                t.stop_event.set()
                new = t.revived()
                self.tracks[i] = new
                new.start()
                out[t.track] = {"restarted": True, "peak": 0.0}
                continue
            out[t.track] = {"restarted": False, "peak": t.peak}
            t.peak = 0.0
        return out

    def restart_mic(self) -> None:
        """Сменился микрофон по умолчанию — переходим на новый, не прерывая запись."""
        for i, t in enumerate(self.tracks):
            if t.track == "mic" and not t.stop_event.is_set():
                t.stop_event.set()
                new = t.revived()
                self.tracks[i] = new
                new.start()

    def stop(self) -> None:
        stopped = time.monotonic()
        if self.keeper:
            self.keeper.stop_event.set()
        for t in self.tracks:
            t.stop_event.set()
        for t in self.tracks:
            t.join(5)
        for t in self.tracks:
            # Хвост тишины после последнего звука собеседников — до момента остановки.
            if t.pad_by_clock and t.first_mono:
                gap = int((stopped - t.first_mono) * RATE) - t.writer.frames
                if gap > RATE // 5:
                    t.writer.silence(gap)
            t.writer.close()
        meta = {"target": "all"}
        for t in self.tracks:
            if t.first_time:
                meta[f"{t.track}_start"] = t.first_time
        from . import osutil
        from .recovery import remove_lock

        osutil.atomic_write_text(self.folder / "meta.json", json.dumps(meta, indent=2))
        remove_lock(self.folder)


def mic_access_hint() -> str:
    """Почему Windows не даёт микрофон: выключил пользователь или закрыто политикой организации."""
    try:
        import winreg

        def val(root, path, name):
            try:
                with winreg.OpenKey(root, path) as k:
                    return winreg.QueryValueEx(k, name)[0]
            except OSError:
                return None

        if val(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows\AppPrivacy", "LetAppsAccessMicrophone") == 2:
            return ("Доступ к микрофону закрыт политикой организации — включить его может только администратор "
                    "(обратитесь в ИТ-поддержку).")
        base = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            if str(val(root, base, "Value")).lower() == "deny":
                return "Доступ к микрофону выключен: Параметры Windows → Конфиденциальность → Микрофон → включите доступ."
        if str(val(winreg.HKEY_CURRENT_USER, base + "\\NonPackaged", "Value")).lower() == "deny":
            return ("Выключено «Разрешить классическим приложениям доступ к микрофону»: Параметры Windows → "
                    "Конфиденциальность → Микрофон.")
    except Exception:  # noqa: BLE001
        pass
    return ("Windows не запрещает доступ — проверьте, подключён ли микрофон и не занят ли он другой программой "
            "в монопольном режиме.")
