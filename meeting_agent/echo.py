"""Подавление эха после встречи (офлайн, без нагрузки во время звонка).

Без наушников голос собеседников из динамиков попадает в микрофон: в расшифровке «Я» повторяет чужие фразы.
У нас обе дорожки записаны отдельно: others.wav — ровно то, что звучало из динамиков.
Сначала оценивается реальная акустическая задержка между дорожками методом GCC-PHAT (и проверяется,
есть ли вообще эхо — в наушниках эха нет и микрофон остаётся нетронутым), а интервалы аппаратного
Mute динамиков из meta.json обнуляются в референсе.

Поддерживаемые движки (автовыбор: dtln → webrtc → nlms):
  1. DTLN-AEC (нейросеть ONNX + CoreML/CPU, модели в MEETING_AGENT_HOME/models/dtln-aec/) — лучше всего
     убирает нелинейное эхо громких динамиков ноутбука.
  2. WebRTC AEC3 (пакет livekit.rtc) — классический движок Chrome/Zoom.
  3. Частотный адаптивный фильтр NLMS + подавление остаточного эха (чистый NumPy/SciPy) — работает
     всегда из коробки без дополнительных пакетов.
"""

import logging
import time
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SR = 16000
FRAME = SR // 100  # 10 мс


def _dtln_models(cfg: dict | None = None) -> tuple[Path, Path] | None:
    """Ищет пару ONNX-моделей DTLN-AEC (*_1.onnx и *_2.onnx) в папке моделей."""
    from . import config

    folder = config.MODELS_DIR / "dtln-aec"
    if not folder.is_dir():
        return None
    for size in (512, 256, 128):
        m1, m2 = folder / f"dtln_aec_{size}_1.onnx", folder / f"dtln_aec_{size}_2.onnx"
        if m1.exists() and m2.exists():
            return m1, m2
    return None


def dtln_available(cfg: dict | None = None) -> bool:
    if not _dtln_models(cfg):
        return False
    try:
        import onnxruntime
        return hasattr(onnxruntime, "InferenceSession")
    except Exception:  # noqa: BLE001
        return False


def webrtc_available() -> bool:
    try:
        from livekit import rtc
        return hasattr(rtc, "AudioProcessingModule")
    except Exception:  # noqa: BLE001
        return False


def available() -> bool:
    """Офлайн-подавление эха доступно всегда (DTLN-AEC / WebRTC AEC3 / встроенный частотный NLMS)."""
    return True


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


def _aligned_ref(mic_len: int, ref: np.ndarray, shift: int, mic_offset: float,
                 muted: list[list[float]]) -> np.ndarray:
    """Выравненный референс длиной mic_len (float32 [-1..1]) с обнулёнными интервалами аппаратного Mute."""
    out = np.zeros(mic_len, dtype=np.float32)
    src_a = max(0, shift)
    dst_a = max(0, -shift)
    n = min(mic_len - dst_a, len(ref) - src_a)
    if n > 0:
        out[dst_a:dst_a + n] = ref[src_a:src_a + n].astype(np.float32) / 32768.0
    if muted:
        for pair in muted:
            if len(pair) >= 2:
                a = max(0, int((pair[0] - mic_offset) * SR))
                b = min(mic_len, int((pair[1] - mic_offset) * SR))
                if b > a:
                    out[a:b] = 0.0
    return out


