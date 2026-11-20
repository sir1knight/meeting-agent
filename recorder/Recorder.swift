// Запись встречи: звук приложения (Zoom или весь системный) через ScreenCaptureKit
// и микрофон через AVAudioEngine — в два отдельных WAV-файла.
// Используется и консольным zoomrec (main.swift), и приложением Meeting Agent.
//
// Надёжность:
//  • recording.json — «замок» идущей записи (pid процесса); meta.json обновляется каждые 30 с. Если процесс
//    упал или Mac выключился, сервер при следующем запуске найдёт запись, починит WAV и обработает её;
//  • смена микрофона (подключили AirPods) — микрофон перезапускается на новом устройстве, пропуск
//    заполняется тишиной по часам, чтобы дорожки не разъехались; захват звука компьютера после сбоя
//    или сна — тоже перезапускается;
//  • уровень и свежесть звука каждой дорожки — для проверки «микрофон молчит» во время встречи;
//  • подавление эха (Voice Processing macOS): голос собеседников из динамиков вычитается из микрофона.
//
// Требуется macOS 13+ и разрешения «Запись экрана и системного звука» и «Микрофон».

import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

func wavSettings(sampleRate: Double, channels: AVAudioChannelCount) -> [String: Any] {
    [
        AVFormatIDKey: kAudioFormatLinearPCM,
        AVSampleRateKey: sampleRate,
        AVNumberOfChannelsKey: channels,
        AVLinearPCMBitDepthKey: 16,
        AVLinearPCMIsFloatKey: false,
        AVLinearPCMIsBigEndianKey: false,
        AVLinearPCMIsNonInterleaved: false,
    ]
}

struct RecorderError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

/// Запись JSON через временный файл: при сбое не остаётся полузаписанного файла.
func writeAtomically(_ object: Any, to url: URL) {
    guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted]) else { return }
    try? data.write(to: url, options: .atomic)
}

/// Тишина нужной длины — в файл (по секунде за раз). Возвращает, сколько кадров записано.
func writeSilence(frames: AVAudioFramePosition, format: AVAudioFormat, to file: AVAudioFile) -> AVAudioFramePosition {
    let chunk = AVAudioFrameCount(format.sampleRate)
    var left = frames, written: AVAudioFramePosition = 0
    while left > 0 {
        let n = AVAudioFrameCount(min(AVAudioFramePosition(chunk), left))
        guard let silence = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: n),
              let data = silence.floatChannelData else { break }
        silence.frameLength = n
        for ch in 0..<Int(format.channelCount) {
            memset(data[ch], 0, Int(n) * MemoryLayout<Float>.size)
        }
        try? file.write(from: silence)
        written += AVAudioFramePosition(n)
        left -= AVAudioFramePosition(n)
    }
    return written
}

/// Пиковый уровень буфера (0 — цифровая тишина: выключен звук устройства или нет доступа).
func peakLevel(_ buf: AVAudioPCMBuffer) -> Float {
    guard let data = buf.floatChannelData else { return 0 }
    var peak: Float = 0
    for ch in 0..<Int(buf.format.channelCount) {
        let p = data[ch]
        for i in 0..<Int(buf.frameLength) { peak = max(peak, abs(p[i])) }
    }
    return peak
}

/// Здоровье дорожки с прошлой проверки.
struct TrackHealth {
    var lastBufferAt: Date?
    var peakSinceCheck: Float = 0
    var stopped = false
    var error: String?
}

// MARK: - Звук приложения (собеседники)

final class AppAudioRecorder: NSObject, SCStreamOutput, SCStreamDelegate {
    private var stream: SCStream?
    private var file: AVAudioFile?
    private var fileFormat: AVAudioFormat?
    private let url: URL
    private let queue = DispatchQueue(label: "app-audio")
    private(set) var firstBufferTime: Date?
    private var firstPTS: Double?
    private var framesWritten: AVAudioFramePosition = 0
    private var resync = false           // после перезапуска: пропуск — по часам, а не по меткам потока
    private var health = TrackHealth()
    private var target = "all"

