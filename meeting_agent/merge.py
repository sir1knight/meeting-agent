"""Склейка двух дорожек (вы / собеседники) в одну расшифровку с подписями."""

from difflib import SequenceMatcher


def assign_speakers(segments: list[dict], turns: list[tuple[float, float, str]],
                    prefix: str) -> dict[str, str]:
    """Каждому фрагменту Whisper — спикер pyannote с наибольшим пересечением по времени.
    В репликах: spk — метка pyannote, speaker — «Собеседник N». Возвращает {метка: «Собеседник N»}."""
    mapping: dict[str, str] = {}
    for s in sorted(segments, key=lambda x: x["start"]):
        best, best_overlap = None, 0.0
        for a, b, spk in turns:
            overlap = min(s["end"], b) - max(s["start"], a)
            if overlap > best_overlap:
                best, best_overlap = spk, overlap
        if best is None and turns:  # нет пересечения — берём ближайший кусок в пределах секунды
            mid = (s["start"] + s["end"]) / 2
            dist, spk = min((min(abs(mid - a), abs(mid - b)), spk) for a, b, spk in turns)
            best = spk if dist <= 1.0 else None
        if best is None:
            s["speaker"] = prefix
            continue
        if best not in mapping:
            mapping[best] = f"{prefix} {len(mapping) + 1}"
        s["speaker"] = mapping[best]
        s["spk"] = best
    if len(mapping) <= 1:  # один собеседник — номер не нужен
        for s in segments:
            s["speaker"] = prefix
        mapping = {k: prefix for k in mapping}
    return mapping


def drop_echo(mine: list[dict], others: list[dict], tolerance: float = 1.5,
              similarity: float = 0.6) -> list[dict]:
    """Убирает из микрофона голоса собеседников, попавшие туда из динамиков."""
    kept = []
    for m in mine:
        echo = False
        for o in others:
            if o["start"] > m["end"] + tolerance:
                break
            if o["end"] < m["start"] - tolerance:
                continue
            if SequenceMatcher(None, m["text"].lower(), o["text"].lower()).ratio() >= similarity:
                echo = True
                break
        if not echo:
            kept.append(m)
    return kept


def merge_tracks(mine: list[dict], others: list[dict], me_offset: float = 0.0,
                 others_offset: float = 0.0) -> list[dict]:
    for s in mine:
        s["start"] += me_offset
        s["end"] += me_offset
    for s in others:
        s["start"] += others_offset
        s["end"] += others_offset
    others = sorted(others, key=lambda x: x["start"])
    mine = drop_echo(sorted(mine, key=lambda x: x["start"]), others)
    return join_consecutive(sorted(mine + others, key=lambda x: x["start"]))


def join_consecutive(segments: list[dict], max_gap: float = 1.5, max_len: int = 600) -> list[dict]:
    out: list[dict] = []
    for s in segments:
        prev = out[-1] if out else None
        if (prev and prev["speaker"] == s["speaker"] and s["start"] - prev["end"] <= max_gap
                and len(prev["text"]) + len(s["text"]) < max_len):
            prev["text"] += " " + s["text"]
            prev["end"] = max(prev["end"], s["end"])
        else:
            out.append(dict(s))
    return out


def ts(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def to_text(segments: list[dict]) -> str:
    return "\n".join(f"[{ts(s['start'])}] {s['speaker']}: {s['text']}" for s in segments)


def to_markdown(segments: list[dict]) -> str:
    return "\n\n".join(f"**[{ts(s['start'])}] {s['speaker']}:** {s['text']}" for s in segments)
