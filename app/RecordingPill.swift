// Плашка записи поверх всех окон: живой индикатор уровня звука (волна), время MM:SS,
// пауза / продолжить, «Стоп», «✕ не записывать», компактный и развёрнутый вид.
// Если с записью что-то не так (микрофон молчит, пропал звук собеседников) — плашка становится жёлтой,
// пишет причину и при возможности предлагает быстрое действие (например, переключить микрофон).
// Также содержит всплывающее уведомление MeetingPromptPanel для режима «Спрашивать перед записью».

import AppKit
import QuartzCore

// MARK: - Живой индикатор звука (5 полос)

final class WaveformView: NSView {
    private var bars: [NSView] = []
    private var levels: [CGFloat] = [0.18, 0.25, 0.35, 0.25, 0.18]
    private var paused = false

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        for _ in 0..<5 {
            let b = NSView()
            b.wantsLayer = true
            b.layer?.cornerRadius = 1.5
            b.layer?.backgroundColor = NSColor.white.withAlphaComponent(0.9).cgColor
            addSubview(b)
            bars.append(b)
        }
        layoutBars()
    }

    required init?(coder: NSCoder) { fatalError() }

    func update(mic: Float, others: Float, paused: Bool) {
        self.paused = paused
        if paused {
            levels = [0.18, 0.18, 0.18, 0.18, 0.18]
        } else {
            // Сжимаем динамический диапазон RMS (речь обычно 0.01…0.25) в 0.18…1.0.
            let m = CGFloat(min(1.0, pow(Double(max(0, mic)) * 6.5, 0.6)))
            let o = CGFloat(min(1.0, pow(Double(max(0, others)) * 6.5, 0.6)))
            let peak = max(m, o)
            let targets: [CGFloat] = [
                max(0.18, o * 0.85),
                max(0.18, (m * 0.4 + o * 0.6) * 0.95),
                max(0.20, peak),
                max(0.18, (m * 0.7 + o * 0.3) * 0.95),
                max(0.18, m * 0.85),
            ]
            for i in 0..<5 {
                levels[i] = levels[i] * 0.45 + targets[i] * 0.55
            }
        }
        let color = paused
            ? NSColor.systemOrange.withAlphaComponent(0.75).cgColor
            : NSColor.white.withAlphaComponent(0.92).cgColor
        for b in bars { b.layer?.backgroundColor = color }
        layoutBars()
    }

    override func layout() {
        super.layout()
        layoutBars()
    }

    private func layoutBars() {
        let barW: CGFloat = 2.6
        let gap: CGFloat = 2.4
        let totalW = CGFloat(bars.count) * barW + CGFloat(bars.count - 1) * gap
        let x0 = (bounds.width - totalW) / 2
        let maxH = max(6, bounds.height - 2)
        for (i, b) in bars.enumerated() {
            let h = max(3, maxH * levels[i])
            let y = (bounds.height - h) / 2
            b.frame = NSRect(x: x0 + CGFloat(i) * (barW + gap), y: y, width: barW, height: h)
        }
    }
}

// MARK: - Плашка записи

final class RecordingPill {
    private var panel: NSPanel?
    private let dot = NSView()
    private let waveform = WaveformView(frame: NSRect(x: 0, y: 0, width: 26, height: 16))
    private let label = NSTextField(labelWithString: "")
    private let warn = NSTextField(labelWithString: "")
    private var pauseButton: NSButton!
    private var stopButton: NSButton!
    private var skipButton: NSButton!
    private var collapseButton: NSButton!
    private var warnActionButton: NSButton!

    private var timer: Timer?
    private var since: Date?
    private var pausedSecondsProvider: (() -> TimeInterval)?
    private var title = ""
    private var isPaused = false
    private var isCompact = false
    private var warning: String?
    private var warningActionTitle: String?

    var onTogglePause: (() -> Void)?
    var onStop: (() -> Void)?
    var onSkip: (() -> Void)?
    var onWarningAction: (() -> Void)?
    var enabled = true { didSet { if !enabled { hide() } } }