    init(url: URL) { self.url = url }

    func start(target: String) async throws {
        self.target = target
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        guard let display = content.displays.first else {
            throw RecorderError(message: "Не найден дисплей для захвата")
        }
        let filter: SCContentFilter
        if target == "all" {
            filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
        } else {
            let apps = content.applications.filter { $0.bundleIdentifier == target }
            guard !apps.isEmpty else {
                throw RecorderError(message: "Приложение \(target) не запущено")
            }
            filter = SCContentFilter(display: display, including: apps, exceptingWindows: [])
        }

        let cfg = SCStreamConfiguration()
        cfg.capturesAudio = true
        cfg.excludesCurrentProcessAudio = true
        cfg.sampleRate = 48000
        cfg.channelCount = 1
        cfg.width = 2   // видео не нужно — делаем его минимальным
        cfg.height = 2
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)

        let s = SCStream(filter: filter, configuration: cfg, delegate: self)
        try s.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        try s.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
        try await s.startCapture()
        queue.sync { self.health.stopped = false; self.health.error = nil }
        stream = s
    }

    /// Захват остановился (сбой, сон Mac, программа встречи перезапустилась) — заново, в тот же файл.
    func restart() async throws {
        try? await stream?.stopCapture()
        stream = nil
        queue.sync { self.resync = self.file != nil }
        try await start(target: target)
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sb.isValid,
              let asbd = sb.formatDescription?.audioStreamBasicDescription,
              let format = AVAudioFormat(standardFormatWithSampleRate: asbd.mSampleRate,
                                         channels: asbd.mChannelsPerFrame)
        else { return }

        let pts = CMSampleBufferGetPresentationTimeStamp(sb).seconds
        try? sb.withAudioBufferList { abl, _ in
            guard let buf = AVAudioPCMBuffer(pcmFormat: format, bufferListNoCopy: abl.unsafePointer) else { return }
            if self.file == nil {
                self.firstBufferTime = Date()
                self.firstPTS = pts
                self.fileFormat = format
                self.file = try? AVAudioFile(
                    forWriting: self.url,
                    settings: wavSettings(sampleRate: format.sampleRate, channels: format.channelCount),
                    commonFormat: .pcmFormatFloat32,
                    interleaved: false)
            }
            guard let file = self.file, let first = self.firstPTS, let ff = self.fileFormat,
                  ff.sampleRate == format.sampleRate, ff.channelCount == format.channelCount else { return }

            if self.resync, let start = self.firstBufferTime {
                // После перезапуска (в том числе после сна) метки потока могли пойти заново — сверяемся с часами.
                self.resync = false
                let expected = AVAudioFramePosition(Date().timeIntervalSince(start) * format.sampleRate)
                let gap = min(expected - self.framesWritten, AVAudioFramePosition(format.sampleRate * 3600))
                if gap > 0 { self.framesWritten += writeSilence(frames: gap, format: format, to: file) }
                if pts.isFinite { self.firstPTS = pts - Double(self.framesWritten) / format.sampleRate }
            } else if pts.isFinite {
                // Система не присылала звук (тишина в Zoom) — дописываем нули,
                // чтобы время в файле совпадало с реальным и с дорожкой микрофона.
                let expected = AVAudioFramePosition((pts - first) * format.sampleRate)
                var gap = expected - self.framesWritten
                if gap > AVAudioFramePosition(format.sampleRate * 0.05) {
                    gap = min(gap, AVAudioFramePosition(format.sampleRate * 3600))
                    self.framesWritten += writeSilence(frames: gap, format: format, to: file)
                }
            }
            try? file.write(from: buf)
            self.framesWritten += AVAudioFramePosition(buf.frameLength)
            self.health.lastBufferAt = Date()
            self.health.peakSinceCheck = max(self.health.peakSinceCheck, peakLevel(buf))
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        fputs("Захват звука остановлен: \(error.localizedDescription)\n", stderr)
        queue.async { self.health.stopped = true; self.health.error = error.localizedDescription }
    }

    func takeHealth() -> TrackHealth {
        queue.sync { let h = self.health; self.health.peakSinceCheck = 0; return h }
    }

    func stop() async {
        try? await stream?.stopCapture()
        queue.sync { self.file = nil }  // закрывает файл
    }
}

