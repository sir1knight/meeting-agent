"""Meeting Agent для Windows: значок в трее, окно (Edge WebView2 через pywebview), запись встреч.
То же, что на Mac делает оболочка на Swift (app/*.swift): запускает сервер, следит за окном
встречи Zoom, пишет звук (winrec.py) и отдаёт запись серверу на обработку.

Запуск: «Meeting Agent.exe» → python\\pythonw.exe -m meeting_agent winapp
"""

import ctypes
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

from . import osutil

log = logging.getLogger("winapp")

HOME = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Meeting Agent"
APP_ROOT = Path(__file__).resolve().parent.parent          # …\Meeting Agent\app
EXE = os.environ.get("MEETING_AGENT_EXE") or str(APP_ROOT.parent / "Meeting Agent.exe")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
TITLE = "Meeting Agent"


def _message(text: str, title: str = TITLE) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)  # MB_ICONINFORMATION


def _unblock_downloaded() -> None:
    """Снимает с библиотек .NET пометку «скачано из интернета» (поток Zone.Identifier).

    Если архив распаковали встроенным в Windows средством, такую пометку получает каждый файл,
    и .NET отказывается загружать помеченные сборки: окно (pywebview → pythonnet) падает с
    «Failed to resolve Python.Runtime.Loader.Initialize». Обычные DLL и .pyd пометка не мешает."""
    site = Path(sys.executable).parent / "Lib" / "site-packages"
    removed = 0
    for sub in ("pythonnet", "clr_loader", "webview"):
        for f in (site / sub).rglob("*"):
            if f.suffix.lower() in (".dll", ".exe", ".pyd") and f.is_file():
                try:
                    os.remove(f"{f}:Zone.Identifier")
                    removed += 1
                except OSError:
                    pass
    if removed:
        log.info("Снял пометку «из интернета» с %d библиотек", removed)


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------- сервер ----------

class Backend:
    """python -m meeting_agent serve в фоне, без окна консоли; перезапуск, если упал."""

    def __init__(self):
        self.port = _free_port()
        self.token = uuid.uuid4().hex
        self.proc: subprocess.Popen | None = None
        self.stopping = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/#token={self.token}"

    def start(self) -> None:
        python = Path(sys.executable).with_name("python.exe")  # сами мы — pythonw.exe
        env = dict(os.environ, PYTHONPATH=str(APP_ROOT), PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1",
                   PYTHONUTF8="1", MEETING_AGENT_HOME=str(HOME), MEETING_AGENT_TOKEN=self.token,
                   MEETING_AGENT_PARENT_PID=str(os.getpid()), HF_HOME=str(HOME / "huggingface"),
                   HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false",
                   HF_HUB_DISABLE_SYMLINKS_WARNING="1")
        logs = HOME / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        current = logs / "backend.log"
        if current.exists():
            current.replace(logs / "backend.prev.log")
        out = open(current, "ab")
        self.proc = subprocess.Popen([str(python), "-m", "meeting_agent", "serve", "--port", str(self.port)],
                                     cwd=APP_ROOT, env=env, stdout=out, stderr=subprocess.STDOUT,
                                     creationflags=osutil.CREATE_NO_WINDOW)
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self) -> None:
        restarts = 0
        while not self.stopping:
            rc = self.proc.wait()
            if self.stopping:
                return
            restarts += 1
            log.warning("Сервер завершился (код %s), перезапускаю", rc)
            time.sleep(min(30, restarts * 2))
            self.start()
            return

    def wait_ready(self, timeout: float = 120) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.get("status") is not None:
                return True
            time.sleep(0.25)
        return False

    def stop(self) -> None:
        self.stopping = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        # Qwen на видеокарте — отдельный процесс сервера; без него он остался бы держать видеопамять.
        from .gpu import SERVER_EXE

        subprocess.run(["taskkill", "/F", "/IM", SERVER_EXE], capture_output=True, **osutil.popen_kwargs())

    def request(self, path: str, body: dict | None = None) -> dict | None:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/{path}",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-Token": self.token, "Content-Type": "application/json"},
                                     method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read() or b"{}")
        except Exception:
            return None

    def get(self, path: str) -> dict | None:
        return self.request(path)

    def post(self, path: str, body: dict) -> dict | None:
        return self.request(path, body)


