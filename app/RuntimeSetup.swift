// Python и пакеты — не внутри приложения, а в папке данных (~/Library/Application Support/Meeting Agent/runtime):
// приложение маленькое (несколько МБ) и обновляется кнопкой «Обновить», как код и интерфейс.
//
// Первый запуск: скачать Python (python-build-standalone, ~17 МБ, сверка SHA-256) и поставить пакеты по списку
// (python -m meeting_agent bootstrap → uv, ~600 МБ с PyPI). Потом — только если список пакетов поменялся.
// Ход установки — в окне «Установка компонентов».

import AppKit
import CryptoKit

final class RuntimeSetup {
    static var runtimeDir: URL { Backend.home.appendingPathComponent("runtime", isDirectory: true) }
    static var python: URL { runtimeDir.appendingPathComponent("python/bin/python3") }

    private var window: NSWindow?
    private let status = NSTextField(labelWithString: "")
    private let logView = NSTextView()
    private let progress = NSProgressIndicator()
    private var buttons: NSStackView!
    private var onDone: ((Bool) -> Void)?
    private var root: URL!

    /// Всё на месте — сразу done(true); иначе окно установки, затем done(успех).
    func ensure(root: URL, done: @escaping (Bool) -> Void) {
        self.root = root
        onDone = done
        if FileManager.default.isExecutableFile(atPath: Self.python.path) {
            // Python есть — проверяем только список пакетов (быстро, без сети).
            DispatchQueue.global().async {
                let code = self.runPython(["-m", "meeting_agent", "bootstrap", "--check"], stream: false)
                DispatchQueue.main.async {
                    if code == 0 { done(true) } else { self.showWindow(); self.installPackages() }
                }
            }
            return
        }
        showWindow()
        installPython()
    }

    // MARK: - Python

    private struct Source: Decodable { let url: String; let sha256: String }
    private struct Manifest: Decodable { let python: [Source] }

    private func installPython() {
        setStatus("Скачиваю Python (~17 МБ)…")
        guard let url = Bundle.main.resourceURL?.appendingPathComponent("runtime.json"),
              let data = try? Data(contentsOf: url),
              let man = try? JSONDecoder().decode(Manifest.self, from: data), !man.python.isEmpty else {
            return fail("В приложении нет сведений, откуда скачать Python (runtime.json). Скачайте приложение заново.")
        }
        DispatchQueue.global().async {
            var lastError = ""
            // Сети с обрывами длинных загрузок: каждый источник — до трёх попыток.
            for src in man.python.flatMap({ [$0, $0, $0] }) {
                guard let remote = URL(string: src.url) else { continue }
                self.log("Скачиваю \(remote.host ?? "")…")
                let sem = DispatchSemaphore(value: 0)
                var file: URL?
                let task = URLSession.shared.downloadTask(with: remote) { tmp, resp, err in
                    if let tmp, (resp as? HTTPURLResponse)?.statusCode == 200 {
                        let keep = FileManager.default.temporaryDirectory.appendingPathComponent("ma-python.tar.gz")
                        try? FileManager.default.removeItem(at: keep)
                        try? FileManager.default.moveItem(at: tmp, to: keep)
                        file = keep
                    } else {
                        lastError = err?.localizedDescription ?? "ответ \((resp as? HTTPURLResponse)?.statusCode ?? 0)"
                    }
                    sem.signal()
                }
                task.resume()
                sem.wait()
                guard let file, let bytes = try? Data(contentsOf: file) else { continue }
                let digest = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
                guard digest == src.sha256 else { lastError = "не совпала контрольная сумма"; continue }
                if self.extract(file) {
                    try? FileManager.default.removeItem(at: file)
                    DispatchQueue.main.async { self.installPackages() }
                    return
                }
                lastError = "не удалось распаковать"
            }
            self.fail("Не удалось скачать Python: \(lastError). Проверьте интернет и нажмите «Повторить».")
        }
    }

    private func extract(_ archive: URL) -> Bool {
        let fm = FileManager.default
        let tmp = Self.runtimeDir.appendingPathComponent(".extract", isDirectory: true)
        try? fm.removeItem(at: tmp)
        try? fm.createDirectory(at: tmp, withIntermediateDirectories: true)
        let tar = Process()
        tar.executableURL = URL(fileURLWithPath: "/usr/bin/tar")
        tar.arguments = ["xzf", archive.path, "-C", tmp.path]
        guard (try? tar.run()) != nil else { return false }
        tar.waitUntilExit()
        let dest = Self.runtimeDir.appendingPathComponent("python", isDirectory: true)
        try? fm.removeItem(at: dest)
        guard tar.terminationStatus == 0, (try? fm.moveItem(at: tmp.appendingPathComponent("python"), to: dest)) != nil
        else { return false }
        try? fm.removeItem(at: tmp)
        log("Python готов")
        return true
    }

    // MARK: - Пакеты

