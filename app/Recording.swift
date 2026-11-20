// Запись встреч внутри приложения: вручную (весь звук системы) или автоматически, когда начинается
// встреча в Zoom, Teams, Яндекс Телемосте или звонок в браузере (Platforms.swift). Готовая запись уходит
// на обработку в сервер; участники и название берутся из календаря (CalendarLookup.swift), если включено.

import AppKit
import AVFoundation
import CoreGraphics

final class RecordingController {
    enum Source: String {
        case manual, zoom, teams, telemost, meet
        var platform: MeetingPlatform? { MeetingPlatform(rawValue: rawValue) }
    }

    private let backend: Backend
    private var recorder: MeetingRecorder?
    private(set) var source: Source?
    private(set) var since: Date?
    private var busy = false            // идёт запуск или остановка

    // Настройки из сервера (меняются в окне приложения).
    private var autoRecord = true
    private var autoPlatforms: Set<String> = Set(MeetingPlatform.allCases.map(\.rawValue))
    private var calendarEnabled = false
    let calendar = CalendarLookup()
    private var windowTitles = ["Zoom Meeting", "Zoom Webinar", "Конференция Zoom", "Вебинар Zoom"]
    private var zoomBundle = "us.zoom.xos"
    private var pollSeconds = 3.0
    private var startAfterPolls = 2
    private var stopGrace = 30.0
    private var minDuration = 60.0
    private var recordingsDir = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("MeetingAgent/recordings")

    private var hits = 0
    private var lastSeen = Date.distantPast
    private var timer: Timer?
    private(set) var titlesHidden = false  // окна Zoom видны без заголовков: нет разрешения «Запись экрана»
    // Запись остановили вручную, а окно встречи ещё открыто: не начинать её снова до конца этой встречи.
    private(set) var skipCurrentMeeting = false

    // Надёжность записи (как у mymeet): проверка дорожек, пауза автостопа после сна и смены устройства.
    private var echoCancel = true
    let pill = RecordingPill()
    private var healthTimer: Timer?
    private var holdStopUntil = Date.distantPast
    private var micSilentFor = 0.0
    private var micRestarts = 0
    private var warning: String?
    private var warnedAbout = Set<String>()
    private var permTicks = 0

    var onChange: (() -> Void)?
    var isRecording: Bool { recorder != nil }
    var autoRecordEnabled: Bool { autoRecord }

