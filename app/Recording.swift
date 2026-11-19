// Запись встреч внутри приложения: вручную (весь звук системы) или автоматически,
// когда появляется окно встречи Zoom. Готовая запись уходит на обработку в сервер.

import AppKit
import AVFoundation
import CoreGraphics

final class RecordingController {
    enum Source: String { case manual, zoom }

    private let backend: Backend
    private var recorder: MeetingRecorder?
    private(set) var source: Source?
    private(set) var since: Date?
    private var busy = false            // идёт запуск или остановка

    // Настройки из сервера (меняются в окне приложения).
    private var autoRecord = true
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

    var onChange: (() -> Void)?
    var isRecording: Bool { recorder != nil }
    var autoRecordEnabled: Bool { autoRecord }

    init(backend: Backend) {
        self.backend = backend
    }

    func reloadSettings(then: (() -> Void)? = nil) {
        backend.get("settings") { [weak self] s in
            guard let self, let s else { then?(); return }
            self.autoRecord = s["auto_record"] as? Bool ?? true
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
    func stopByUser(reason: String) {
        guard isRecording else { return }
        if source == .zoom || zoomMeetingVisible() {
            skipCurrentMeeting = true
        }
        stop(reason: reason)
    }

    // MARK: - Слежение за Zoom

    private func startWatching() {
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: pollSeconds, repeats: true) { [weak self] _ in
            self?.tick()
        }
    }

    private func tick() {
        guard !busy else { return }
        let active = zoomMeetingVisible()
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
            if hits >= startAfterPolls {
                hits = 0
                start(source: .zoom)
            }
        } else if source == .zoom, now.timeIntervalSince(lastSeen) > stopGrace {
            stop(reason: "окно встречи закрыто")
        }
    }

    private func zoomMeetingVisible() -> Bool {
        guard let list = CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as? [[String: Any]] else {
            return false
        }
        let wanted = windowTitles.map { $0.lowercased() }
        var zoomWindows = 0, named = 0
        for w in list {
            let owner = (w[kCGWindowOwnerName as String] as? String ?? "").lowercased()
            guard owner.contains("zoom") else { continue }
            zoomWindows += 1
            let name = (w[kCGWindowName as String] as? String ?? "").lowercased()
            if !name.isEmpty { named += 1 }
            if !name.isEmpty, wanted.contains(where: { name.contains($0) }) { return true }
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
            let target = source == .zoom ? self.zoomBundle : "all"
            let rec = MeetingRecorder(folder: folder, target: target)
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
        }
    }

    func stop(reason: String) {
        guard let rec = recorder, !busy else { return }
        busy = true
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
            let tooShort = src == .zoom ? length < self.minDuration : length < 3
            if tooShort {
                try? FileManager.default.removeItem(at: rec.folder)
            } else {
                self.backend.post("process", ["folder": rec.folder.path])
            }
            NSLog("Meeting Agent: запись остановлена (\(reason)), \(Int(length)) с")
            self.notifyBackend()
            self.onChange?()
        }
    }

    /// Чтобы окно приложения показывало, что идёт запись.
    func notifyBackend(error: String? = nil) {
        var body: [String: Any] = ["state": isRecording ? "recording" : "idle"]
        if let since { body["since"] = since.timeIntervalSince1970 }
        if let source { body["source"] = source.rawValue }
        if let folder = recorder?.folder { body["folder"] = folder.path }
        if let error { body["error"] = error }
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
        if source == .zoom, text.contains("не запущено") { return }  // Zoom закрылся — не беспокоим
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