// MARK: - Микрофон (вы)

final class MicRecorder {
    private var engine = AVAudioEngine()
    private var file: AVAudioFile?
    private var fileFormat: AVAudioFormat?
    private var converter: AVAudioConverter?
    private let lock = NSLock()
    private(set) var firstBufferTime: Date?
    private var framesWritten: AVAudioFramePosition = 0
    private var resync = false
    private var health = TrackHealth()
    private var url: URL?
    private var observer: NSObjectProtocol?
    private var wantEcho = false
    private(set) var echoCancel = false
    var onDeviceChange: (() -> Void)?

    /// echoCancel — Voice Processing macOS (подавление эха и шума). Не включилось — пишем как обычно.
    func start(url: URL, echoCancel: Bool) throws {
        self.url = url
        wantEcho = echoCancel
        try startEngine()
        observer = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: nil, queue: .main) { [weak self] note in
            guard let self, (note.object as? AVAudioEngine) === self.engine else { return }
            self.restart()
            self.onDeviceChange?()
        }
    }

    private func startEngine() throws {
        let input = engine.inputNode
        echoCancel = false
        if wantEcho {
            do {
                try input.setVoiceProcessingEnabled(true)
                if #available(macOS 14.0, *) {
                    // Не приглушать звук собеседников, пока работает подавление эха.
                    input.voiceProcessingOtherAudioDuckingConfiguration =
                        AVAudioVoiceProcessingOtherAudioDuckingConfiguration(enableAdvancedDucking: false, duckingLevel: .min)
                }
                echoCancel = true
            } catch {
                fputs("Подавление эха не включилось: \(error.localizedDescription)\n", stderr)
            }
        }
        let fmt = input.outputFormat(forBus: 0)
        guard fmt.sampleRate > 0, fmt.channelCount > 0 else { throw RecorderError(message: "Микрофон недоступен") }
        lock.lock()
        if file == nil, let url {
            // Файл — всегда моно: Voice Processing может отдавать несколько каналов.
            let mono = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: fmt.sampleRate, channels: 1, interleaved: false)!
            file = try? AVAudioFile(forWriting: url, settings: wavSettings(sampleRate: mono.sampleRate, channels: 1),
                                    commonFormat: .pcmFormatFloat32, interleaved: false)
            fileFormat = mono
        }
        if let ff = fileFormat, fmt.sampleRate != ff.sampleRate || fmt.channelCount != ff.channelCount {
            converter = AVAudioConverter(from: fmt, to: ff)
        } else {
            converter = nil
        }
        let ok = file != nil
        lock.unlock()
        guard ok else { throw RecorderError(message: "Не удалось создать файл записи микрофона") }
        input.installTap(onBus: 0, bufferSize: 4096, format: fmt) { [weak self] buf, _ in
            self?.write(buf)
        }
        engine.prepare()
        try engine.start()
    }

    private func write(_ buf: AVAudioPCMBuffer) {
        lock.lock(); defer { lock.unlock() }
        guard let file, let ff = fileFormat else { return }
        var out = buf
        if let converter {
            let cap = AVAudioFrameCount(Double(buf.frameLength) * ff.sampleRate / buf.format.sampleRate) + 64
            guard let conv = AVAudioPCMBuffer(pcmFormat: ff, frameCapacity: cap) else { return }
            var fed = false
            var err: NSError?
            converter.convert(to: conv, error: &err) { _, status in
                if fed { status.pointee = .noDataNow; return nil }
                fed = true
                status.pointee = .haveData
                return buf
            }
            if err != nil { return }
            out = conv
        }
        if firstBufferTime == nil { firstBufferTime = Date() }
        if resync, let start = firstBufferTime {
            resync = false
            let gap = AVAudioFramePosition(Date().timeIntervalSince(start) * ff.sampleRate) - framesWritten
            if gap > AVAudioFramePosition(ff.sampleRate * 0.1) {
                framesWritten += writeSilence(frames: min(gap, AVAudioFramePosition(ff.sampleRate * 3600)), format: ff, to: file)
            }
        }
        try? file.write(from: out)
        framesWritten += AVAudioFramePosition(out.frameLength)
        health.lastBufferAt = Date()
        health.peakSinceCheck = max(health.peakSinceCheck, peakLevel(out))
    }

    /// Сменился микрофон или формат (AirPods, сон Mac): тот же файл, новое устройство, пропуск — тишиной.
    func restart() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        engine = AVAudioEngine()
        lock.lock(); resync = firstBufferTime != nil; lock.unlock()
        do {
            try startEngine()
            lock.lock(); health.stopped = false; health.error = nil; lock.unlock()
        } catch {
            lock.lock(); health.stopped = true; health.error = error.localizedDescription; lock.unlock()
        }
    }

    func takeHealth() -> TrackHealth {
        lock.lock(); defer { lock.unlock() }
        let h = health
        health.peakSinceCheck = 0
        return h
    }

    func stop() {
        if let observer { NotificationCenter.default.removeObserver(observer) }
        observer = nil
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        lock.lock(); file = nil; lock.unlock()
    }
}

