"""Расшифровка речи. Движок выбирается в настройках (whisper.engine):
  whisper-turbo / whisper-large — Whisper: на Mac через mlx-whisper (видеоядра Apple Silicon),
    на Windows через faster-whisper (видеокарта NVIDIA, если есть, иначе процессор);
  gigaam — GigaAM-v3 (Сбер) через PyTorch: обучен на русской речи, сам ставит знаки препинания.
"""

import logging
from pathlib import Path

from . import osutil

log = logging.getLogger(__name__)

# Фразы, которые Whisper «додумывает» на тишине (наследие субтитров в обучающих данных).
HALLUCINATIONS = (
    "продолжение следует", "субтитры", "редактор субтитров", "спасибо за просмотр",
    "подписывайтесь на канал", "ставьте лайк", "dimatorzok", "amara.org",
)
SR = 16000


def transcribe(path: Path, cfg: dict, regions: list[tuple[float, float, str]] | None = None) -> list[dict]:
    from . import audio

    return transcribe_array(audio.load(path), cfg, regions, path.name)


def transcribe_array(wav, cfg: dict, regions: list[tuple[float, float, str]] | None = None,
                     name: str = "звук") -> list[dict]:
    """Без regions — вся дорожка целиком (микрофон). С regions [(начало, конец, метка)] — каждая
    реплика из разметки собеседников отдельно: в один кусок не попадают два человека,
    и у каждой реплики сразу есть метка собеседника (spk)."""
    from . import models

    engine = models.asr_engine(cfg)
    how = f"по репликам: {len(regions)}" if regions is not None else "целиком"
    log.info("Расшифровка %s (%s, %s)…", name, models.ASR_ENGINES[engine]["title"], how)
    if engine == "gigaam":
        return transcribe_gigaam(wav, cfg, regions)
    if regions is not None:
        return transcribe_whisper_regions(wav, cfg, regions)
    return transcribe_whisper(wav, cfg)


# ---------- реплики из разметки собеседников ----------

REGION_MAX = 20.0   # и GigaAM (до 25 с), и Whisper (окно 30 с) берут кусок целиком
REGION_GAP = 1.0    # реплики одного человека с паузой короче — в один кусок
REGION_MIN = 0.3


