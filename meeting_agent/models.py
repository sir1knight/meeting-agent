"""Модели: где лежат, скачаны ли, загрузка с докачкой и прогрессом.

Whisper и Qwen качаются обычным HTTP по файлам (с продолжением с места обрыва):
штатный загрузчик Hugging Face на нестабильной сети подвисает и начинает файл заново.
Модели pyannote маленькие и требуют токен — их берёт сам pyannote в кэш Hugging Face.
"""

import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import requests

from . import config
from .osutil import IS_WIN

log = logging.getLogger(__name__)

HF = "https://huggingface.co"
COMPLETE = ".complete"

# Движки распознавания речи (whisper.engine). repo «имя@ветка» — модель из отдельной ветки репозитория,
# «имя#файл» — только этот файл из репозитория. На Mac Whisper работает через MLX, на Windows — faster-whisper.
ASR_ENGINES = {
    "whisper-turbo": {"title": "Whisper large-v3-turbo", "repo": "mlx-community/whisper-large-v3-turbo",
                      "size": 1.6e9, "desc": "Быстрый, на видеоядрах Mac (MLX). Ошибается чаще на именах и шумной речи."},
    "whisper-large": {"title": "Whisper large-v3", "repo": "mlx-community/whisper-large-v3-mlx",
                      "size": 3.1e9, "desc": "Полная версия Whisper: точнее turbo, обработка примерно вдвое дольше."},
    "gigaam": {"title": "GigaAM-v3 (Сбер)", "repo": "ai-sage/GigaAM-v3@e2e_rnnt",
               "size": 0.45e9, "desc": "Обучен на русской речи, в том числе на созвонах и шумной речи. "
                                      "Сам расставляет знаки препинания. Только русский язык."},
}
if IS_WIN:
    ASR_ENGINES["whisper-turbo"].update(
        repo="deepdml/faster-whisper-large-v3-turbo-ct2",
        desc="Быстрый вариант Whisper. С видеокартой NVIDIA — минуты, на обычном процессоре заметно дольше. "
             "Ошибается чаще на именах и шумной речи.")
    ASR_ENGINES["whisper-large"].update(
        repo="Systran/faster-whisper-large-v3",
        desc="Полная версия Whisper: точнее turbo, но примерно вдвое медленнее. Без видеокарты NVIDIA — долго.")
    ASR_ENGINES["gigaam"]["desc"] += " Быстрее Whisper на обычном процессоре."

# Локальная модель протоколов: на Mac — MLX, на Windows — GGUF для llama.cpp.
WIN_LLM = "bartowski/Qwen2.5-7B-Instruct-GGUF#Qwen2.5-7B-Instruct-Q4_K_M.gguf"


def llm_repo(cfg: dict) -> str:
    model = cfg["llm"]["model"]
    if IS_WIN and "gguf" not in model.lower():
        return WIN_LLM  # в config.toml указана MLX-модель для Mac
    return model


def asr_engine(cfg: dict) -> str:
    e = cfg["whisper"].get("engine", "whisper-turbo")
    return e if e in ASR_ENGINES else "whisper-turbo"


def asr_repo(cfg: dict) -> str:
    # Ручной выбор модели в config.toml (whisper.model) действует только для Whisper turbo на Mac.
    if asr_engine(cfg) == "whisper-turbo" and cfg["whisper"].get("model") and not IS_WIN:
        return cfg["whisper"]["model"]
    return ASR_ENGINES[asr_engine(cfg)]["repo"]


def engine_ready(cfg: dict, engine: str) -> bool:
    c = dict(cfg, whisper=dict(cfg["whisper"], engine=engine))
    return is_ready("whisper", c)


def _split(repo: str) -> tuple[str, str]:
    name, _, rev = repo.partition("#")[0].partition("@")
    return name, rev or "main"


def _only_file(repo: str) -> str | None:
    return repo.partition("#")[2] or None


# Что показывать в окне приложения. size — для оценки до получения списка файлов.
CATALOG = {
    "whisper": {"title": "Распознавание речи", "size": 1.6e9, "required": True},
    "llm": {"title": "Языковая модель для протоколов (Qwen 2.5 7B)", "size": 4.7e9 if IS_WIN else 4.3e9,
            "required": False},
    "diarization": {"title": "Разделение собеседников (pyannote)", "size": 0.03e9, "required": False},
}


