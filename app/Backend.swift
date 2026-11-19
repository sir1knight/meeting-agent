// Запуск Python-сервера (обработка, заметки, интерфейс) и обращения к нему.

import Foundation

final class Backend {
    let port: Int
    let token = UUID().uuidString.replacingOccurrences(of: "-", with: "")
    private var process: Process?
    private var stopping = false
    private var restarts = 0
    var onReady: (() -> Void)?

    static let home: URL = {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let dir = base.appendingPathComponent("Meeting Agent", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }()

    static let logURL: URL = {
        let dir = home.appendingPathComponent("logs", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("backend.log")
    }()

    var baseURL: URL { URL(string: "http://127.0.0.1:\(port)")! }
    var uiURL: URL { URL(string: "http://127.0.0.1:\(port)/#token=\(token)")! }

    init() {
        port = Backend.freePort()
    }

    /// Где лежит Python и код: внутри .app (Resources/python, Resources/app),
    /// а для разработки — папка проекта из MEETING_AGENT_DEV.
    private func locate() -> (python: URL, root: URL)? {
        let env = ProcessInfo.processInfo.environment
        if let dev = env["MEETING_AGENT_DEV"] {
            let root = URL(fileURLWithPath: dev)
            return (root.appendingPathComponent(".venv/bin/python"), root)
        }
        guard let res = Bundle.main.resourceURL else { return nil }
        let python = res.appendingPathComponent("python/bin/python3")
        guard FileManager.default.isExecutableFile(atPath: python.path) else { return nil }
        return (python, res.appendingPathComponent("app"))
    }

    func start() {
        guard let (python, root) = locate() else {
            NSLog("Meeting Agent: не найден встроенный Python")
            return
        }
        let p = Process()
        p.executableURL = python
        p.arguments = ["-m", "meeting_agent", "serve", "--port", String(port)]
        p.currentDirectoryURL = root
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = root.path
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["MEETING_AGENT_HOME"] = Backend.home.path
        env["MEETING_AGENT_TOKEN"] = token
        env["HF_HOME"] = Backend.home.appendingPathComponent("huggingface").path
        env["HF_HUB_OFFLINE"] = "1"           // модели скачиваются только по кнопке в окне
        env["HF_HUB_DISABLE_TELEMETRY"] = "1"
        env["TOKENIZERS_PARALLELISM"] = "false"
        // У приложений из Finder короткий PATH: добавляем Homebrew (ffmpeg, Claude Code).
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:\(NSHomeDirectory())/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        p.environment = env

        // Лог текущего запуска; предыдущий сохраняется рядом как backend.prev.log.
        let fm = FileManager.default
        if restarts == 0, fm.fileExists(atPath: Backend.logURL.path) {
            let prev = Backend.logURL.deletingLastPathComponent().appendingPathComponent("backend.prev.log")
            try? fm.removeItem(at: prev)
            try? fm.moveItem(at: Backend.logURL, to: prev)
        }
        if !fm.fileExists(atPath: Backend.logURL.path) {
            fm.createFile(atPath: Backend.logURL.path, contents: nil)
        }
        if let log = try? FileHandle(forWritingTo: Backend.logURL) {
            log.seekToEndOfFile()
            p.standardOutput = log
            p.standardError = log
        }
        p.terminationHandler = { [weak self] proc in
            guard let self, !self.stopping else { return }
            NSLog("Meeting Agent: сервер завершился (код \(proc.terminationStatus)), перезапускаю")
            self.restarts += 1
            DispatchQueue.main.asyncAfter(deadline: .now() + min(30, Double(self.restarts) * 2)) {
                self.start()
            }
        }
        do {
            try p.run()
            process = p
            waitUntilReady()
        } catch {
            NSLog("Meeting Agent: не удалось запустить сервер: \(error)")
        }
    }

    func stop() {
        stopping = true
        process?.terminate()
    }

    private func waitUntilReady(attempt: Int = 0) {
        get("status") { [weak self] result in
            guard let self else { return }
            if result != nil {
                self.restarts = 0
                self.onReady?()
            } else if attempt < 240 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self.waitUntilReady(attempt: attempt + 1) }
            }
        }
    }

    // MARK: - HTTP

    func get(_ path: String, completion: @escaping ([String: Any]?) -> Void) {
        request(path, method: "GET", body: nil, completion: completion)
    }

    func post(_ path: String, _ body: [String: Any], completion: (([String: Any]?) -> Void)? = nil) {
        request(path, method: "POST", body: body) { completion?($0) }
    }

    private func request(_ path: String, method: String, body: [String: Any]?,
                         completion: @escaping ([String: Any]?) -> Void) {
        var req = URLRequest(url: baseURL.appendingPathComponent("api/\(path)"), timeoutInterval: 10)
        req.httpMethod = method
        req.setValue(token, forHTTPHeaderField: "X-Token")
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }
        URLSession.shared.dataTask(with: req) { data, resp, _ in
            var result: [String: Any]?
            if let data, let http = resp as? HTTPURLResponse, http.statusCode == 200 {
                result = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            }
            DispatchQueue.main.async { completion(result) }
        }.resume()
    }

    private static func freePort() -> Int {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        addr.sin_port = 0
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        _ = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, len) }
        }
        _ = withUnsafeMutablePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(fd, $0, &len) }
        }
        let port = Int(UInt16(bigEndian: addr.sin_port))
        return port > 0 ? port : 48765
    }
}
