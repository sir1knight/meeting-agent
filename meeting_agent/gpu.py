"""Ускорение на видеокарте NVIDIA (Windows), скачивается по желанию в настройках.

В приложение входят только сборки для процессора: библиотеки CUDA весят больше гигабайта и нужны не всем.
Кнопка «Ускорение NVIDIA» скачивает в <данные приложения>\\gpu\\bin-<время установки>:
  - llama.cpp с CUDA 12.4 (официальная сборка ggml-org) — Qwen работает в отдельном процессе llama-server
    целиком на видеокарте;
  - библиотеки CUDA 12 (cudart, cuBLAS) из той же сборки и cuDNN 9 (пакет NVIDIA с PyPI) — с ними
    Whisper (faster-whisper / CTranslate2) считает на видеокарте.
GigaAM и разделение собеседников (PyTorch) остаются на процессоре.
"""

import logging
import os
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path

from . import config, osutil

log = logging.getLogger(__name__)

LLAMA_TAG = "b12549"
_LLAMA = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_TAG}/"
CUDNN = ("nvidia-cudnn-cu12", "9.28.1.4")
# (имя, адрес, примерный размер, какие файлы брать из архива: префикс пути или None — все)
PARTS = [
    ("llama.cpp", _LLAMA + f"llama-{LLAMA_TAG}-bin-win-cuda-12.4-x64.zip", 364e6, None),
    ("CUDA", _LLAMA + "cudart-llama-bin-win-cuda-12.4-x64.zip", 391e6, None),
    ("cuDNN", None, 696e6, "nvidia/cudnn/bin/"),
]
GPU_DIR = config.HOME / "gpu"
READY = GPU_DIR / ".ready"  # имя текущей папки с библиотеками (bin-<время установки>)
TAG_FILE = GPU_DIR / "llama-tag.txt"  # какая сборка llama.cpp стоит (новая версия кода может требовать новее)
SERVER_EXE = "meeting-agent-llm.exe"  # переименованный llama-server.exe: свой процесс легко найти и закрыть
MIN_LLM_VRAM_MB = 7000  # Qwen 7B (4,7 ГБ) + контекст 16 тыс. токенов (~1 ГБ) с запасом

_lock = threading.Lock()
_state = {"state": "idle", "done": 0, "total": 0, "part": "", "error": ""}
_nvidia: dict | None = None


def nvidia() -> dict | None:
    """Видеокарта NVIDIA: {name, vram_mb, driver} или None (нет карты или драйвера)."""
    global _nvidia
    if not osutil.IS_WIN:
        return None
    if _nvidia is None:
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=15, **osutil.popen_kwargs()).stdout.strip()
            name, vram, driver = [x.strip() for x in out.splitlines()[0].split(",")]
            _nvidia = {"name": name, "vram_mb": int(float(vram)), "driver": driver}
        except Exception as e:
            log.info("Видеокарта NVIDIA не найдена (%s)", e)
            _nvidia = {}
    return _nvidia or None


def bin_dir() -> Path | None:
    """Текущая папка с библиотеками. Каждая установка — в новую папку: DLL, загруженные работающим
    процессом, Windows не даёт ни удалить, ни переименовать; старые папки убираются при следующем запуске."""
    try:
        name = READY.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return GPU_DIR / name if name.startswith("bin-") else None


def ready() -> bool:
    b = bin_dir()
    return osutil.IS_WIN and b is not None and (b / SERVER_EXE).exists()


def _cleanup_old() -> None:
    current = bin_dir()
    for d in GPU_DIR.glob("bin-*"):
        if d != current:
            shutil.rmtree(d, ignore_errors=True)  # занятые файлы останутся до следующего раза


def llm_on_gpu() -> bool:
    info = nvidia()
    return ready() and bool(info) and info["vram_mb"] >= MIN_LLM_VRAM_MB


def status() -> dict:
    info = nvidia()
    with _lock:
        st = dict(_state)
    if st["state"] == "idle" and ready():
        st["state"] = "ready"
    try:
        installed = TAG_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        installed = ""
    st.update(installed_tag=installed, latest_tag=LLAMA_TAG,
              outdated=st["state"] == "ready" and installed != LLAMA_TAG)
    st.update(available=osutil.IS_WIN, nvidia=info, llm_on_gpu=llm_on_gpu(),
              min_llm_vram_mb=MIN_LLM_VRAM_MB, size=sum(p[2] for p in PARTS))
    return st


def activate() -> None:
    """Библиотеки CUDA видны процессу: для CTranslate2 (Whisper) и всего, что грузит их по имени."""
    if not ready():
        return
    _cleanup_old()
    path = str(bin_dir())
    if path not in os.environ.get("PATH", ""):
        os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
    try:
        os.add_dll_directory(path)
    except (OSError, AttributeError):
        pass


# ---------- загрузка ----------

def install() -> None:
    with _lock:
        if _state["state"] == "downloading":
            return
        if not nvidia():
            raise RuntimeError("Видеокарта NVIDIA не найдена. Проверьте, что установлен драйвер NVIDIA.")
        _state.update(state="downloading", done=0, total=int(sum(p[2] for p in PARTS)), part="", error="")
    threading.Thread(target=_install, daemon=True, name="gpu-install").start()


def _cudnn_url() -> str:
    import requests

    name, ver = CUDNN
    r = requests.get(f"https://pypi.org/pypi/{name}/{ver}/json", timeout=30)
    r.raise_for_status()
    for f in r.json()["urls"]:
        if f["filename"].endswith("win_amd64.whl"):
            return f["url"]
    raise RuntimeError("Не нашёл cuDNN для Windows на PyPI")


