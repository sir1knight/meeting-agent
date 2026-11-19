"""Локальный сервер для окна приложения: список встреч, заметки, настройки, очередь обработки.

Слушает только 127.0.0.1. Запросы к /api/ принимаются лишь с секретным токеном,
который приложение передаёт при запуске: так другие сайты в браузере не могут им управлять.
"""

import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import config, models, osutil, participants, pipeline, voices
from .summarize import claude_cli

log = logging.getLogger(__name__)
UI_DIR = config.ROOT / "ui"
STAGES = {"transcribe": "Расшифровка", "diarize": "Разделение собеседников",
          "refine": "Уточняю, кто что сказал", "voices": "Узнаю голоса", "names": "Угадываю имена", "summarize": "Составление протокола"}
BACKENDS = {"local": "На этом Mac (Qwen)", "claude-code": "Claude по подписке (Claude Code)",
            "claude": "Claude API по ключу", "gemini": "Gemini API по ключу",
            "openai": "ChatGPT (OpenAI API) по ключу"}


# ---------- очередь обработки ----------

class Jobs:
    """Встречи обрабатываются по одной, каждая — в отдельном процессе (модели освобождают память)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.items: list[dict] = []
        self.wake = threading.Event()
        self.before_run = None  # ждать, пока распознавание по ходу встречи закончит свою часть
        threading.Thread(target=self._loop, daemon=True).start()

    def add(self, folder: Path, template: str | None = None, llm: str | None = None,
            retranscribe: bool = False, asr: str | None = None) -> dict:
        folder = folder.resolve()
        with self.lock:
            for j in self.items:  # одну и ту же встречу дважды в очередь не ставим
                if j["folder"] == str(folder) and j["status"] in ("queued", "running"):
                    return j
            job = {"id": uuid.uuid4().hex[:8], "folder": str(folder), "template": template,
                   "llm": llm, "retranscribe": retranscribe, "asr": asr, "status": "queued", "stage": None,
                   "error": None, "note": None, "added": time.time()}
            self.items.append(job)
            self.items = [j for j in self.items if j["status"] != "done" or time.time() - j["added"] < 3600]
        self.wake.set()
        return job

    def snapshot(self) -> list[dict]:
        with self.lock:
            return [dict(j, stage_title=STAGES.get(j["stage"] or "", "")) for j in self.items]

    def dismiss(self, job_id: str) -> None:
        with self.lock:
            self.items = [j for j in self.items if j["id"] != job_id or j["status"] == "running"]

    def _next(self) -> dict | None:
        with self.lock:
            return next((j for j in self.items if j["status"] == "queued"), None)

    def _loop(self) -> None:
        while True:
            job = self._next()
            if not job:
                self.wake.wait(5)
                self.wake.clear()
                continue
            self._run(job)

    def _run(self, job: dict) -> None:
        job["status"] = "running"
        if self.before_run:
            self.before_run(job["folder"])
        cmd = [sys.executable, "-m", "meeting_agent", "process", job["folder"]]
        if job["template"]:
            cmd += ["--template", job["template"]]
        if job["llm"]:
            cmd += ["--llm", job["llm"]]
        if job["retranscribe"]:
            cmd += ["--retranscribe"]
            if job.get("asr"):
                cmd += ["--asr", job["asr"]]
        log.info("Обработка %s", job["folder"])
        proc = subprocess.Popen(cmd, cwd=config.ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, **osutil.popen_kwargs())
        tail: list[str] = []
        for line in proc.stdout:
            line = line.rstrip()
            log.info("  %s", line)
            m = re.search(r"@stage (\w+)", line)
            if m:
                job["stage"] = m.group(1)
            m = re.search(r"Заметка готова: (.+\.md)$", line)
            if m:
                job["note"] = Path(m.group(1)).name
            tail = (tail + [line])[-30:]
        rc = proc.wait()
        if rc == 0:
            job["status"] = "done"
        else:
            job["status"] = "error"
            job["error"] = _error_from(tail) or f"обработка завершилась с кодом {rc}"


def _error_from(lines: list[str]) -> str | None:
    for line in reversed(lines):
        # «RuntimeError: …», «huggingface_hub.errors.LocalEntryNotFoundError: …»
        m = re.match(r"^(?:[\w.]+\.)?\w*(?:Error|Exception)\w*: (.+)$", line.strip())
        if m:
            return m.group(1)
    return None


# ---------- встречи и заметки ----------

def _front_matter(text: str) -> tuple[dict, str]:
    meta = {}
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end > 0:
            for line in text[4:end].splitlines():
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
            text = text[end + 5:]
    return meta, text


def _note_info(path: Path, full: bool = False) -> dict:
    meta, body = _front_matter(path.read_text(encoding="utf-8"))
    m = re.search(r"^# (.+)$", body, re.M)
    info = {"id": path.name, "title": m.group(1).strip() if m else path.stem,
            "date": meta.get("date", ""), "duration": meta.get("duration", ""),
            "participants": meta.get("participants", ""), "template": meta.get("template", ""),
            "llm": meta.get("llm", ""), "asr": meta.get("asr", ""), "source": meta.get("source", "")}
    if full:
        if m:
            body = body[m.end():]
        summary, _, transcript = body.partition("\n## Расшифровка\n")
        info["summary"] = summary.strip().removesuffix("---").strip()
        info["transcript"] = transcript.strip()
        src = Path(info["source"]) if info["source"] else None
        info["has_audio"] = bool(src and src.is_dir()
                                 and (pipeline.find_track(src, "mic") or pipeline.find_track(src, "others")))
        info["speakers"] = _meeting_speakers(src) if src and src.is_dir() else []
        info["segments"], info["protocol_stale"] = _meeting_segments(src) if src and src.is_dir() else ([], False)
        err = src / pipeline.PROTOCOL_ERROR if src and src.is_dir() else None
        info["protocol_error"] = err.read_text(encoding="utf-8").strip() if err and err.exists() else ""
        info["expected"] = participants.read(src) if src and src.is_dir() else []
    return info


def _meeting_speakers(work: Path) -> list[dict]:
    cfg = config.load()
    profiles = {p["id"]: p for p in voices.load_profiles(cfg)}
    out = []
    for label, sp in voices.read_speakers(work).get("speakers", {}).items():
        prof = profiles.get(sp.get("voice_id") or "", {})
        out.append({"label": label, "display": voices.display_name(sp), "base": sp.get("base", ""),
                    "name": sp.get("name", ""), "voice_id": sp.get("voice_id"),
                    "guess": "" if sp.get("name") else sp.get("guess", ""),
                    "guess_evidence": sp.get("guess_evidence", ""),
                    "uncertain": bool(sp.get("uncertain")), "auto": bool(sp.get("auto")),
                    "score": sp.get("score"), "duration": sp.get("duration", 0),
                    "has_embedding": bool(sp.get("embedding")),
                    "has_sample": bool(sp.get("sample") and (work / voices.SAMPLES_DIR / sp["sample"]).exists()),
                    "email": prof.get("email", ""), "telegram": prof.get("telegram", "")})

    def order(x):
        m = re.search(r"(\d+)$", x["base"])
        return int(m.group(1)) if m else 0
    return sorted(out, key=order)


def _meeting_segments(work: Path) -> tuple[list[dict], bool]:
    """Реплики для вкладки «Расшифровка» (с метками собеседников — для ручного разделения)."""
    try:
        segs = json.loads((work / "segments.json").read_text(encoding="utf-8"))["segments"]
    except (OSError, ValueError, KeyError):
        return [], False
    segs = voices.apply_names(segs, work)
    out = [{"i": i, "start": s["start"], "speaker": s.get("speaker", ""), "label": s.get("spk"),
            "text": s.get("text", "")} for i, s in enumerate(segs)]
    return out, bool(voices.read_speakers(work).get("protocol_stale"))


def voices_status(cfg: dict) -> tuple[bool, str]:
    """Можно ли пользоваться слепками голоса и что сделать, если нельзя."""
    if not models.pyannote_installed():
        return False, ("В этой сборке нет модуля разделения собеседников (pyannote). "
                       "Пересоберите приложение: ./build_app.sh — пакеты из requirements-diarization.txt входят в сборку.")
    if not cfg["diarization"].get("enabled"):
        return False, "Включите «Разделять собеседников по голосам» в Настройках."
    if not config.hf_token(cfg):
        return False, ("Нужен бесплатный токен Hugging Face: Настройки → Разделение собеседников. "
                       "Он нужен один раз, чтобы скачать модель; дальше всё работает без интернета.")
    if not models.is_ready("diarization", cfg):
        return False, "Скачайте модель разделения собеседников: Настройки → Модели."
    if not cfg["voices"].get("enabled", True):
        return False, "Слепки голоса выключены в config.toml ([voices] enabled = false)."
    return True, ""


def _note_path(cfg: dict, note_id: str) -> Path:
    path = (cfg["paths"]["notes"] / note_id).resolve()
    if path.parent != cfg["paths"]["notes"].resolve() or path.suffix != ".md" or not path.exists():
        raise LookupError("Встреча не найдена")
    return path


def list_meetings(cfg: dict) -> list[dict]:
    notes_dir: Path = cfg["paths"]["notes"]
    out = []
    if notes_dir.exists():
        for p in notes_dir.glob("*.md"):
            try:
                out.append(_note_info(p))
            except (OSError, UnicodeDecodeError):
                continue
    out.sort(key=lambda x: (x["date"], x["id"]), reverse=True)
    return out


# ---------- настройки ----------

def get_settings(cfg: dict) -> dict:
    from . import ask as ask_mod

    templates = []
    for t in sorted(config.TEMPLATES_DIR.glob("*.md")):
        templates.append({"id": t.stem, "title": TEMPLATE_TITLES.get(t.stem, t.stem)})
    return {
        "llm_backend": cfg["llm"].get("backend", "local"),
        "backends": BACKENDS,
        "asr_engine": models.asr_engine(cfg),
        "asr_engines": {k: {"title": v["title"], "desc": v["desc"], "size": v["size"],
                            "ready": models.engine_ready(cfg, k)} for k, v in models.ASR_ENGINES.items()},
        "template": cfg["output"]["template"],
        "templates": templates,
        "glossary": cfg["whisper"].get("glossary", []),
        "language": cfg["whisper"].get("language", "ru"),
        "keep_audio": cfg["output"].get("keep_audio", True),
        "diarization_enabled": cfg["diarization"].get("enabled", True),
        "voices_enabled": cfg["voices"].get("enabled", True),
        "names_guess": cfg["names"].get("guess", True),
        "live_enabled": cfg.get("live", {}).get("enabled", True),
        "ask_backend": ask_mod.backend(cfg),
        "ask_backends": ask_mod.BACKENDS,
        "gemini_key_set": bool(config.read_secret("gemini_key")),
        "gemini_model": cfg.get("gemini", {}).get("model", "gemini-2.5-flash"),
        "openai_key_set": bool(config.openai_key(cfg)),
        "openai_model": cfg.get("openai", {}).get("model", "gpt-5"),
        "voices_thresholds": {k: cfg["voices"][k] for k in ("match_threshold", "uncertain_threshold",
                                                             "min_speech_seconds")},
        "window_titles": cfg["zoom"]["window_titles"],
        "zoom": {k: cfg["zoom"][k] for k in ("bundle_id", "poll_seconds", "start_after_polls",
                                            "stop_grace_seconds", "min_duration_seconds")},
        "auto_record": cfg.get("app", {}).get("auto_record", True),
        "claude_code_path": claude_cli(),
        "claude_code_model": cfg["claude"].get("code_model", ""),
        "claude_model": cfg["claude"].get("model", "claude-opus-5-5"),
        "hf_token_set": bool(config.hf_token(cfg)),
        "anthropic_key_set": bool(config.anthropic_key(cfg)),
        "notes_dir": str(cfg["paths"]["notes"]),
        "platform": "windows" if osutil.IS_WIN else "mac",
        "recordings_dir": str(cfg["paths"]["recordings"]),
    }


TEMPLATE_TITLES = {"meeting": "Рабочая встреча", "standup": "Стендап",
                   "client": "Встреча с партнёром", "interview": "Custdev-интервью"}


def save_settings(data: dict) -> None:
    changes: dict = {}

    def put(section: str, key: str, value):
        changes.setdefault(section, {})[key] = value

    if data.get("llm_backend") in BACKENDS:
        put("llm", "backend", data["llm_backend"])
    from .ask import BACKENDS as ASK_BACKENDS
    if data.get("ask_backend") in ASK_BACKENDS:
        put("ask", "backend", data["ask_backend"])
    if data.get("asr_engine") in models.ASR_ENGINES:
        put("whisper", "engine", data["asr_engine"])
    if "template" in data and (config.TEMPLATES_DIR / f"{data['template']}.md").exists():
        put("output", "template", data["template"])
    if isinstance(data.get("glossary"), list):
        put("whisper", "glossary", [str(x).strip() for x in data["glossary"] if str(x).strip()][:200])
    if isinstance(data.get("window_titles"), list):
        put("zoom", "window_titles", [str(x).strip() for x in data["window_titles"] if str(x).strip()])
    for key, (section, name) in {"keep_audio": ("output", "keep_audio"),
                                 "diarization_enabled": ("diarization", "enabled"),
                                 "auto_record": ("app", "auto_record"),
                                 "voices_enabled": ("voices", "enabled"),
                                 "names_guess": ("names", "guess"),
                                 "live_enabled": ("live", "enabled")}.items():
        if isinstance(data.get(key), bool):
            put(section, name, data[key])
    if isinstance(data.get("claude_code_model"), str):
        put("claude", "code_model", data["claude_code_model"].strip())
    for key, section in {"gemini_model": "gemini", "openai_model": "openai"}.items():
        if isinstance(data.get(key), str) and data[key].strip():
            put(section, "model", data[key].strip())
    if changes:
        config.save_user_settings(changes)
    for secret in ("hf_token", "anthropic_key", "gemini_key", "openai_key"):
        if isinstance(data.get(secret), str):
            value = re.sub(r"\s+", "", data[secret])
            prefix = {"hf_token": "hf_", "anthropic_key": "sk-ant-", "openai_key": "sk-"}.get(secret, "")
            if value and secret == "gemini_key" and len(value) < 20:
                raise ValueError("Ключ Gemini API слишком короткий — скопируйте его целиком из aistudio.google.com")
            if value and secret == "openai_key" and value.startswith("sk-ant-"):
                raise ValueError("Это ключ Claude API, а не OpenAI — ключ OpenAI берётся на platform.openai.com")
            if value and prefix and (not value.startswith(prefix) or value.count(prefix) > 1):
                raise ValueError(f"Это не похоже на ключ: он должен начинаться с {prefix} "
                                 "и быть вставлен один раз")
            config.write_secret(secret, value)


def check_hf(cfg: dict) -> dict:
    """Проверяет токен Hugging Face и доступ к моделям pyannote.
    Прямыми запросами: у сервера включён HF_HUB_OFFLINE, и huggingface_hub в сеть не ходит."""
    import requests

    token = config.hf_token(cfg)
    if not token:
        return {"ok": False, "message": "Токен не введён"}
    headers = {"Authorization": f"Bearer {token}"}
    try:
        r = requests.get(f"{models.HF}/api/whoami-v2", headers=headers, timeout=20)
        if r.status_code in (401, 403):
            return {"ok": False, "message": "Hugging Face не принял токен"}
        r.raise_for_status()
        user = r.json().get("name", "")
        missing = []
        for repo in (cfg["diarization"]["model"], *models.PYANNOTE_PARTS):
            if requests.get(f"{models.HF}/api/models/{repo}/auth-check", headers=headers,
                            timeout=20).status_code != 200:
                missing.append(repo)
    except (requests.RequestException, ValueError):
        return {"ok": False, "message": "Нет связи с Hugging Face: проверьте интернет"}
    if missing:
        return {"ok": False, "user": user, "missing": missing,
                "message": "Нужно принять условия на страницах моделей"}
    return {"ok": True, "user": user, "message": f"Токен работает (аккаунт {user})"}


# ---------- HTTP ----------

class Live:
    """Процесс распознавания по ходу встречи (python -m meeting_agent live) — по одному на запись."""

    def __init__(self):
        self.procs: dict[str, subprocess.Popen] = {}

    def start(self, folder: str) -> None:
        if folder in self.procs and self.procs[folder].poll() is None:
            return
        # Пониженный приоритет: процессор в первую очередь у Zoom.
        cmd = osutil.low_priority_cmd([sys.executable, "-m", "meeting_agent", "live", folder])
        proc = subprocess.Popen(cmd, cwd=config.ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, **osutil.popen_kwargs())
        self.procs[folder] = proc

        def pump():
            for line in proc.stdout:
                if line.strip():
                    log.info("  live: %s", line.rstrip())
        threading.Thread(target=pump, daemon=True).start()

    def stop(self, folder: str) -> None:
        proc = self.procs.get(folder)
        if proc and proc.poll() is None:
            self._ask_stop(folder, proc)  # доделает текущий кусок, сохранит результат и выйдет

    @staticmethod
    def _ask_stop(folder: str, proc: subprocess.Popen) -> None:
        from .live import request_stop

        request_stop(Path(folder))
        if not osutil.IS_WIN:  # на Windows terminate() убивает сразу — там хватает файла-просьбы
            proc.terminate()

    def wait(self, folder: str, timeout: float = 300) -> None:
        proc = self.procs.pop(str(folder), None)
        if proc and proc.poll() is None:
            self._ask_stop(str(folder), proc)
            try:
                proc.wait(timeout)
            except subprocess.TimeoutExpired:
                proc.kill()


class State:
    def __init__(self, token: str, port: int):
        self.token = token
        self.port = port
        self.jobs = Jobs()
        self.downloads = models.Downloader()
        self.recording: dict = {"state": "idle"}
        self.live = Live()
        self.jobs.before_run = self.live.wait


class Handler(BaseHTTPRequestHandler):
    server_version = "MeetingAgent"
    state: State

    def log_message(self, fmt, *args):  # без построчного лога каждого запроса
        pass

    # --- ответы ---
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _error(self, message: str, code: int = 400) -> None:
        self._json({"error": message}, code)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _data(self) -> dict:
        try:
            return json.loads(self._body() or b"{}")
        except ValueError:
            return {}

    # --- проверки ---
    def _allowed(self) -> bool:
        host = self.headers.get("Host", "")
        if host not in (f"127.0.0.1:{self.state.port}", f"localhost:{self.state.port}"):
            return False  # защита от подмены DNS
        if self.path.startswith("/api/"):
            return self.headers.get("X-Token") == self.state.token
        return True

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        if not self._allowed():
            return self._error("Нет доступа", 403)
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            return self._static(url.path) if method == "GET" else self._error("Не найдено", 404)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        route = getattr(self, f"api_{method.lower()}_{url.path[5:].replace('/', '_')}", None)
        if not route:
            return self._error("Не найдено", 404)
        try:
            route(q)
        except LookupError as e:
            self._error(str(e), 404)
        except (ValueError, RuntimeError) as e:
            self._error(str(e))
        except Exception as e:
            log.exception("Ошибка запроса %s", url.path)
            self._error(f"Внутренняя ошибка: {e}", 500)

    def _static(self, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        f = (UI_DIR / rel).resolve()
        if UI_DIR.resolve() not in f.parents or not f.is_file():
            return self._error("Не найдено", 404)
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, f.read_bytes(), ctype)

    # --- API ---
    def api_get_status(self, q):
        cfg = config.load()
        downloads = self.state.downloads.status(cfg)
        self._json({
            "recording": self.state.recording,
            "jobs": self.state.jobs.snapshot(),
            "models": downloads,
            # Разделение собеседников — по желанию, без него приложение работает.
            "setup_needed": any(m["required"] and m["status"] != "ready"
                                for name, m in downloads.items() if name != "diarization"),
            "llm_backend": cfg["llm"].get("backend", "local"),
        })

    def api_get_meetings(self, q):
        self._json(list_meetings(config.load()))

    def api_get_meeting(self, q):
        self._json(_note_info(_note_path(config.load(), q.get("id", "")), full=True))

    def api_delete_meeting(self, q):
        cfg = config.load()
        path = _note_path(cfg, q.get("id", ""))
        info = _note_info(path)
        path.unlink()
        src = Path(info["source"]) if info["source"] else None
        if q.get("audio") == "1" and src and src.is_dir() \
                and cfg["paths"]["recordings"].resolve() in src.resolve().parents:
            shutil.rmtree(src, ignore_errors=True)
        self._json({"ok": True})

    def api_post_reprocess(self, q):
        data = self._data()
        cfg = config.load()
        info = _note_info(_note_path(cfg, data.get("id", "")))
        if not info["source"] or not Path(info["source"]).exists():
            raise ValueError("Исходная запись удалена — пересоздать протокол нельзя")
        llm = data.get("llm") if data.get("llm") in BACKENDS else None
        asr = data.get("asr") if data.get("asr") in models.ASR_ENGINES else None
        if asr and data.get("retranscribe") and not models.engine_ready(cfg, asr):
            raise ValueError("Эта модель распознавания ещё не скачана: Настройки → Распознавание речи")
        job = self.state.jobs.add(Path(info["source"]), data.get("template"), llm,
                                  bool(data.get("retranscribe")), asr)
        self._json(job)

    def api_post_reveal(self, q):
        data = self._data()
        cfg = config.load()
        target = _note_path(cfg, data["id"]) if data.get("id") else cfg["paths"]["notes"]
        cfg["paths"]["notes"].mkdir(parents=True, exist_ok=True)
        osutil.reveal(target)
        self._json({"ok": True})

    def api_post_open(self, q):
        """Открыть заметку в приложении по умолчанию для .md (например, Obsidian или TextEdit)."""
        path = _note_path(config.load(), self._data().get("id", ""))
        osutil.open_path(path)
        self._json({"ok": True})

    def api_post_import(self, q):
        cfg = config.load()
        name = unquote(self.headers.get("X-Filename") or "audio.m4a")
        ext = Path(name).suffix.lower()
        if ext not in pipeline.AUDIO_EXT:
            raise ValueError("Поддерживаются аудиофайлы: m4a, mp3, wav, aac, mp4, mov, flac, ogg, opus, webm")
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        folder = cfg["paths"]["recordings"] / f"{stamp}_импорт"
        folder.mkdir(parents=True, exist_ok=True)
        total = int(self.headers.get("Content-Length") or 0)
        with open(folder / f"others{ext}", "wb") as f:
            left = total
            while left > 0:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        if left > 0 or total == 0:  # загрузка оборвалась — недокачанный файл в очередь не ставим
            shutil.rmtree(folder, ignore_errors=True)
            raise ValueError("Файл загрузился не полностью — попробуйте импортировать его ещё раз")
        meta = {"imported": name}
        try:
            meta["started"] = int(self.headers.get("X-Mtime") or 0) / 1000 or None
        except ValueError:
            pass
        (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
        participants.take_next(folder)
        self._json(self.state.jobs.add(folder, self.headers.get("X-Template") or None))

    def api_post_jobs_dismiss(self, q):
        self.state.jobs.dismiss(self._data().get("id", ""))
        self._json({"ok": True})

    def api_post_jobs_retry(self, q):
        data = self._data()
        job = next((j for j in self.state.jobs.snapshot() if j["id"] == data.get("id")), None)
        if not job:
            raise LookupError("Задача не найдена")
        self.state.jobs.dismiss(job["id"])
        self._json(self.state.jobs.add(Path(job["folder"]), job["template"], job["llm"], job["retranscribe"],
                                       job.get("asr")))

    # --- собеседники встречи и слепки голоса ---
    def _wav(self, path: Path) -> None:
        self._send(200, path.read_bytes(), "audio/wav")

    def api_post_speaker_rename(self, q):
        data = self._data()
        cfg = config.load()
        note = _note_path(cfg, data.get("id", ""))
        info = _note_info(note)
        if not info["source"]:
            raise ValueError("У этой встречи нет исходной записи")
        voices.rename_speaker(cfg, Path(info["source"]), str(data.get("speaker", "")),
                              str(data.get("name", "")), bool(data.get("remember")), note)
        self._json(_note_info(note, full=True))

    def api_post_segment_assign(self, q):
        data = self._data()
        cfg = config.load()
        note = _note_path(cfg, data.get("id", ""))
        info = _note_info(note)
        if not info["source"]:
            raise ValueError("У этой встречи нет исходной записи")
        voices.reassign(cfg, Path(info["source"]), int(data.get("index", -1)), str(data.get("to", "")), note)
        self._json(_note_info(note, full=True))

    def api_get_speaker_sample(self, q):
        cfg = config.load()
        info = _note_info(_note_path(cfg, q.get("id", "")))
        self._wav(voices.sample_path(cfg, Path(info["source"]).name, q.get("speaker", "")))

    def api_get_voices(self, q):
        cfg = config.load()
        ok, reason = voices_status(cfg)
        self._json({"available": ok, "reason": reason,
                    "voices": sorted((voices.public(cfg, p) for p in voices.load_profiles(cfg)),
                                     key=lambda v: v["name"].casefold())})

    def api_get_voices_sample(self, q):
        cfg = config.load()
        voices.get_profile(cfg, q.get("id", ""))
        f = voices.voices_dir(cfg) / q["id"] / "sample.wav"
        if not f.exists():
            raise LookupError("У этого слепка нет образца")
        self._wav(f)

    def api_get_voices_fragments(self, q):
        self._json(voices.fragments(config.load()))

    def api_get_voices_fragment(self, q):
        self._wav(voices.sample_path(config.load(), q.get("meeting", ""), q.get("speaker", "")))

    def api_post_voices_update(self, q):
        d = self._data()
        cfg = config.load()
        p = voices.update_profile(cfg, d.get("id", ""), d.get("name"), d.get("email"), d.get("telegram"))
        self._json(voices.public(cfg, p))

    def api_post_voices_merge(self, q):
        d = self._data()
        cfg = config.load()
        self._json(voices.public(cfg, voices.merge_profiles(cfg, d.get("id", ""), d.get("into", ""))))

    def api_post_voices_set_sample(self, q):
        d = self._data()
        voices.set_sample(config.load(), d.get("id", ""), d.get("meeting", ""), d.get("speaker", ""))
        self._json({"ok": True})

    def api_delete_voices(self, q):
        voices.delete_profile(config.load(), q.get("id", ""))
        self._json({"ok": True})

    def api_post_ask(self, q):
        from .ask import ask
        d = self._data()
        history = [h for h in d.get("history", []) if isinstance(h, dict) and h.get("q")][-3:]
        self._json(ask(str(d.get("question", "")), history, config.load()))

    def api_get_settings(self, q):
        self._json(get_settings(config.load()))

    def api_post_settings(self, q):
        save_settings(self._data())
        self._json(get_settings(config.load()))

    def api_get_gemini_models(self, q):
        from .summarize import gemini_models

        self._json(gemini_models())

    def api_post_check_hf(self, q):
        self._json(check_hf(config.load()))

    def api_post_models_download(self, q):
        name = self._data().get("name")
        if name not in models.CATALOG:
            raise ValueError("Неизвестная модель")
        self.state.downloads.start(name, config.load())
        self._json({"ok": True})

    def api_post_models_pause(self, q):
        self.state.downloads.pause(str(self._data().get("name", "")))
        self._json({"ok": True})

    # --- от нативной оболочки ---
    def api_post_recording(self, q):
        data = self._data()
        prev = self.state.recording
        self.state.recording = {k: data.get(k) for k in ("state", "folder", "since", "source", "error", "skip_meeting")}
        folder = data.get("folder")
        if data.get("state") == "recording" and folder and folder != prev.get("folder"):
            participants.take_next(Path(folder))  # список «следующей встречи» достаётся этой записи
            self.state.live.start(folder)
        if data.get("state") != "recording" and prev.get("folder"):
            self.state.live.stop(prev["folder"])
        self._json({"ok": True})

    # --- списки участников ---
    def api_get_participants(self, q):
        rec = self.state.recording
        current = participants.read(Path(rec["folder"])) if rec.get("state") == "recording" and rec.get("folder") else []
        names = sorted({p.get("name", "") for p in voices.load_profiles(config.load()) if p.get("name")},
                       key=str.casefold)
        self._json({"next": participants.read_next(), "current": current, "known": names})

    def api_post_participants(self, q):
        """{"target": "next" | "current" | <id заметки>, "names": [...]}"""
        d = self._data()
        target, names = d.get("target"), d.get("names", [])
        if target == "next":
            saved = participants.write_next(names)
        elif target == "current":
            rec = self.state.recording
            if rec.get("state") != "recording" or not rec.get("folder"):
                raise ValueError("Сейчас запись не идёт")
            saved = participants.write(Path(rec["folder"]), names)
        else:
            info = _note_info(_note_path(config.load(), str(target or "")))
            if not info["source"] or not Path(info["source"]).is_dir():
                raise ValueError("У этой встречи нет исходной записи")
            saved = participants.write(Path(info["source"]), names)
        self._json({"names": saved})

    def api_post_process(self, q):
        folder = Path(self._data().get("folder", ""))
        cfg = config.load()
        if not folder.is_dir() or cfg["paths"]["recordings"].resolve() not in folder.resolve().parents:
            raise ValueError("Папка записи не найдена")
        self._json(self.state.jobs.add(folder))


def _exit_with_parent() -> None:
    """Если приложение закрылось аварийно, сервер не остаётся висеть в фоне.
    На Windows у осиротевшего процесса номер родителя не меняется — проверяем, жив ли он."""
    parent = int(os.environ.get("MEETING_AGENT_PARENT_PID") or os.getppid())
    while True:
        time.sleep(3)
        if (not osutil.process_alive(parent)) if osutil.IS_WIN else os.getppid() != parent:
            os._exit(0)


def _single_instance() -> object:
    """Один сервер на папку данных: иначе приложение и браузерная версия обработают запись дважды."""
    config.HOME.mkdir(parents=True, exist_ok=True)
    f = open(config.HOME / "server.lock", "w")
    if not osutil.lock_exclusive(f):
        raise SystemExit("Meeting Agent уже запущен (приложение или ./agent web). "
                         "Закройте его или откройте окно через значок в строке меню.")
    return f


def serve(port: int, token: str | None = None, open_browser: bool = False) -> None:
    lock = _single_instance()
    from_app = bool(os.environ.get("MEETING_AGENT_TOKEN"))
    token = token or os.environ.get("MEETING_AGENT_TOKEN") or uuid.uuid4().hex
    state = State(token, port)
    state.lock = lock  # файл блокировки держим открытым, пока работает сервер
    Handler.state = state
    cfg = config.load()
    for folder in pipeline.pending(cfg):
        log.info("Найдена необработанная запись: %s", folder.name)
        state.jobs.add(folder)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    if from_app:
        threading.Thread(target=_exit_with_parent, daemon=True).start()
    url = f"http://127.0.0.1:{port}/#token={token}"
    print(f"SERVING {url}", flush=True)
    if open_browser:
        import webbrowser

        print("Meeting Agent открыт в браузере. Чтобы остановить — Ctrl+C в этом окне.", flush=True)
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
