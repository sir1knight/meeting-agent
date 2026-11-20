"""Восстановление записей, прерванных сбоем: приложение упало, Mac/ПК выключился, кончилась батарея.

Пока идёт запись, в её папке лежит recording.json с pid записывающего процесса (Recorder.swift на Mac,
winrec.py на Windows), а meta.json обновляется каждые 30 секунд. Если процесса уже нет, запись прервалась:
чиним заголовки WAV (длина в них пишется только при закрытии файла), закрываем meta.json и отдаём запись
в обработку как обычную. Пока процесс жив — запись идёт, трогать её нельзя.
"""

import json
import logging
import os
import struct
import time
from pathlib import Path

from . import osutil

log = logging.getLogger(__name__)

LOCK = "recording.json"


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def is_live(folder: Path) -> bool:
    """Запись в этой папке ещё идёт (записывающий процесс жив)."""
    lock = folder / LOCK
    if not lock.exists():
        return False
    pid = _read_json(lock).get("pid")
    try:
        return bool(pid) and osutil.process_alive(int(pid))
    except (TypeError, ValueError):
        return False


def is_interrupted(folder: Path) -> bool:
    return (folder / LOCK).exists() and not is_live(folder)


def repair_wav(path: Path) -> bool:
    """Длины RIFF и data по фактическому размеру файла. True — файл пригоден."""
    try:
        size = path.stat().st_size
        if size < 64:
            return False
        with open(path, "r+b") as f:
            head = f.read(12)
            if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return False
            pos = 12
            while pos + 8 <= size:
                f.seek(pos)
                cid, clen = struct.unpack("<4sI", f.read(8))
                if cid == b"data":
                    data_len = size - (pos + 8)
                    if data_len % 2:
                        data_len -= 1
                    f.seek(pos + 4)
                    f.write(struct.pack("<I", data_len))
                    f.seek(4)
                    f.write(struct.pack("<I", pos + 8 + data_len - 8))
                    return data_len > 0
                if clen == 0 or pos + 8 + clen > size:
                    return False
                pos += 8 + clen + (clen % 2)
    except OSError as e:
        log.warning("Не удалось починить %s: %s", path, e)
    return False


def recover(folder: Path) -> bool:
    """Чинит прерванную запись. True — в ней есть звук и её можно обрабатывать."""
    ok = False
    for wav in folder.glob("*.wav"):
        ok = repair_wav(wav) or ok
    lock = _read_json(folder / LOCK)
    meta_path = folder / "meta.json"
    meta = _read_json(meta_path)
    meta.pop("in_progress", None)
    meta.setdefault("started", lock.get("started"))
    meta.setdefault("target", lock.get("target", "all"))
    meta["recovered"] = True
    osutil.atomic_write_text(meta_path, json.dumps(meta, ensure_ascii=False, indent=2))
    try:
        (folder / LOCK).unlink()
    except OSError:
        pass
    log.info("Восстановлена прерванная запись %s (%s)", folder.name, "есть звук" if ok else "пустая")
    return ok


def write_lock(folder: Path, target: str) -> None:
    """Для записи на Windows (winrec.py): тот же замок, что пишет Recorder.swift."""
    osutil.atomic_write_text(folder / LOCK, json.dumps({"pid": os.getpid(), "started": time.time(), "target": target}))


def remove_lock(folder: Path) -> None:
    try:
        (folder / LOCK).unlink()
    except OSError:
        pass