    func show(since: Date, title: String, pausedSeconds: @escaping () -> TimeInterval = { 0 }) {
        guard enabled else { return }
        self.since = since
        self.title = title
        self.pausedSecondsProvider = pausedSeconds
        self.isPaused = false
        if panel == nil { build() }
        refresh()
        panel?.orderFrontRegardless()
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in self?.refreshTime() }
    }

    func hide() {
        timer?.invalidate()
        timer = nil
        warning = nil
        warningActionTitle = nil
        isPaused = false
        panel?.orderOut(nil)
    }

    func setPaused(_ paused: Bool) {
        isPaused = paused
        if paused {
            dot.layer?.removeAnimation(forKey: "pulse")
            dot.layer?.opacity = 1
            dot.layer?.backgroundColor = NSColor.systemOrange.cgColor
        } else {
            dot.layer?.backgroundColor = NSColor.systemRed.cgColor
            startPulse()
        }
        pauseButton?.title = paused ? "▶" : "⏸"
        pauseButton?.toolTip = paused ? "Продолжить запись" : "Пауза записи"
        waveform.update(mic: 0, others: 0, paused: paused)
        refresh()
    }

    func updateLevels(mic: Float, others: Float) {
        guard panel?.isVisible == true else { return }
        waveform.update(mic: mic, others: others, paused: isPaused)
    }

    func setWarning(_ text: String?, actionTitle: String? = nil) {
        warning = text
        warningActionTitle = text != nil ? actionTitle : nil
        if text != nil && isCompact { isCompact = false }
        refresh()
    }

    private func elapsedText() -> String {
        let wall = Date().timeIntervalSince(since ?? Date())
        let paused = pausedSecondsProvider?() ?? 0
        let secs = max(0, Int(wall - paused))
        let h = secs / 3600
        let m = (secs % 3600) / 60
        let s = secs % 60
        if h > 0 { return String(format: "%d:%02d:%02d", h, m, s) }
        return String(format: "%02d:%02d", m, s)
    }

    private func refreshTime() {
        guard panel?.isVisible == true else { return }
        let t = elapsedText()
        if isCompact {
            label.stringValue = isPaused ? "Пауза" : t
        } else {
            let prefix = isPaused ? "Пауза" : (title.isEmpty ? "Запись" : title)
            label.stringValue = "\(prefix) · \(t)"
        }
    }

    private func refresh() {
        guard let panel else { return }
        refreshTime()
        let showWarn = warning != nil && !isCompact
        warn.stringValue = warning ?? ""
        warn.isHidden = !showWarn
        warnActionButton.isHidden = !(showWarn && warningActionTitle != nil)
        if let at = warningActionTitle { warnActionButton.title = at }

        pauseButton.isHidden = isCompact
        stopButton.isHidden = isCompact
        skipButton.isHidden = isCompact
        collapseButton.title = isCompact ? "›" : "‹"
        collapseButton.toolTip = isCompact ? "Развернуть плашку" : "Свернуть плашку"

        let bg: NSColor
        if showWarn {
            bg = NSColor(calibratedRed: 0.45, green: 0.30, blue: 0.02, alpha: 0.96)
        } else if isPaused {
            bg = NSColor(calibratedRed: 0.24, green: 0.19, blue: 0.08, alpha: 0.94)
        } else {
            bg = NSColor(white: 0.11, alpha: 0.93)
        }
        panel.contentView?.layer?.backgroundColor = bg.cgColor

        let width: CGFloat
        if isCompact {
            width = 136
        } else {
            let warnW = (warning.map { CGFloat($0.count) * 6.5 } ?? 0) + (warningActionTitle != nil ? 128 : 36)
            width = max(330, min(540, warnW))
        }
        let h: CGFloat = showWarn ? 66 : 40
        var f = panel.frame
        f.origin.y += f.height - h
        f.size = NSSize(width: width, height: h)
        panel.setFrame(f, display: true)
        layout()
    }

    private func build() {
        let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 330, height: 40),
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
        v.layer?.borderWidth = 0.5
        v.layer?.borderColor = NSColor.white.withAlphaComponent(0.14).cgColor
        p.contentView = v

        dot.wantsLayer = true
        dot.layer?.backgroundColor = NSColor.systemRed.cgColor
        dot.layer?.cornerRadius = 4.5
        for t in [label, warn] {
            t.textColor = .white
            t.font = t === label
                ? .monospacedDigitSystemFont(ofSize: 12.5, weight: .medium)
                : .systemFont(ofSize: 12, weight: .regular)
            t.lineBreakMode = .byTruncatingTail
        }
        warn.textColor = NSColor(white: 1, alpha: 0.9)
        pauseButton = pillButton("⏸", action: #selector(pauseTapped), tip: "Пауза записи")
        stopButton = pillButton("Стоп", action: #selector(stopTapped), tip: "Остановить запись и обработать")
        skipButton = pillButton("✕", action: #selector(skipTapped), tip: "Остановить и больше не записывать эту встречу")
        collapseButton = pillButton("‹", action: #selector(collapseTapped), tip: "Свернуть плашку")
        warnActionButton = pillButton("Переключить", action: #selector(warnActionTapped), tip: "Переключить микрофон")
        [dot, waveform, label, warn, pauseButton, stopButton, skipButton, collapseButton, warnActionButton]
            .forEach { v.addSubview($0) }

        // Позиция: последняя или правый верхний угол экрана.
        let d = UserDefaults.standard
        if let s = d.string(forKey: "pillOrigin") {
            p.setFrameOrigin(NSPointFromString(s))
        } else if let screen = NSScreen.main?.visibleFrame {
            p.setFrameOrigin(NSPoint(x: screen.maxX - 350, y: screen.maxY - 56))
        }
        NotificationCenter.default.addObserver(forName: NSWindow.didMoveNotification, object: p, queue: .main) { _ in
            d.set(NSStringFromPoint(p.frame.origin), forKey: "pillOrigin")
        }
        panel = p
        startPulse()
    }

    private func startPulse() {
        let pulse = CABasicAnimation(keyPath: "opacity")
        pulse.fromValue = 1; pulse.toValue = 0.32; pulse.duration = 0.9
        pulse.autoreverses = true; pulse.repeatCount = .infinity
        dot.layer?.add(pulse, forKey: "pulse")
    }

    private func layout() {
        guard let v = panel?.contentView else { return }
        let w = v.bounds.width, h = v.bounds.height
        let top = h - 40
        dot.frame = NSRect(x: 12, y: top + 15.5, width: 9, height: 9)
        waveform.frame = NSRect(x: 26, y: top + 12, width: 26, height: 16)
        if isCompact {
            collapseButton.frame = NSRect(x: w - 28, y: top + 8, width: 22, height: 24)
            label.frame = NSRect(x: 56, y: top + 11, width: w - 56 - 30, height: 18)
        } else {
            collapseButton.frame = NSRect(x: w - 28, y: top + 8, width: 22, height: 24)
            skipButton.frame = NSRect(x: w - 28 - 28, y: top + 8, width: 24, height: 24)
            stopButton.frame = NSRect(x: w - 28 - 28 - 54, y: top + 8, width: 50, height: 24)
            pauseButton.frame = NSRect(x: w - 28 - 28 - 54 - 30, y: top + 8, width: 26, height: 24)
            label.frame = NSRect(x: 58, y: top + 11, width: w - 58 - 146, height: 18)
        }
        let actionW: CGFloat = warnActionButton.isHidden ? 0 : 112
        if !warnActionButton.isHidden {
            warnActionButton.frame = NSRect(x: w - actionW - 10, y: 6, width: actionW, height: 22)
        }
        warn.frame = NSRect(x: 12, y: 8, width: w - 24 - (actionW > 0 ? actionW + 8 : 0), height: 18)
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

    @objc private func pauseTapped() { onTogglePause?() }
    @objc private func stopTapped() { onStop?() }
    @objc private func skipTapped() { onSkip?() }
    @objc private func collapseTapped() { isCompact.toggle(); refresh() }
    @objc private func warnActionTapped() { onWarningAction?() }
}

// MARK: - Всплывающий вопрос «Записать эту встречу?» (режим auto_record_mode == "ask")

final class MeetingPromptPanel {
    private var panel: NSPanel?
    private let titleLabel = NSTextField(labelWithString: "")
    private let subLabel = NSTextField(labelWithString: "Начать запись и составить протокол?")
    private var recordBtn: NSButton!
    private var skipBtn: NSButton!

    var onAccept: (() -> Void)?
    var onDismiss: (() -> Void)?
    var isVisible: Bool { panel?.isVisible == true }

    func show(platformTitle: String) {
        if panel == nil { build() }
        titleLabel.stringValue = "Началась встреча · \(platformTitle)"
        if let screen = NSScreen.main?.visibleFrame, let p = panel {
            p.setFrameOrigin(NSPoint(x: screen.maxX - p.frame.width - 18, y: screen.maxY - p.frame.height - 18))
        }
        panel?.orderFrontRegardless()
    }

    func hide() {
        panel?.orderOut(nil)
    }

    private func build() {
        let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 340, height: 76),
                        styleMask: [.nonactivatingPanel, .borderless], backing: .buffered, defer: false)
        p.level = .floating
        p.isFloatingPanel = true
        p.hidesOnDeactivate = false
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        p.isMovableByWindowBackground = true
        p.backgroundColor = .clear
        p.isOpaque = false
        p.hasShadow = true

        let v = NSView(frame: NSRect(x: 0, y: 0, width: 340, height: 76))
        v.wantsLayer = true
        v.layer?.cornerRadius = 13
        v.layer?.backgroundColor = NSColor(white: 0.12, alpha: 0.96).cgColor
        v.layer?.borderWidth = 0.6
        v.layer?.borderColor = NSColor.white.withAlphaComponent(0.18).cgColor
        p.contentView = v

        titleLabel.textColor = .white
        titleLabel.font = .systemFont(ofSize: 13, weight: .semibold)
        titleLabel.frame = NSRect(x: 14, y: 46, width: 312, height: 18)

        subLabel.textColor = NSColor(white: 1, alpha: 0.72)
        subLabel.font = .systemFont(ofSize: 11.5)
        subLabel.frame = NSRect(x: 14, y: 28, width: 180, height: 16)

        recordBtn = NSButton(title: "Записать", target: self, action: #selector(acceptTapped))
        recordBtn.bezelStyle = .rounded
        recordBtn.contentTintColor = .systemRed
        recordBtn.font = .systemFont(ofSize: 12, weight: .semibold)
        recordBtn.frame = NSRect(x: 152, y: 10, width: 88, height: 26)

        skipBtn = NSButton(title: "Пропустить", target: self, action: #selector(dismissTapped))
        skipBtn.bezelStyle = .inline
        skipBtn.contentTintColor = .white
        skipBtn.font = .systemFont(ofSize: 12)
        skipBtn.frame = NSRect(x: 244, y: 11, width: 84, height: 24)

        [titleLabel, subLabel, recordBtn, skipBtn].forEach { v.addSubview($0) }
        panel = p
    }

    @objc private func acceptTapped() {
        hide()
        onAccept?()
    }

    @objc private func dismissTapped() {
        hide()
        onDismiss?()
    }
}