    init(backend: Backend) {
        self.backend = backend
        pill.onStop = { [weak self] in self?.stopByUser(reason: "остановлено на плашке") }
        pill.onSkip = { [weak self] in self?.discardCurrent() }
        // После сна: встреча могла продолжиться — минуту не останавливаем запись, дорожки перезапускаем.
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didWakeNotification, object: nil,
                                                          queue: .main) { [weak self] _ in
            guard let self else { return }
            self.holdStop(60)
            guard let rec = self.recorder else { return }
            rec.restartMic()
            Task { try? await rec.restartOthers() }
        }
    }

    private func holdStop(_ seconds: TimeInterval) {
        holdStopUntil = max(holdStopUntil, Date().addingTimeInterval(seconds))
        lastSeen = max(lastSeen, Date())
    }

    func reloadSettings(then: (() -> Void)? = nil) {
        backend.get("settings") { [weak self] s in
            guard let self, let s else { then?(); return }
            self.autoRecord = s["auto_record"] as? Bool ?? true
            if let p = s["auto_platforms"] as? [String] { self.autoPlatforms = Set(p) }
            self.calendarEnabled = s["calendar_participants"] as? Bool ?? false
            self.echoCancel = s["echo_cancel"] as? Bool ?? true
            self.pill.enabled = s["recording_pill"] as? Bool ?? true
            if self.pill.enabled, let since = self.since, self.isRecording { self.pill.show(since: since, title: self.pillTitle()) }
            if let t = s["window_titles"] as? [String], !t.isEmpty { self.windowTitles = t }
            if let dir = s["recordings_dir"] as? String { self.recordingsDir = URL(fileURLWithPath: dir) }
            if let z = s["zoom"] as? [String: Any] {
                self.zoomBundle = z["bundle_id"] as? String ?? self.zoomBundle
                self.pollSeconds = (z["poll_seconds"] as? NSNumber)?.doubleValue ?? self.pollSeconds
                self.startAfterPolls = (z["start_after_polls"] as? NSNumber)?.intValue ?? self.startAfterPolls
                self.stopGrace = (z["stop_grace_seconds"] as? NSNumber)?.doubleValue ?? self.stopGrace
                self.minDuration = (z["min_duration_seconds"] as? NSNumber)?.doubleValue ?? self.minDuration
            }
            self.startWatching()
            self.reportPermissions()
            self.onChange?()
            then?()
        }
    }

    func setAutoRecord(_ on: Bool) {
        autoRecord = on
        backend.post("settings", ["auto_record": on])
        onChange?()
    }

    // MARK: - Ручная запись

    func toggleManual() {
        isRecording ? stopByUser(reason: "остановлено вручную") : start(source: .manual)
    }

    /// Остановка по кнопке. Без этого автозапись через пару проверок начала бы новую запись той же встречи.
    func stopByUser(reason: String, process: Bool = true) {
        guard isRecording else { return }
        if source != .manual || activePlatform() != nil {
            skipCurrentMeeting = true
        }
        stop(reason: reason, process: process)
    }

    // MARK: - Слежение за Zoom

    private func startWatching() {
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: pollSeconds, repeats: true) { [weak self] _ in
            self?.tick()
        }
    }

    private func tick() {
        permTicks += 1
        if permTicks * Int(pollSeconds) >= 60 { permTicks = 0; reportPermissions() }
        guard !busy else { return }
        let current = isRecording ? source : nil
        let found = activePlatform(recording: current == .manual ? nil : current)
        let active = isRecording && source != .manual ? found?.rawValue == source?.rawValue : found != nil
        let now = Date()
        if active { lastSeen = now }

        if !isRecording {
            if skipCurrentMeeting {
                hits = 0
                if now.timeIntervalSince(lastSeen) > stopGrace {  // встреча закончилась — снова ждём следующую
                    skipCurrentMeeting = false
                    notifyBackend()
                    onChange?()
                }
                return
            }
            guard autoRecord else { hits = 0; return }
            hits = active ? hits + 1 : 0
            if hits >= startAfterPolls, let platform = found {
                hits = 0
                start(source: Source(rawValue: platform.rawValue) ?? .zoom)
            }
        } else if source != .manual, now.timeIntervalSince(lastSeen) > stopGrace, now >= holdStopUntil {
            stop(reason: "встреча закончилась")
        }
    }

    /// Какая встреча идёт сейчас. recording — платформа идущей автозаписи: для неё признаки мягче
    /// (вкладку со звонком в браузере могли переключить — пока браузер пишет микрофон, встреча идёт).
    private func activePlatform(recording current: Source? = nil) -> MeetingPlatform? {
        let wins = Platforms.windows()
        if autoPlatforms.contains("zoom"), zoomMeetingVisible(wins) { return .zoom }
        let users = Platforms.micUsers()
        func app(_ p: MeetingPlatform, bundles: [String], owners: [String]) -> Bool {
            guard autoPlatforms.contains(p.rawValue) else { return false }
            if let users { return Platforms.uses(users, bundles) }
            // macOS 13–14.1: кто пишет микрофон, не узнать — окно программы плюс работающий микрофон.
            return Platforms.appWindow(wins, owners) && (current == Source(rawValue: p.rawValue) || Platforms.micRunningSomewhere())
        }
        if app(.teams, bundles: Platforms.teamsBundles, owners: ["microsoft teams", "teams"]) { return .teams }
        if app(.telemost, bundles: Platforms.telemostBundles, owners: ["телемост", "telemost"]) { return .telemost }
        if autoPlatforms.contains("meet") {
            let browserMic = users.map { Platforms.uses($0, Platforms.browserBundles) } ?? Platforms.micRunningSomewhere()
            if browserMic && (current == .meet || Platforms.browserCallWindow(wins)) { return .meet }
        }
        return nil
    }

    private func zoomMeetingVisible(_ wins: [(owner: String, name: String)]) -> Bool {
        let wanted = windowTitles.map { $0.lowercased() }
        var zoomWindows = 0, named = 0
        for w in wins where w.owner.contains("zoom") {
            zoomWindows += 1
            if !w.name.isEmpty { named += 1 }
            if !w.name.isEmpty, wanted.contains(where: { w.name.contains($0) }) { return true }
        }
        // Без разрешения «Запись экрана» macOS не отдаёт заголовки окон чужих приложений.
        let hidden = zoomWindows > 0 && named == 0
        if hidden != titlesHidden {
            titlesHidden = hidden
            onChange?()
        }
        return false
    }

    // MARK: - Старт и стоп

    private func start(source: Source) {
        guard !isRecording, !busy else { return }
        busy = true
        Task { @MainActor in
            defer { self.busy = false }
            guard await self.ensureMicrophone() else {
                self.alert("Нет доступа к микрофону",
                           "Разрешите Meeting Agent доступ к микрофону в Системных настройках → Конфиденциальность и безопасность → Микрофон.",
                           settingsPane: "Privacy_Microphone")
                return
            }
            let fmt = DateFormatter()
            fmt.dateFormat = "yyyy-MM-dd_HH-mm-ss"
            let folder = self.recordingsDir.appendingPathComponent(fmt.string(from: Date()), isDirectory: true)
            // Zoom — только его звук; остальные (Teams, Телемост, браузер) — весь звук системы:
            // их звук играют вспомогательные процессы, и по одной программе его можно не поймать.
            let target = source == .zoom ? self.zoomBundle : "all"
            let rec = MeetingRecorder(folder: folder, target: target)
            rec.echoCancel = self.echoCancel
            rec.onDeviceChange = { [weak self] in self?.holdStop(20) }  // сменили микрофон — это не конец встречи
            do {
                try await rec.start()
            } catch {
                try? FileManager.default.removeItem(at: folder)
                self.reportStartError(error, source: source)
                return
            }
            self.recorder = rec
            self.source = source
            self.since = Date()
            self.lastSeen = Date()
            self.notifyBackend()
            self.onChange?()
            self.sendCalendar(folder: folder)
            self.pill.show(since: self.since ?? Date(), title: self.pillTitle())
            self.startHealthChecks()
        }
    }

    private func pillTitle() -> String {
        switch source {
        case .zoom: return "Zoom"
        case .teams: return "Teams"
        case .telemost: return "Телемост"
        case .meet: return "звонок в браузере"
        default: return ""
        }
    }

    // MARK: - Проверка записи во время встречи

    private func startHealthChecks() {
        micSilentFor = 0; micRestarts = 0; warning = nil; warnedAbout = []
        healthTimer?.invalidate()
        healthTimer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in self?.checkHealth() }
    }

    private func checkHealth() {
        guard let rec = recorder, !busy else { return }
        let (mic, others) = rec.health()
        let now = Date()
        var problems: [String] = []
        // Микрофон перестал присылать звук (устройство пропало) — перезапускаем на текущем.
        if mic.stopped || (mic.lastBufferAt.map { now.timeIntervalSince($0) > 10 } ?? true) {
            micRestarts += 1
            rec.restartMic()
            if micRestarts >= 2 { problems.append("Микрофон не отвечает — проверьте, подключён ли он") }
        } else {
            micRestarts = 0
            micSilentFor = mic.peakSinceCheck == 0 ? micSilentFor + 15 : 0  // ровно 0 — выключен звук или нет доступа
            if micSilentFor >= 45 {
                problems.append("Микрофон не слышно — не выключен ли звук микрофона и тот ли вход выбран в macOS")
            }
        }
        if others.stopped {
            problems.append("Пропал звук собеседников — восстанавливаю")
            Task { try? await rec.restartOthers() }
        }
        let text = problems.first
        if text != warning {
            warning = text
            pill.setWarning(text)
            notifyBackend()
            if let text, !warnedAbout.contains(text) {
                warnedAbout.insert(text)
                Self.notify("Meeting Agent: проверьте запись", text)
            }
        }
    }

    static func notify(_ title: String, _ text: String) {
        let esc = { (s: String) in s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"") }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        p.arguments = ["-e", "display notification \"\(esc(text))\" with title \"\(esc(title))\""]
        try? p.run()
    }

    /// «✕» на плашке: остановить и не сохранять эту встречу (после подтверждения).
    private func discardCurrent() {
        guard let rec = recorder else { return }
        let a = NSAlert()
        a.messageText = "Не записывать эту встречу?"
        a.informativeText = "Запись остановится и будет удалена, протокол не составится. Следующая встреча запишется как обычно."
        a.addButton(withTitle: "Удалить запись")
        a.addButton(withTitle: "Отмена")
        NSApp.activate(ignoringOtherApps: true)
        guard a.runModal() == .alertFirstButtonReturn else { return }
        let folder = rec.folder
        stopByUser(reason: "не записывать эту встречу", process: false)
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
            try? FileManager.default.trashItem(at: folder, resultingItemURL: nil)
        }
    }

    // MARK: - Разрешения macOS (окно приложения подсказывает, что включить)

    func reportPermissions() {
        let mic: String
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: mic = "granted"
        case .notDetermined: mic = "unknown"
        default: mic = "denied"
        }
        var body: [String: Any] = ["mic": mic, "screen": CGPreflightScreenCaptureAccess() ? "granted" : "denied",
                                   "titles_hidden": titlesHidden]
        if calendarEnabled { body["calendar"] = calendar.authorized ? "granted" : "denied" }
        backend.post("permissions", body)
    }

    func stop(reason: String, process: Bool = true) {
        guard let rec = recorder, !busy else { return }
        busy = true
        healthTimer?.invalidate()
        healthTimer = nil
        pill.hide()
        warning = nil
        let started = since ?? Date()
        let src = source
        Task { @MainActor in
            await rec.stop()
            self.recorder = nil
            self.source = nil
            self.since = nil
            self.busy = false
            let length = Date().timeIntervalSince(started)
            // Случайно открытое окно Zoom на пару секунд — не встреча.
            let tooShort = src != .manual ? length < self.minDuration : length < 3
            if tooShort {
                try? FileManager.default.removeItem(at: rec.folder)
            } else if process {
                self.backend.post("process", ["folder": rec.folder.path])
            }
            NSLog("Meeting Agent: запись остановлена (\(reason)), \(Int(length)) с")
            self.notifyBackend()
            self.onChange?()
        }
    }

    /// Название и участники встречи из календаря — серверу (станут списком участников записи).
    private func sendCalendar(folder: URL) {
        guard calendarEnabled, calendar.authorized, let ev = calendar.currentEvent() else { return }
        backend.post("calendar", ["folder": folder.path, "title": ev.title, "attendees": ev.attendees])
    }

    func requestCalendarAccess() {
        calendar.requestAccess { [weak self] ok in
            guard let self, !ok else { return }
            Task { @MainActor in self.alert("Нет доступа к календарю",
                       "Разрешите Meeting Agent доступ к календарям в Системных настройках → Конфиденциальность и безопасность → Календари.",
                       settingsPane: "Privacy_Calendars") }
        }
    }

    /// Чтобы окно приложения показывало, что идёт запись.
    func notifyBackend(error: String? = nil) {
        var body: [String: Any] = ["state": isRecording ? "recording" : "idle"]
        if let since { body["since"] = since.timeIntervalSince1970 }
        if let source { body["source"] = source.rawValue }
        if let folder = recorder?.folder { body["folder"] = folder.path }
        if let error { body["error"] = error }
        if let warning, isRecording { body["warning"] = warning }
        if let rec = recorder { body["echo_cancel"] = rec.echoCancelActive }
        if skipCurrentMeeting && !isRecording { body["skip_meeting"] = true }
        backend.post("recording", body)
    }

    private func ensureMicrophone() async -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return true
        case .notDetermined: return await AVCaptureDevice.requestAccess(for: .audio)
        default: return false
        }
    }

    @MainActor
    private func reportStartError(_ error: Error, source: Source) {
        let text = error.localizedDescription
        NSLog("Meeting Agent: не удалось начать запись: \(text)")
        notifyBackend(error: text)
        if source != .manual, text.contains("не запущено") { return }  // программа встречи закрылась — не беспокоим
        let lower = text.lowercased()
        if lower.contains("permission") || lower.contains("разрешен") || lower.contains("denied")
            || lower.contains("declined") || lower.contains("tcc") || (error as NSError).code == -3801 {
            alert("Нет разрешения на запись звука",
                  "Разрешите Meeting Agent «Запись экрана и системного звука» в Системных настройках → Конфиденциальность и безопасность, затем перезапустите приложение. Без этого разрешения не слышно собеседников.",
                  settingsPane: "Privacy_ScreenCapture")
        } else {
            alert("Не удалось начать запись", text, settingsPane: nil)
        }
    }

    @MainActor
    private func alert(_ title: String, _ text: String, settingsPane: String?) {
        let a = NSAlert()
        a.messageText = title
        a.informativeText = text
        if settingsPane != nil { a.addButton(withTitle: "Открыть настройки") }
        a.addButton(withTitle: settingsPane != nil ? "Позже" : "ОК")
        NSApp.activate(ignoringOtherApps: true)
        if a.runModal() == .alertFirstButtonReturn, let pane = settingsPane,
           let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?\(pane)") {
            NSWorkspace.shared.open(url)
        }
    }
}
