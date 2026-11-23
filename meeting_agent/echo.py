"""Подавление эха после встречи (WebRTC AEC3 — тот же, что в Chrome и Zoom).

Без наушников голос собеседников из динамиков попадает в микрофон: в расшифровке «Я» повторяет чужие фразы,
а распознавание путается. У нас обе дорожки записаны отдельно: others.wav — ровно то, что звучало из
динамиков. Её и вычитаем из микрофона — по кадрам 10 мс, с уточнением реальной акустической задержки
через кросс-корреляцию GCC-PHAT и учётом интервалов аппаратного Mute динамиков (meta.json).
Плюс подавление шума. Работает после встречи, при обработке — во время звонка компьютер не нагружается.

На Mac эхо обычно уже убрано при записи (Voice Processing macOS, meta.json → echo_cancel) — тогда пропускаем.
Пакет livekit (модуль обработки звука WebRTC) — в списке пакетов; без него просто ничего не делаем.
"""

import logging
import time
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SR = 16000
FRAME = SR // 100  # 10 мс


def available() -> bool:
    try:
        from livekit import rtc
        return hasattr(rtc, "AudioProcessingModule")
    except Exception:  # noqa: BLE001
        return False


def estimate_delay_gcc_phat(mic: np.ndarray, ref: np.ndarray, initial_shift: int = 0,
                            sr: int = SR, max_lag_ms: int = 300) -> tuple[int, float]:
    """Оценивает реальную задержку эха (в сэмплах) между дорожкой собеседников (ref) и микрофоном (mic)
    методом GCC-PHAT по активным окнам речи.

    Положительный lag означает, что звук ref[t] приходит в mic[t + lag] (физическая задержка динамики→микрофон
    плюс разница буферов). Возвращает (lag_samples, confidence).
    """
    win = 16384  # ~1.024 с
    nfft = win * 2
    max_lag = int(sr * max_lag_ms / 1000)
    limit = min(len(mic), sr * 15 * 60)  # достаточно первых 15 минут встречи
    if limit < win or len(ref) < win:
        return 0, 0.0

    accum = np.zeros(2 * max_lag + 1, dtype=np.float64)
    used = 0
    step = sr * 2  # проверяем каждые 2 секунды
    for a in range(0, limit - win, step):
        j = a + initial_shift
        if j < 0 or j + win > len(ref):
            continue
        r = ref[j:j + win].astype(np.float32) / 32768.0
        if float(np.sqrt(np.mean(r * r))) < 0.015:
            continue
        m = mic[a:a + win].astype(np.float32) / 32768.0
        if float(np.sqrt(np.mean(m * m))) < 0.003:
            continue
        R = np.fft.rfft(m, n=nfft) * np.conj(np.fft.rfft(r, n=nfft))
        G = R / (np.abs(R) + 1e-10)
        cc = np.fft.irfft(G, n=nfft)
        # Индексы [-max_lag .. +max_lag]: cc[nfft - max_lag:] + cc[:max_lag + 1]
        seg = np.concatenate((cc[nfft - max_lag:], cc[:max_lag + 1]))
        accum += seg
        used += 1
        if used >= 14:
            break

    if used < 2:
        return 0, 0.0
    accum /= used
    peak_idx = int(np.argmax(accum))
    peak = float(accum[peak_idx])
    noise = float(np.median(np.abs(accum - np.median(accum)))) * 1.4826 + 1e-9
    confidence = peak / noise
    lag = peak_idx - max_lag
    return lag, confidence


def _is_muted_at(t_sec: float, intervals: list[list[float]]) -> bool:
    for pair in intervals:
        if len(pair) >= 2 and pair[0] <= t_sec <= pair[1]:
            return True
    return False


def cancel(mic16: Path, others16: Path, mic_offset: float, others_offset: float, out: Path,
           muted_intervals: list[list[float]] | None = None) -> Path | None:
    """mic16, others16 — 16 кГц моно; *_offset — начало дорожки от начала встречи, с.
    Возвращает путь к очищенному микрофону или None, если не вышло (тогда распознаём исходный)."""
    import soundfile as sf
    from livekit import rtc

    started = time.time()
    mic, sr1 = sf.read(str(mic16), dtype="int16")
    ref, sr2 = sf.read(str(others16), dtype="int16")
    if sr1 != SR or sr2 != SR or mic.ndim != 1 or ref.ndim != 1 or not len(mic):
        return None

    # Базовый сдвиг по часам: сэмпл i микрофона звучал одновременно с сэмплом i + shift дорожки собеседников.
    base_shift = int(round((mic_offset - others_offset) * SR))
    lag, conf = estimate_delay_gcc_phat(mic, ref, initial_shift=base_shift, sr=SR)
    target_lead_ms = 20
    if conf >= 5.0:
        # Оставляем ровный причинный запас 20 мс, чтобы AEC3 видел референс чуть раньше эха.
        adj = lag - int(SR * target_lead_ms / 1000)
        shift = base_shift - adj
        delay_ms = target_lead_ms
        log.info("Эхо (GCC-PHAT): задержка %+.1f мс (уверенность %.1f) → выравнивание %d мс",
                 lag * 1000.0 / SR, conf, delay_ms)
    else:
        shift = base_shift
        delay_ms = 0

    apm = rtc.AudioProcessingModule(echo_cancellation=True, noise_suppression=True, high_pass_filter=True)
    apm.set_stream_delay_ms(delay_ms)
    muted = muted_intervals or []
    n = len(mic) // FRAME
    clean = np.empty(n * FRAME, dtype=np.int16)
    zero = np.zeros(FRAME, dtype=np.int16)
    for k in range(n):
        a = k * FRAME
        j = a + shift
        if muted and _is_muted_at(mic_offset + a / SR, muted):
            r = zero
        else:
            r = ref[j:j + FRAME] if 0 <= j and j + FRAME <= len(ref) else zero
        rf = rtc.AudioFrame(r.tobytes(), SR, 1, FRAME)
        apm.process_reverse_stream(rf)
        mf = rtc.AudioFrame(mic[a:a + FRAME].tobytes(), SR, 1, FRAME)
        apm.process_stream(mf)
        clean[a:a + FRAME] = np.frombuffer(bytes(mf.data), dtype=np.int16)
    sf.write(str(out), clean, SR, subtype="PCM_16")
    log.info("Эхо: микрофон очищен за %d с (%d мин записи)", time.time() - started, len(mic) // SR // 60)
    return out
