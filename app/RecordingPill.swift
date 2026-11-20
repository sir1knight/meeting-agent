// Плашка записи поверх всех окон: идёт запись, сколько минут, какая встреча; «Стоп» и «Не записывать».
// Если с записью что-то не так (микрофон молчит, пропал звук собеседников) — плашка становится жёлтой
// и пишет, что случилось. Плашку можно перетащить; место запоминается.

import AppKit
import QuartzCore

final class RecordingPill {
    private var panel: NSPanel?
    private let dot = NSView()
    private let label = NSTextField(labelWithString: "")
    private let warn = NSTextField(labelWithString: "")
    private var stopButton: NSButton!
    private var skipButton: NSButton!
    private var timer: Timer?
    private var since: Date?
    private var title = ""
    private var warning: String?

    var onStop: (() -> Void)?
    var onSkip: (() -> Void)?
    var enabled = true { didSet { if !enabled { hide() } } }

    func show(since: Date, title: String) {
        guard enabled else { return }
        self.since = since
        self.title = title
        if panel == nil { build() }
        refresh()
        panel?.orderFrontRegardless()
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in self?.refresh() }
    }

    func hide() {
        timer?.invalidate()
        timer = nil
        warning = nil
        panel?.orderOut(nil)
    }

    func setWarning(_ text: String?) {
        warning = text
        refresh()
    }

    private func refresh() {
        guard let panel else { return }
        let mins = Int(Date().timeIntervalSince(since ?? Date()) / 60)
        label.stringValue = "Запись\(title.isEmpty ? "" : " · \(title)") · \(mins) мин"
        warn.stringValue = warning ?? ""
        warn.isHidden = warning == nil
        let bg: NSColor = warning == nil ? NSColor(white: 0.12, alpha: 0.92) : NSColor(calibratedRed: 0.45, green: 0.32, blue: 0.02, alpha: 0.95)
        panel.contentView?.layer?.backgroundColor = bg.cgColor
        let width: CGFloat = max(300, min(520, (warning.map { CGFloat($0.count) * 6.6 } ?? 0) + 40))
        var f = panel.frame
        let h: CGFloat = warning == nil ? 40 : 62
        f.origin.y += f.height - h
        f.size = NSSize(width: width, height: h)
        panel.setFrame(f, display: true)
        layout()
    }

    private func build() {
        let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 300, height: 40),
                        styleMask: [.nonactivatingPanel, .borderless], backing: .buffered, defer: false)
        p.level = .floating
        p.isFloatingPanel = true
        p.hidesOnDeactivate = false
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        p.isMovableByWindowBackground = true
        p.backgroundColor = .clear
        p.isOpaque = false
        p.hasShadow = true
        let v = NSView()
        v.wantsLayer = true
        v.layer?.cornerRadius = 12
        p.contentView = v

        dot.wantsLayer = true
        dot.layer?.backgroundColor = NSColor.systemRed.cgColor
        dot.layer?.cornerRadius = 5
        for t in [label, warn] {
            t.textColor = .white
            t.font = .systemFont(ofSize: 12.5, weight: t === label ? .medium : .regular)
            t.lineBreakMode = .byTruncatingTail
        }
        warn.textColor = NSColor(white: 1, alpha: 0.85)
        stopButton = pillButton("Стоп", action: #selector(stopTapped), tip: "Остановить запись и обработать")
        skipButton = pillButton("✕", action: #selector(skipTapped), tip: "Остановить и больше не записывать эту встречу")
        [dot, label, warn, stopButton, skipButton].forEach { v.addSubview($0) }

        // Позиция: последняя или правый верхний угол экрана.
        let d = UserDefaults.standard
        if let s = d.string(forKey: "pillOrigin") {
            p.setFrameOrigin(NSPointFromString(s))
        } else if let screen = NSScreen.main?.visibleFrame {
            p.setFrameOrigin(NSPoint(x: screen.maxX - 320, y: screen.maxY - 56))
        }
        NotificationCenter.default.addObserver(forName: NSWindow.didMoveNotification, object: p, queue: .main) { _ in
            d.set(NSStringFromPoint(p.frame.origin), forKey: "pillOrigin")
        }
        panel = p
        let pulse = CABasicAnimation(keyPath: "opacity")
        pulse.fromValue = 1; pulse.toValue = 0.35; pulse.duration = 0.9
        pulse.autoreverses = true; pulse.repeatCount = .infinity
        dot.layer?.add(pulse, forKey: "pulse")
    }

    private func layout() {
        guard let v = panel?.contentView else { return }
        let w = v.bounds.width, h = v.bounds.height
        let top = h - 40
        dot.frame = NSRect(x: 14, y: top + 15, width: 10, height: 10)
        skipButton.frame = NSRect(x: w - 36, y: top + 8, width: 26, height: 24)
        stopButton.frame = NSRect(x: w - 36 - 56, y: top + 8, width: 52, height: 24)
        label.frame = NSRect(x: 32, y: top + 11, width: w - 32 - 100, height: 18)
        warn.frame = NSRect(x: 14, y: 6, width: w - 28, height: 18)
    }

    private func pillButton(_ title: String, action: Selector, tip: String) -> NSButton {
        let b = NSButton(title: title, target: self, action: action)
        b.bezelStyle = .inline
        b.isBordered = true
        b.font = .systemFont(ofSize: 12, weight: .medium)
        b.toolTip = tip
        b.contentTintColor = .white
        return b
    }

    @objc private func stopTapped() { onStop?() }
    @objc private func skipTapped() { onSkip?() }
}