def _cancel_dtln(mic_f: np.ndarray, ref_f: np.ndarray, models: tuple[Path, Path]) -> np.ndarray:
    """Нейросетевое подавление эха DTLN-AEC (16 кГц, 512 FFT, 128 hop) на ONNX Runtime (CoreML/CPU)."""
    import onnxruntime as ort

    providers = ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    avail = set(ort.get_available_providers())
    use_prov = [p for p in providers if p in avail] or ["CPUExecutionProvider"]
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 2
    s1 = ort.InferenceSession(str(models[0]), sess_options=opts, providers=use_prov)
    s2 = ort.InferenceSession(str(models[1]), sess_options=opts, providers=use_prov)

    in1 = s1.get_inputs()
    in2 = s2.get_inputs()
    state1 = np.zeros(in1[2].shape, dtype=np.float32)
    state2 = np.zeros(in2[2].shape, dtype=np.float32)

    block_len, hop = 512, 128
    n = len(mic_f)
    pad = block_len
    m_pad = np.pad(mic_f, (pad, pad))
    r_pad = np.pad(ref_f, (pad, pad))
    out_pad = np.zeros_like(m_pad)

    for idx in range(0, len(m_pad) - block_len, hop):
        m_blk = m_pad[idx:idx + block_len]
        r_blk = r_pad[idx:idx + block_len]
        m_fft = np.fft.rfft(m_blk)
        r_fft = np.fft.rfft(r_blk)
        m_mag = np.abs(m_fft).astype(np.float32).reshape(1, 1, -1)
        r_mag = np.abs(r_fft).astype(np.float32).reshape(1, 1, -1)
        mask, state1 = s1.run(None, {in1[0].name: m_mag, in1[1].name: r_mag, in1[2].name: state1})
        est_blk = np.fft.irfft(m_fft * mask.reshape(-1)).astype(np.float32)
        est_in = est_blk.reshape(1, 1, block_len)
        r_in = r_blk.astype(np.float32).reshape(1, 1, block_len)
        clean_blk, state2 = s2.run(None, {in2[0].name: est_in, in2[1].name: r_in, in2[2].name: state2})
        out_pad[idx:idx + block_len] += clean_blk.reshape(-1)

    return np.clip(out_pad[pad:pad + n], -1.0, 1.0)


def _cancel_webrtc(mic: np.ndarray, ref: np.ndarray, shift: int, delay_ms: int,
                   mic_offset: float, muted: list[list[float]]) -> np.ndarray:
    from livekit import rtc

    apm = rtc.AudioProcessingModule(echo_cancellation=True, noise_suppression=True, high_pass_filter=True)
    apm.set_stream_delay_ms(delay_ms)
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
    return clean


def _cancel_nlms(mic_f: np.ndarray, ref_f: np.ndarray) -> np.ndarray:
    """Частотный многополосный адаптивный фильтр (subband NLMS + мягкое подавление остаточного эха).
    Работает на чистом NumPy/SciPy без внешних пакетов."""
    nfft = 512
    hop = 128
    nbins = nfft // 2 + 1
    taps = 6  # 6 × 8 мс = 48 мс импульсной характеристики комнаты после GCC-PHAT выравнивания
    win = np.sqrt(np.hanning(nfft).astype(np.float32))
    n = len(mic_f)
    pad = nfft
    m_pad = np.pad(mic_f, (pad, pad))
    r_pad = np.pad(ref_f, (pad, pad))
    out_pad = np.zeros_like(m_pad)
    norm_pad = np.zeros_like(m_pad)

    W = np.zeros((taps, nbins), dtype=np.complex64)
    X_hist = np.zeros((taps, nbins), dtype=np.complex64)
    mu = np.float32(0.22)
    eps = np.float32(1e-5)

    for idx in range(0, len(m_pad) - nfft, hop):
        M = np.fft.rfft(m_pad[idx:idx + nfft] * win).astype(np.complex64)
        R = np.fft.rfft(r_pad[idx:idx + nfft] * win).astype(np.complex64)
        X_hist[1:] = X_hist[:-1]
        X_hist[0] = R

        Y = np.sum(np.conj(W) * X_hist, axis=0)
        E = M - Y

        # Защита от double-talk: адаптируем фильтр сильнее, когда референс доминирует над ошибкой.
        r_pow = np.sum(np.abs(X_hist) ** 2, axis=0) + eps
        e_pow = np.abs(E) ** 2
        m_pow = np.abs(M) ** 2 + eps
        dt_scale = np.minimum(np.float32(1.0), np.abs(Y) ** 2 / (e_pow + eps) * np.float32(0.5) + np.float32(0.15))
        step = (mu * dt_scale) / r_pow
        W += step[None, :] * X_hist * np.conj(E)[None, :]

        # Мягкий Винеровский подавитель остаточного эха (пол 0.12 — голос «Я» при перебивании сохраняется).
        y_pow = np.abs(Y) ** 2
        gain = np.clip((e_pow - np.float32(0.35) * y_pow) / (e_pow + eps), 0.12, 1.0)
        # Если в референсе тишина — пропускаем микрофон 1:1.
        quiet_ref = (np.abs(R) ** 2) < np.float32(1e-7)
        gain = np.where(quiet_ref, np.float32(1.0), gain)
        gain = np.where(m_pow < np.float32(1e-8), np.float32(1.0), gain)

        clean_blk = np.fft.irfft(E * gain, n=nfft).astype(np.float32) * win
        out_pad[idx:idx + nfft] += clean_blk
        norm_pad[idx:idx + nfft] += win * win

    norm_pad = np.maximum(norm_pad, np.float32(1e-6))
    return np.clip((out_pad / norm_pad)[pad:pad + n], -1.0, 1.0)


