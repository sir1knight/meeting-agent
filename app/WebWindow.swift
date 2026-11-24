// Окно приложения: веб-интерфейс локального сервера во встроенном WKWebView.

import AppKit
import WebKit

final class WebWindowController: NSWindowController, WKScriptMessageHandler, WKNavigationDelegate, WKUIDelegate,
                                  NSWindowDelegate {
    private let backend: Backend
    private let webView: WKWebView
    var onCommand: ((String, [String: Any]) -> Void)?
    var keepInTrayOnClose = true

    init(backend: Backend) {
        self.backend = backend
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()  // ничего не кэшируем на диск
        let controller = WKUserContentController()
        config.userContentController = controller
        webView = WKWebView(frame: .zero, configuration: config)
        webView.setValue(false, forKey: "drawsBackground")  // без белой вспышки в тёмной теме

        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 720),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable],
                              backing: .buffered, defer: false)
        window.title = "Meeting Agent"
        window.minSize = NSSize(width: 760, height: 480)
        window.contentView = webView
        window.setFrameAutosaveName("MeetingAgentMain")
        if !window.setFrameUsingName("MeetingAgentMain") { window.center() }
        window.isReleasedWhenClosed = false
        super.init(window: window)
        window.delegate = self
        controller.add(self, name: "app")
        webView.navigationDelegate = self
        webView.uiDelegate = self
    }

    required init?(coder: NSCoder) { fatalError() }

    func load() {
        webView.load(URLRequest(url: backend.uiURL))
    }

    func showAndFocus() {
        if webView.url == nil { load() }
        NSApp.setActivationPolicy(.regular)  // пока окно открыто — значок в Dock и Cmd+Tab
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Сообщить интерфейсу о событии (например, запись остановлена — обновить статус).
    func event(_ name: String) {
        webView.evaluateJavaScript("window.agentEvent && window.agentEvent('\(name)')")
    }

    func windowWillClose(_ notification: Notification) {
        if keepInTrayOnClose {
            NSApp.setActivationPolicy(.accessory)  // остаётся только значок в строке меню
        } else {
            NSApp.terminate(nil)
        }
    }

    // MARK: - Сообщения из интерфейса

    func userContentController(_ ucc: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let cmd = body["cmd"] as? String else { return }
        if cmd == "openURL", let s = body["url"] as? String, let url = URL(string: s),
           url.scheme == "https" || url.scheme == "http" {
            NSWorkspace.shared.open(url)
            return
        }
        onCommand?(cmd, body)
    }

    // MARK: - Навигация: только наш сервер, внешние ссылки — в браузере

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.cancel) }
        if url.host == "127.0.0.1", url.port == backend.port {
            decisionHandler(.allow)
        } else {
            if url.scheme == "https" || url.scheme == "http" { NSWorkspace.shared.open(url) }
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        retryLoad()
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        retryLoad()
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        load()
    }

    private func retryLoad() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in self?.load() }
    }

    // Кнопка «Импорт записи» (<input type=file>).
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [.audio, .movie, .mpeg4Movie, .quickTimeMovie]
        panel.beginSheetModal(for: window!) { resp in
            completionHandler(resp == .OK ? panel.urls : nil)
        }
    }

    // confirm()/alert() из интерфейса
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.beginSheetModal(for: window!) { _ in completionHandler() }
    }
}
