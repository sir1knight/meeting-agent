"""Ручные правки встречи: текст протокола целиком и «найти и заменить» по всей встрече
(протокол, расшифровка, версии «Для кого?», название). Перед каждой правкой — снимок для отмены
(один шаг назад): <запись>/.undo/ — заметка, segments.json и версии.
"""

import json
import re
import shutil
from pathlib import Path

from . import osutil, versions, voices

UNDO = ".undo"
_TR = "\n## Расшифровка\n"


def _split_note(text: str) -> tuple[str, str, str, str]:
    """Заметка → (шапка до заголовка включительно, заголовок, протокол, хвост «---» + расшифровка)."""
    m = re.search(r"^# (.+)$", text, re.M)
    if not m:
        raise ValueError("В заметке нет заголовка — правка невозможна")
    head, rest = text[:m.end()], text[m.end():]
    summary, sep, tail = rest.partition(_TR)
    summary = summary.rstrip()
    trailer = ""
    if summary.endswith("---"):
        summary, trailer = summary[:-3].rstrip(), "---"
    return head, m.group(1), summary.strip(), (f"\n\n{trailer}\n" if trailer else "\n") + (sep + tail if sep else "")


def _join(head: str, summary: str, tail: str) -> str:
    return f"{head}\n\n{summary.strip()}\n{tail}"


def snapshot(note: Path, work: Path | None) -> None:
    d = (work or note.parent) / UNDO if work else None
    if not d:
        return
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    shutil.copy2(note, d / "note.md")
    (d / "note.json").write_text(json.dumps({"path": str(note)}), encoding="utf-8")
    if (work / voices.SEGMENTS_FILE).exists():
        shutil.copy2(work / voices.SEGMENTS_FILE, d / voices.SEGMENTS_FILE)
    if (work / versions.DIR).is_dir():
        shutil.copytree(work / versions.DIR, d / versions.DIR)


def can_undo(work: Path | None) -> bool:
    return bool(work and (work / UNDO / "note.md").exists())


def undo(note: Path, work: Path) -> None:
    d = work / UNDO
    if not (d / "note.md").exists():
        raise ValueError("Отменять нечего")
    osutil.atomic_write_text(note, (d / "note.md").read_text(encoding="utf-8"))
    if (d / voices.SEGMENTS_FILE).exists():
        shutil.copy2(d / voices.SEGMENTS_FILE, work / voices.SEGMENTS_FILE)
        (work / "transcript.md").unlink(missing_ok=True)
    if (d / versions.DIR).is_dir():
        shutil.rmtree(work / versions.DIR, ignore_errors=True)
        shutil.copytree(d / versions.DIR, work / versions.DIR)
    shutil.rmtree(d, ignore_errors=True)


def set_summary(note: Path, work: Path | None, text: str) -> None:
    """Протокол целиком, как его поправил пользователь (Markdown)."""
    text = text.replace("\r", "").strip()
    if not text:
        raise ValueError("Протокол пустой — удалите встречу, если она не нужна")
    if work:
        snapshot(note, work)
    head, _, _, tail = _split_note(note.read_text(encoding="utf-8"))
    osutil.atomic_write_text(note, _join(head, text, tail))


def _pattern(find: str, whole_word: bool, case: bool) -> re.Pattern:
    body = re.escape(find)
    if whole_word:
        body = rf"(?<![\w-]){body}(?![\w-])"
    return re.compile(body, 0 if case else re.IGNORECASE)


def _sub(pat: re.Pattern, repl: str, text: str) -> tuple[str, int]:
    def keep_case(m: re.Match) -> str:  # «илья» → «ильяс», «Илья» → «Ильяс», «ИЛЬЯ» → «ИЛЬЯС»
        s = m.group(0)
        if s.isupper() and len(s) > 1:
            return repl.upper()
        if s[:1].isupper():
            return repl[:1].upper() + repl[1:]
        if s[:1].islower() and repl[:1].isupper() and not repl.isupper():
            return repl  # в замене имя с большой буквы — так и пишем
        return repl
    return pat.subn(keep_case, text)


def replace(note: Path, work: Path | None, find: str, repl: str, whole_word: bool = True,
            case: bool = False) -> dict:
    """«Найти и заменить» по всей встрече: название, протокол, реплики расшифровки и версии.
    Имена собеседников в подписях меняются через «Участники» (там же слепок голоса)."""
    find, repl = find.strip(), repl.strip()
    if not find:
        raise ValueError("Укажите, что заменить")
    if find == repl:
        raise ValueError("Текст замены совпадает с исходным")
    pat = _pattern(find, whole_word, case)
    if work:
        snapshot(note, work)
    counts = {"protocol": 0, "transcript": 0, "versions": 0}

    head, title, summary, tail = _split_note(note.read_text(encoding="utf-8"))
    new_title, n_title = _sub(pat, repl, title)
    summary, n = _sub(pat, repl, summary)
    counts["protocol"] = n + n_title
    if n_title:
        head = head[: len(head) - len(title)] + new_title
    if work and (work / voices.SEGMENTS_FILE).exists():
        data = json.loads((work / voices.SEGMENTS_FILE).read_text(encoding="utf-8"))
        for seg in data.get("segments", []):
            seg["text"], k = _sub(pat, repl, seg.get("text", ""))
            if k:
                seg["edited"] = True
                counts["transcript"] += k
        if counts["transcript"]:
            osutil.atomic_write_text(work / voices.SEGMENTS_FILE, json.dumps(data, ensure_ascii=False))
    else:  # старая встреча без segments.json — правим расшифровку прямо в заметке
        tail, counts["transcript"] = _sub(pat, repl, tail)
    osutil.atomic_write_text(note, _join(head, summary, tail))
    if work and counts["transcript"] and (work / voices.SEGMENTS_FILE).exists():
        voices.rewrite_transcript(work, note)
    if work:
        for v in versions.list_all(work):
            text, k = _sub(pat, repl, v.get("text", ""))
            t2, k2 = _sub(pat, repl, v.get("title", ""))
            if k or k2:
                v.update(text=text, title=t2)
                osutil.atomic_write_text(work / versions.DIR / f"{v['id']}.json", json.dumps(v, ensure_ascii=False))
                counts["versions"] += k + k2
    counts["total"] = sum(counts.values())
    if not counts["total"] and work:
        shutil.rmtree(work / UNDO, ignore_errors=True)  # ничего не поменялось — и отменять нечего
    return counts
