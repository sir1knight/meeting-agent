// Meeting Agent.app (точка входа): значок в строке меню, окно с протоколами, запись встреч.
//
// Сборка — ./build_app.sh (Python, модели и интерфейс кладутся внутрь .app).

import AppKit
import ServiceManagement

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let backend = Backend()
    private lazy var recording = RecordingController(backend: backend)
    private lazy var window = WebWindowController(backend: backend)
    private var statusItem: NSStatusItem!
    private var processing = 0      // встреч в обработке
    private var failed = 0
    private var statusTimer: Timer?

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.setActivationPolicy(.accessory)
        buildMainMenu()
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        let menu = NSMenu()
        menu.delegate = self
        statusItem.menu = menu
        updateIcon()

        recording.onChange = { [weak self] in
            self?.updateIcon()
            self?.window.event("status")
        }
        window.onCommand = { [weak self] cmd, _ in
            guard let self else { return }
            switch cmd {
            case "startRecording": if !self.recording.isRecording { self.recording.toggleManual() }
            case "stopRecording": self.recording.stopByUser(reason: "остановлено в окне")
            case "reloadSettings": self.recording.reloadSettings()
            case "calendarAccess": self.recording.requestCalendarAccess()
            default: break
            }
        }
        backend.onReady = { [weak self] in
            guard let self else { return }
            self.recording.reloadSettings()
            self.recording.notifyBackend()
            self.window.showAndFocus()
            self.pollStatus()
        }
        backend.start()
    }

    func applicationWillTerminate(_ note: Notification) {
        if recording.isRecording {
            // Дописываем файлы, чтобы запись не потерялась; обработается при следующем запуске.
            let done = DispatchSemaphore(value: 0)
            recording.stop(reason: "выход из приложения")
            DispatchQueue.global().asyncAfter(deadline: .now() + 3) { done.signal() }
            _ = done.wait(timeout: .now() + 4)
        }
        backend.stop()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window.showAndFocus()
        return true
    }

    // MARK: - Строка меню

    private func updateIcon() {
        guard let button = statusItem?.button else { return }
        let symbol: String
        if recording.isRecording { symbol = "record.circle.fill" }
        else if processing > 0 { symbol = "waveform.badge.magnifyingglass" }
        else { symbol = "waveform" }
        let img = NSImage(systemSymbolName: symbol, accessibilityDescription: "Meeting Agent")
            ?? NSImage(systemSymbolName: "waveform", accessibilityDescription: "Meeting Agent")
        img?.isTemplate = !recording.isRecording
        if recording.isRecording, let img {
            button.image = img.withSymbolConfiguration(.init(paletteColors: [.systemRed]))
        } else {
            button.image = img
        }
        button.toolTip = statusText()
    }

    private func statusText() -> String {
        if recording.isRecording {
            let mins = Int(Date().timeIntervalSince(recording.since ?? Date()) / 60)
            let what = recording.source?.platform.map { "Идёт запись встречи \($0.title)" } ?? "Идёт запись"
            return what + " · \(mins) мин"
        }
        if processing > 0 { return "Обработка встреч: \(processing)" }
        if recording.titlesHidden { return "Нет разрешения «Запись экрана» — встречи не определяются" }
        if recording.skipCurrentMeeting { return "Запись остановлена — эту встречу больше не записываю" }
        return recording.autoRecordEnabled ? "Жду начала встречи" : "Автозапись выключена"
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let status = NSMenuItem(title: statusText(), action: nil, keyEquivalent: "")
        status.isEnabled = false
        menu.addItem(status)
        if failed > 0 {
            let f = NSMenuItem(title: "Не удалось обработать: \(failed) — подробности в окне", action: nil, keyEquivalent: "")
            f.isEnabled = false
            menu.addItem(f)
        }
        menu.addItem(.separator())
        menu.addItem(item("Открыть Meeting Agent", #selector(openWindow), "o"))
        menu.addItem(item(recording.isRecording ? "Остановить запись" : "Записать сейчас", #selector(toggleRecording), "r"))
        menu.addItem(.separator())
        let auto = item("Записывать встречи автоматически", #selector(toggleAuto), "")
        auto.state = recording.autoRecordEnabled ? .on : .off
        menu.addItem(auto)
        let login = item("Запускать при входе в систему", #selector(toggleLogin), "")
        login.state = SMAppService.mainApp.status == .enabled ? .on : .off
        menu.addItem(login)
        if recording.titlesHidden {
            menu.addItem(item("Дать разрешение «Запись экрана»…", #selector(openScreenSettings), ""))
        }
        menu.addItem(.separator())
        menu.addItem(item("Настройки…", #selector(openSettings), ","))
        menu.addItem(item("Выйти из Meeting Agent", #selector(quit), "q"))
    }

    private func item(_ title: String, _ action: Selector, _ key: String) -> NSMenuItem {
        let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
        i.target = self
        return i
    }

    @objc private func openWindow() { window.showAndFocus() }
    @objc private func toggleRecording() { recording.toggleManual() }
    @objc private func toggleAuto() { recording.setAutoRecord(!recording.autoRecordEnabled) }
    @objc private func openSettings() { window.showAndFocus(); window.event("settings") }
    @objc private func quit() { NSApp.terminate(nil) }

    @objc private func openScreenSettings() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc private func toggleLogin() {
        do {
            if SMAppService.mainApp.status == .enabled { try SMAppService.mainApp.unregister() }
            else { try SMAppService.mainApp.register() }
        } catch {
            let a = NSAlert()
            a.messageText = "Не удалось изменить автозапуск"
            a.informativeText = "Переместите Meeting Agent в папку «Программы» и попробуйте снова. (\(error.localizedDescription))"
            a.runModal()
        }
    }

    // MARK: - Состояние обработки (для значка)

    private func pollStatus() {
        statusTimer?.invalidate()
        statusTimer = Timer.scheduledTimer(withTimeInterval: 4, repeats: true) { [weak self] _ in
            self?.backend.get("status") { s in
                guard let self, let jobs = s?["jobs"] as? [[String: Any]] else { return }
                self.processing = jobs.filter { ["queued", "running"].contains($0["status"] as? String ?? "") }.count
                self.failed = jobs.filter { ($0["status"] as? String) == "error" }.count
                self.updateIcon()
            }
        }
    }

    // MARK: - Главное меню (Cmd+C/V/A, Cmd+W в окне)

    private func buildMainMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Скрыть Meeting Agent", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Выйти из Meeting Agent", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let editItem = NSMenuItem()
        let edit = NSMenu(title: "Правка")
        edit.addItem(withTitle: "Отменить", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Вернуть", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Вырезать", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Копировать", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Вставить", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Выбрать все", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)

        let winItem = NSMenuItem()
        let win = NSMenu(title: "Окно")
        win.addItem(withTitle: "Закрыть", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        win.addItem(withTitle: "Свернуть", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        winItem.submenu = win
        main.addItem(winItem)
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