# ---------- запись ----------

class Recording:
    """Порт app/Recording.swift: ручная запись и автозапись по окну встречи Zoom."""

    def __init__(self, backend: Backend):
        self.backend = backend
        self.recorder = None
        self.source: str | None = None       # "manual" | "zoom" | "teams" | "telemost" | "meet"
        self.since: float | None = None
        self.busy = False
        self.lock = threading.RLock()
        self.on_change = lambda: None
        self.toast = lambda title, text: None  # уведомление из трея (задаёт App)
        self.auto_record = True
        self.platforms = ["zoom", "teams", "telemost", "meet"]
        self.calendar = False
        self.window_titles = ["Zoom Meeting", "Zoom Webinar", "Конференция Zoom", "Вебинар Zoom"]
        self.poll = 3.0
        self.start_after = 2
        self.stop_grace = 30.0
        self.min_duration = 60.0
        self.recordings = Path.home() / "MeetingAgent" / "recordings"
        self.hits = 0
        self.last_seen = 0.0
        self.skip_meeting = False  # запись остановили вручную, окно встречи ещё открыто
        self.cooldown_until = 0.0  # кулдаун 60 с после ручной остановки
        self.hold_until = 0.0      # после смены микрофона/динамиков/сна не останавливаем запись
        self.mic_id: str | None = None
        self.speaker_id: str | None = None
        self._dev_err: str | None = None
        self.silent_checks = 0
        self.warning: str | None = None
        self.health_at = 0.0

    @property
    def is_recording(self) -> bool:
        return self.recorder is not None

    @property
    def is_paused(self) -> bool:
        return bool(self.recorder and self.recorder.is_paused)

    def reload_settings(self) -> None:
        s = self.backend.get("settings")
        if not s:
            return
        self.auto_record = s.get("auto_record", True)
        self.platforms = s.get("auto_platforms", self.platforms)
        self.calendar = bool(s.get("calendar_participants", False))
        self.window_titles = s.get("window_titles") or self.window_titles
        self.recordings = Path(s.get("recordings_dir") or self.recordings)
        z = s.get("zoom") or {}
        self.poll = float(z.get("poll_seconds", self.poll))
        self.start_after = int(z.get("start_after_polls", self.start_after))
        self.stop_grace = float(z.get("stop_grace_seconds", self.stop_grace))
        self.min_duration = float(z.get("min_duration_seconds", self.min_duration))
        self.on_change()

    def set_auto(self, on: bool) -> None:
        self.auto_record = on
        self.backend.post("settings", {"auto_record": on})
        self.on_change()

    @property
    def auto_source(self) -> str | None:
        return self.source if self.source and self.source not in ("manual", "mic_only") else None

    def watch(self) -> None:
        from .detect import active_platform
        from .winrec import _com_init

        _com_init()  # аудиосессии WASAPI читаются через COM в этом потоке
        prev_wall = time.time()
        while True:
            time.sleep(self.poll)
            now_wall = time.time()
            # Скачок времени > 15 с между 3-секундными итерациями — компьютер выходил из сна.
            if now_wall - prev_wall > max(15.0, self.poll * 4):
                sleep_dt = max(0.0, (now_wall - prev_wall) - self.poll)
                log.info("Компьютер вышел из сна (пропуск %.0f с) — сдвигаю часы записи и обновляю устройства",
                         sleep_dt)
                self.hold_until = max(self.hold_until, now_wall + 60)
                self.last_seen = max(self.last_seen, now_wall)
                if self.recorder:
                    self.recorder.note_sleep(sleep_dt)
                    self.recorder.restart_mic()
                    self.recorder.restart_speaker()
            prev_wall = now_wall
            try:
                self._watch_devices()
                if self.recorder:
                    self.recorder.poll_speaker_mute()
                self._tick(active_platform(self.window_titles, self.platforms, self.auto_source))
                self._check_health()
            except Exception:
                log.exception("Проверка встречи не удалась")

    def _watch_devices(self) -> None:
        """Сменился микрофон или динамики/наушники (гарнитура, док-станция): запись переходит на новое
        устройство и 20 с не останавливается — пока программа звонка переключается, звук может «пропасть»."""
        from . import detect, winrec

        err_now: str | None = None
        mid = (detect.meeting_capture_device_id("mic_only" if self.recorder.mic_only else self.source)
               if self.recorder else None)
        if not mid:
            try:
                mid = winrec.soundcard().default_microphone().id
            except Exception as e:
                mid = None
                err_now = winrec._err_text(e)
        if mid and self.mic_id and mid != self.mic_id:
            log.info("Сменился микрофон (по умолчанию / eCommunications)")
            self.hold_until = max(self.hold_until, time.time() + 20)
            self.last_seen = max(self.last_seen, time.time())
            if self.recorder:
                self.recorder.restart_mic(mid)
        if mid:
            self.mic_id = mid

        # Отслеживаем смену устройства вывода (включая активную аудиосессию встречи / eCommunications).
        sid = detect.meeting_render_device_id(self.source) if self.recorder else None
        if not sid:
            try:
                sid = winrec.soundcard().default_speaker().id
            except Exception as e:
                sid = None
                err_now = err_now or winrec._err_text(e)
        if sid and self.speaker_id and sid != self.speaker_id:
            log.info("Сменилось устройство вывода звука")
            self.hold_until = max(self.hold_until, time.time() + 20)
            self.last_seen = max(self.last_seen, time.time())
            if self.recorder:
                self.recorder.restart_speaker(sid)
        if sid:
            self.speaker_id = sid

        if err_now and err_now != self._dev_err:
            log.warning("Не удалось прочитать список аудиоустройств: %s", err_now)
        self._dev_err = err_now

    def _check_health(self) -> None:
        """Раз в 15 с: дорожки живы (упавшие перезапускаются), микрофон не молчит нулями."""
        if not self.recorder or time.time() - self.health_at < 15:
            return
        self.health_at = time.time()
        h = self.recorder.health()
        if self.is_paused:
            return
        mic = h.get("mic", {})
        if mic.get("restarted"):
            self.silent_checks = 0
        else:
            self.silent_checks = self.silent_checks + 1 if mic.get("peak", 1) == 0 else 0
        warn = None
        if self.silent_checks >= 3:
            warn = "Микрофон не слышно — не выключен ли он (кнопка или Параметры → Звук) и тот ли выбран по умолчанию"
        elif h.get("others", {}).get("restarted") and not self.recorder.is_speaker_muted:
            warn = "Пропадал звук собеседников — восстановил запись"
        try:
            import shutil

            free_b = shutil.disk_usage(self.recorder.folder).free
            if free_b < 150 * 1024 * 1024:
                self.toast("Meeting Agent: место на диске закончилось",
                           "Запись остановлена и сохранена, чтобы не повредить файлы.")
                self.stop("закончилось место на диске")
                return
            if free_b < 500 * 1024 * 1024 and not warn:
                warn = "Мало места на диске (< 500 МБ)"
        except OSError:
            pass
        if warn != self.warning:
            self.warning = warn
            self.notify_backend()
            if warn:
                log.warning(warn)
                self.toast("Meeting Agent: проверьте запись", warn)

    def _tick(self, found: str | None) -> None:
        if self.busy:
            return
        now = time.time()
        active = found == self.auto_source if self.auto_source else found is not None
        if active:
            self.last_seen = now
        if not self.is_recording:
            if self.skip_meeting:
                self.hits = 0
                if now - self.last_seen > self.stop_grace:  # встреча закончилась — снова ждём следующую
                    self.skip_meeting = False
                    self.notify_backend()
                    self.on_change()
                return
            if now < self.cooldown_until:
                self.hits = 0
                return
            if not self.auto_record:
                self.hits = 0
                return
            self.hits = self.hits + 1 if active else 0
            if self.hits >= self.start_after and found:
                self.hits = 0
                self.start(found)
        elif self.auto_source and not self.is_paused and now - self.last_seen > self.stop_grace and now >= self.hold_until:
            self.stop("встреча закончилась")

    def toggle_manual(self) -> None:
        if self.is_recording:
            self.stop_by_user("остановлено вручную")
        else:
            self.start("manual")

    def start_mic_only(self) -> None:
        if not self.is_recording:
            self.start("mic_only")

    def toggle_pause(self) -> None:
        if not self.recorder or self.busy:
            return
        if self.recorder.is_paused:
            self.recorder.resume()
            self.silent_checks = 0
        else:
            self.recorder.pause()
        self.notify_backend()
        self.on_change()

    def stop_by_user(self, reason: str) -> None:
        """Без этого автозапись через пару проверок начала бы новую запись той же встречи."""
        if not self.is_recording:
            return
        if self.auto_source or time.time() - self.last_seen <= self.stop_grace:
            self.skip_meeting = True
            self.cooldown_until = time.time() + 60
        self.stop(reason)

    def start(self, source: str) -> None:
        from .winrec import MeetingRecorder

        with self.lock:
            if self.is_recording or self.busy:
                return
            self.busy = True
        try:
            folder = self.recordings / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            rec = MeetingRecorder(folder, source=source, mic_only=(source == "mic_only"))
            try:
                rec.start()
            except Exception as e:
                import shutil

                shutil.rmtree(folder, ignore_errors=True)
                log.error("Не удалось начать запись: %s", e)
                self.notify_backend(error=str(e))
                threading.Thread(target=_message, args=(f"Не удалось начать запись.\n\n{e}",), daemon=True).start()
                return
            self.recorder, self.source, self.since, self.last_seen = rec, source, time.time(), time.time()
            self.speaker_id = rec.speaker_id
            self.warning, self.silent_checks, self.health_at = None, 0, time.time()
            log.info("Запись началась (%s): %s", source, folder.name)
            self.notify_backend()
            self.on_change()
            if self.calendar:
                threading.Thread(target=self._send_calendar, args=(folder,), daemon=True).start()
        finally:
            self.busy = False

    def stop(self, reason: str) -> None:
        with self.lock:
            rec = self.recorder
            if rec is None or self.busy:
                return
            self.busy = True
        try:
            started, src = self.since or time.time(), self.source
            paused_total = rec.current_paused_seconds()
            rec.stop()
            self.recorder = self.source = self.since = None
            length = max(0.0, time.time() - started - paused_total)
            # Случайно открытое окно Zoom на пару секунд — не встреча.
            too_short = length < self.min_duration if src not in ("manual", "mic_only") else length < 3
            if too_short:
                import shutil

                shutil.rmtree(rec.folder, ignore_errors=True)
            else:
                self.backend.post("process", {"folder": str(rec.folder)})
            log.info("Запись остановлена (%s), %d с", reason, length)
        finally:
            self.busy = False
        self.notify_backend()
        self.on_change()

    def notify_backend(self, error: str | None = None) -> None:
        body: dict = {"state": "recording" if self.is_recording else "idle"}
        if self.since:
            body["since"] = self.since
        if self.source:
            body["source"] = self.source
        if self.recorder:
            body["folder"] = str(self.recorder.folder)
            body["paused"] = self.recorder.is_paused
            body["paused_seconds"] = self.recorder.current_paused_seconds()
            body["speaker_muted"] = self.recorder.is_speaker_muted
        if error:
            body["error"] = error
        if self.skip_meeting and not self.is_recording:
            body["skip_meeting"] = True
        if self.warning and self.is_recording:
            body["warning"] = self.warning
        self.backend.post("recording", body)

    def status_text(self) -> str:
        if self.is_recording:
            paused_s = self.recorder.current_paused_seconds() if self.recorder else 0.0
            mins = max(0, int((time.time() - (self.since or time.time()) - paused_s) / 60))
            what = PLATFORM_TITLES.get(self.source or "")
            prefix = "Пауза записи" if self.is_paused else "Идёт запись"
            return (f"{prefix} встречи {what}" if what else prefix) + f" · {mins} мин"
        if self.skip_meeting:
            return "Запись остановлена — эту встречу больше не записываю"
        return "Жду начала встречи" if self.auto_record else "Автозапись выключена"

    def _send_calendar(self, folder: Path) -> None:
        ev = outlook_current_event()
        if ev:
            self.backend.post("calendar", {"folder": str(folder), **ev})


