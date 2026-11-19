// Запись встречи: звук приложения (Zoom или весь системный) через ScreenCaptureKit
// и микрофон через AVAudioEngine — в два отдельных WAV-файла.
// Используется и консольным zoomrec (main.swift), и приложением Meeting Agent.
//
// Требуется macOS 13+ и разрешения «Запись экрана и системного звука» и «Микрофон»
// для приложения, из которого запускается.

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

// MARK: - Звук приложения (собеседники)

final class AppAudioRecorder: NSObject, SCStreamOutput, SCStreamDelegate {
    private var stream: SCStream?
    private var file: AVAudioFile?
    private let url: URL
    private let queue = DispatchQueue(label: "app-audio")
    private(set) var firstBufferTime: Date?
    private var firstPTS: Double?
    private var framesWritten: AVAudioFramePosition = 0

    init(url: URL) { self.url = url }

    func start(target: String) async throws {
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
        // Видео не нужно — делаем его минимальным.
        cfg.width = 2
        cfg.height = 2
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)

        let s = SCStream(filter: filter, configuration: cfg, delegate: self)
        try s.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        try s.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
        try await s.startCapture()
        stream = s
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
                self.file = try? AVAudioFile(
                    forWriting: self.url,
                    settings: wavSettings(sampleRate: format.sampleRate, channels: format.channelCount),
                    commonFormat: .pcmFormatFloat32,
                    interleaved: false)
            }
            guard let file = self.file, let first = self.firstPTS else { return }

            // Если система не присылала звук (тишина в Zoom), дописываем нули,
            // чтобы время в файле совпадало с реальным и с дорожкой микрофона.
            if pts.isFinite {
                let expected = AVAudioFramePosition((pts - first) * format.sampleRate)
                var gap = expected - self.framesWritten
                if gap > AVAudioFramePosition(format.sampleRate * 0.05) {
                    gap = min(gap, AVAudioFramePosition(format.sampleRate * 3600))
                    self.writeSilence(frames: gap, format: format, to: file)
                }
            }
            try? file.write(from: buf)
            self.framesWritten += AVAudioFramePosition(buf.frameLength)
        }
    }

    private func writeSilence(frames: AVAudioFramePosition, format: AVAudioFormat, to file: AVAudioFile) {
        let chunk = AVAudioFrameCount(format.sampleRate)  // по секунде
        var left = frames
        while left > 0 {
            let n = AVAudioFrameCount(min(AVAudioFramePosition(chunk), left))
            guard let silence = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: n),
                  let data = silence.floatChannelData else { return }
            silence.frameLength = n
            for ch in 0..<Int(format.channelCount) {
                memset(data[ch], 0, Int(n) * MemoryLayout<Float>.size)
            }
            try? file.write(from: silence)
            framesWritten += AVAudioFramePosition(n)
            left -= AVAudioFramePosition(n)
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        fputs("Захват звука остановлен: \(error.localizedDescription)\n", stderr)
    }

    func stop() async {
        try? await stream?.stopCapture()
        queue.sync { self.file = nil }  // закрывает файл
    }
}

// MARK: - Микрофон (вы)

final class MicRecorder {
    private let engine = AVAudioEngine()
    private var file: AVAudioFile?
    private let lock = NSLock()
    private(set) var firstBufferTime: Date?

    func start(url: URL) throws {
        let input = engine.inputNode
        let fmt = input.outputFormat(forBus: 0)
        guard fmt.sampleRate > 0 else { throw RecorderError(message: "Микрофон недоступен") }
        file = try AVAudioFile(
            forWriting: url,
            settings: wavSettings(sampleRate: fmt.sampleRate, channels: fmt.channelCount),
            commonFormat: .pcmFormatFloat32,
            interleaved: false)
        input.installTap(onBus: 0, bufferSize: 4096, format: fmt) { [weak self] buf, _ in
            guard let self else { return }
            self.lock.lock(); defer { self.lock.unlock() }
            if self.firstBufferTime == nil { self.firstBufferTime = Date() }
            try? self.file?.write(from: buf)
        }
        try engine.start()
    }

    func stop() {
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

    init(folder: URL, target: String) {
        self.folder = folder
        self.target = target
        appRec = AppAudioRecorder(url: folder.appendingPathComponent("others.wav"))
    }

    func start() async throws {
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        try micRec.start(url: folder.appendingPathComponent("mic.wav"))
        do {
            try await appRec.start(target: target)
        } catch {
            micRec.stop()
            throw error
        }
    }

    func stop() async {
        await appRec.stop()
        micRec.stop()
        writeMeta()
    }

    private func writeMeta() {
        var meta: [String: Any] = ["target": target]
        if let t = appRec.firstBufferTime { meta["others_start"] = t.timeIntervalSince1970 }
        if let t = micRec.firstBufferTime { meta["mic_start"] = t.timeIntervalSince1970 }
        if let data = try? JSONSerialization.data(withJSONObject: meta, options: [.prettyPrinted]) {
            try? data.write(to: folder.appendingPathComponent("meta.json"))
        }
    }
}
