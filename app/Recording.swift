// Запись встреч внутри приложения: вручную (весь звук системы) или автоматически / по подтверждению,
// когда начинается встреча в Zoom, Teams, Яндекс Телемосте, Slack, Discord, Webex, Telegram, WhatsApp
// или звонок в браузере (Platforms.swift). Готовая запись уходит на обработку в сервер;
// участники и название берутся из календаря (CalendarLookup.swift), если включено.

import AppKit
import AVFoundation
import CoreAudio
import CoreGraphics
import IOKit

final class RecordingController {
    enum Source: String {
        case manual, mic_only, zoom, teams, telemost, meet, slack, discord, webex, telegram, whatsapp
        case jazz, ktalk, vkcalls, dion, trueconf
        var platform: MeetingPlatform? { MeetingPlatform(rawValue: rawValue) }
        var isManualLike: Bool { self == .manual || self == .mic_only }
    }

    private let backend: Backend
    private var recorder: MeetingRecorder?
    private(set) var source: Source?
    private(set) var since: Date?
    private var busy = false            // идёт запуск или остановка

    // Настройки из сервера (меняются в окне приложения).
    private var autoRecord = true
    private var autoRecordMode = "auto"   // "auto" | "ask"
    private var autoPlatforms: Set<String> = Set(["zoom", "teams", "telemost", "meet"])
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
    private var triggerCooldownUntil = Date.distantPast
    private var promptedPlatform: MeetingPlatform?

    // Надёжность записи: проверка дорожек, пауза автостопа после сна/Breakout Rooms/смены устройства,
    // защита от iPhone Continuity Mic и поиск активного микрофона.
    private var echoCancel = true
    private var skipContinuityMic = true
    private(set) var showInTray = true
    private(set) var minimizeToTray = true
    let pill = RecordingPill()
    private let meetingPrompt = MeetingPromptPanel()
    private var healthTimer: Timer?
    private var levelTimer: Timer?
    private var holdStopUntil = Date.distantPast
    private var zoomWindowWasVisible = false
    private var micSilentFor = 0.0
    private var micRestarts = 0
    private var probingMic = false
    private var suggestedMic: (id: AudioDeviceID, name: String)?
    private var warning: String?
    private var warnedAbout = Set<String>()
    private var permTicks = 0

    var onChange: (() -> Void)?
    var isRecording: Bool { recorder != nil }
    var isPaused: Bool { recorder?.isPaused ?? false }
    var autoRecordEnabled: Bool { autoRecord }

