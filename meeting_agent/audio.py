"""Подготовка аудио: перевод в 16 кГц моно.

На Mac основной способ — встроенная утилита afconvert (wav, m4a, mp3, aac, aiff, caf).
ffmpeg нужен только для экзотики вроде ogg/opus/webm, и то если он установлен:
приложение, запущенное из Finder, программ Homebrew в PATH не видит.
На Windows — библиотека PyAV (FFmpeg внутри, ставится вместе с faster-whisper): читает всё.
"""

import shutil
import subprocess
from pathlib import Path

from .osutil import IS_WIN

AFCONVERT_EXT = {".wav", ".m4a", ".mp3", ".mp4", ".aac", ".aif", ".aiff", ".caf", ".mov", ".flac"}


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg") or next(
        (p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg") if Path(p).exists()), None)


def to_16k_mono(src: Path, dst: Path) -> Path:
    if IS_WIN:
        return _convert_av(src, dst)
    if src.suffix.lower() in AFCONVERT_EXT:
        r = subprocess.run(["/usr/bin/afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
                            str(src), str(dst)], capture_output=True, text=True)
        if r.returncode == 0:
            return dst
    ff = _ffmpeg()
    if not ff:
        raise RuntimeError(f"Не умею читать формат {src.suffix}: сохраните запись в m4a, mp3 или wav")
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", "16000", str(dst)],
                   check=True)
    return dst


def _convert_av(src: Path, dst: Path) -> Path:
    """Потоком, кусками: часовая встреча не занимает память целиком."""
    import av
    import soundfile as sf

    try:
        with av.open(str(src)) as box, sf.SoundFile(str(dst), "w", 16000, 1, "PCM_16") as out:
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
            for frame in box.decode(box.streams.audio[0]):
                for f in resampler.resample(frame):
                    out.write(f.to_ndarray().reshape(-1))
            for f in resampler.resample(None):
                out.write(f.to_ndarray().reshape(-1))
    except (av.error.FFmpegError, IndexError) as e:
        raise RuntimeError(f"Не удалось прочитать {src.name}: {e}") from e
    return dst


def load(path: Path):
    """Звук 16 кГц моно как массив float32 — для Whisper (без вызова ffmpeg внутри mlx-whisper)."""
    import numpy as np
    import soundfile as sf

    data, _ = sf.read(str(path), dtype="float32", always_2d=True)
    return np.ascontiguousarray(data.mean(axis=1))


def duration(path: Path) -> float:
    import soundfile as sf

    info = sf.info(str(path))
    return info.frames / info.samplerate if info.samplerate else 0.0


def is_silent(path: Path, threshold: float = 0.003) -> bool:
    """Грубая проверка: в дорожке нет ничего, кроме тишины (например, микрофон был выключен)."""
    import numpy as np
    import soundfile as sf

    peak_rms = 0.0
    for block in sf.blocks(str(path), blocksize=16000 * 30, dtype="float32"):
        if block.ndim > 1:
            block = block.mean(axis=1)
        if len(block):
            peak_rms = max(peak_rms, float(np.sqrt(np.mean(block ** 2))))
        if peak_rms > threshold:
            return False
    return True


def repair_wav(path: Path) -> bool:
    """Чинит заголовок WAV, который не закрыли (запись оборвалась или ещё идёт): размер данных
    в заголовке 0 или меньше реального — тогда файл читается пустым. Возвращает True, если чинил."""
    import struct

    try:
        with open(path, "r+b") as f:
            head = f.read(65536)
            if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return False
            size = f.seek(0, 2)
            pos = 12
            while pos + 8 <= len(head):
                cid, csize = head[pos:pos + 4], struct.unpack("<I", head[pos + 4:pos + 8])[0]
                if cid == b"data":
                    real = size - (pos + 8)
                    # Чиним только незакрытый (0) или обрезанный файл: у нормального после данных
                    # могут идти служебные блоки, их нельзя считать звуком.
                    if 0 < csize <= real:
                        return False
                    f.seek(pos + 4)
                    f.write(struct.pack("<I", min(real, 0xFFFFFFFF)))
                    f.seek(4)
                    f.write(struct.pack("<I", min(size - 8, 0xFFFFFFFF)))
                    return True
                pos += 8 + csize + (csize & 1)
    except OSError:
        return False
    return False


def compress_to_m4a(src: Path, dst: Path, bitrate: int = 48000) -> Path | None:
    """Сжимает WAV в компактный AAC (.m4a, ~20 МБ/час вместо 345 МБ) через встроенный afconvert (Mac)
    или ffmpeg (если установлен). Возвращает dst при успехе или None."""
    part = dst.with_suffix(".part.m4a")
    try:
        if not IS_WIN:
            r = subprocess.run(
                ["/usr/bin/afconvert", "-f", "m4af", "-d", "aac", "-b", str(bitrate), "-c", "1",
                 str(src), str(part)],
                capture_output=True, text=True,
            )
            if r.returncode == 0 and part.exists() and part.stat().st_size > 1024:
                part.replace(dst)
                return dst
            part.unlink(missing_ok=True)
        ff = _ffmpeg()
        if ff:
            r = subprocess.run(
                [ff, "-y", "-loglevel", "error", "-i", str(src), "-ac", "1",
                 "-c:a", "aac", "-b:a", f"{bitrate // 1000}k", "-movflags", "+faststart", str(part)],
                capture_output=True, text=True,
            )
            if r.returncode == 0 and part.exists() and part.stat().st_size > 1024:
                part.replace(dst)
                return dst
            part.unlink(missing_ok=True)
        if IS_WIN and _compress_av_m4a(src, part, bitrate):
            part.replace(dst)
            return dst
    except OSError:
        pass
    part.unlink(missing_ok=True)
    return None


def _compress_av_m4a(src: Path, dst: Path, bitrate: int = 48000) -> bool:
    """Кодирование в AAC (.m4a) через встроенный PyAV (Windows, без внешнего ffmpeg.exe)."""
    try:
        import av

        with av.open(str(src)) as inp, av.open(str(dst), "w", format="ipod") as out:
            in_stream = inp.streams.audio[0]
            rate = in_stream.rate or 16000
            out_stream = out.add_stream("aac", rate=rate)
            out_stream.layout = "mono"
            out_stream.bit_rate = bitrate
            resampler = av.AudioResampler(format="fltp", layout="mono", rate=rate)
            for frame in inp.decode(in_stream):
                for rf in resampler.resample(frame):
                    for pkt in out_stream.encode(rf):
                        out.mux(pkt)
            for rf in resampler.resample(None):
                for pkt in out_stream.encode(rf):
                    out.mux(pkt)
            for pkt in out_stream.encode(None):
                out.mux(pkt)
        return dst.exists() and dst.stat().st_size > 1024
    except Exception:  # noqa: BLE001
        return False