def regions_from_speakers(speakers: dict) -> list[tuple[float, float, str]]:
    """Реплики собеседников (без наложений) → куски до REGION_MAX, каждый — один человек."""
    turns = sorted((s, e, label) for label, sp in speakers.items() for s, e in sp.segments if e - s > 0.05)
    merged: list[list] = []
    for s, e, label in turns:
        last = merged[-1] if merged else None
        if last and last[2] == label and s - last[1] <= REGION_GAP and e - last[0] <= REGION_MAX:
            last[1] = max(last[1], e)
        else:
            merged.append([s, e, label])
    out = []
    for s, e, label in merged:
        if e - s < REGION_MIN:
            continue
        n = int((e - s) // REGION_MAX) + 1
        step = (e - s) / n
        out += [(s + i * step, s + (i + 1) * step, label) for i in range(n)]
    return out


def transcribe_whisper_regions(wav, cfg: dict, regions) -> list[dict]:
    prompt = _whisper_prompt(cfg)
    out = []
    for s, e, label in regions:
        piece = wav[int(s * SR):int(e * SR)]
        if len(piece) < int(REGION_MIN * SR):
            continue
        texts = [x["text"] for x in clean_segments(_whisper_raw(piece, cfg, prompt))]
        text = " ".join(t.strip() for t in texts).strip()
        if text:
            out.append({"start": round(s, 2), "end": round(e, 2), "text": text, "spk": label})
    return out


def _whisper_prompt(cfg: dict) -> str | None:
    from .participants import of
    from .voices import load_profiles

    # Имена участников (указанные вручную) и известные голоса — чтобы «Даша» не превращалась в «нашу».
    from .namefix import my_name

    names = [my_name(cfg)] + of(cfg) + [p.get("name", "") for p in load_profiles(cfg)]
    glossary = list(dict.fromkeys([n for n in names if n] + list(cfg["whisper"].get("glossary") or [])))[:60]
    return ", ".join(glossary) if glossary else None


# ---------- Whisper ----------

def transcribe_whisper(wav, cfg: dict) -> list[dict]:
    if len(wav) < SR // 2:
        return []
    return clean_segments(_whisper_raw(wav, cfg, _whisper_prompt(cfg)))


def _whisper_raw(wav, cfg: dict, prompt: str | None) -> list[dict]:
    """Сегменты Whisper как словари {start, end, text, no_speech_prob, avg_logprob, compression_ratio}."""
    from . import models

    repo = models.require(models.asr_repo(cfg), "Модель распознавания речи")
    language = cfg["whisper"].get("language") or None
    if not osutil.IS_WIN:
        import mlx_whisper

        r = mlx_whisper.transcribe(wav, path_or_hf_repo=repo, language=language,
                                   condition_on_previous_text=False, initial_prompt=prompt, verbose=None)
        return r.get("segments", [])
    def run():
        segments, _ = _faster_whisper(repo).transcribe(
            wav, language=language, beam_size=1, condition_on_previous_text=False, initial_prompt=prompt)
        return [{"start": x.start, "end": x.end, "text": x.text, "no_speech_prob": x.no_speech_prob,
                 "avg_logprob": x.avg_logprob, "compression_ratio": x.compression_ratio} for x in segments]

    try:
        return run()
    except RuntimeError as e:
        # Драйвер NVIDIA есть, а библиотек CUDA (cuBLAS, cuDNN) нет — ошибка всплывает на первой фразе.
        if _fw is None or _fw[2] != "cuda":
            raise
        log.warning("Whisper не смог работать на видеокарте (%s) — перехожу на процессор", e)
        _faster_whisper(repo, force_cpu=True)
        return run()


_fw = None  # (папка модели, модель faster-whisper, устройство)


def _faster_whisper(path: str, force_cpu: bool = False):
    """Видеокарта NVIDIA, если её библиотеки на месте, иначе процессор (int8 — вдвое быстрее и легче)."""
    global _fw
    if _fw is None or _fw[0] != path or (force_cpu and _fw[2] != "cpu"):
        import os

        from . import gpu

        gpu.activate()  # библиотеки CUDA из «Ускорения NVIDIA», если скачаны
        from faster_whisper import WhisperModel

        model, device = None, "cpu"
        if not force_cpu:
            try:
                import ctranslate2

                if ctranslate2.get_cuda_device_count() > 0:
                    try:
                        model = WhisperModel(path, device="cuda", compute_type="float16")
                    except ValueError:  # старые карты (MX150, GTX 10xx) не умеют float16 — выбор за CTranslate2
                        model = WhisperModel(path, device="cuda", compute_type="auto")
                    device = "cuda"
                    log.info("Whisper на видеокарте NVIDIA")
            except Exception as e:
                log.info("Видеокарта NVIDIA недоступна для Whisper (%s) — считаю на процессоре", e)
        if model is None:
            model = WhisperModel(path, device="cpu", compute_type="int8", cpu_threads=max(1, (os.cpu_count() or 4) - 1))
        _fw = (path, model, device)
    return _fw[1]


def free_whisper() -> None:
    global _fw
    _fw = None
    if not osutil.IS_WIN:
        try:
            import mlx.core as mx
            from mlx_whisper.transcribe import ModelHolder

            ModelHolder.model = None
            ModelHolder.model_path = None
            mx.clear_cache()
        except Exception:
            pass


def clean_segments(raw: list[dict]) -> list[dict]:
    out = []
    for s in raw:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        if s.get("no_speech_prob", 0) > 0.6 and s.get("avg_logprob", 0) < -1.0:
            continue
        if s.get("compression_ratio", 0) > 2.6:  # зацикленные повторы
            continue
        low = text.lower()
        if len(low) < 80 and any(h in low for h in HALLUCINATIONS):
            continue
        out.append({"start": float(s["start"]), "end": float(s["end"]), "text": text})
    return out


# ---------- GigaAM ----------

_gigaam = None
CHUNK_MAX = 20.0   # GigaAM принимает до 25 с за раз
JOIN_GAP = 0.4     # участки речи с паузой короче склеиваем в один кусок
BATCH = 8


def _load_gigaam(cfg: dict):
    """Собираем модель напрямую из её кода и весов. Через transformers.AutoModel нельзя:
    transformers 5.x создаёт модель на устройстве meta, и код GigaAM (рассчитанный на 4.57) падает."""
    global _gigaam
    if _gigaam is None:
        import importlib
        import json
        import sys

        import torch
        from hydra.utils import instantiate

        from . import models

        path = Path(models.require(models.asr_repo(cfg), "Модель GigaAM"))
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))  # код модели (modeling_gigaam.py) из ai-sage/GigaAM-v3 лежит рядом с весами
        importlib.import_module("modeling_gigaam")
        conf = json.loads((path / "config.json").read_text(encoding="utf-8"))["cfg"]["model"]
        decoding = conf["cfg"].get("decoding", {})
        if "model_path" in decoding:
            decoding["model_path"] = str(path / "tokenizer.model")
        core = instantiate(conf, _recursive_=False)
        state = torch.load(path / "pytorch_model.bin", map_location="cpu", weights_only=True)
        core.load_state_dict({k.removeprefix("model."): v for k, v in state.items()})
        device = osutil.torch_device().type
        try:
            core.to(device)
        except Exception:
            device = "cpu"
            core.to(device)
        core.eval()
        _gigaam = (core, device)
    return _gigaam


