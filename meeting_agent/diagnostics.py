"""Экспорт обезличенной диагностики (.zip) для разбора проблем на Mac и Windows.

Собирает версии, состояние моделей, список аудиоустройств, свободное место на диске и хвост логов.
Автоматически вырезает токены/ключи, пути с именем пользователя, e-mail, адреса серверов,
названия встреч и имена участников (в кавычках «…» и после известных меток).
"""

import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from . import config, models, osutil, updates

_SECRET_RE = re.compile(
    r"(?:sk-ant-[A-Za-z0-9_-]{10,}|sk-[A-Za-z0-9_-]{15,}|hf_[A-Za-z0-9]{15,}|"
    r"glpat-[A-Za-z0-9_-]{10,}|AIza[A-Za-z0-9_-]{20,}|"
    r"(?:Bearer|Basic|PRIVATE-TOKEN)\s+[A-Za-z0-9._+/=-]{8,})",
    re.I,
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_QUOTED_RE = re.compile(r"«[^»\n]{1,120}»")
_USER_PATH_RE = re.compile(r"(?:/Users/[^/\s\"']+|C:\\Users\\[^\\\s\"']+)", re.I)


def scrub(text: str) -> str:
    """Обезличивает произвольный текст лога: пути пользователя, ключи, e-mail, имена/названия в «…»."""
    home = str(Path.home())
    if home:
        text = text.replace(home, "~")
    text = _USER_PATH_RE.sub("~", text)
    text = _SECRET_RE.sub("[REDACTED_TOKEN]", text)
    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = _QUOTED_RE.sub("«[REDACTED]»", text)
    return text


def _audio_devices() -> str:
    if not osutil.IS_WIN:
        try:
            r = subprocess.run(["/usr/sbin/system_profiler", "SPAudioDataType"],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                return scrub(r.stdout)
        except Exception:  # noqa: BLE001
            pass
        return "(system_profiler unavailable)"
    try:
        from . import winrec

        sc = winrec.soundcard()
        inited = winrec._com_init()
        try:
            lines = ["Speakers:"]
            for s in sc.all_speakers():
                lines.append(f"  - {s.name}")
            lines.append("Microphones:")
            for m in sc.all_microphones(include_loopback=False):
                lines.append(f"  - {m.name}")
            return scrub("\n".join(lines))
        finally:
            winrec._com_done(inited)
    except Exception as e:  # noqa: BLE001
        return f"(soundcard error: {type(e).__name__}: {e})"


def _safe_settings(cfg: dict) -> dict:
    """Только технические флаги настроек — без имён, словарей, URL и ключей."""
    return {
        "llm_backend": cfg.get("llm", {}).get("backend"),
        "asr_engine": models.asr_engine(cfg),
        "language": cfg.get("whisper", {}).get("language"),
        "diarization_enabled": cfg.get("diarization", {}).get("enabled"),
        "voices_enabled": cfg.get("voices", {}).get("enabled"),
        "live_enabled": cfg.get("live", {}).get("enabled"),
        "keep_audio": cfg.get("output", {}).get("keep_audio"),
        "compress_audio": cfg.get("output", {}).get("compress_audio", True),
        "auto_record": cfg.get("app", {}).get("auto_record", True),
        "auto_record_mode": cfg.get("app", {}).get("auto_record_mode", "auto"),
        "auto_platforms": cfg.get("zoom", {}).get("platforms", []),
        "echo_cancel": cfg.get("recording", {}).get("echo_cancel", True),
        "skip_continuity_mic": cfg.get("recording", {}).get("skip_continuity_mic", True),
        "recording_pill": cfg.get("recording", {}).get("pill", True),
        "aec_backend": cfg.get("recording", {}).get("aec_backend", "auto"),
    }


def _tail_bytes(path: Path, max_bytes: int = 200_000) -> str:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            raw = f.read()
        return scrub(raw.decode("utf-8", errors="replace"))
    except OSError:
        return ""


def build_zip(cfg: dict, state_permissions: dict | None = None) -> bytes:
    """Собирает обезличенный zip-архив диагностики в памяти."""
    rec_dir: Path = cfg["paths"]["recordings"]
    disk = {}
    try:
        du = shutil.disk_usage(rec_dir if rec_dir.exists() else Path.home())
        disk = {"total_gb": round(du.total / 1e9, 1), "free_gb": round(du.free / 1e9, 2)}
    except OSError:
        pass

    manifest = {}
    try:
        manifest = json.loads((config.ROOT / "version.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass

    info = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "os": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "app_version": manifest.get("app"),
        "shell_version": updates.shell_version(),
        "python_bundled": updates.python_bundled(),
        "disk": disk,
        "permissions": state_permissions or {},
        "settings": _safe_settings(cfg),
        "models_ready": {
            "whisper": models.is_ready("whisper", cfg),
            "diarization": models.is_ready("diarization", cfg),
            "llm": models.is_ready("llm", cfg),
        },
        "env_flags": {
            "MEETING_AGENT_HOME_set": bool(os.environ.get("MEETING_AGENT_HOME")),
            "HF_HUB_OFFLINE": os.environ.get("HF_HUB_OFFLINE"),
        },
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("system.json", json.dumps(info, ensure_ascii=False, indent=2))
        z.writestr("audio_devices.txt", _audio_devices())
        logs_dir = config.HOME / "logs"
        if logs_dir.is_dir():
            for name in ("backend.log", "backend.prev.log", "shell-update.log", "app.log"):
                p = logs_dir / name
                if p.exists():
                    z.writestr(f"logs/{name}", _tail_bytes(p))
    return buf.getvalue()
