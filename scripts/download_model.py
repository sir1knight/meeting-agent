"""Скачивает модель с Hugging Face с докачкой и перезапуском при зависании сети.

Использование: python scripts/download_model.py <repo_id> [<repo_id> ...]
"""

import multiprocessing as mp
import sys
import time
from pathlib import Path

STALL_SECONDS = 60   # сколько ждать, если файл перестал расти
ATTEMPTS = 20


def _download(repo: str) -> None:
    from huggingface_hub import snapshot_download

    snapshot_download(repo)


def _size(repo: str) -> int:
    from huggingface_hub.constants import HF_HUB_CACHE

    d = Path(HF_HUB_CACHE) / ("models--" + repo.replace("/", "--"))
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) if d.exists() else 0


def fetch(repo: str) -> None:
    for attempt in range(1, ATTEMPTS + 1):
        p = mp.Process(target=_download, args=(repo,))
        p.start()
        last, last_change = _size(repo), time.time()
        while p.is_alive():
            p.join(5)
            size = _size(repo)
            if size != last:
                last, last_change = size, time.time()
                print(f"  {repo}: {size / 1e9:.2f} ГБ", flush=True)
            elif time.time() - last_change > STALL_SECONDS:
                print(f"  загрузка зависла, перезапускаю (попытка {attempt + 1})", flush=True)
                p.terminate()
                p.join()
                break
        if p.exitcode == 0:
            print(f"Готово: {repo}", flush=True)
            return
        time.sleep(3)
    raise SystemExit(f"Не удалось скачать {repo}: проверьте интернет и запустите ещё раз")


if __name__ == "__main__":
    for r in sys.argv[1:]:
        fetch(r)
