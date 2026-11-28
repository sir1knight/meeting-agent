"""Разделение собеседников по голосам через pyannote (локально, после однократной загрузки модели).

Кроме разметки «кто когда говорил» возвращает для каждого собеседника эмбеддинг голоса
(для слепков голоса) и сохраняет образец его речи на 10–15 секунд.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import osutil
from .config import hf_token

log = logging.getLogger(__name__)

EMBEDDING_FALLBACK = "pyannote/wespeaker-voxceleb-resnet34-LM"
SAMPLE_MIN, SAMPLE_MAX = 10.0, 15.0     # длина образца голоса, секунды
FRAGMENT_MIN = 1.5                      # фрагменты короче в образец не берём


@dataclass
class Speaker:
    label: str                          # SPEAKER_00 …
    duration: float = 0.0               # сколько секунд говорил (без наложений)
    embedding: list[float] | None = None
    segments: list[tuple[float, float]] = field(default_factory=list)  # без наложений, для образца
    sample: str | None = None           # имя файла образца в папке voices/ встречи


def _load_pipeline(cfg: dict):
    """Пайплайн pyannote 3.1. В pyannote 4.x он по умолчанию тянет PLDA из закрытого репозитория
    speaker-diarization-community-1, хотя для агломеративной кластеризации 3.1 PLDA не нужна —
    поэтому собираем пайплайн сами из его config.yaml."""
    import yaml
    from huggingface_hub import snapshot_download
    from pyannote.audio.pipelines import SpeakerDiarization

    d = cfg["diarization"]
    token = hf_token(cfg)
    snap = snapshot_download(d["model"], token=token)
    with open(os.path.join(snap, "config.yaml"), encoding="utf-8") as f:
        conf = yaml.safe_load(f)
    params = dict(conf["pipeline"]["params"])
    try:
        from pyannote.audio.core.plda import PLDA  # есть только в pyannote 4.x
        if params.get("clustering", "AgglomerativeClustering") != "VBxClustering":
            params["plda"] = PLDA.__new__(PLDA)   # заглушка: при этой кластеризации не используется
        pipe = SpeakerDiarization(**params, token=token)
    except ImportError:
        pipe = SpeakerDiarization(**params, use_auth_token=token)
    pipe.instantiate(conf.get("params", {}))
    return pipe, params.get("embedding") or EMBEDDING_FALLBACK


def _emb_token(repo: str, cfg: dict):
    return False if repo == EMBEDDING_FALLBACK else hf_token(cfg)


def _energy_vad_chunks(mono, sr: int) -> list[tuple[float, float]]:
    """Быстрый локальный VAD по энергии сигнала с нарезкой длинных фраз на 3-секундные окна."""
    import numpy as np

    hop = max(1, int(0.03 * sr))
    n = len(mono) // hop
    if not n:
        return []
    frames = mono[: n * hop].reshape(n, hop)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    noise = float(np.percentile(rms, 20))
    thr = max(0.006, noise * 2.8)
    active = rms > thr

    raw: list[tuple[float, float]] = []
    start = None
    for i, on in enumerate(active):
        t = i * hop / sr
        if on and start is None:
            start = t
        elif not on and start is not None:
            if raw and start - raw[-1][1] < 0.35:
                raw[-1] = (raw[-1][0], t)
            else:
                raw.append((start, t))
            start = None
    if start is not None:
        t = n * hop / sr
        if raw and start - raw[-1][1] < 0.35:
            raw[-1] = (raw[-1][0], t)
        else:
            raw.append((start, t))

    chunks: list[tuple[float, float]] = []
    for s, e in raw:
        if e - s < 0.75:
            continue
        cur = s
        while e - cur > 4.0:
            chunks.append((round(cur, 3), round(cur + 3.0, 3)))
            cur += 3.0
        if e - cur >= 0.75:
            chunks.append((round(cur, 3), round(e, 3)))
        elif chunks and chunks[-1][1] == round(cur, 3):
            chunks[-1] = (chunks[-1][0], round(e, 3))
    return chunks


def _diarize_wespeaker(path: Path, cfg: dict, samples_dir: Path | None = None
                       ) -> tuple[list[tuple[float, float, str]], dict[str, Speaker]]:
    """Локальное разделение собеседников через WeSpeaker ResNet34-LM без токена Hugging Face."""
    import numpy as np
    import soundfile as sf
    import torch
    from pyannote.audio import Inference, Model
    from sklearn.cluster import AgglomerativeClustering

    d = cfg["diarization"]
    device = osutil.torch_device(d.get("device"))
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    if sr != 16000 and len(mono) > 1:
        n_new = int(round(len(mono) * 16000 / sr))
        mono = np.interp(np.linspace(0, len(mono) - 1, n_new), np.arange(len(mono)), mono).astype("float32")
        sr = 16000

    chunks = _energy_vad_chunks(mono, sr)
    if not chunks:
        return [], {}

    from . import models

    log.info("Разделение собеседников через локальный WeSpeaker (%d фрагм.)…", len(chunks))
    model = Model.from_pretrained(models.diarization_local_path(EMBEDDING_FALLBACK), token=False)
    infer = Inference(model, window="whole")
    infer.to(device)

    embs, valid = [], []
    for s, e in chunks:
        piece = mono[int(s * sr):int(e * sr)]
        if len(piece) < sr // 2:
            continue
        v = np.asarray(infer({"waveform": torch.from_numpy(piece[None].copy()), "sample_rate": sr}), dtype="float32")
        norm = float(np.linalg.norm(v))
        if np.all(np.isfinite(v)) and norm > 0:
            embs.append(v / norm)
            valid.append((s, e))

    if not valid:
        return [], {}

    if len(valid) == 1:
        labels = [0]
    else:
        X = np.vstack(embs)
        durs = np.array([e - s for s, e in valid], dtype="float32")
        min_k = int(d.get("min_speakers") or 0)
        max_k = int(d.get("max_speakers") or 0)
        from .participants import of
        listed = len(of(cfg))
        if listed >= 3 and min_k == 0:
            min_k = listed - 1

        def _cent(idxs: list[int]) -> np.ndarray:
            w = durs[idxs][:, None]
            v = (X[idxs] * w).sum(axis=0)
            return v / (float(np.linalg.norm(v)) + 1e-9)

        # Короткие реплики (< 2 с) не создают отдельные кластеры, чтобы не дробить говорящих на сотни меток.
        long_idx = [i for i, dur in enumerate(durs) if dur >= 2.0]
        if len(long_idx) < 2:
            long_idx = list(range(len(valid)))

        cl = AgglomerativeClustering(n_clusters=None, metric="cosine", linkage="average", distance_threshold=0.60)
        raw_lbls = cl.fit_predict(X[long_idx]).tolist()
        clusters: dict[int, list[int]] = {}
        for idx, lbl_id in zip(long_idx, raw_lbls):
            clusters.setdefault(int(lbl_id), []).append(idx)

        # Мелкие кластеры (< 6 с речи) и короткие фрагменты присоединяем к ближайшему крупному кластеру.
        big = {k: idxs for k, idxs in clusters.items() if float(durs[idxs].sum()) >= 6.0}
        if not big:
            big = clusters
        small = [idx for k, idxs in clusters.items() if k not in big for idx in idxs]
        long_set = set(long_idx)
        small.extend(i for i in range(len(valid)) if i not in long_set)

        big_keys = list(big.keys())
        if small:
            C_big = np.vstack([_cent(big[k]) for k in big_keys])
            for idx in small:
                best = int(np.argmax(C_big @ X[idx]))
                big[big_keys[best]].append(idx)

        # Склеиваем кластеры с близкими центроидами (расстояние < 0.45) или сверх max_k.
        merged = list(big.values())
        while len(merged) > 1:
            if min_k > 1 and len(merged) <= min_k:
                break
            C = np.vstack([_cent(idxs) for idxs in merged])
            dists = 1.0 - (C @ C.T)
            np.fill_diagonal(dists, 9.0)
            i, j = divmod(int(np.argmin(dists)), len(merged))
            over_max = max_k >= 1 and len(merged) > max_k
            if not over_max and dists[i, j] >= 0.45:
                break
            a, b = min(i, j), max(i, j)
            merged[a] = merged[a] + merged[b]
            merged.pop(b)

        if min_k > 1 and len(merged) < min_k <= len(valid):
            labels = AgglomerativeClustering(n_clusters=min_k, metric="cosine", linkage="average").fit_predict(X).tolist()
        else:
            labels = [0] * len(valid)
            for cid, idxs in enumerate(merged):
                for idx in idxs:
                    labels[idx] = cid

    # Стабильная нумерация по порядку первого появления: SPEAKER_00, SPEAKER_01…
    remap: dict[int, str] = {}
    turns: list[tuple[float, float, str]] = []
    by_spk: dict[str, list[np.ndarray]] = {}
    for (s, e), raw_lbl, vec in zip(valid, labels, embs):
        if raw_lbl not in remap:
            remap[raw_lbl] = f"SPEAKER_{len(remap):02d}"
        lbl = remap[raw_lbl]
        by_spk.setdefault(lbl, []).append(vec)
        if turns and turns[-1][2] == lbl and s - turns[-1][1] <= 0.6:
            turns[-1] = (turns[-1][0], e, lbl)
        else:
            turns.append((s, e, lbl))

    speakers: dict[str, Speaker] = {}
    for lbl, vecs in by_spk.items():
        sp_turns = [(s, e) for s, e, k in turns if k == lbl]
        mean_v = np.mean(np.vstack(vecs), axis=0)
        norm = float(np.linalg.norm(mean_v))
        emb = (mean_v / norm).round(6).tolist() if norm > 0 else None
        speakers[lbl] = Speaker(
            label=lbl,
            duration=round(sum(e - s for s, e in sp_turns), 1),
            embedding=emb,
            segments=sp_turns,
        )

    if samples_dir is not None:
        try:
            _write_samples(mono, sr, speakers, _NoOverlap(), EMBEDDING_FALLBACK, cfg, device, samples_dir)
        except Exception as e:
            log.warning("Не удалось сохранить образцы голоса: %s", e)

    del infer, model
    osutil.empty_cache(device)
    return turns, speakers


def diarize(path: Path, cfg: dict, samples_dir: Path | None = None
            ) -> tuple[list[tuple[float, float, str]], dict[str, Speaker]]:
    """Возвращает (реплики (начало, конец, метка), {метка: Speaker})."""
    from . import models

    if models.diarization_engine(cfg) == "wespeaker":
        return _diarize_wespeaker(path, cfg, samples_dir)

    import numpy as np
    import soundfile as sf
    import torch

    d = cfg["diarization"]
    try:
        pipe, embedding_name = _load_pipeline(cfg)
    except Exception as e:
        raise RuntimeError(f"Модель pyannote не загрузилась ({e}). Проверьте токен Hugging Face "
                           "и что условия моделей приняты") from e
    device = osutil.torch_device(d.get("device"))
    pipe.to(device)

    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    audio = {"waveform": torch.from_numpy(data.T.copy()), "sample_rate": sr}
    kwargs = {}
    if d.get("min_speakers"):
        kwargs["min_speakers"] = int(d["min_speakers"])
    if d.get("max_speakers"):
        kwargs["max_speakers"] = int(d["max_speakers"])
    from .participants import of
    listed = len(of(cfg))
    if listed >= 3 and "min_speakers" not in kwargs:
        # По списку участников знаем, сколько примерно голосов ждать (минус один — вдруг кто-то промолчит).
        kwargs["min_speakers"] = listed - 1
        log.info("По списку участников жду не меньше %d собеседников", listed - 1)

    log.info("Разделение собеседников по голосам…")
    try:
        result = pipe(audio, **kwargs)                       # pyannote 4.x: DiarizeOutput
        annotation = result.speaker_diarization
        exclusive = getattr(result, "exclusive_speaker_diarization", None) or annotation
        embeddings = result.speaker_embeddings
    except AttributeError:
        annotation, embeddings = pipe(audio, return_embeddings=True, **kwargs)  # pyannote 3.x
        exclusive = annotation

    turns = [(float(t.start), float(t.end), str(spk)) for t, _, spk in annotation.itertracks(yield_label=True)]
    speakers: dict[str, Speaker] = {}
    labels = annotation.labels()
    for i, label in enumerate(labels):
        sp = Speaker(label=str(label))
        timeline = exclusive.label_timeline(label) if label in exclusive.labels() else annotation.label_timeline(label)
        sp.segments = [(float(t.start), float(t.end)) for t in timeline]
        sp.duration = round(sum(e - s for s, e in sp.segments), 1)
        if embeddings is not None and i < len(embeddings):
            v = np.asarray(embeddings[i], dtype="float32")
            if np.all(np.isfinite(v)) and np.linalg.norm(v) > 0:
                sp.embedding = (v / np.linalg.norm(v)).round(6).tolist()
        speakers[sp.label] = sp

    if samples_dir is not None:
        mono = data.mean(axis=1)
        try:
            _write_samples(mono, sr, speakers, annotation, embedding_name, cfg, device, samples_dir)
        except Exception as e:  # образцы — приятное дополнение, не повод ронять обработку
            log.warning("Не удалось сохранить образцы голоса: %s", e)

    del pipe
    osutil.empty_cache(device)
    return turns, speakers


def _write_samples(mono, sr: int, speakers: dict[str, Speaker], annotation, embedding_name: str,
                   cfg: dict, device, out_dir: Path) -> None:
    """Образец 10–15 с на собеседника: фрагменты без наложений, больше всего похожие на его голос."""
    import numpy as np
    import soundfile as sf
    import torch
    from pyannote.audio import Inference, Model
    from . import models

    model = Model.from_pretrained(models.diarization_local_path(embedding_name), token=_emb_token(embedding_name, cfg))
    infer = Inference(model, window="whole")
    infer.to(device)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Время, где говорят сразу несколько человек, — в образец не берём.
    overlap = annotation.get_overlap()

    for sp in speakers.values():
        candidates = []
        for s, e in sp.segments:
            if e - s < FRAGMENT_MIN:
                continue
            # обрезаем края на 0,15 с: там часто хвосты чужих реплик
            s, e = s + 0.15, e - 0.15
            if any(o.start < e and o.end > s for o in overlap):
                continue
            candidates.append((s, e))
        if not candidates:
            continue
        candidates = sorted(candidates, key=lambda x: x[1] - x[0], reverse=True)[:25]

        ref = np.asarray(sp.embedding, dtype="float32") if sp.embedding else None
        scored = []
        for s, e in candidates:
            chunk = mono[int(s * sr):int(min(e, s + SAMPLE_MAX) * sr)]
            rms = float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0
            peak = float(np.max(np.abs(chunk))) if len(chunk) else 0.0
            if rms < 0.005 or peak > 0.995:   # тишина или перегруз
                continue
            score = 0.0
            if ref is not None and e - s >= 2.0:
                v = infer({"waveform": torch.from_numpy(chunk[None].copy()), "sample_rate": sr})
                v = np.asarray(v, dtype="float32")
                score = float(v @ ref / (np.linalg.norm(v) + 1e-9))
            # чем больше похож на голос и чем длиннее — тем лучше
            scored.append((score + 0.02 * min(e - s, 8), s, e))
        if not scored:
            continue

        picked, total = [], 0.0
        for _, s, e in sorted(scored, reverse=True):
            take = min(e - s, SAMPLE_MAX - total)
            picked.append((s, s + take))
            total += take
            if total >= SAMPLE_MIN:
                break
        picked.sort()
        gap = np.zeros(int(0.25 * sr), dtype="float32")
        parts = []
        for s, e in picked:
            parts += [mono[int(s * sr):int(e * sr)], gap]
        audio = np.concatenate(parts[:-1])
        peak = float(np.max(np.abs(audio))) or 1.0
        audio = audio * min(1.0, 0.9 / peak)    # без тихих образцов
        name = f"{sp.label}.wav"
        sf.write(str(out_dir / name), audio, sr, subtype="PCM_16")
        sp.sample = name


class _NoOverlap:
    """Заглушка разметки для _write_samples, когда реплики заданы вручную (наложения не известны)."""

    def get_overlap(self):
        return []


def refresh_speakers(path: Path, intervals: dict[str, list[tuple[float, float]]], cfg: dict,
                     samples_dir: Path) -> dict[str, Speaker]:
    """Слепок голоса и образец для собеседников по их репликам — после ручного разделения
    (реплики перенесли к другому или новому собеседнику), без повторного разделения всей записи."""
    import numpy as np
    import soundfile as sf
    import torch
    from pyannote.audio import Inference, Model
    from . import models

    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    device = osutil.torch_device(cfg["diarization"].get("device"))
    infer = Inference(Model.from_pretrained(models.diarization_local_path(EMBEDDING_FALLBACK), token=False), window="whole")
    infer.to(device)
    speakers: dict[str, Speaker] = {}
    for label, segs in intervals.items():
        sp = Speaker(label=label, segments=sorted(segs))
        sp.duration = round(sum(e - s for s, e in sp.segments), 1)
        pieces = [mono[int(s * sr):int(e * sr)] for s, e in sp.segments if e - s >= 0.5]
        if pieces:
            x = np.concatenate(pieces)[: sr * 60]
            if len(x) >= sr:  # меньше секунды речи — слепок бессмысленен
                v = np.asarray(infer({"waveform": torch.from_numpy(x[None].copy()), "sample_rate": sr}), dtype="float32")
                if np.all(np.isfinite(v)) and np.linalg.norm(v) > 0:
                    sp.embedding = (v / np.linalg.norm(v)).round(6).tolist()
        speakers[label] = sp
    try:
        _write_samples(mono, sr, speakers, _NoOverlap(), EMBEDDING_FALLBACK, cfg, device, samples_dir)
    except Exception as e:
        log.warning("Не удалось сохранить образцы голоса: %s", e)
    return speakers


def refine(path: Path, segments: list[dict], speakers: dict[str, Speaker], cfg: dict) -> set[str]:
    """Уточнение разметки: реплика, голос которой явно ближе к другому собеседнику встречи,
    переходит к нему. pyannote любит складывать короткие обрывки разных людей в отдельный
    маленький «собеседник». Меняет segments[*]["spk"]; возвращает затронутые метки."""
    import numpy as np
    import soundfile as sf
    import torch
    from pyannote.audio import Inference, Model
    from . import models

    d = cfg["diarization"]
    min_len, min_sim, margin = float(d.get("refine_min_seconds", 1.5)), float(d.get("refine_min_sim", 0.5)), \
        float(d.get("refine_margin", 0.15))
    cent = {k: np.asarray(sp.embedding, dtype="float32") for k, sp in speakers.items() if sp.embedding}
    if len(cent) < 2:
        return set()
    mono, sr = sf.read(str(path), dtype="float32", always_2d=True)
    mono = mono.mean(axis=1)
    device = osutil.torch_device(d.get("device"))
    infer = Inference(Model.from_pretrained(models.diarization_local_path(EMBEDDING_FALLBACK), token=False), window="whole")
    infer.to(device)
    changed: set[str] = set()
    moved = 0
    for s in segments:
        own = s.get("spk")
        if own not in cent or s["end"] - s["start"] < min_len:
            continue
        piece = mono[int(s["start"] * sr):int(s["end"] * sr)]
        v = np.asarray(infer({"waveform": torch.from_numpy(piece[None].copy()), "sample_rate": sr}), dtype="float32")
        if not np.all(np.isfinite(v)) or not np.linalg.norm(v):
            continue
        v /= np.linalg.norm(v)
        sims = {k: float(v @ c) for k, c in cent.items()}
        best = max((k for k in sims if k != own), key=sims.get)
        if sims[best] >= min_sim and sims[best] - sims[own] >= margin:
            s["spk"] = best
            changed |= {own, best}
            moved += 1
    if moved:
        log.info("Уточнение разметки: %d реплик перешли к другим собеседникам", moved)
    return changed
