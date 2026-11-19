"""Фоновый режим: ждёт начала встречи Zoom, пишет её и отправляет на обработку."""

import logging
import queue
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import pipeline
from .config import ROOT
from .detect import in_meeting
from .recorder import Recorder

log = logging.getLogger(__name__)


class Processor:
    """Очередь обработки. Каждая встреча — в отдельном процессе, чтобы модели освобождали память."""

    def __init__(self, config_path: str | None):
        self.q: queue.Queue[Path] = queue.Queue()
        self.config_path = config_path
        threading.Thread(target=self._run, daemon=True).start()

    def add(self, folder: Path) -> None:
        self.q.put(folder)

    def _run(self) -> None:
        while True:
            folder = self.q.get()
            cmd = [sys.executable, "-m", "meeting_agent"]
            if self.config_path:
                cmd += ["--config", self.config_path]
            cmd += ["process", str(folder)]
            log.info("Обработка %s…", folder.name)
            # Отдельная сессия: Ctrl+C в терминале не прерывает уже начатую обработку.
            rc = subprocess.run(cmd, cwd=ROOT, start_new_session=True).returncode
            if rc != 0:
                log.error("Обработка %s завершилась с ошибкой (код %d). "
                          "Повторить: ./agent process \"%s\"", folder.name, rc, folder)
            self.q.task_done()

    def wait(self) -> None:
        self.q.join()


def watch(cfg: dict, config_path: str | None = None) -> None:
    z = cfg["zoom"]
    rec = Recorder(z["bundle_id"])
    proc = Processor(config_path)
    for folder in pipeline.pending(cfg):
        log.info("Найдена необработанная запись: %s", folder.name)
        proc.add(folder)

    hits, last_seen, started_at = 0, 0.0, 0.0
    current: Path | None = None

    def finish(reason: str) -> None:
        nonlocal current
        rec.stop()
        length = last_seen - started_at
        log.info("Встреча закончилась (%s), длительность ~%d мин", reason, length // 60)
        if length < z["min_duration_seconds"]:
            log.info("Слишком короткая запись — удаляю")
            shutil.rmtree(current, ignore_errors=True)
        else:
            proc.add(current)
        current = None

    log.info("Жду встречу в Zoom… (Ctrl+C — выход)")
    try:
        while True:
            try:
                active = in_meeting(z["window_titles"])
            except Exception as e:
                log.warning("Не удалось проверить окна Zoom: %s", e)
                active = False
            now = time.time()
            if active:
                last_seen = now

            if current is None:
                hits = hits + 1 if active else 0
                if hits >= z["start_after_polls"]:
                    hits = 0
                    folder = cfg["paths"]["recordings"] / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                    try:
                        rec.start(folder)
                        current, started_at = folder, now
                        pipeline.notify(cfg, "Meeting Agent", "Идёт запись встречи")
                    except Exception as e:
                        log.error("Не удалось начать запись: %s", e)
                        time.sleep(30)
            elif not rec.alive():
                log.error("Рекордер неожиданно остановился")
                finish("сбой рекордера")
            elif now - last_seen > z["stop_grace_seconds"]:
                finish("окно встречи закрыто")

            time.sleep(z["poll_seconds"])
    except KeyboardInterrupt:
        if current is not None:
            last_seen = time.time()
            finish("агент остановлен")
        log.info("Дожидаюсь окончания обработки…")
        proc.wait()
