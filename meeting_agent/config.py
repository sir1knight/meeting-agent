import json
import os
import tomllib
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECORDER_BIN = ROOT / "bin" / "zoomrec"
TEMPLATES_DIR = ROOT / "templates"
# Изменяемые данные: настройки из окна приложения, ключи, скачанные модели.
# Приложение задаёт ~/Library/Application Support/Meeting Agent, из терминала — папка проекта.
HOME = Path(os.environ.get("MEETING_AGENT_HOME") or ROOT)
SETTINGS_FILE = HOME / "settings.json"
MODELS_DIR = HOME / "models"
SECRET_FILES = {"hf_token": ".hf_token", "anthropic_key": ".anthropic_key", "gemini_key": ".gemini_key",
                "openai_key": ".openai_key", "gigachat_key": ".gigachat_key"}

DEFAULTS = {
    "paths": {"recordings": "~/MeetingAgent/recordings", "notes": "~/MeetingAgent/notes",
              "voices": "~/MeetingAgent/voices"},
    "zoom": {
        "bundle_id": "us.zoom.xos",
        "window_titles": ["Zoom Meeting", "Zoom Webinar", "Конференция Zoom", "Вебинар Zoom"],
        "poll_seconds": 3,
        "start_after_polls": 2,
        "stop_grace_seconds": 30,
        "min_duration_seconds": 60,
        # Автозапись: zoom — по заголовку окна; teams, telemost — программа пишет микрофон;
        # meet — браузер пишет микрофон и открыта вкладка звонка (Google Meet, Телемост, Teams в браузере).
        "platforms": ["zoom", "teams", "telemost", "meet"],
    },
    "speakers": {"me": "Я", "others_prefix": "Собеседник"},
    "whisper": {"engine": "whisper-turbo", "model": "mlx-community/whisper-large-v3-turbo", "language": "ru",
                "glossary": []},
    "diarization": {
        "enabled": True,
        "model": "pyannote/speaker-diarization-3.1",
        "hf_token": "",
        "device": "mps",
        "min_speakers": 0,
        "max_speakers": 0,
        "refine": True,
        "refine_min_seconds": 1.5,
        "refine_min_sim": 0.5,
        "refine_margin": 0.15,
    },
    "voices": {
        "enabled": True,
        "match_threshold": 0.75,
        "uncertain_threshold": 0.55,
        "min_speech_seconds": 15,
        "method": "max",
        "max_embeddings": 20,
        "participant_bonus": 0.10,
    },
    "names": {"guess": True},
    "live": {"enabled": True},
    "recording": {"echo_cancel": True, "pill": True, "skip_continuity_mic": True},
    "app": {"auto_record": True, "auto_record_mode": "auto", "show_in_tray": True,
            "minimize_to_tray": True, "launch_at_login": False},
    "ask": {"backend": "", "gemini_model": "gemini-3.8-flash"},
    "llm": {
        "backend": "local",
        "model": "mlx-community/Qwen2.5-7B-Instruct-4bit",
        "max_tokens": 3000,
        "temperature": 0.2,
        "chunk_chars": 12000,
    },
    "claude": {"code_model": "", "model": "claude-opus-5-5", "effort": "medium", "api_key": ""},
    "gemini": {"model": "gemini-3.8-flash"},
    "openai": {"model": "gpt-5", "api_key": ""},
    "gigachat": {"model": "GigaChat-2", "scope": "GIGACHAT_API_PERS", "api_key": ""},
    "output": {"template": "meeting", "keep_audio": True, "compress_audio": True, "notify": True},
}


def _merge(base: dict, extra: dict) -> dict:
    out = deepcopy(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: str | Path | None = None) -> dict:
    """DEFAULTS ← config.toml ← settings.json (то, что поменяли в окне приложения)."""
    path = Path(path) if path else ROOT / "config.toml"
    cfg = deepcopy(DEFAULTS)
    if path.exists():
        with open(path, "rb") as f:
            cfg = _merge(cfg, tomllib.load(f))
    cfg = _merge(cfg, user_settings())
    for key in ("recordings", "notes", "voices"):
        cfg["paths"][key] = Path(os.path.expanduser(cfg["paths"][key]))
    return cfg


def user_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def save_user_settings(changes: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    data = _merge(user_settings(), changes)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)


def read_secret(name: str) -> str | None:
    for base in (HOME, ROOT):
        f = base / SECRET_FILES[name]
        if f.exists():
            value = f.read_text().strip()
            if value:
                return value
    return None


def write_secret(name: str, value: str) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    f = HOME / SECRET_FILES[name]
    if not value:
        f.unlink(missing_ok=True)
        return
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(value)


def hf_token(cfg: dict) -> str | None:
    return cfg["diarization"].get("hf_token") or os.environ.get("HF_TOKEN") or read_secret("hf_token")


def anthropic_key(cfg: dict) -> str | None:
    return (cfg.get("claude", {}).get("api_key") or os.environ.get("ANTHROPIC_API_KEY")
            or read_secret("anthropic_key"))


def openai_key(cfg: dict) -> str | None:
    return (cfg.get("openai", {}).get("api_key") or os.environ.get("OPENAI_API_KEY")
            or read_secret("openai_key"))


def gigachat_key(cfg: dict) -> str | None:
    return (cfg.get("gigachat", {}).get("api_key") or os.environ.get("GIGACHAT_CREDENTIALS")
            or os.environ.get("GIGACHAT_API_KEY") or read_secret("gigachat_key"))