    init(backend: Backend) {
        self.backend = backend
        pill.onTogglePause = { [weak self] in self?.togglePause() }
        pill.onStop = { [weak self] in self?.stopByUser(reason: "остановлено на плашке") }
        pill.onSkip = { [weak self] in self?.discardCurrent() }
        pill.onWarningAction = { [weak self] in self?.applySuggestedMic() }
        meetingPrompt.onAccept = { [weak self] in
            guard let self, let p = self.promptedPlatform else { return }
            self.promptedPlatform = nil
            self.start(source: Source(rawValue: p.rawValue) ?? .zoom)
        }
        meetingPrompt.onDismiss = { [weak self] in
            guard let self else { return }
            self.promptedPlatform = nil
            self.skipCurrentMeeting = true
            self.triggerCooldownUntil = Date().addingTimeInterval(60)
            self.notifyBackend()
            self.onChange?()
        }
        // Перед сном (кроме режима закрытой крышки с внешним монитором): сбрасываем meta.json и держим автостоп.
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.willSleepNotification, object: nil,
                                                          queue: .main) { [weak self] _ in
            guard let self else { return }
            if !Self.isClamshellWithExternalDisplay() {
                self.holdStop(90)
                self.recorder?.flushMeta()
            }
        }
        // После сна: встреча могла продолжиться — минуту не останавливаем запись, дорожки перезапускаем.
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didWakeNotification, object: nil,
                                                          queue: .main) { [weak self] _ in
            guard let self else { return }
            self.holdStop(60)
            guard let rec = self.recorder else { return }
            rec.restartMic()
            if !rec.micOnly { Task { try? await rec.restartOthers() } }
        }
    }

    /// Закрыта ли крышка MacBook при подключённом внешнем мониторе (Clamshell mode — это не сон).
    private static func isClamshellWithExternalDisplay() -> Bool {
        var displays = [CGDirectDisplayID](repeating: 0, count: 8)
        var count: UInt32 = 0
        guard CGGetOnlineDisplayList(8, &displays, &count) == .success, count > 0 else { return false }
        let hasExternal = (0..<Int(count)).contains { CGDisplayIsBuiltin(displays[$0]) == 0 }
        guard hasExternal else { return false }
        let svc = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOPMrootDomain"))
        guard svc != 0 else { return false }
        defer { IOObjectRelease(svc) }
        return (IORegistryEntryCreateCFProperty(svc, "AppleClamshellState" as CFString, kCFAllocatorDefault, 0)?
            .takeRetainedValue() as? Bool) == true
    }

    private func holdStop(_ seconds: TimeInterval) {
        holdStopUntil = max(holdStopUntil, Date().addingTimeInterval(seconds))
        lastSeen = max(lastSeen, Date())
    }

    func reloadSettings(then: (() -> Void)? = nil) {
        backend.get("settings") { [weak self] s in
            guard let self, let s else { then?(); return }
            self.autoRecord = s["auto_record"] as? Bool ?? true
            self.autoRecordMode = s["auto_record_mode"] as? String ?? "auto"
            if let p = s["auto_platforms"] as? [String] { self.autoPlatforms = Set(p) }
            self.calendarEnabled = s["calendar_participants"] as? Bool ?? false
            self.echoCancel = s["echo_cancel"] as? Bool ?? true
            self.skipContinuityMic = s["skip_continuity_mic"] as? Bool ?? true
            self.showInTray = s["show_in_tray"] as? Bool ?? true
            self.minimizeToTray = s["minimize_to_tray"] as? Bool ?? true
            self.pill.enabled = s["recording_pill"] as? Bool ?? true
            if self.pill.enabled, let since = self.since, let rec = self.recorder {
                self.pill.show(since: since, title: self.pillTitle(), pausedSeconds: { [weak rec] in rec?.currentPausedSeconds() ?? 0 })
                self.pill.setPaused(rec.isPaused)
            }
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
        if !on { meetingPrompt.hide(); promptedPlatform = nil }
        onChange?()
    }

    // MARK: - Ручная запись и пауза

    func toggleManual() {
        isRecording ? stopByUser(reason: "остановлено вручную") : start(source: .manual)
    }

    /// Очная встреча / диктофон: записывается только микрофон, системный звук не трогается.
    func startMicOnly() {
        guard !isRecording else { return }
        start(source: .mic_only)
    }

    func togglePause() {
        guard let rec = recorder, !busy else { return }
        if rec.isPaused {
            rec.resume()
            micSilentFor = 0
        } else {
            rec.pause()
        }
        pill.setPaused(rec.isPaused)
        notifyBackend()
        onChange?()
    }

    /// Остановка по кнопке. Без этого автозапись через пару проверок начала бы новую запись той же встречи.
    func stopByUser(reason: String, process: Bool = true) {
        guard isRecording else { return }
        if source?.isManualLike == false || activePlatform() != nil {
            skipCurrentMeeting = true
            triggerCooldownUntil = Date().addingTimeInterval(60)
        }
        stop(reason: reason, process: process)
    }

    // MARK: - Слежение за встречами

    private func startWatching() {
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: pollSeconds, repeats: true) { [weak self] _ in
            self?.tick()
        }
    }

    private func tick() {
        permTicks += 1
        if permTicks * Int(max(1, pollSeconds)) >= 60 { permTicks = 0; reportPermissions() }
        guard !busy else { return }
        let current = isRecording ? source : nil
        let found = activePlatform(recording: current?.isManualLike == true ? nil : current)
        let active = isRecording && source?.isManualLike == false ? found?.rawValue == source?.rawValue : found != nil
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
            if now < triggerCooldownUntil {
                hits = 0
                return
            }
            if promptedPlatform != nil {
                if !active && now.timeIntervalSince(lastSeen) > stopGrace {
                    meetingPrompt.hide()
                    promptedPlatform = nil
                }
                return
            }
            guard autoRecord else { hits = 0; return }
            hits = active ? hits + 1 : 0
            if hits >= startAfterPolls, let platform = found {
                hits = 0
                if autoRecordMode == "ask" {
                    promptedPlatform = platform
                    meetingPrompt.show(platformTitle: platform.shortTitle)
                } else {
                    start(source: Source(rawValue: platform.rawValue) ?? .zoom)
                }
            }
        } else if source?.isManualLike == false, !isPaused, now.timeIntervalSince(lastSeen) > stopGrace, now >= holdStopUntil {
            stop(reason: "встреча закончилась")
        }
    }

    /// Какая встреча идёт сейчас. recording — платформа идущей автозаписи: для неё признаки мягче
    /// (вкладку со звонком в браузере могли переключить — пока браузер пишет микрофон, встреча идёт;
    /// в Zoom при переходе в Breakout Room окно исчезает, но аудиопроцесс Zoom ещё жив).
    private func activePlatform(recording current: Source? = nil) -> MeetingPlatform? {
        let wins = Platforms.windows()
        let users = Platforms.micUsers()
        if autoPlatforms.contains("zoom") {
            let visible = zoomMeetingVisible(wins)
            let zoomPrefix = [zoomBundle.lowercased()]
            if visible {
                zoomWindowWasVisible = true
                return .zoom
            }
            // Без разрешения «Запись экрана» (или во время перехода в Breakout Room): проверяем аудио Zoom.
            if let users, Platforms.uses(users, zoomPrefix) {
                return .zoom
            }
            if current == .zoom {
                // Переход в сессионный зал (Breakout Room): окно закрылось, но Zoom ещё выводит звук или только что был активен.
                if let outUsers = Platforms.outputUsers(), Platforms.uses(outUsers, zoomPrefix) {
                    return .zoom
                }
                if zoomWindowWasVisible && Platforms.isZoomRunning(bundleId: zoomBundle) {
                    zoomWindowWasVisible = false
                    holdStop(max(stopGrace + 35, 65))
                }
            }
        }
        func app(_ p: MeetingPlatform, bundles: [String], owners: [String]) -> Bool {
            let enabled = autoPlatforms.contains(p.rawValue) || (p.isCorp && autoPlatforms.contains("corp"))
            guard enabled else { return false }
            if let users { return Platforms.uses(users, bundles) }
            // macOS 13–14.1: кто пишет микрофон, не узнать — окно программы плюс работающий микрофон.
            return Platforms.appWindow(wins, owners) && (current == Source(rawValue: p.rawValue) || Platforms.micRunningSomewhere())
        }
        if app(.teams, bundles: Platforms.teamsBundles, owners: ["microsoft teams", "teams"]) { return .teams }
        if app(.telemost, bundles: Platforms.telemostBundles, owners: ["телемост", "telemost"]) { return .telemost }
        if app(.slack, bundles: Platforms.slackBundles, owners: ["slack"]) { return .slack }
        if app(.discord, bundles: Platforms.discordBundles, owners: ["discord"]) { return .discord }
        if app(.webex, bundles: Platforms.webexBundles, owners: ["webex", "cisco"]) { return .webex }
        if app(.telegram, bundles: Platforms.telegramBundles, owners: ["telegram"]) { return .telegram }
        if app(.whatsapp, bundles: Platforms.whatsappBundles, owners: ["whatsapp"]) { return .whatsapp }
        if app(.jazz, bundles: Platforms.jazzBundles, owners: ["salutejazz", "jazz"]) { return .jazz }
        if app(.ktalk, bundles: Platforms.ktalkBundles, owners: ["контур.толк", "ktalk", "толк"]) { return .ktalk }
        if app(.vkcalls, bundles: Platforms.vkCallsBundles, owners: ["vk звонки", "vk calls"]) { return .vkcalls }
        if app(.dion, bundles: Platforms.dionBundles, owners: ["dion"]) { return .dion }
        if app(.trueconf, bundles: Platforms.trueconfBundles, owners: ["trueconf"]) { return .trueconf }
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
        meetingPrompt.hide()
        promptedPlatform = nil
        busy = true
        Task { @MainActor in
            defer { self.busy = false }
            guard await self.ensureMicrophone() else {
                self.alert("Нет доступа к микрофону",
                           "Разрешите Meeting Agent доступ к микрофону в Системных настройках → Конфиденциальность и безопасность → Микрофон.",
                           settingsPane: "Privacy_Microphone")
                return
            }
            if let free = CoreAudioTools.freeDiskSpaceBytes(for: self.recordingsDir), free < 500 * 1024 * 1024 {
                self.alert("Недостаточно места на диске",
                           "Свободно меньше 500 МБ (\(free / (1024 * 1024)) МБ). Освободите место на диске перед записью встречи.",
                           settingsPane: nil)
                return
            }
            let fmt = DateFormatter()
            fmt.dateFormat = "yyyy-MM-dd_HH-mm-ss"
            let folder = self.recordingsDir.appendingPathComponent(fmt.string(from: Date()), isDirectory: true)
            let micOnly = (source == .mic_only)
            // Zoom — только его звук; остальные площадки — весь звук системы; mic_only — только микрофон.
            let target = source == .zoom ? self.zoomBundle : "all"
            let rec = MeetingRecorder(folder: folder, target: target, micOnly: micOnly)
            rec.echoCancel = self.echoCancel
            rec.skipContinuityMic = self.skipContinuityMic
            rec.onDeviceChange = { [weak controller = self] in controller?.holdStop(20) }  // сменили микрофон — это не конец встречи
            rec.onSpeakerMuteChange = { [weak controller = self] _ in
                DispatchQueue.main.async { controller?.notifyBackend() }
            }
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
            self.zoomWindowWasVisible = (source == .zoom)
            self.notifyBackend()
            self.onChange?()
            self.sendCalendar(folder: folder)
            self.pill.show(since: self.since ?? Date(), title: self.pillTitle(),
                           pausedSeconds: { [weak rec] in rec?.currentPausedSeconds() ?? 0 })
            self.startHealthChecks()
            self.startLevelMeter()
            if let rej = rec.continuityRejected, !rec.activeMicName.isEmpty {
                Self.notify("Meeting Agent: микрофон iPhone пропущен",
                            "Запись идёт с «\(rec.activeMicName)» вместо «\(rej)».")
            }
        }
    }

    private func pillTitle() -> String {
        if source == .mic_only { return "Микрофон" }
        return source?.platform?.shortTitle ?? ""
    }

    // MARK: - Живой индикатор звука и проверка записи во время встречи

    private func startLevelMeter() {
        levelTimer?.invalidate()
        levelTimer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            guard let self, let rec = self.recorder else { return }
            let (m, o) = rec.levels()
            self.pill.updateLevels(mic: m, others: o)
        }
    }

    private func startHealthChecks() {
        micSilentFor = 0; micRestarts = 0; probingMic = false; suggestedMic = nil
        warning = nil; warnedAbout = []
        healthTimer?.invalidate()
        healthTimer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in self?.checkHealth() }
    }

    private func checkHealth() {
        guard let rec = recorder, !busy, !rec.isPaused else { return }
        let (mic, others) = rec.health()
        let now = Date()
        var problems: [String] = []
        var actionTitle: String? = nil

        // Микрофон перестал присылать звук (устройство пропало) — перезапускаем на текущем.
        if mic.stopped || (mic.lastBufferAt.map { now.timeIntervalSince($0) > 10 } ?? true) {
            micRestarts += 1
            suggestedMic = nil
            rec.restartMic()
            if micRestarts >= 2 { problems.append("Микрофон не отвечает — проверьте, подключён ли он") }
        } else {
            micRestarts = 0
            if mic.peakSinceCheck == 0 {
                micSilentFor += 15
                // Реальный микрофон всегда имеет фоновый шум > 0; ровные нули при включённом VPIO —
                // признак конфликта Voice Processing с устройством/Zoom: отключаем VPIO и перезапускаем.
                if rec.echoCancelActive && micSilentFor >= 15 {
                    NSLog("Meeting Agent: Voice Processing отдаёт нули — перезапускаю микрофон без VPIO")
                    micSilentFor = 0
                    rec.disableEchoCancelAndRestart()
                    notifyBackend()
                    return
                }
            } else {
                micSilentFor = 0
                suggestedMic = nil
            }
            if micSilentFor >= 45 {
                if let sug = suggestedMic {
                    problems.append("Микрофон молчит — слышно «\(sug.name)»")
                    actionTitle = "Переключить"
                } else {
                    problems.append("Микрофон не слышно — проверьте вход и кнопку mute")
                    if !probingMic {
                        probingMic = true
                        DispatchQueue.global(qos: .utility).async { [weak self, weak rec] in
                            let found = rec?.probeOtherMic()
                            DispatchQueue.main.async {
                                guard let self else { return }
                                self.probingMic = false
                                if let found, self.micSilentFor >= 45, self.isRecording {
                                    self.suggestedMic = (found.id, found.name)
                                    let msg = "Микрофон молчит — слышно «\(found.name)»"
                                    self.warning = msg
                                    self.pill.setWarning(msg, actionTitle: "Переключить")
                                    self.notifyBackend()
                                    if !self.warnedAbout.contains(msg) {
                                        self.warnedAbout.insert(msg)
                                        Self.notify("Meeting Agent: проверьте микрофон",
                                                    "Текущий микрофон молчит, но есть звук на «\(found.name)». Нажмите «Переключить» на плашке.")
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        if !rec.micOnly && others.stopped && !rec.isSpeakerMuted {
            problems.append("Пропал звук собеседников — восстанавливаю")
            Task { try? await rec.restartOthers() }
        }
        if let free = CoreAudioTools.freeDiskSpaceBytes(for: rec.folder) {
            if free < 150 * 1024 * 1024 {
                Self.notify("Meeting Agent: место на диске закончилось",
                            "Запись остановлена и сохранена, чтобы не повредить файлы.")
                stop(reason: "закончилось место на диске")
                return
            } else if free < 500 * 1024 * 1024 {
                problems.append("Мало места на диске (< 500 МБ)")
            }
        }
        let text = problems.first
        if text != warning || (text != nil && actionTitle != nil) {
            warning = text
            pill.setWarning(text, actionTitle: actionTitle)
            notifyBackend()
            if let text, !warnedAbout.contains(text) {
                warnedAbout.insert(text)
                Self.notify("Meeting Agent: проверьте запись", text)
            }
        }
    }

    private func applySuggestedMic() {
        guard let rec = recorder, let sug = suggestedMic else { return }
        suggestedMic = nil
        micSilentFor = 0
        warning = nil
        pill.setWarning(nil)
        holdStop(20)
        rec.switchMic(to: sug.id)
        notifyBackend()
        Self.notify("Meeting Agent", "Микрофон переключён на «\(sug.name)»")
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

    // MARK: - Разрешения macOS (свежая проверка TCC в дочернем процессе без кэша)

    func reportPermissions() {
        let mic: String
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: mic = "granted"
        case .notDetermined: mic = "unknown"
        default: mic = "denied"
        }
        let inProcessScreen = CGPreflightScreenCaptureAccess()
        let processTapSupported: Bool
        if #available(macOS 14.2, *) { processTapSupported = true } else { processTapSupported = false }

        let send: (String) -> Void = { [weak self] screenState in
            guard let self else { return }
            var body: [String: Any] = [
                "mic": mic,
                "screen": screenState,
                "titles_hidden": self.titlesHidden,
                "process_tap": processTapSupported,
            ]
            if self.calendarEnabled { body["calendar"] = self.calendar.authorized ? "granted" : "denied" }
            self.backend.post("permissions", body)
        }

        if inProcessScreen {
            send("granted")
            return
        }
        // Свежая проверка TCC в коротком дочернем процессе: CGPreflightScreenCaptureAccess кэшируется в процессе.
        DispatchQueue.global(qos: .utility).async {
            let fresh = Self.probeScreenCapturePermissionFresh()
            DispatchQueue.main.async {
                send(fresh ? "granted_needs_restart" : "denied")
            }
        }
    }

    private static func probeScreenCapturePermissionFresh() -> Bool {
        guard let exe = Bundle.main.executablePath else { return CGPreflightScreenCaptureAccess() }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = ["--check-screen-perm"]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do {
            try p.run()
            p.waitUntilExit()
            return p.terminationStatus == 0
        } catch {
            return CGPreflightScreenCaptureAccess()
        }
    }

    func stop(reason: String, process: Bool = true) {
        guard let rec = recorder, !busy else { return }
        busy = true
        healthTimer?.invalidate(); healthTimer = nil
        levelTimer?.invalidate(); levelTimer = nil
        pill.hide()
        warning = nil
        suggestedMic = nil
        let started = since ?? Date()
        let pausedTotal = rec.currentPausedSeconds()
        let src = source
        Task { @MainActor in
            await rec.stop()
            self.recorder = nil
            self.source = nil
            self.since = nil
            self.busy = false
            self.zoomWindowWasVisible = false
            let length = max(0, Date().timeIntervalSince(started) - pausedTotal)
            // Случайно открытое окно Zoom на пару секунд — не встреча.
            let tooShort = src?.isManualLike == false ? length < self.minDuration : length < 3
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
        if let rec = recorder {
            body["echo_cancel"] = rec.echoCancelActive
            body["paused"] = rec.isPaused
            body["paused_seconds"] = rec.currentPausedSeconds()
            body["capture_mode"] = rec.captureMode
            body["speaker_muted"] = rec.isSpeakerMuted
        }
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
                  "Разрешите Meeting Agent запись системного звука в Системных настройках → Конфиденциальность и безопасность → Запись экрана и системного звука, затем перезапустите приложение.",
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