// MARK: - Запись встречи целиком

/// Обе дорожки вместе: others.wav (собеседники), mic.wav (вы) и meta.json со временем начала каждой.
final class MeetingRecorder {
    let folder: URL
    let target: String
    private let appRec: AppAudioRecorder
    private let micRec = MicRecorder()
    private var checkpoint: DispatchSourceTimer?
    private var started = Date()
    var echoCancel = false
    var onDeviceChange: (() -> Void)? {
        get { micRec.onDeviceChange }
        set { micRec.onDeviceChange = newValue }
    }
    var echoCancelActive: Bool { micRec.echoCancel }

    init(folder: URL, target: String) {
        self.folder = folder
        self.target = target
        appRec = AppAudioRecorder(url: folder.appendingPathComponent("others.wav"))
    }

    private var lockURL: URL { folder.appendingPathComponent("recording.json") }

    func start() async throws {
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        started = Date()
        // Замок: пока процесс жив — запись идёт; если нет, сервер восстановит её при следующем запуске.
        writeAtomically(["pid": Int(getpid()), "started": started.timeIntervalSince1970, "target": target], to: lockURL)
        try micRec.start(url: folder.appendingPathComponent("mic.wav"), echoCancel: echoCancel)
        do {
            try await appRec.start(target: target)
        } catch {
            micRec.stop()
            try? FileManager.default.removeItem(at: lockURL)
            throw error
        }
        writeMeta(inProgress: true)
        let t = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
        t.schedule(deadline: .now() + 30, repeating: 30)
        t.setEventHandler { [weak self] in self?.writeMeta(inProgress: true) }
        t.resume()
        checkpoint = t
    }

    /// Здоровье дорожек с прошлой проверки: микрофон и собеседники.
    func health() -> (mic: TrackHealth, others: TrackHealth) { (micRec.takeHealth(), appRec.takeHealth()) }

    func restartMic() { micRec.restart() }
    func restartOthers() async throws { try await appRec.restart() }

    func stop() async {
        checkpoint?.cancel()
        checkpoint = nil
        await appRec.stop()
        micRec.stop()
        writeMeta(inProgress: false)
        try? FileManager.default.removeItem(at: lockURL)
    }

    private func writeMeta(inProgress: Bool) {
        var meta: [String: Any] = ["target": target, "started": started.timeIntervalSince1970,
                                   "echo_cancel": micRec.echoCancel]
        if let t = appRec.firstBufferTime { meta["others_start"] = t.timeIntervalSince1970 }
        if let t = micRec.firstBufferTime { meta["mic_start"] = t.timeIntervalSince1970 }
        if inProgress {
            meta["in_progress"] = true
            meta["checkpoint"] = Date().timeIntervalSince1970
        }
        writeAtomically(meta, to: folder.appendingPathComponent("meta.json"))
    }
}