def cancel(mic16: Path, others16: Path, mic_offset: float, others_offset: float, out: Path,
           muted_intervals: list[list[float]] | None = None,
           backend: str = "auto", cfg: dict | None = None) -> Path | None:
    """mic16, others16 — 16 кГц моно; *_offset — начало дорожки от начала встречи, с.
    Возвращает путь к очищенному микрофону или None, если эха нет (наушники) или очистка не потребовалась."""
    import soundfile as sf

    started = time.time()
    mic, sr1 = sf.read(str(mic16), dtype="int16")
    ref, sr2 = sf.read(str(others16), dtype="int16")
    if sr1 != SR or sr2 != SR or mic.ndim != 1 or ref.ndim != 1 or not len(mic):
        return None

    base_shift = int(round((mic_offset - others_offset) * SR))
    lag, conf = estimate_delay_gcc_phat(mic, ref, initial_shift=base_shift, sr=SR)
    muted = muted_intervals or []

    # Выбираем движок: dtln → webrtc → nlms.
    chosen = backend
    if chosen == "auto":
        if dtln_available(cfg):
            chosen = "dtln"
        elif webrtc_available():
            chosen = "webrtc"
        else:
            chosen = "nlms"

    # В наушниках эха нет (GCC-PHAT не находит пика корреляции): для NLMS/DTLN не трогаем чистый микрофон.
    if conf < 5.0 and chosen in ("nlms", "dtln"):
        log.info("Эхо (GCC-PHAT): акустическое эхо не обнаружено (уверенность %.1f) — микрофон чистый", conf)
        return None

    if chosen == "dtln" and dtln_available(cfg):
        models = _dtln_models(cfg)
        assert models is not None
        shift = base_shift - lag
        mic_f = mic.astype(np.float32) / 32768.0
        ref_f = _aligned_ref(len(mic_f), ref, shift, mic_offset, muted)
        clean_f = _cancel_dtln(mic_f, ref_f, models)
        sf.write(str(out), (clean_f * 32767.0).astype(np.int16), SR, subtype="PCM_16")
        log.info("Эхо (DTLN-AEC, лаг %+.1f мс, увер. %.1f): микрофон очищен за %d с",
                 lag * 1000.0 / SR, conf, time.time() - started)
        return out

    if chosen == "webrtc" and webrtc_available():
        target_lead_ms = 20
        if conf >= 5.0:
            adj = lag - int(SR * target_lead_ms / 1000)
            shift = base_shift - adj
            delay_ms = target_lead_ms
        else:
            shift, delay_ms = base_shift, 0
        clean = _cancel_webrtc(mic, ref, shift, delay_ms, mic_offset, muted)
        sf.write(str(out), clean, SR, subtype="PCM_16")
        log.info("Эхо (WebRTC AEC3, лаг %+.1f мс, увер. %.1f): микрофон очищен за %d с",
                 lag * 1000.0 / SR, conf, time.time() - started)
        return out

    # Встроенный частотный NLMS (всегда доступен).
    shift = base_shift - lag
    mic_f = mic.astype(np.float32) / 32768.0
    ref_f = _aligned_ref(len(mic_f), ref, shift, mic_offset, muted)
    clean_f = _cancel_nlms(mic_f, ref_f)
    sf.write(str(out), (clean_f * 32767.0).astype(np.int16), SR, subtype="PCM_16")
    log.info("Эхо (NLMS, лаг %+.1f мс, увер. %.1f): микрофон очищен за %.1f с",
             lag * 1000.0 / SR, conf, time.time() - started)
    return out