def _install() -> None:
    try:
        stop_server()
        GPU_DIR.mkdir(parents=True, exist_ok=True)
        READY.unlink(missing_ok=True)
        tmp = GPU_DIR / "download"
        tmp.mkdir(exist_ok=True)
        new_bin = GPU_DIR / f"bin-{int(time.time())}"
        shutil.rmtree(new_bin, ignore_errors=True)
        new_bin.mkdir()
        base = 0
        for name, url, size, prefix in PARTS:
            with _lock:
                _state["part"] = name
            url = url or _cudnn_url()
            archive = tmp / url.rsplit("/", 1)[-1]
            got = _download(url, archive, base)
            base += got
            _extract(archive, new_bin, prefix)
        server = new_bin / "llama-server.exe"
        if not server.exists():
            raise RuntimeError("В сборке llama.cpp нет llama-server.exe")
        server.rename(new_bin / SERVER_EXE)
        shutil.rmtree(tmp, ignore_errors=True)
        READY.write_text(new_bin.name, encoding="utf-8")
        TAG_FILE.write_text(LLAMA_TAG, encoding="utf-8")
        activate()
        log.info("Ускорение NVIDIA установлено (%s, llama.cpp %s)", new_bin, LLAMA_TAG)
        with _lock:
            _state.update(state="idle", part="", done=_state["total"])
    except Exception as e:
        log.exception("Не удалось установить ускорение NVIDIA")
        with _lock:
            _state.update(state="error", error=str(e))


def _download(url: str, dest: Path, base: int) -> int:
    """Скачивает с продолжением после обрыва; возвращает размер файла."""
    import requests

    for attempt in range(6):
        have = dest.stat().st_size if dest.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with requests.get(url, headers=headers, stream=True, timeout=60) as r:
                if r.status_code == 416:  # уже скачан целиком
                    return have
                r.raise_for_status()
                if have and r.status_code != 206:
                    have = 0  # сервер не умеет продолжать — начинаем заново
                total = have + int(r.headers.get("Content-Length", 0))
                with open(dest, "ab" if have else "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                        have += len(chunk)
                        with _lock:
                            _state["done"] = base + have
            if total and have < total:
                raise IOError("соединение оборвалось")
            return have
        except (requests.RequestException, IOError) as e:
            log.warning("Загрузка %s прервалась (%s), повтор…", dest.name, e)
            time.sleep(min(30, 3 * (attempt + 1)))
    raise RuntimeError(f"Не удалось скачать {dest.name}. Проверьте интернет и попробуйте снова.")


def _extract(archive: Path, dest: Path, prefix: str | None) -> None:
    """Все файлы архива — плоско в dest (у llama.cpp они и так в корне), из колеса — только DLL по префиксу."""
    with zipfile.ZipFile(archive) as z:
        for m in z.infolist():
            if m.is_dir() or (prefix and not (m.filename.startswith(prefix) and m.filename.endswith(".dll"))):
                continue
            with z.open(m) as src, open(dest / Path(m.filename).name, "wb") as out:
                shutil.copyfileobj(src, out)


def remove() -> None:
    with _lock:
        if _state["state"] == "downloading":
            raise RuntimeError("Дождитесь конца загрузки")
    stop_server()
    READY.unlink(missing_ok=True)
    shutil.rmtree(GPU_DIR, ignore_errors=True)
    with _lock:
        _state.update(state="idle", done=0, error="")


# ---------- Qwen на видеокарте: llama-server ----------

_server: tuple[str, subprocess.Popen, int] | None = None  # (модель, процесс, порт)
_stale_killed = False


def _kill_stale() -> None:
    """Сервер от прошлого запуска (приложение закрыли, пока он работал) держит видеопамять — закрываем."""
    global _stale_killed
    if not _stale_killed:
        _stale_killed = True
        subprocess.run(["taskkill", "/F", "/IM", SERVER_EXE], capture_output=True, **osutil.popen_kwargs())


def server_url(model_path: str) -> str:
    """Запускает llama-server с моделью (если ещё не запущен) и возвращает его адрес."""
    global _server
    import requests

    if _server and _server[0] == model_path and _server[1].poll() is None:
        return f"http://127.0.0.1:{_server[2]}"
    stop_server()
    _kill_stale()
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    logs = config.HOME / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    bin_ = bin_dir()
    cmd = [str(bin_ / SERVER_EXE), "-m", model_path, "-c", "16384", "-np", "1", "-ngl", "99",
           "--host", "127.0.0.1", "--port", str(port)]
    log.info("Запускаю Qwen на видеокарте: %s", " ".join(cmd[1:]))
    out = open(logs / "llama-server.log", "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, cwd=str(bin_),
                            env=dict(os.environ, PATH=str(bin_) + os.pathsep + os.environ.get("PATH", "")),
                            **osutil.popen_kwargs())
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 180
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"llama-server завершился (код {proc.returncode}), см. logs\\llama-server.log")
        try:
            if requests.get(url + "/health", timeout=2).status_code == 200:
                _server = (model_path, proc, port)
                return url
        except requests.RequestException:
            pass
        time.sleep(0.5)
    proc.kill()
    raise RuntimeError("llama-server не запустился за 3 минуты, см. logs\\llama-server.log")


def chat(model_path: str, messages: list[dict], max_tokens: int, temperature: float) -> str:
    import requests

    url = server_url(model_path)
    r = requests.post(url + "/v1/chat/completions", timeout=3600,
                      json={"messages": messages, "max_tokens": max_tokens, "temperature": temperature})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"] or ""


def stop_server() -> None:
    """Освобождает видеопамять (после обработки встречи и при выходе)."""
    global _server
    if _server:
        proc = _server[1]
        _server = None
        try:
            proc.terminate()
            proc.wait(10)
        except Exception:
            proc.kill()