def repo_for(name: str, cfg: dict) -> str:
    return {"whisper": asr_repo(cfg), "llm": llm_repo(cfg),
            "diarization": cfg["diarization"]["model"]}[name]


def local_dir(repo: str) -> Path:
    return config.MODELS_DIR / repo.replace("/", "--").replace("#", "--")


def resolve(repo: str) -> str:
    """Папка со скачанной моделью: из загрузок приложения или из кэша Hugging Face (после setup.sh).
    Если модели нет нигде — имя на Hugging Face (тогда её попробует скачать сама библиотека)."""
    d = local_dir(repo)
    if (d / COMPLETE).exists():
        return str(d)
    snap = _hf_cache_snapshot(repo)
    return str(snap) if snap and _has_weights(snap) else repo


def require(repo: str, what: str) -> str:
    """Папка модели (или путь к файлу для «имя#файл»)."""
    path = resolve(repo)
    if _only_file(repo) and path != repo:
        return str(Path(path) / _only_file(repo))
    if path == repo and os.environ.get("HF_HUB_OFFLINE") == "1":
        raise RuntimeError(f"{what} не скачана: откройте Meeting Agent → Настройки → Модели")
    return path


def _hf_cache_snapshot(repo: str) -> Path | None:
    """Готовая копия в обычном кэше Hugging Face (например, после setup.sh)."""
    from huggingface_hub.constants import HF_HUB_CACHE

    name, branch = _split(repo)
    for base in {Path(HF_HUB_CACHE), Path.home() / ".cache/huggingface/hub"}:
        d = base / ("models--" + name.replace("/", "--"))
        try:
            rev = (d / "refs" / branch).read_text().strip()
        except OSError:
            continue
        snap = d / "snapshots" / rev
        if snap.is_dir() and not any(snap.rglob("*.incomplete")):
            return snap
    return None


# Пайплайн 3.1 и модели, на которые ссылается его config.yaml.
PYANNOTE_PARTS = ("pyannote/segmentation-3.0", "pyannote/wespeaker-voxceleb-resnet34-LM")


def _pyannote_ready(cfg: dict) -> bool:
    from huggingface_hub.constants import HF_HUB_CACHE

    for repo in (cfg["diarization"]["model"], *PYANNOTE_PARTS):
        d = Path(HF_HUB_CACHE) / ("models--" + repo.replace("/", "--"))
        if not (d / "refs" / "main").exists():
            return False
    return True


def pyannote_installed() -> bool:
    import importlib.util

    return importlib.util.find_spec("pyannote.audio") is not None


def is_ready(name: str, cfg: dict) -> bool:
    if name == "diarization":
        return _pyannote_ready(cfg)
    repo = repo_for(name, cfg)
    if (local_dir(repo) / COMPLETE).exists():
        return True
    snap = _hf_cache_snapshot(repo)
    return bool(snap and _has_weights(snap))


def _has_weights(d: Path) -> bool:
    return any(any(d.glob(p)) for p in ("*.safetensors", "*.npz", "*.bin", "*.gguf"))


def needed(cfg: dict) -> list[str]:
    """Какие модели нужны при текущих настройках."""
    names = ["whisper"]
    if cfg["llm"].get("backend", "local") == "local":
        names.append("llm")
    if cfg["diarization"].get("enabled") and config.hf_token(cfg):
        names.append("diarization")
    return names


class Paused(Exception):
    """Загрузку поставили на паузу: скачанное лежит в *.part и докачается при продолжении."""


PAUSABLE = ("whisper", "llm")  # pyannote (30 МБ) качается целиком через huggingface_hub


def _partial_bytes(repo: str) -> int:
    """Сколько уже лежит на диске от незаконченной загрузки (в том числе после перезапуска)."""
    d = local_dir(repo)
    if not d.exists() or (d / COMPLETE).exists():
        return 0
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())