    private func installPackages() {
        setStatus("Ставлю пакеты Python (около 600 МБ, 3–10 минут)…")
        DispatchQueue.global().async {
            let code = self.runPython(["-m", "meeting_agent", "bootstrap"], stream: true)
            DispatchQueue.main.async {
                if code == 0 {
                    self.window?.close()
                    self.window = nil
                    self.onDone?(true)
                } else {
                    self.fail("Не удалось поставить пакеты (код \(code)). Проверьте интернет и нажмите «Повторить».")
                }
            }
        }
    }

    @discardableResult
    private func runPython(_ args: [String], stream: Bool) -> Int32 {
        let p = Process()
        p.executableURL = Self.python
        p.arguments = args
        p.currentDirectoryURL = root
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = root.path
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["MEETING_AGENT_HOME"] = Backend.home.path
        env["MEETING_AGENT_APP"] = Bundle.main.bundlePath
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        p.environment = env
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        if stream {
            pipe.fileHandleForReading.readabilityHandler = { [weak self] h in
                let d = h.availableData
                guard !d.isEmpty, let s = String(data: d, encoding: .utf8) else { return }
                self?.log(s.replacingOccurrences(of: "\r", with: "\n"), raw: true)
            }
        }
        do { try p.run() } catch { return -1 }
        p.waitUntilExit()
        pipe.fileHandleForReading.readabilityHandler = nil
        return p.terminationStatus
    }

    // MARK: - Окно

    private func showWindow() {
        if window != nil { return }
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 620, height: 380),
                         styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        w.title = "Meeting Agent — установка компонентов"
        w.isReleasedWhenClosed = false
        let v = NSView()
        w.contentView = v
        let head = NSTextField(wrappingLabelWithString:
            "Один раз скачиваются Python и пакеты для распознавания речи и протоколов (~600 МБ). "
            + "Потом они обновляются сами и только частями. Записи и расшифровки остаются на этом Mac.")
        head.font = .systemFont(ofSize: 13)
        status.font = .systemFont(ofSize: 13, weight: .semibold)
        progress.style = .bar
        progress.isIndeterminate = true
        progress.startAnimation(nil)
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        logView.isEditable = false
        logView.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        logView.textColor = .secondaryLabelColor
        logView.backgroundColor = .textBackgroundColor
        scroll.documentView = logView
        logView.autoresizingMask = [.width]
        let retry = NSButton(title: "Повторить", target: self, action: #selector(retryTapped))
        let quit = NSButton(title: "Закрыть", target: self, action: #selector(quitTapped))
        buttons = NSStackView(views: [quit, retry])
        buttons.isHidden = true
        for x in [head, status, progress, scroll, buttons!] as [NSView] {
            x.translatesAutoresizingMaskIntoConstraints = false
            v.addSubview(x)
        }
        NSLayoutConstraint.activate([
            head.topAnchor.constraint(equalTo: v.topAnchor, constant: 18),
            head.leadingAnchor.constraint(equalTo: v.leadingAnchor, constant: 20),
            head.trailingAnchor.constraint(equalTo: v.trailingAnchor, constant: -20),
            status.topAnchor.constraint(equalTo: head.bottomAnchor, constant: 14),
            status.leadingAnchor.constraint(equalTo: head.leadingAnchor),
            status.trailingAnchor.constraint(equalTo: head.trailingAnchor),
            progress.topAnchor.constraint(equalTo: status.bottomAnchor, constant: 8),
            progress.leadingAnchor.constraint(equalTo: head.leadingAnchor),
            progress.trailingAnchor.constraint(equalTo: head.trailingAnchor),
            scroll.topAnchor.constraint(equalTo: progress.bottomAnchor, constant: 10),
            scroll.leadingAnchor.constraint(equalTo: head.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: head.trailingAnchor),
            scroll.bottomAnchor.constraint(equalTo: buttons.topAnchor, constant: -10),
            buttons.trailingAnchor.constraint(equalTo: head.trailingAnchor),
            buttons.bottomAnchor.constraint(equalTo: v.bottomAnchor, constant: -14),
        ])
        w.center()
        window = w
        NSApp.activate(ignoringOtherApps: true)
        w.makeKeyAndOrderFront(nil)
    }

    private func setStatus(_ s: String) {
        DispatchQueue.main.async {
            self.status.stringValue = s
            self.buttons?.isHidden = true
            self.progress.isHidden = false
        }
    }

    private func log(_ s: String, raw: Bool = false) {
        DispatchQueue.main.async {
            let text = raw ? s : s + "\n"
            self.logView.textStorage?.append(NSAttributedString(string: text, attributes: [
                .font: NSFont.monospacedSystemFont(ofSize: 11, weight: .regular), .foregroundColor: NSColor.secondaryLabelColor]))
            self.logView.scrollToEndOfDocument(nil)
        }
    }

    private func fail(_ s: String) {
        DispatchQueue.main.async {
            self.showWindow()
            self.status.stringValue = s
            self.progress.isHidden = true
            self.buttons.isHidden = false
        }
    }

    @objc private func retryTapped() {
        buttons.isHidden = true
        if FileManager.default.isExecutableFile(atPath: Self.python.path) { installPackages() } else { installPython() }
    }

    @objc private func quitTapped() { NSApp.terminate(nil) }
}
