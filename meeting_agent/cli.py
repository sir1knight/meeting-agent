import argparse
import logging
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from . import config


def main() -> None:  # pipeline импортируется по месту: bootstrap и winapp — на голом Python
    p = argparse.ArgumentParser(prog="agent", description="Локальный агент саммари встреч")
    p.add_argument("--config", help="путь к config.toml")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("watch", help="ждать встречи Zoom и записывать их автоматически")

    r = sub.add_parser("record", help="записать прямо сейчас (Ctrl+C — стоп), затем обработать")
    r.add_argument("--app", default=None,
                   help="bundle id приложения (по умолчанию Zoom) или all — весь звук системы")
    r.add_argument("--template", default=None)
    r.add_argument("--llm", choices=["local", "claude-code", "claude", "gemini", "openai", "gigachat"], help="кто пишет протокол")

    pr = sub.add_parser("process", help="обработать папку записи или любой аудиофайл")
    pr.add_argument("path")
    pr.add_argument("--template", default=None)
    pr.add_argument("--llm", choices=["local", "claude-code", "claude", "gemini", "openai", "gigachat"], help="кто пишет протокол")
    pr.add_argument("--gemini-model", default=None, help="разовая модель Gemini для этого протокола")
    pr.add_argument("--model-override", default=None, help="разовая модель выбранного облачного сервиса")
    pr.add_argument("--asr", choices=["whisper-turbo", "whisper-large", "gigaam", "apple-speech"],
                    help="чем распознавать речь (по умолчанию — из настроек)")
    pr.add_argument("--retranscribe", action="store_true",
                    help="расшифровать заново, даже если расшифровка уже есть")

    sub.add_parser("ask-llm", help="служебная: ответ локальной модели на вопрос (JSON из stdin)")
    ak = sub.add_parser("ask", help="задать вопрос по всем встречам")
    ak.add_argument("question", nargs="+")

    lv = sub.add_parser("live", help="распознавать запись, пока она идёт (запускает приложение)")
    lv.add_argument("folder")

    bs = sub.add_parser("bootstrap", help="установить или обновить пакеты Python (Windows, Mac с Python вне приложения)")
    bs.add_argument("--check", action="store_true", help="только проверить: код 0 — всё на месте, 10 — нужна установка")
    sub.add_parser("winapp", help="приложение для Windows: трей, окно, запись (запускает Meeting Agent.exe)")

    sv = sub.add_parser("serve", help="локальный сервер окна приложения")
    sv.add_argument("--port", type=int, default=8765)

    wb = sub.add_parser("web", help="открыть Meeting Agent в браузере (без приложения)")
    wb.add_argument("--port", type=int, default=8765)
    wb.add_argument("--no-open", action="store_true", help="не открывать браузер, только показать адрес")

    sub.add_parser("pending", help="обработать все записи, которые ещё не обработаны")
    sub.add_parser("windows", help="показать окна Zoom (для настройки определения встречи)")
    sub.add_parser("templates", help="список шаблонов саммари")

    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    cfg = config.load(args.config)
    if getattr(args, "llm", None):
        cfg["llm"]["backend"] = args.llm
    if getattr(args, "gemini_model", None):
        cfg.setdefault("gemini", {})["model"] = args.gemini_model
    if getattr(args, "model_override", None) and cfg["llm"].get("backend") in ("gemini", "gigachat", "openai", "claude"):
        cfg.setdefault(cfg["llm"]["backend"], {})["model"] = args.model_override
    if getattr(args, "asr", None):
        cfg["whisper"]["engine"] = args.asr

    if args.cmd in ("record", "process", "pending"):
        from . import pipeline  # тянет requests и numpy — только там, где нужно

    if args.cmd == "watch":
        from .watcher import watch
        watch(cfg, args.config)

    elif args.cmd == "record":
        from .recorder import Recorder
        rec = Recorder(args.app or cfg["zoom"]["bundle_id"])
        folder = cfg["paths"]["recordings"] / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        rec.start(folder)
        print("Идёт запись. Ctrl+C — остановить и сделать заметку.")
        signal.signal(signal.SIGINT, signal.default_int_handler)
        try:
            while rec.alive():
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        rec.stop()
        print(pipeline.process(folder, cfg, args.template))

    elif args.cmd == "process":
        print(pipeline.process(Path(args.path), cfg, args.template, args.retranscribe))

    elif args.cmd == "ask-llm":
        import json as _json
        from .summarize import chat_local
        req = _json.loads(sys.stdin.read())
        print(chat_local(cfg, req["user"], req["system"]))

    elif args.cmd == "ask":
        from .ask import ask
        r = ask(" ".join(args.question), [], cfg)
        print(r["answer"])
        for s in r["sources"]:
            where = s["ts"] or s["section"]
            print(f"  [{s['n']}] {s['date']} {s['title']} — {where}")

    elif args.cmd == "live":
        from .live import run
        run(Path(args.folder), cfg)

    elif args.cmd == "bootstrap":
        from .bootstrap import run as bootstrap_run
        if args.check:
            from .updates import packages_needed
            sys.exit(10 if packages_needed() else 0)
        sys.exit(bootstrap_run())

    elif args.cmd == "winapp":
        from .winapp import main as winapp_main
        winapp_main()

    elif args.cmd == "serve":
        from .server import serve
        serve(args.port)

    elif args.cmd == "web":
        from .server import serve
        serve(args.port, open_browser=not args.no_open)

    elif args.cmd == "pending":
        items = pipeline.pending(cfg)
        if not items:
            print("Необработанных записей нет")
        for folder in items:
            try:
                print(pipeline.process(folder, cfg))
            except Exception as e:
                logging.error("%s: %s", folder.name, e)

    elif args.cmd == "windows":
        from .detect import zoom_windows
        wins = zoom_windows()
        if not wins:
            print("Окон Zoom не найдено")
        for owner, name in wins:
            print(f"{owner!r:14} {name!r}")

    elif args.cmd == "templates":
        from .templates import list_all
        for t in list_all():
            mark = " (по умолчанию)" if t["id"] == cfg["output"]["template"] else ""
            print(f"{t['id']:12} {t['title']}{' — свой' if t['custom'] else ''}{mark}")

    sys.exit(0)
