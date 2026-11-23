"""Запись встречи на Windows: микрофон и звук компьютера (собеседники) — в два WAV, как на Mac:
mic.wav, others.wav и meta.json со временем начала каждой дорожки.

Улучшения надёжности:
 • динамический выбор устройства вывода (включая устройство, куда реально идёт аудиосессия встречи,
   и eCommunications — гарнитуру связи) и переключение без разрыва при смене наушников/динамиков;
 • приоритет MMCSS (AvSetMmThreadCharacteristicsW "Audio") для потоков захвата;
 • защита от зависания WASAPI (15+ подряд пустых чтений → немедленный перезапуск дорожки);
 • пауза и возобновление без рассинхрона дорожек;
 • фиксация интервалов аппаратного Mute динамиков (для офлайн-подавления эха WebRTC AEC3);
 • чекпоинт meta.json каждые 30 с — при сбое или перезагрузке смещения дорожек сохраняются.
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


def _err_text(e: BaseException) -> str:
    """Читаемый текст ошибки (включая исключения вроде голого AssertionError без сообщения)."""
    msg = str(e).strip()
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__


def _pack_wfext(sr: int, ch: int, mask: int) -> bytes:
    """Упакованная (#pragma pack(1), 40 байт) структура WAVEFORMATEXTENSIBLE (IEEE float32)."""
    return struct.pack(
        "<HHIIHHHHIIHH8s",
        0xFFFE, ch, sr, sr * ch * 4, ch * 4, 32, 22,
        32, mask,
        0x00000003, 0x0000, 0x0010, b"\x80\x00\x00\xaa\x00\x38\x9b\x71",
    )


def _pack_wf_float(sr: int, ch: int) -> bytes:
    """Упакованная (18 байт) структура WAVEFORMATEX (WAVE_FORMAT_IEEE_FLOAT, cbSize=0)."""
    return struct.pack("<HHIIHHH", 0x0003, ch, sr, sr * ch * 4, ch * 4, 32, 0)


def _patch_soundcard_mf(sc_mod) -> None:
    """Патчит два бага soundcard 0.4.6 на Windows (mediafoundation.py):
    1) _AudioClient.__init__ жёстко требует wFormatTag==0xFFFE и cbSize==22 и падает с AssertionError
       на USB-микрофонах с WAVE_FORMAT_IEEE_FLOAT (0x0003, cbSize=0, например GM7); кроме того,
       в CFFI-заголовке soundcard не указан #pragma pack(1) для WAVEFORMATEXTENSIBLE.
       Собираем собственный упакованный 40-байтный WAVEFORMATEXTENSIBLE (с fallback на 18-байтный
       WAVEFORMATEX IEEE_FLOAT) — WASAPI сам конвертирует формат благодаря AUTOCONVERTPCM.
    2) _Device.channels делает assert cbSize == 40, хотя читает только nChannels (первые 4 байта)."""
    import collections.abc

    mf = getattr(sc_mod, "mediafoundation", None)
    if mf is None or getattr(mf, "_meeting_agent_patched", False):
        return

    _ffi = mf._ffi
    _ole32 = mf._ole32
    _com = mf._com

    def _patched_init(self, ptr, samplerate, channels, blocksize, isloopback, exclusive_mode=False):
        self._ptr = ptr
        if isinstance(channels, int):
            self.channelmap = list(range(channels))
        elif isinstance(channels, collections.abc.Iterable):
            self.channelmap = list(channels)
        else:
            raise TypeError("channels must be iterable or integer")

        if list(range(len(set(self.channelmap)))) != sorted(list(set(self.channelmap))):
            raise TypeError("Due to limitations of WASAPI, channel maps on Windows "
                            "must be a combination of `range(0, x)`.")

        if blocksize is None:
            blocksize = self.deviceperiod[0] * samplerate

        n_channels = len(set(self.channelmap))
        sr = int(samplerate)

        # Аккуратно подсматриваем dwChannelMask из GetMixFormat, если устройство отдало EXTENSIBLE.
        mix_mask = None
        ppMixFormat = _ffi.new("WAVEFORMATEXTENSIBLE**")
        hr_mix = self._ptr[0][0].lpVtbl.GetMixFormat(self._ptr[0], ppMixFormat)
        if hr_mix == 0 and ppMixFormat[0] != _ffi.NULL:
            try:
                raw18 = bytes(_ffi.buffer(ppMixFormat[0], 18))
                tag, mix_ch, _, _, _, _, cb = struct.unpack("<HHIIHHH", raw18)
                if tag == 0xFFFE and cb >= 22 and mix_ch == n_channels:
                    raw40 = bytes(_ffi.buffer(ppMixFormat[0], 40))
                    mix_mask = struct.unpack_from("<I", raw40, 20)[0]
            except Exception:  # noqa: BLE001
                pass
            finally:
                _ole32.CoTaskMemFree(ppMixFormat[0])

        default_mask = {1: 0x4, 2: 0x3}.get(n_channels, 0)
        mask = mix_mask if mix_mask is not None else default_mask
        sharemode = _ole32.AUDCLNT_SHAREMODE_EXCLUSIVE if exclusive_mode else _ole32.AUDCLNT_SHAREMODE_SHARED
        #             resample   | remix      | better-SRC | nopersist
        streamflags = 0x00100000 | 0x80000000 | 0x08000000 | 0x00080000
        if isloopback:
            streamflags |= 0x00020000
        bufferduration = int(blocksize / samplerate * 10000000)

        candidates = [_pack_wfext(sr, n_channels, mask)]
        if mask != 0:
            candidates.append(_pack_wfext(sr, n_channels, 0))
        candidates.append(_pack_wf_float(sr, n_channels))

        hr = -1
        for raw_fmt in candidates:
            buf = _ffi.new("unsigned char[]", raw_fmt)
            fmt_ptr = _ffi.cast("WAVEFORMATEXTENSIBLE*", buf)
            hr = self._ptr[0][0].lpVtbl.Initialize(
                self._ptr[0], sharemode, streamflags, bufferduration, 0, fmt_ptr, _ffi.NULL
            )
            if hr == 0:
                break
        _com.check_error(hr)
        self.samplerate = samplerate
        self._idle_start_time = None

    mf._AudioClient.__init__ = _patched_init

    # Патч _Device.channels: допускаем cbSize >= 18 (WAVEFORMATEX 18B и WAVEFORMATEXTENSIBLE 40B).
    _orig_channels = mf._Device.channels.fget

    def _patched_channels(self):
        try:
            return _orig_channels(self)
        except AssertionError:
            ppPropertyStore = _ffi.new("IPropertyStore **")
            ptr = self._device_ptr()
            hr = ptr[0][0].lpVtbl.OpenPropertyStore(ptr[0], 0, ppPropertyStore)
            _com.release(ptr)
            _com.check_error(hr)
            propvariant = mf._PropVariant()
            pkey = _ffi.new("PROPERTYKEY *", [
                [0xf19f064d, 0x82c, 0x4e27, [0xbc, 0x73, 0x68, 0x82, 0xa1, 0xbb, 0x8e, 0x4c]], 0
            ])
            hr = ppPropertyStore[0][0].lpVtbl.GetValue(ppPropertyStore[0], pkey, propvariant.ptr)
            _com.release(ppPropertyStore)
            _com.check_error(hr)
            if propvariant.ptr[0].vt != 65:
                return 1
            blob = _ffi.cast("BLOB_PROPVARIANT *", propvariant.ptr)[0].blob
            if blob.cbSize >= 18 and blob.pBlobData != _ffi.NULL:
                wf = _ffi.cast("WAVEFORMATEX *", blob.pBlobData)
                return int(wf[0].nChannels) or 1
            return 1

    mf._Device.channels = property(_patched_channels)
    mf._meeting_agent_patched = True


_SC = None
_SC_LOCK = threading.Lock()


def soundcard():
    """Потокобезопасный импорт soundcard: первый импорт выполняется в чистом потоке без COM,
    чтобы soundcard/mediafoundation._COMLibrary не падал с Error 0x100000001 (S_FALSE)
    из-за ранее вызванного CoInitializeEx в рабочем потоке."""
    global _SC
    if _SC is not None:
        return _SC
    with _SC_LOCK:
        if _SC is not None:
            return _SC
        from . import osutil

        box: dict = {}

        def _imp() -> None:
            try:
                import soundcard as m
                if osutil.IS_WIN:
                    _patch_soundcard_mf(m)
                    mf = getattr(m, "mediafoundation", None)
                    if mf is not None and getattr(getattr(mf, "_com", None), "com_loaded", False):
                        mf._com.com_loaded = False
                        mf._ole32.CoUninitialize()
                box["m"] = m
            except BaseException as e:  # noqa: BLE001
                box["e"] = e

        th = threading.Thread(target=_imp, daemon=True, name="soundcard-init")
        th.start()
        th.join()
        if "e" in box:
            raise box["e"]
        _SC = box["m"]
        return _SC


def _mmcss_enter():
    """Поднимает приоритет потока захвата через Windows MMCSS, чтобы локальный ASR не рвал буферы WASAPI."""
    try:
        import ctypes
        from ctypes import wintypes

        avrt = ctypes.WinDLL("avrt")
        avrt.AvSetMmThreadCharacteristicsW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        avrt.AvSetMmThreadCharacteristicsW.restype = wintypes.HANDLE
        idx = wintypes.DWORD(0)
        return avrt.AvSetMmThreadCharacteristicsW("Audio", ctypes.byref(idx))
    except Exception:  # noqa: BLE001
        return None


def _mmcss_leave(handle) -> None:
    if not handle:
        return
    try:
        import ctypes
        from ctypes import wintypes

        avrt = ctypes.WinDLL("avrt")
        avrt.AvRevertMmThreadCharacteristics.argtypes = [wintypes.HANDLE]
        avrt.AvRevertMmThreadCharacteristics.restype = wintypes.BOOL
        avrt.AvRevertMmThreadCharacteristics(handle)
    except Exception:  # noqa: BLE001
        pass


class Track(threading.Thread):
    def __init__(self, name: str, writer: WavWriter, open_device, pad_by_clock: bool,
                 paused_event: threading.Event | None = None):
        super().__init__(daemon=True, name=f"rec-{name}")
        self.track = name
        self.writer = writer
        self.open_device = open_device
        self.pad_by_clock = pad_by_clock
        self.paused_event = paused_event or threading.Event()
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
        t = Track(self.track, self.writer, self.open_device, self.pad_by_clock, self.paused_event)
        t.first_time, t.first_mono, t.resync = self.first_time, self.first_mono, self.first_mono is not None
        return t

    def shift_clock(self, paused_dt: float) -> None:
        """Сдвигает точку отсчёта на длительность паузы, чтобы пауза не превратилась в тишину."""
        if self.first_mono is not None and paused_dt > 0:
            self.first_mono += paused_dt

    def run(self) -> None:
        inited = False
        mmcss = None
        try:
            inited = _com_init()
            mmcss = _mmcss_enter()
            empty_reads = 0
            with self.open_device().recorder(samplerate=RATE, blocksize=BLOCK) as rec:
                self.ready.set()
                while not self.stop_event.is_set():
                    data = rec.record(numframes=BLOCK)
                    now, now_mono = time.time(), time.monotonic()
                    if not len(data):
                        empty_reads += 1
                        if empty_reads >= 15:
                            raise RuntimeError(f"Устройство {self.track} перестало отдавать звук (пустые буферы)")
                        continue
                    empty_reads = 0
                    self.last_block = now_mono
                    if self.paused_event.is_set():
                        continue
                    mono = data.mean(axis=1) if data.ndim > 1 else data
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
                            self.writer.silence(min(gap, RATE * 3600))
                    self.writer.write(mono.astype(np.float32))
        except Exception as e:  # покажем пользователю через MeetingRecorder.error
            self.error = e
            log.exception("Запись дорожки %s оборвалась", self.track)
        finally:
            self.ready.set()
            _mmcss_leave(mmcss)
            _com_done(inited)


class SilenceKeeper(threading.Thread):
    """Беззвучный поток в устройство вывода: без него петля WASAPI молчит, пока никто не говорит."""

    def __init__(self, open_speaker):
        super().__init__(daemon=True, name="rec-keepalive")
        self.open_speaker = open_speaker
        self.stop_event = threading.Event()

    def run(self) -> None:
        inited = False
        try:
            inited = _com_init()
            zeros = np.zeros((BLOCK, 1), dtype=np.float32)
            with self.open_speaker().player(samplerate=RATE, channels=1, blocksize=BLOCK) as p:
                while not self.stop_event.is_set():
                    p.play(zeros)
        except Exception as e:
            log.warning("Беззвучный поток не запустился (%s) — пропуски заполню по часам", e)
        finally:
            _com_done(inited)


class MeetingRecorder:
    def __init__(self, folder: Path, source: str = "all", mic_only: bool = False):
        self.folder = folder
        self.source = source
        self.mic_only = mic_only
        self.tracks: list[Track] = []
        self.keeper: SilenceKeeper | None = None
        self.mic_id: str | None = None
        self.speaker_id: str | None = None
        self.paused_event = threading.Event()
        self._pause_started: float | None = None
        self.total_paused = 0.0
        self.pauses: list[list[float]] = []
        self._started_wall = 0.0
        self._started_mono = 0.0
        self._speaker_muted = False
        self._muted_intervals: list[list[float]] = []
        self._last_checkpoint = 0.0

    @property
    def is_paused(self) -> bool:
        return self.paused_event.is_set()

    @property
    def is_speaker_muted(self) -> bool:
        return self._speaker_muted

    def _resolve_mic(self):
        sc = soundcard()
        if self.mic_id:
            try:
                m = sc.get_microphone(self.mic_id)
                if m is not None:
                    return m
            except Exception:
                pass
        m = sc.default_microphone()
        self.mic_id = m.id
        return m

    def _resolve_speaker(self):
        sc = soundcard()
        if self.speaker_id:
            try:
                sp = sc.get_speaker(self.speaker_id)
                if sp is not None:
                    return sp
            except Exception:
                pass
        return sc.default_speaker()

    def _open_loopback(self):
        sc = soundcard()
        sp = self._resolve_speaker()
        self.speaker_id = sp.id
        return sc.get_microphone(sp.id, include_loopback=True)

    def start(self) -> None:
        import shutil

        from . import detect, osutil

        sc = soundcard()  # до _com_init(): первый импорт в чистом потоке без COM
        _com_init()       # запись начинают из потока слежения за встречами или из меню трея
        self.folder.mkdir(parents=True, exist_ok=True)
        free_bytes = shutil.disk_usage(self.folder).free
        if free_bytes < 500 * 1024 * 1024:
            raise RuntimeError(f"Недостаточно места на диске (свободно {free_bytes // (1024 * 1024)} МБ, "
                               "нужно минимум 500 МБ). Освободите место перед записью.")
        self._started_wall = time.time()
        self._started_mono = time.monotonic()
        self._last_checkpoint = self._started_mono

        if self.mic_only:
            speaker_name = "не используется (только микрофон)"
        else:
            self.speaker_id = detect.meeting_render_device_id(self.source)
            try:
                speaker = self._resolve_speaker()
                self.speaker_id = speaker.id
                speaker_name = speaker.name
            except Exception as e:
                no_speakers = False
                try:
                    no_speakers = len(sc.all_speakers()) == 0
                except Exception:
                    pass
                if no_speakers:
                    raise RuntimeError(f"Нет устройства вывода звука ({_err_text(e)}): подключите динамики "
                                       "или наушники и выберите их в Параметрах Windows → Система → Звук") from e
                raise RuntimeError(f"Не удалось открыть устройство вывода звука ({_err_text(e)})") from e

        from .recovery import write_lock

        write_lock(self.folder, "mic_only" if self.mic_only else self.source)
        self.mic_id = detect.meeting_capture_device_id("mic_only" if self.mic_only else self.source)
        try:
            mic_name = self._resolve_mic().name
        except Exception:
            mic_name = "?"
        mic = Track("mic", WavWriter(self.folder / "mic.wav", RATE), self._resolve_mic,
                    pad_by_clock=False, paused_event=self.paused_event)
        if self.mic_only:
            self.tracks = [mic]
        else:
            others = Track("others", WavWriter(self.folder / "others.wav", RATE), self._open_loopback,
                           pad_by_clock=True, paused_event=self.paused_event)
            self.keeper = SilenceKeeper(self._resolve_speaker)
            self.keeper.start()
            self.tracks = [mic, others]
        for t in self.tracks:
            t.start()
        for t in self.tracks:
            t.ready.wait(10)
        if mic.error:
            self.stop()
            raise RuntimeError(f"Микрофон «{mic_name}» недоступен ({_err_text(mic.error)}). {mic_access_hint()}")
        if not self.mic_only and len(self.tracks) > 1 and self.tracks[1].error:
            self.stop()
            raise RuntimeError(f"Не удалось записывать звук компьютера ({_err_text(self.tracks[1].error)})")
        osutil.prevent_sleep(True)
        if not self.mic_only:
            self.poll_speaker_mute()
        self.write_meta(in_progress=True)
        log.info("Запись: микрофон «%s», звук компьютера: %s", mic_name, speaker_name)

    def pause(self) -> None:
        if self.is_paused:
            return
        self._pause_started = time.monotonic()
        self.paused_event.set()
        self.write_meta(in_progress=True)

    def resume(self) -> None:
        if not self.is_paused:
            return
        now_m = time.monotonic()
        dt = max(0.0, now_m - (self._pause_started or now_m))
        audio_at = max(0.0, (self._pause_started or now_m) - self._started_mono - self.total_paused)
        self.total_paused += dt
        if dt >= 0.5:
            self.pauses.append([round(audio_at, 1), round(dt, 1)])
        self._pause_started = None
        for t in self.tracks:
            t.shift_clock(dt)
        self.paused_event.clear()
        self.write_meta(in_progress=True)

    def current_paused_seconds(self) -> float:
        extra = max(0.0, time.monotonic() - self._pause_started) if self.is_paused and self._pause_started else 0.0
        return self.total_paused + extra

    def poll_speaker_mute(self) -> bool:
        """Фиксирует переходы аппаратного Mute динамиков (для офлайн-AEC)."""
        if self.mic_only:
            return False
        from . import detect

        muted = detect.is_render_device_muted(self.speaker_id)
        now_sec = max(0.0, time.monotonic() - self._started_mono - self.current_paused_seconds())
        if muted != self._speaker_muted:
            self._speaker_muted = muted
            if muted:
                self._muted_intervals.append([round(now_sec, 2), -1.0])
            elif self._muted_intervals and self._muted_intervals[-1][1] < 0:
                self._muted_intervals[-1][1] = round(now_sec, 2)
        return muted

    def alive(self) -> bool:
        return any(t.is_alive() for t in self.tracks)

    def health(self) -> dict:
        """Состояние дорожек с прошлой проверки; упавшие дорожки перезапускаются."""
        out = {}
        now = time.monotonic()
        self.poll_speaker_mute()
        if now - self._last_checkpoint >= 30:
            self._last_checkpoint = now
            self.write_meta(in_progress=True)
        if self.is_paused:
            return {t.track: {"restarted": False, "peak": 0.0} for t in self.tracks}
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

    def note_sleep(self, sleep_dt: float) -> None:
        """Компьютер выходил из сна: на Windows time.monotonic() продолжает тикать во сне.
        Сдвигаем часы дорожек на время сна, чтобы не залить WAV многоминутной тишиной."""
        if sleep_dt < 1.0:
            return
        if self.is_paused:
            return
        audio_at = max(0.0, time.monotonic() - self._started_mono - self.total_paused - sleep_dt)
        self.total_paused += sleep_dt
        self.pauses.append([round(audio_at, 1), round(sleep_dt, 1)])
        for t in self.tracks:
            t.shift_clock(sleep_dt)
        self.write_meta(in_progress=True)

    def restart_mic(self, new_mic_id: str | None = None) -> None:
        """Сменился микрофон (по умолчанию или eCommunications) — переходим на новый, не прерывая запись."""
        if new_mic_id:
            self.mic_id = new_mic_id
        for i, t in enumerate(self.tracks):
            if t.track == "mic" and not t.stop_event.is_set():
                t.stop_event.set()
                new = t.revived()
                self.tracks[i] = new
                new.start()

    def restart_speaker(self, new_speaker_id: str | None = None) -> None:
        """Сменилось устройство вывода (наушники / гарнитура) — перезапускаем петлю WASAPI и SilenceKeeper."""
        if self.mic_only:
            return
        if new_speaker_id:
            self.speaker_id = new_speaker_id
        if self.keeper:
            self.keeper.stop_event.set()
        self.keeper = SilenceKeeper(self._resolve_speaker)
        self.keeper.start()
        for i, t in enumerate(self.tracks):
            if t.track == "others" and not t.stop_event.is_set():
                t.stop_event.set()
                new = t.revived()
                self.tracks[i] = new
                new.start()

    def write_meta(self, in_progress: bool) -> None:
        from . import osutil

        now_sec = max(0.0, time.monotonic() - self._started_mono - self.current_paused_seconds())
        meta: dict = {"target": "mic_only" if self.mic_only else self.source, "started": self._started_wall}
        if self.mic_only:
            meta["mic_only"] = True
        for t in self.tracks:
            if t.first_time:
                meta[f"{t.track}_start"] = t.first_time
        if self.total_paused > 0:
            meta["paused_seconds"] = round(self.total_paused, 1)
        if self.pauses:
            meta["pauses"] = self.pauses
        if self._muted_intervals:
            meta["speaker_muted"] = [[a, b if b >= 0 else round(now_sec, 2)] for a, b in self._muted_intervals]
        if in_progress:
            meta["in_progress"] = True
            meta["paused"] = self.is_paused
            meta["checkpoint"] = time.time()
        osutil.atomic_write_text(self.folder / "meta.json", json.dumps(meta, indent=2))

    def stop(self) -> None:
        from . import osutil

        osutil.prevent_sleep(False)
        if self.is_paused:
            self.resume()
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
                    t.writer.silence(min(gap, RATE * 3600))
            t.writer.close()
        now_sec = max(0.0, stopped - self._started_mono - self.total_paused)
        if self._muted_intervals and self._muted_intervals[-1][1] < 0:
            self._muted_intervals[-1][1] = round(now_sec, 2)
        self.write_meta(in_progress=False)
        from .recovery import remove_lock

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