PLATFORM_TITLES = {"zoom": "Zoom", "teams": "Teams", "telemost": "Телемоста", "meet": "в браузере",
                   "slack": "Slack", "discord": "Discord", "telegram": "Telegram", "whatsapp": "WhatsApp",
                   "jazz": "SaluteJazz", "trueconf": "TrueConf", "ktalk": "Контур.Толка", "vkcalls": "VK Звонков",
                   "dion": "Дион", "webex": "Webex"}

# Текущее событие календаря Outlook: только из уже запущенного Outlook (сами его не открываем).
_OUTLOOK_PS = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
try { $o = [Runtime.InteropServices.Marshal]::GetActiveObject('Outlook.Application') } catch { exit 0 }
$items = $o.GetNamespace('MAPI').GetDefaultFolder(9).Items
$items.IncludeRecurrences = $true
$items.Sort('[Start]')
$now = Get-Date
$f = "[Start] <= '" + $now.AddMinutes(10).ToString('g') + "' AND [End] >= '" + $now.ToString('g') + "'"
$best = $null
foreach ($it in $items.Restrict($f)) {
  if ($it.AllDayEvent) { continue }
  if ($best -eq $null -or $it.Recipients.Count -gt $best.Recipients.Count) { $best = $it }
}
if ($best -eq $null) { exit 0 }
$people = @()
foreach ($r in $best.Recipients) {
  $mail = ''
  try { $mail = $r.AddressEntry.GetExchangeUser().PrimarySmtpAddress } catch { }
  if (-not $mail -and $r.Address -like '*@*') { $mail = $r.Address }
  $people += @{ name = $r.Name; email = $mail }
}
@{ title = $best.Subject; attendees = $people } | ConvertTo-Json -Depth 4 -Compress
"""


def outlook_current_event() -> dict | None:
    """{title, attendees: [{name, email}]} идущей встречи из Outlook или None."""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _OUTLOOK_PS],
                           capture_output=True, timeout=30, creationflags=osutil.CREATE_NO_WINDOW)
        out = r.stdout.decode("utf-8", "replace").strip()
        if not out:
            return None
        data = json.loads(out)
        people = data.get("attendees") or []
        if isinstance(people, dict):  # ConvertTo-Json превращает массив из одного элемента в объект
            people = [people]
        me = os.environ.get("USERNAME", "").lower()
        people = [p for p in people if isinstance(p, dict) and (p.get("name") or "").lower() != me]
        return {"title": str(data.get("title") or ""), "attendees": people}
    except Exception as e:
        log.info("Календарь Outlook недоступен: %s", e)
        return None


# ---------- автозапуск и ярлык ----------

def autostart_enabled() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, TITLE)
            return True
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, TITLE, 0, winreg.REG_SZ, f'"{EXE}"')
        else:
            try:
                winreg.DeleteValue(k, TITLE)
            except OSError:
                pass


def ensure_start_menu_shortcut() -> None:
    """Ярлык в меню «Пуск» (один раз и после переноса папки приложения)."""
    programs = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs"
    link = programs / f"{TITLE}.lnk"
    marker = HOME / "shortcut.txt"
    if link.exists() and marker.exists() and marker.read_text(encoding="utf-8") == EXE:
        return
    def q(v) -> str:  # строка PowerShell в одинарных кавычках: апостроф удваивается (C:\Users\O'Connor)
        return "'" + str(v).replace("'", "''") + "'"

    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({q(link)});"
          f"$s.TargetPath={q(EXE)};$s.WorkingDirectory={q(Path(EXE).parent)};$s.Save()")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, creationflags=osutil.CREATE_NO_WINDOW)
    if r.returncode == 0:
        marker.write_text(EXE, encoding="utf-8")
        if autostart_enabled():
            set_autostart(True)  # путь к программе мог поменяться


# ---------- окно и трей ----------

class App:
    def __init__(self):
        self.backend = Backend()
        self.recording = Recording(self.backend)
        self.window = None
        self.icon = None
        self.processing = 0
        self.failed = 0
        self.seen_done: set[str] = set()
        self.quitting = False
        self.browser_mode = False  # окно не открылось — интерфейс в браузере

    # --- команды из окна (app.js → native()) ---
    def command(self, msg: dict) -> bool:
        cmd = (msg or {}).get("cmd")
        if cmd == "startRecording" and not self.recording.is_recording:
            threading.Thread(target=self.recording.start, args=("manual",), daemon=True).start()
        elif cmd == "startMicOnly" and not self.recording.is_recording:
            threading.Thread(target=self.recording.start_mic_only, daemon=True).start()
        elif cmd == "stopRecording":
            threading.Thread(target=self.recording.stop_by_user, args=("остановлено в окне",), daemon=True).start()
        elif cmd == "togglePause":
            threading.Thread(target=self.recording.toggle_pause, daemon=True).start()
        elif cmd == "reloadSettings":
            threading.Thread(target=self.recording.reload_settings, daemon=True).start()
        elif cmd == "restartApp":
            threading.Thread(target=self._restart, daemon=True).start()
        elif cmd == "openURL" and str(msg.get("url", "")).startswith(("https://", "http://")):
            import webbrowser

            webbrowser.open(msg["url"])
        return True

    def _restart(self) -> None:
        # Новый экземпляр ждёт, пока старый закроется (мьютекс), поэтому сначала выходим, потом запускаемся.
        subprocess.Popen(["cmd", "/c", "ping", "-n", "4", "127.0.0.1", ">nul", "&", "start", "", EXE],
                         creationflags=osutil.CREATE_NO_WINDOW)
        self.quit()

    def event(self, name: str) -> None:
        if self.window:
            try:
                self.window.evaluate_js(f"window.agentEvent && window.agentEvent('{name}')")
            except Exception:
                pass

    def show_window(self, view: str | None = None) -> None:
        if self.browser_mode:
            import webbrowser

            webbrowser.open(self.backend.url)
            return
        if self.window:
            self.window.show()
            self.window.restore()
            if view:
                self.event(view)

    # --- трей ---
    def _image(self):
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        if self.recording.is_recording:
            fill = (232, 152, 32, 255) if self.recording.is_paused else (229, 72, 77, 255)
            d.ellipse((6, 6, 58, 58), fill=fill)
        else:
            d.ellipse((2, 2, 62, 62), fill=(47, 111, 228, 255))
            for i, h in enumerate((14, 30, 22, 36, 18)):  # «волна»
                x = 13 + i * 9
                d.rounded_rectangle((x, 32 - h // 2, x + 5, 32 + h // 2), 2, fill="white")
        return img

    def _menu(self):
        import pystray as ps

        r = self.recording

        def status(_):
            text = r.status_text()
            if not r.is_recording and self.processing:
                text = f"Обработка встреч: {self.processing}"
            return text

        def toggle_auto(*_):
            r.set_auto(not r.auto_record)

        def toggle_login(*_):
            try:
                set_autostart(not autostart_enabled())
            except OSError as e:
                _message(f"Не удалось изменить автозапуск: {e}")

        return ps.Menu(
            ps.MenuItem(status, None, enabled=False),
            ps.MenuItem(lambda _: f"Не удалось обработать: {self.failed} — подробности в окне", None,
                        enabled=False, visible=lambda _: self.failed > 0),
            ps.Menu.SEPARATOR,
            ps.MenuItem("Открыть Meeting Agent", lambda *_: self.show_window(), default=True),
            ps.MenuItem(lambda _: "Остановить запись" if r.is_recording else "Записать сейчас",
                        lambda *_: threading.Thread(target=r.toggle_manual, daemon=True).start()),
            ps.MenuItem("Записать только микрофон (очная встреча)",
                        lambda *_: threading.Thread(target=r.start_mic_only, daemon=True).start(),
                        visible=lambda _: not r.is_recording),
            ps.MenuItem(lambda _: "Продолжить запись" if r.is_paused else "Пауза записи",
                        lambda *_: threading.Thread(target=r.toggle_pause, daemon=True).start(),
                        visible=lambda _: r.is_recording),
            ps.Menu.SEPARATOR,
            ps.MenuItem("Записывать встречи автоматически", toggle_auto, checked=lambda _: r.auto_record),
            ps.MenuItem("Запускать при входе в Windows", toggle_login, checked=lambda _: autostart_enabled()),
            ps.Menu.SEPARATOR,
            ps.MenuItem("Настройки…", lambda *_: self.show_window("settings")),
            ps.MenuItem("Выйти из Meeting Agent", lambda *_: self.quit()),
        )

    def refresh(self) -> None:
        if self.icon:
            self.icon.icon = self._image()
            self.icon.title = self.recording.status_text()
            self.icon.update_menu()
        self.event("status")

    # --- состояние обработки: значок и уведомления о готовых заметках ---
    def poll_status(self) -> None:
        while not self.quitting:
            time.sleep(4)
            s = self.backend.get("status")
            if not s:
                continue
            jobs = s.get("jobs") or []
            self.processing = sum(j.get("status") in ("queued", "running") for j in jobs)
            self.failed = sum(j.get("status") == "error" for j in jobs)
            for j in jobs:
                if j.get("status") == "done" and j["id"] not in self.seen_done:
                    self.seen_done.add(j["id"])
                    if j.get("note") and self.icon:
                        self.icon.notify(Path(j["note"]).stem, "Заметка готова")
            if self.icon:
                self.icon.title = self.recording.status_text()

    def quit(self) -> None:
        self.quitting = True
        if self.recording.is_recording:
            self.recording.stop("выход из приложения")  # дописываем файлы, обработается при следующем запуске
        self.backend.stop()
        if self.icon:
            self.icon.stop()
        if self.window:
            self.window.destroy()

    def run(self) -> None:
        import pystray

        self.backend.start()
        if not self.backend.wait_ready():
            _message("Не удалось запустить Meeting Agent. Подробности — в файле\n"
                     f"{HOME / 'logs' / 'backend.log'}")
            self.backend.stop()
            return
        self.recording.on_change = self.refresh
        self.recording.toast = lambda title, text: self.icon and self.icon.notify(text, title)
        self.recording.reload_settings()
        self.recording.notify_backend()
        threading.Thread(target=self.recording.watch, daemon=True).start()
        threading.Thread(target=self.poll_status, daemon=True).start()
        try:
            ensure_start_menu_shortcut()
        except Exception as e:
            log.warning("Ярлык в меню «Пуск» не создан: %s", e)

        self.icon = pystray.Icon("MeetingAgent", self._image(), TITLE, self._menu())
        threading.Thread(target=self.icon.run, daemon=True).start()

        try:
            import webview

            self.window = webview.create_window(TITLE, self.backend.url, width=1180, height=780,
                                                min_size=(820, 560), js_api=Bridge(self), text_select=True)

            def closing():
                if self.quitting:
                    return True
                self.window.hide()  # закрытие окна прячет его: запись и автозапись продолжают работать
                return False

            self.window.events.closing += closing
            webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
            webview.start(gui="edgechromium", private_mode=False, storage_path=str(HOME / "webview"))
        except Exception as e:
            # Окно не открылось (нет .NET/WebView2, библиотеки заблокированы) — запись и трей работают,
            # а интерфейс открываем в браузере по умолчанию.
            if self.quitting:
                return
            log.exception("Окно приложения не открылось — открываю интерфейс в браузере")
            self.window = None
            self.browser_mode = True
            self.show_window()
            _message("Окно Meeting Agent не открылось, поэтому интерфейс открыт в браузере.\n"
                     "Запись и автозапись Zoom работают; значок — в трее, рядом с часами.\n\n"
                     f"Причина: {e}")
            while not self.quitting:
                time.sleep(1)
        self.quitting = True
        self.backend.stop()


class Bridge:
    """window.pywebview.api.command({cmd, …}) из app.js."""

    def __init__(self, app: App):
        self._app = app

    def command(self, msg):
        return self._app.command(msg)


def _ensure_packages() -> bool:
    """Нет пакетов Python (маленький установщик) или поменялся их список (обновление) — ставим их в окне
    консоли и ждём. False — не получилось, приложение не запускаем."""
    from .updates import packages_needed

    if not packages_needed():
        return True
    log.info("Устанавливаю пакеты Python…")
    python = Path(sys.executable).with_name("python.exe")
    r = subprocess.run([str(python), "-m", "meeting_agent", "bootstrap"], cwd=str(APP_ROOT),
                       creationflags=subprocess.CREATE_NEW_CONSOLE)
    if r.returncode != 0 or packages_needed():
        _message("Компоненты не установлены. Проверьте интернет и запустите Meeting Agent снова.")
        return False
    return True


_INSTANCE_MUTEX = None
_HEARTBEAT_FILE = HOME / ".instance.json"
_HEARTBEAT_INTERVAL = 10.0
_HEARTBEAT_STALE = 35.0


def _write_heartbeat() -> None:
    try:
        osutil.atomic_write_text(_HEARTBEAT_FILE, json.dumps({"pid": os.getpid(), "ts": time.time()}))
    except OSError:
        pass


def _start_heartbeat(stop: threading.Event) -> None:
    _write_heartbeat()

    def loop() -> None:
        while not stop.wait(_HEARTBEAT_INTERVAL):
            _write_heartbeat()

    threading.Thread(target=loop, daemon=True, name="instance-heartbeat").start()


def _read_heartbeat() -> tuple[int, float] | None:
    try:
        data = json.loads(_HEARTBEAT_FILE.read_text(encoding="utf-8"))
        pid = int(data.get("pid", 0))
        ts = float(data.get("ts", 0))
        return (pid, ts) if pid > 0 and ts > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _already_running() -> bool:
    """Второй запуск: показываем окно работающей копии; если старая копия зависла (heartbeat устарел),
    предлагаем пользователю перезапустить её вместо молчаливого выхода."""
    global _INSTANCE_MUTEX
    k32 = ctypes.windll.kernel32
    h = k32.CreateMutexW(None, False, "Local\\MeetingAgentApp")
    if k32.GetLastError() != 183:  # ERROR_ALREADY_EXISTS
        _INSTANCE_MUTEX = h
        return False
    if h:
        k32.CloseHandle(h)
    hb = _read_heartbeat()
    if hb:
        pid, ts = hb
        age = time.time() - ts
        if age >= _HEARTBEAT_STALE and osutil.process_alive(pid):
            # MB_YESNO (0x04) | MB_ICONWARNING (0x30); IDYES == 6
            ans = ctypes.windll.user32.MessageBoxW(
                None,
                "Предыдущая копия Meeting Agent, похоже, перестала отвечать.\n"
                "Закрыть зависший процесс и запустить приложение заново?",
                TITLE,
                0x34,
            )
            if ans == 6:
                log.warning("Завершаю зависший экземпляр PID=%d (heartbeat устарел на %.0f с)", pid, age)
                osutil.terminate_process(pid)
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline and osutil.process_alive(pid):
                    time.sleep(0.15)
                h2 = k32.CreateMutexW(None, False, "Local\\MeetingAgentApp")
                if k32.GetLastError() != 183:
                    _INSTANCE_MUTEX = h2
                    return False
                if h2:
                    k32.CloseHandle(h2)
    hwnd = ctypes.windll.user32.FindWindowW(None, TITLE)
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        ctypes.windll.user32.SetForegroundWindow(hwnd)
    else:
        _message("Meeting Agent уже запущен — его значок в трее, рядом с часами.")
    return True


def main() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "logs").mkdir(exist_ok=True)
    # force: cli.main уже настроил вывод в stderr, а у pythonw.exe его нет.
    logging.basicConfig(filename=HOME / "logs" / "app.log", level=logging.INFO, encoding="utf-8", force=True,
                        format="%(asctime)s  %(name)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # чёткий текст на экранах с масштабом
    except Exception:
        pass
    if _already_running():
        return
    hb_stop = threading.Event()
    _start_heartbeat(hb_stop)
    try:
        try:
            _unblock_downloaded()
        except Exception as e:
            log.warning("Не снял пометку «из интернета»: %s", e)
        try:
            if not _ensure_packages():
                return
        except Exception as e:
            log.exception("Установка пакетов не удалась")
            _message(f"Не удалось установить компоненты: {e}\n\nЖурнал: {HOME / 'logs' / 'app.log'}")
            return
        try:
            from . import winrec

            winrec.soundcard()  # прогрев soundcard в чистом потоке до инициализации COM
        except Exception as e:
            log.warning("Предзагрузка soundcard не удалась: %s", e)
        try:
            App().run()
        except Exception as e:
            log.exception("Сбой приложения")
            _message(f"Meeting Agent завершился с ошибкой:\n{e}\n\nЖурнал: {HOME / 'logs' / 'app.log'}")
    finally:
        hb_stop.set()
        osutil.safe_unlink(_HEARTBEAT_FILE)