def free_gigaam() -> None:
    global _gigaam
    if _gigaam is not None:
        import torch

        device = _gigaam[1]
        _gigaam = None
        osutil.empty_cache(torch.device(device))


_vad = None


def speech_regions(wav, cfg: dict) -> list[tuple[float, float]]:
    return _speech_regions(wav, cfg)


def _speech_regions(wav, cfg: dict) -> list[tuple[float, float]]:
    """Где в записи речь. pyannote (если есть и скачан), иначе — по громкости."""
    from . import models

    try:
        if not models.pyannote_installed():
            raise ImportError
        import torch
        from pyannote.audio import Model
        from pyannote.audio.pipelines import VoiceActivityDetection

        from .config import hf_token

        global _vad
        if _vad is None:
            seg = Model.from_pretrained("pyannote/segmentation-3.0", token=hf_token(cfg))
            _vad = VoiceActivityDetection(segmentation=seg)
            _vad.instantiate({"min_duration_on": 0.0, "min_duration_off": 0.0})
        ann = _vad({"waveform": torch.from_numpy(wav[None].copy()), "sample_rate": SR})
        regions = [(float(s.start), float(s.end)) for s in ann.get_timeline().support()]
        if regions:
            return regions
    except Exception as e:
        log.info("Поиск речи по громкости (pyannote недоступен: %s)", type(e).__name__)
    return _energy_regions(wav)


def _energy_regions(wav) -> list[tuple[float, float]]:
    import numpy as np

    hop = int(0.03 * SR)
    frames = wav[: len(wav) // hop * hop].reshape(-1, hop)
    rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
    thr = max(np.percentile(rms, 20) * 3, 0.004)
    speech = rms > thr
    regions, start = [], None
    for i, on in enumerate(speech):
        if on and start is None:
            start = i
        elif not on and start is not None:
            if i - start >= 7:  # от 0,2 с
                regions.append((start * 0.03, i * 0.03))
            start = None
    if start is not None:
        regions.append((start * 0.03, len(speech) * 0.03))
    return regions


def _chunks(regions: list[tuple[float, float]], join_gap: float = JOIN_GAP,
            chunk_max: float = CHUNK_MAX) -> list[tuple[float, float]]:
    """Куски до chunk_max: короткие паузы склеиваем, длинную речь режем поровну."""
    merged: list[list[float]] = []
    for s, e in regions:
        if merged and s - merged[-1][1] <= join_gap and e - merged[-1][0] <= chunk_max:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    out = []
    for s, e in merged:
        n = int((e - s) // chunk_max) + 1
        step = (e - s) / n
        out += [(s + i * step, s + (i + 1) * step) for i in range(n)]
    return out


def transcribe_gigaam(wav, cfg: dict, regions=None) -> list[dict]:
    import torch

    core, device = _load_gigaam(cfg)
    if regions is not None:
        chunks = [r for r in regions if r[1] - r[0] >= REGION_MIN]
    else:
        chunks = [(s, e, None) for s, e in _chunks(_speech_regions(wav, cfg)) if e - s >= 0.3]
    dtype = getattr(core, "_dtype", torch.float32)
    out = []
    with torch.inference_mode():
        for i in range(0, len(chunks), BATCH):
            batch = chunks[i:i + BATCH]
            pieces = [torch.from_numpy(wav[int(s * SR):int(e * SR)].copy()) for s, e, _ in batch]
            lengths = torch.tensor([len(p) for p in pieces], device=device)
            x = torch.zeros(len(pieces), int(lengths.max()))
            for j, p in enumerate(pieces):
                x[j, :len(p)] = p
            x = x.to(device).to(dtype)
            encoded, enc_len = core.forward(x, lengths)
            texts = core.decoding.decode(core.head, encoded, enc_len)
            for (s, e, label), text in zip(batch, texts):
                text = (text or "").strip()
                if text:
                    seg = {"start": round(s, 2), "end": round(e, 2), "text": text}
                    if label:
                        seg["spk"] = label
                    out.append(seg)
    return out
