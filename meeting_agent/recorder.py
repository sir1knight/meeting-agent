"""Обёртка над Swift-рекордером bin/zoomrec."""

import logging
import signal
import subprocess
import threading
from pathlib import Path

from .config import RECORDER_BIN

log = logging.getLogger(__name__)


class Recorder:
    def __init__(self, target: str):
        self.target = target
        self.proc: subprocess.Popen | None = None
        self.out_dir: Path | None = None

    def start(self, out_dir: Path) -> None:
        if not RECORDER_BIN.exists():
            raise RuntimeError(f"Нет рекордера {RECORDER_BIN}. Запустите ./setup.sh")
        self.out_dir = out_dir
        ready = threading.Event()
        self.proc = subprocess.Popen(
            [str(RECORDER_BIN), str(out_dir), self.target],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,  # останавливаем сами, корректно, через stop()
        )

        def pump(stream, level):
            for line in stream:
                line = line.strip()
                if line.startswith("RECORDING"):
                    ready.set()
                elif line:
                    log.log(level, "zoomrec: %s", line)

        threading.Thread(target=pump, args=(self.proc.stdout, logging.INFO), daemon=True).start()
        threading.Thread(target=pump, args=(self.proc.stderr, logging.WARNING), daemon=True).start()

        for _ in range(100):  # до 10 секунд
            if ready.wait(0.1):
                log.info("Запись началась: %s", out_dir)
                return
            if self.proc.poll() is not None:
                raise RuntimeError(f"Рекордер завершился с кодом {self.proc.returncode} "
                                   "(проверьте разрешения на запись экрана и микрофон)")
        self.stop()
        raise RuntimeError("Рекордер не начал запись за 10 секунд")

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if not self.proc:
            return
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        log.info("Запись остановлена")
        self.proc = None