class Downloader:
    """Фоновые загрузки, по одной на модель. Состояние читает окно приложения."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state: dict[str, dict] = {}
        self.stop: dict[str, threading.Event] = {}

    def status(self, cfg: dict) -> dict:
        out = {}
        for name, info in CATALOG.items():
            with self.lock:
                st = dict(self.state.get(name, {}))
            if st.get("repo") and st["repo"] != repo_for(name, cfg):
                st = {}  # загрузка относилась к другому движку
            ready = is_ready(name, cfg)
            if ready and st.get("status") != "error":
                st = {"status": "ready"}
            size = ASR_ENGINES[asr_engine(cfg)]["size"] if name == "whisper" else info["size"]
            if not ready and not st and name in PAUSABLE:
                part = _partial_bytes(repo_for(name, cfg))
                if part:  # начатая раньше загрузка — можно продолжить
                    st = {"status": "paused", "done": part, "total": max(size, part)}
            title = info["title"] + (f" — {ASR_ENGINES[asr_engine(cfg)]['title']}" if name == "whisper" else "")
            out[name] = {"title": title, "required": name in needed(cfg),
                         "status": st.get("status", "ready" if ready else "missing"),
                         "done": st.get("done", 0), "total": st.get("total", size),
                         "speed": st.get("speed", 0), "error": st.get("error"),
                         "pausable": name in PAUSABLE}
        return out

    def pause(self, name: str) -> None:
        ev = self.stop.get(name)
        if ev:
            ev.set()

    def start(self, name: str, cfg: dict) -> None:
        with self.lock:
            if self.state.get(name, {}).get("status") == "downloading":
                return
            size = ASR_ENGINES[asr_engine(cfg)]["size"] if name == "whisper" else CATALOG[name]["size"]
            done = _partial_bytes(repo_for(name, cfg)) if name in PAUSABLE else 0
            self.state[name] = {"status": "downloading", "done": done, "total": max(size, done),
                                "repo": repo_for(name, cfg), "speed": 0, "_mark": (time.time(), done)}
            self.stop[name] = threading.Event()
        threading.Thread(target=self._run, args=(name, cfg), daemon=True).start()

    def _set(self, name: str, **kw) -> None:
        with self.lock:
            st = self.state.setdefault(name, {})
            if "done" in kw:  # скорость — по окну в пару секунд, сглаженно
                t0, d0 = st.get("_mark", (time.time(), kw["done"]))
                now = time.time()
                if now - t0 >= 2:
                    cur = (kw["done"] - d0) / (now - t0)
                    st["speed"] = cur if not st.get("speed") else 0.7 * st["speed"] + 0.3 * cur
                    st["_mark"] = (now, kw["done"])
            st.update(kw)

    def _run(self, name: str, cfg: dict) -> None:
        try:
            if name == "diarization":
                self._pyannote(cfg)
            else:
                self._repo(name, repo_for(name, cfg))
            self._set(name, status="ready")
        except Paused:
            log.info("Загрузка %s на паузе", name)
            self._set(name, status="paused", speed=0)
        except Exception as e:  # показываем в окне, а не роняем сервер
            log.exception("Загрузка %s не удалась", name)
            self._set(name, status="error", error=str(e))

    def _repo(self, name: str, repo: str) -> None:
        dest = local_dir(repo)
        dest.mkdir(parents=True, exist_ok=True)
        snap = _hf_cache_snapshot(repo)
        if snap and _has_weights(snap):
            # Уже скачано раньше (setup.sh): копируем клонами APFS — мгновенно и без места на диске.
            log.info("Беру %s из кэша Hugging Face", repo)
            for f in snap.iterdir():
                target = dest / f.name
                if not target.exists():
                    if IS_WIN:
                        shutil.copy2(f.resolve(), target)
                    else:
                        subprocess.run(["cp", "-c", str(f.resolve()), str(target)], check=True)
            (dest / COMPLETE).write_text("from-cache")
            return

        files = _repo_files(repo)
        total = sum(size for _, size in files)
        self._set(name, total=total)
        done_before = 0
        repo_name, branch = _split(repo)
        stop = self.stop.get(name) or threading.Event()
        for path, size in files:
            target = dest / path
            target.parent.mkdir(parents=True, exist_ok=True)
            _fetch(f"{HF}/{repo_name}/resolve/{branch}/{path}", target, size,
                   lambda n: self._set(name, done=done_before + n), stop=stop)
            done_before += size
        (dest / COMPLETE).write_text(time.strftime("%Y-%m-%d %H:%M"))

    def _pyannote(self, cfg: dict) -> None:
        """Скачивает пайплайн 3.1 и его модели целиком (в pyannote 4.x Pipeline.from_pretrained
        для 3.1 ещё и требует доступ к speaker-diarization-community-1 — он не нужен)."""
        token = config.hf_token(cfg)
        if not token:
            raise RuntimeError("Сначала введите токен Hugging Face в настройках")
        if not pyannote_installed():
            raise RuntimeError("В этой сборке нет pyannote: пересоберите приложение с requirements-diarization.txt")
        # Отдельный процесс: у сервера включён офлайн-режим Hugging Face.
        env = dict(os.environ, HF_HUB_OFFLINE="0", HF_TOKEN=token)
        code = ("import os, sys\n"
                "from huggingface_hub import snapshot_download\n"
                "for repo in sys.argv[1:]:\n"
                "    try:\n"
                "        snapshot_download(repo, token=os.environ['HF_TOKEN'])\n"
                "    except Exception as e:\n"
                "        print('FAILED', repo, type(e).__name__, flush=True)\n"
                "        sys.exit(3)\n")
        repos = [cfg["diarization"]["model"], *PYANNOTE_PARTS]
        r = subprocess.run([sys_executable(), "-c", code, *repos], env=env, capture_output=True, text=True)
        if r.returncode != 0:
            failed = next((line.split() for line in r.stdout.splitlines() if line.startswith("FAILED")), None)
            if failed and failed[2] in ("GatedRepoError", "RepositoryNotFoundError"):
                raise RuntimeError(f"Нет доступа к модели {failed[1]}: откройте https://huggingface.co/{failed[1]} "
                                   "и нажмите «Agree»")
            err = (r.stderr.strip().splitlines() or ["проверьте интернет"])[-1]
            raise RuntimeError(f"Не удалось скачать модели pyannote: {err}")

def sys_executable() -> str:
    import sys

    return sys.executable


def _repo_files(repo: str) -> list[tuple[str, int]]:
    name, branch = _split(repo)
    r = requests.get(f"{HF}/api/models/{name}/tree/{branch}", params={"recursive": "true"}, timeout=30)
    r.raise_for_status()
    files = [(e["path"], int(e.get("size", 0))) for e in r.json() if e.get("type") == "file"]
    only = _only_file(repo)
    if only:
        files = [f for f in files if f[0] == only]
    if not files:
        raise RuntimeError(f"Не нашёл файлов модели {repo}")
    return files


def _fetch(url: str, target: Path, size: int, progress, attempts: int = 50,
           stop: threading.Event | None = None) -> None:
    """Скачивает файл с продолжением с места обрыва и перезапуском при зависании.
    stop — «пауза»: прерывает загрузку, недокачанное остаётся в *.part."""
    if target.exists() and target.stat().st_size == size:
        progress(size)
        return
    part = target.with_name(target.name + ".part")
    for attempt in range(attempts):
        if stop and stop.is_set():
            raise Paused()
        have = part.stat().st_size if part.exists() else 0
        if have >= size > 0:
            break
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            # timeout=(подключение, тишина в чтении): 30 с без данных — переподключаемся
            with requests.get(url, headers=headers, stream=True, timeout=(20, 30)) as r:
                if r.status_code == 200 and have:  # сервер не умеет докачку — начинаем заново
                    have = 0
                    part.unlink(missing_ok=True)
                r.raise_for_status()
                with open(part, "ab") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                        have += len(chunk)
                        progress(have)
                        if stop and stop.is_set():
                            raise Paused()
            if size == 0 or have >= size:
                break
        except (requests.RequestException, OSError) as e:
            log.warning("Загрузка %s прервалась (%s), повторяю", target.name, e)
            if stop and stop.wait(min(30, 2 + attempt * 2)):  # пауза во время ожидания повтора
                raise Paused()
            elif not stop:
                time.sleep(min(30, 2 + attempt * 2))
    else:
        raise RuntimeError(f"Не удалось скачать {target.name}: проверьте интернет")
    if size and part.stat().st_size != size:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"Файл {target.name} скачался повреждённым, попробуйте ещё раз")
    shutil.move(part, target)

