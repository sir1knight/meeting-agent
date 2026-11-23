// Запись встречи: звук приложения (Zoom или весь системный) через Core Audio Process Tap (macOS 14.2+,
// без разрешения «Запись экрана») или ScreenCaptureKit (запасной вариант) и микрофон через AVAudioEngine —
// в два отдельных WAV-файла. Используется и консольным zoomrec (main.swift), и приложением Meeting Agent.
//
// Надёжность:
//  • recording.json — «замок» идущей записи (pid процесса); meta.json обновляется каждые 30 с. Если процесс
//    упал или Mac выключился, сервер при следующем запуске найдёт запись, починит WAV и обработает её;
//  • смена микрофона (подключили AirPods) — микрофон перезапускается на новом устройстве, пропуск
//    заполняется тишиной по часам, чтобы дорожки не разъехались; захват звука компьютера после сбоя
//    или сна — тоже перезапускается;
//  • защита от iPhone Continuity Microphone — не даёт macOS молча увести запись на телефон рядом;
//  • мониторинг аппаратного Mute динамиков — фиксирует интервалы выключенного звука (для AEC и проверок);
//  • пауза / возобновление без разрыва встречи и без рассинхрона дорожек;
//  • уровень и свежесть звука каждой дорожки + проверка соседних микрофонов при тишине;
//  • подавление эха (Voice Processing macOS): голос собеседников из динамиков вычитается из микрофона.
//
// Требуется macOS 13+. На macOS 14.2+ системный звук пишется через Process Tap (достаточно разрешения
// «Только системное аудио»), на macOS 13–14.1 — через ScreenCaptureKit («Запись экрана и системного звука»).

import AppKit
import AudioToolbox
import AVFoundation
import CoreAudio
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

/// Пиковый и RMS уровень буфера (0 — цифровая тишина: выключен звук устройства или нет доступа).
func bufferLevels(_ buf: AVAudioPCMBuffer) -> (peak: Float, rms: Float) {
    guard let data = buf.floatChannelData, buf.frameLength > 0 else { return (0, 0) }
    var peak: Float = 0
    var sumSq: Double = 0
    let frames = Int(buf.frameLength)
    let channels = Int(buf.format.channelCount)
    for ch in 0..<channels {
        let p = data[ch]
        for i in 0..<frames {
            let v = p[i]
            let av = abs(v)
            if av > peak { peak = av }
            sumSq += Double(v) * Double(v)
        }
    }
    let rms = Float(sqrt(sumSq / Double(max(1, frames * channels))))
    return (peak, rms)
}

/// Здоровье дорожки с прошлой проверки.
struct TrackHealth {
    var lastBufferAt: Date?
    var peakSinceCheck: Float = 0
    var stopped = false
    var error: String?
}

// MARK: - Core Audio утилиты (устройства ввода, Continuity Mic, Mute динамиков, поиск активного микрофона)

struct InputDeviceInfo {
    let id: AudioDeviceID
    let name: String
    let uid: String
    let transport: UInt32
    let isContinuity: Bool
    let isVirtual: Bool
}

enum CoreAudioTools {
    static let systemObject = AudioObjectID(kAudioObjectSystemObject)

    static func defaultDevice(selector: AudioObjectPropertySelector) -> AudioDeviceID {
        var dev = AudioDeviceID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        var addr = AudioObjectPropertyAddress(mSelector: selector,
                                              mScope: kAudioObjectPropertyScopeGlobal,
                                              mElement: kAudioObjectPropertyElementMain)
        _ = AudioObjectGetPropertyData(systemObject, &addr, 0, nil, &size, &dev)
        return dev
    }

    static func defaultInputDevice() -> AudioDeviceID {
        defaultDevice(selector: kAudioHardwarePropertyDefaultInputDevice)
    }

    static func defaultOutputDevice() -> AudioDeviceID {
        defaultDevice(selector: kAudioHardwarePropertyDefaultOutputDevice)
    }

    static func stringProperty(_ id: AudioObjectID, selector: AudioObjectPropertySelector) -> String {
        var cf: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        var addr = AudioObjectPropertyAddress(mSelector: selector,
                                              mScope: kAudioObjectPropertyScopeGlobal,
                                              mElement: kAudioObjectPropertyElementMain)
        guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &cf) == noErr,
              let val = cf?.takeRetainedValue() else { return "" }
        return val as String
    }

    static func uint32Property(_ id: AudioObjectID, selector: AudioObjectPropertySelector,
                               scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal,
                               element: AudioObjectPropertyElement = kAudioObjectPropertyElementMain) -> UInt32? {
        var v: UInt32 = 0
        var size = UInt32(MemoryLayout<UInt32>.size)
        var addr = AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: element)
        guard AudioObjectHasProperty(id, &addr),
              AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &v) == noErr else { return nil }
        return v
    }

    static func isDeviceAlive(_ id: AudioDeviceID) -> Bool {
        guard id != kAudioObjectUnknown else { return false }
        return (uint32Property(id, selector: kAudioDevicePropertyDeviceIsAlive) ?? 0) != 0
    }

    static func hasInputStreams(_ id: AudioDeviceID) -> Bool {
        var addr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreams,
                                              mScope: kAudioDevicePropertyScopeInput,
                                              mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        return AudioObjectGetPropertyDataSize(id, &addr, 0, nil, &size) == noErr && size > 0
    }

    static func inputDevices() -> [InputDeviceInfo] {
        var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices,
                                              mScope: kAudioObjectPropertyScopeGlobal,
                                              mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(systemObject, &addr, 0, nil, &size) == noErr, size > 0 else { return [] }
        var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(systemObject, &addr, 0, nil, &size, &ids) == noErr else { return [] }
        var result: [InputDeviceInfo] = []
        for id in ids where isDeviceAlive(id) && hasInputStreams(id) {
            let name = stringProperty(id, selector: kAudioObjectPropertyName)
            let uid = stringProperty(id, selector: kAudioDevicePropertyDeviceUID)
            let transport = uint32Property(id, selector: kAudioDevicePropertyTransportType) ?? 0
            let uidLower = uid.lowercased()
            let nameLower = name.lowercased()
            let isContinuity = transport == kAudioDeviceTransportTypeContinuityCaptureWired
                || transport == kAudioDeviceTransportTypeContinuityCaptureWireless
                || uidLower.contains("continuitycapture")
            let isVirtual = transport == kAudioDeviceTransportTypeVirtual
                || transport == kAudioDeviceTransportTypeAggregate
                || uidLower.hasPrefix("local.meeting-agent.")
                || nameLower.contains("blackhole")
                || nameLower.contains("zoomaudiodevice")
                || nameLower.contains("teamsaudio")
                || nameLower.contains("soundflower")
                || nameLower.contains("loopback")
            result.append(InputDeviceInfo(id: id, name: name, uid: uid, transport: transport,
                                          isContinuity: isContinuity, isVirtual: isVirtual))
        }
        return result
    }

    /// Выбирает надёжный входной микрофон: если система сама переключилась на iPhone Continuity Mic,
    /// остаёмся на локальном микрофоне (USB / Bluetooth / встроенный).
    static func resolveInputDevice(skipContinuity: Bool) -> (id: AudioDeviceID, name: String, rejectedContinuity: String?) {
        let defID = defaultInputDevice()
        let all = inputDevices()
        let defInfo = all.first { $0.id == defID }
        if !skipContinuity || defInfo == nil || !(defInfo?.isContinuity ?? false) {
            return (defID, defInfo?.name ?? "", nil)
        }
        // Системный вход — iPhone Continuity Mic: подбираем лучший локальный микрофон.
        let candidates = all.filter { !$0.isContinuity }
        func rank(_ d: InputDeviceInfo) -> Int {
            if d.isVirtual { return 0 }
            if d.transport == kAudioDeviceTransportTypeUSB { return 4 }
            if d.transport == kAudioDeviceTransportTypeBluetooth || d.transport == kAudioDeviceTransportTypeBluetoothLE { return 3 }
            if d.transport == kAudioDeviceTransportTypeBuiltIn { return 2 }
            return 1
        }
        if let best = candidates.max(by: { rank($0) < rank($1) }) {
            return (best.id, best.name, defInfo?.name ?? "iPhone")
        }
        return (defID, defInfo?.name ?? "", nil)
    }

    /// Выключен ли аппаратно звук динамиков (кнопка Mute или громкость 0).
    static func isOutputMuted(_ deviceID: AudioDeviceID = defaultOutputDevice()) -> Bool {
        guard deviceID != kAudioObjectUnknown else { return false }
        for elem: AudioObjectPropertyElement in [kAudioObjectPropertyElementMain, 1, 2] {
            if let m = uint32Property(deviceID, selector: kAudioDevicePropertyMute,
                                      scope: kAudioDevicePropertyScopeOutput, element: elem), m != 0 {
                return true
            }
        }
        var vol: Float32 = 1.0
        var size = UInt32(MemoryLayout<Float32>.size)
        var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwareServiceDeviceProperty_VirtualMainVolume,
                                              mScope: kAudioDevicePropertyScopeOutput,
                                              mElement: kAudioObjectPropertyElementMain)
        if AudioObjectHasProperty(deviceID, &addr),
           AudioObjectGetPropertyData(deviceID, &addr, 0, nil, &size, &vol) == noErr {
            return vol <= 0.001
        }
        return false
    }

    /// Ищет другой подключённый микрофон, на котором сейчас есть реальный звук (речь), пока активный молчит.
    static func probeOtherActiveMic(excluding currentID: AudioDeviceID, skipContinuity: Bool,
                                    duration: Double = 0.5) -> (id: AudioDeviceID, name: String, peak: Float)? {
        let candidates = inputDevices().filter { d in
            d.id != currentID && !d.isVirtual && (!skipContinuity || !d.isContinuity)
        }
        var best: (id: AudioDeviceID, name: String, peak: Float)?
        for dev in candidates {
            let peak = measureDevicePeak(dev.id, duration: duration)
            if peak > 0.015, best == nil || peak > best!.peak {
                best = (dev.id, dev.name, peak)
            }
        }
        return best
    }

    private static func measureDevicePeak(_ devID: AudioDeviceID, duration: Double) -> Float {
        var asbd = AudioStreamBasicDescription()
        var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        var addr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreamFormat,
                                              mScope: kAudioDevicePropertyScopeInput,
                                              mElement: kAudioObjectPropertyElementMain)
        guard AudioObjectGetPropertyData(devID, &addr, 0, nil, &size, &asbd) == noErr,
              asbd.mSampleRate > 0,
              (asbd.mFormatFlags & kAudioFormatFlagIsFloat) != 0 else { return 0 }

        var peak: Float = 0
        let lock = NSLock()
        var procID: AudioDeviceIOProcID?
        let q = DispatchQueue(label: "mic-probe")
        let st = AudioDeviceCreateIOProcIDWithBlock(&procID, devID, q) { _, inInputData, _, _, _ in
            let list = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inInputData))
            var localPeak: Float = 0
            for buf in list {
                guard let raw = buf.mData else { continue }
                let count = Int(buf.mDataByteSize) / MemoryLayout<Float>.size
                let ptr = raw.assumingMemoryBound(to: Float.self)
                for i in 0..<count {
                    let a = abs(ptr[i])
                    if a > localPeak { localPeak = a }
                }
            }
            lock.lock()
            if localPeak > peak { peak = localPeak }
            lock.unlock()
        }
        guard st == noErr, let pid = procID else { return 0 }
        if AudioDeviceStart(devID, pid) == noErr {
            Thread.sleep(forTimeInterval: duration)
            _ = AudioDeviceStop(devID, pid)
        }
        _ = AudioDeviceDestroyIOProcID(devID, pid)
        lock.lock()
        let out = peak
        lock.unlock()
        return out
    }

    /// AudioObjectID процессов Core Audio (macOS 14.2+) для Process Tap.
    @available(macOS 14.2, *)
    static func processObjectIDs(bundlePrefix: String? = nil, matchingPID: pid_t? = nil) -> [AudioObjectID] {
        var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyProcessObjectList,
                                              mScope: kAudioObjectPropertyScopeGlobal,
                                              mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(systemObject, &addr, 0, nil, &size) == noErr, size > 0 else { return [] }
        var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
        guard AudioObjectGetPropertyData(systemObject, &addr, 0, nil, &size, &ids) == noErr else { return [] }
        let prefix = bundlePrefix?.lowercased()
        var out: [AudioObjectID] = []
        for id in ids {
            var pid: pid_t = 0
            var sz = UInt32(MemoryLayout<pid_t>.size)
            var a = AudioObjectPropertyAddress(mSelector: kAudioProcessPropertyPID,
                                               mScope: kAudioObjectPropertyScopeGlobal,
                                               mElement: kAudioObjectPropertyElementMain)
            let hasPid = AudioObjectGetPropertyData(id, &a, 0, nil, &sz, &pid) == noErr
            if let wantPid = matchingPID {
                if hasPid && pid == wantPid { out.append(id) }
                continue
            }
            if let prefix {
                var bid = stringProperty(id, selector: kAudioProcessPropertyBundleID).lowercased()
                if bid.isEmpty, hasPid, let app = NSRunningApplication(processIdentifier: pid), let b = app.bundleIdentifier {
                    bid = b.lowercased()
                }
                if bid.hasPrefix(prefix) { out.append(id) }
            }
        }
        return out
    }
}

// MARK: - Мониторинг аппаратного Mute динамиков

final class SpeakerMuteMonitor {
    private let lock = NSLock()
    private var outputDeviceID = AudioDeviceID(kAudioObjectUnknown)
    private var defaultListener: AudioObjectPropertyListenerBlock?
    private var muteListener: AudioObjectPropertyListenerBlock?
    private var volListener: AudioObjectPropertyListenerBlock?
    private let queue = DispatchQueue(label: "speaker-mute")
    private var _isMuted = false
    private var startedAt = Date()
    private var mutedIntervals: [(start: Double, end: Double?)] = []
    var onChange: ((Bool) -> Void)?

    var isMuted: Bool { lock.lock(); defer { lock.unlock() }; return _isMuted }

    func start(recordingStart: Date) {
        lock.lock()
        startedAt = recordingStart
        mutedIntervals = []
        _isMuted = CoreAudioTools.isOutputMuted()
        if _isMuted { mutedIntervals.append((0.0, nil)) }
        lock.unlock()

        var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice,
                                              mScope: kAudioObjectPropertyScopeGlobal,
                                              mElement: kAudioObjectPropertyElementMain)
        let block: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            self?.attachToDefaultOutput()
            self?.refreshMuteState()
        }
        defaultListener = block
        AudioObjectAddPropertyListenerBlock(CoreAudioTools.systemObject, &addr, queue, block)
        attachToDefaultOutput()
    }

    func stop() {
        let nowSec = max(0, Date().timeIntervalSince(startedAt))
        lock.lock()
        if let last = mutedIntervals.indices.last, mutedIntervals[last].end == nil {
            mutedIntervals[last].end = nowSec
        }
        lock.unlock()
        detachOutputListeners()
        if let l = defaultListener {
            var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice,
                                                  mScope: kAudioObjectPropertyScopeGlobal,
                                                  mElement: kAudioObjectPropertyElementMain)
            AudioObjectRemovePropertyListenerBlock(CoreAudioTools.systemObject, &addr, queue, l)
            defaultListener = nil
        }
    }

    func intervalsSnapshot() -> [[Double]] {
        let nowSec = max(0, Date().timeIntervalSince(startedAt))
        lock.lock(); defer { lock.unlock() }
        return mutedIntervals.map { [round($0.start * 100) / 100, round(($0.end ?? nowSec) * 100) / 100] }
    }

    private func attachToDefaultOutput() {
        detachOutputListeners()
        let dev = CoreAudioTools.defaultOutputDevice()
        guard dev != kAudioObjectUnknown else { return }
        outputDeviceID = dev
        let block: AudioObjectPropertyListenerBlock = { [weak self] _, _ in self?.refreshMuteState() }
        muteListener = block
        volListener = block
        var muteAddr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyMute,
                                                  mScope: kAudioDevicePropertyScopeOutput,
                                                  mElement: kAudioObjectPropertyElementMain)
        if AudioObjectHasProperty(dev, &muteAddr) {
            AudioObjectAddPropertyListenerBlock(dev, &muteAddr, queue, block)
        }
        var volAddr = AudioObjectPropertyAddress(mSelector: kAudioHardwareServiceDeviceProperty_VirtualMainVolume,
                                                 mScope: kAudioDevicePropertyScopeOutput,
                                                 mElement: kAudioObjectPropertyElementMain)
        if AudioObjectHasProperty(dev, &volAddr) {
            AudioObjectAddPropertyListenerBlock(dev, &volAddr, queue, block)
        }
    }

    private func detachOutputListeners() {
        let dev = outputDeviceID
        guard dev != kAudioObjectUnknown else { return }
        if let l = muteListener {
            var muteAddr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyMute,
                                                      mScope: kAudioDevicePropertyScopeOutput,
                                                      mElement: kAudioObjectPropertyElementMain)
            AudioObjectRemovePropertyListenerBlock(dev, &muteAddr, queue, l)
            muteListener = nil
        }
        if let l = volListener {
            var volAddr = AudioObjectPropertyAddress(mSelector: kAudioHardwareServiceDeviceProperty_VirtualMainVolume,
                                                     mScope: kAudioDevicePropertyScopeOutput,
                                                     mElement: kAudioObjectPropertyElementMain)
            AudioObjectRemovePropertyListenerBlock(dev, &volAddr, queue, l)
            volListener = nil
        }
        outputDeviceID = AudioDeviceID(kAudioObjectUnknown)
    }

    private func refreshMuteState() {
        let muted = CoreAudioTools.isOutputMuted()
        let nowSec = max(0, Date().timeIntervalSince(startedAt))
        var changed = false
        lock.lock()
        if muted != _isMuted {
            _isMuted = muted
            changed = true
            if muted {
                mutedIntervals.append((nowSec, nil))
            } else if let last = mutedIntervals.indices.last, mutedIntervals[last].end == nil {
                mutedIntervals[last].end = nowSec
            }
        }
        lock.unlock()
        if changed { onChange?(muted) }
    }
}

// MARK: - Звук приложения (собеседники): Core Audio Process Tap (macOS 14.2+) + ScreenCaptureKit fallback

final class AppAudioRecorder: NSObject, SCStreamOutput, SCStreamDelegate {
    private var stream: SCStream?
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateDeviceID = AudioDeviceID(kAudioObjectUnknown)
    private var ioProcID: AudioDeviceIOProcID?
    private var tapFormat = AudioStreamBasicDescription()

    private var file: AVAudioFile?
    private var fileFormat: AVAudioFormat?
    private var converter: AVAudioConverter?
    private let url: URL
    private let queue = DispatchQueue(label: "app-audio")
    private(set) var firstBufferTime: Date?
    private var firstPTS: Double?
    private var framesWritten: AVAudioFramePosition = 0
    private var resync = false           // после перезапуска: пропуск — по часам, а не по меткам потока
    private var paused = false
    private var resetPTSAfterPause = false
    private var health = TrackHealth()
    private var liveRms: Float = 0
    private var target = "all"
    private(set) var captureMode = "screencapturekit"  // "process_tap" | "screencapturekit"
    var preferProcessTap = true

    init(url: URL) { self.url = url }

    func start(target: String) async throws {
        self.target = target
        if preferProcessTap, #available(macOS 14.2, *) {
            do {
                try startProcessTap(target: target)
                captureMode = "process_tap"
                return
            } catch let err as RecorderError where err.message.contains("не запущено") {
                throw err
            } catch {
                fputs("Process Tap не запустился (\(error.localizedDescription)) — пробую ScreenCaptureKit\n", stderr)
                cleanupProcessTap()
            }
        }
        try await startScreenCaptureKit(target: target)
        captureMode = "screencapturekit"
    }

    @available(macOS 14.2, *)
    private func startProcessTap(target: String) throws {
        let tapDesc: CATapDescription
        if target == "all" {
            let me = CoreAudioTools.processObjectIDs(matchingPID: getpid())
            tapDesc = CATapDescription(stereoGlobalTapButExcludeProcesses: me)
        } else {
            let running = NSRunningApplication.runningApplications(withBundleIdentifier: target)
            guard !running.isEmpty else {
                throw RecorderError(message: "Приложение \(target) не запущено")
            }
            let procs = CoreAudioTools.processObjectIDs(bundlePrefix: target)
            if !procs.isEmpty {
                tapDesc = CATapDescription(stereoMixdownOfProcesses: procs)
            } else {
                // Приложение запущено, но ещё не открыло аудиопоток — захватываем весь звук (кроме себя).
                let me = CoreAudioTools.processObjectIDs(matchingPID: getpid())
                tapDesc = CATapDescription(stereoGlobalTapButExcludeProcesses: me)
            }
        }
        tapDesc.name = "Meeting Agent System Audio"
        tapDesc.muteBehavior = .unmuted
        tapDesc.isPrivate = true

        var createdTap = AudioObjectID(kAudioObjectUnknown)
        let tapStatus = AudioHardwareCreateProcessTap(tapDesc, &createdTap)
        guard tapStatus == noErr, createdTap != kAudioObjectUnknown else {
            throw RecorderError(message: "Не удалось создать Process Tap (OSStatus \(tapStatus))")
        }
        tapID = createdTap

        let aggUID = "local.meeting-agent.tap.\(UUID().uuidString)"
        let aggDesc: [String: Any] = [
            kAudioAggregateDeviceNameKey as String: "Meeting Agent Tap",
            kAudioAggregateDeviceUIDKey as String: aggUID,
            kAudioAggregateDeviceIsPrivateKey as String: true,
            kAudioAggregateDeviceIsStackedKey as String: false,
            kAudioAggregateDeviceTapAutoStartKey as String: true,
            kAudioAggregateDeviceTapListKey as String: [
                [
                    kAudioSubTapUIDKey as String: tapDesc.uuid.uuidString,
                    kAudioSubTapDriftCompensationKey as String: true,
                ]
            ],
        ]
        var aggID = AudioDeviceID(kAudioObjectUnknown)
        let aggStatus = AudioHardwareCreateAggregateDevice(aggDesc as CFDictionary, &aggID)
        guard aggStatus == noErr, aggID != kAudioObjectUnknown else {
            cleanupProcessTap()
            throw RecorderError(message: "Не удалось создать агрегатное устройство (OSStatus \(aggStatus))")
        }
        aggregateDeviceID = aggID

        var asbd = AudioStreamBasicDescription()
        var sz = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        var fmtAddr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreamFormat,
                                                 mScope: kAudioDevicePropertyScopeInput,
                                                 mElement: kAudioObjectPropertyElementMain)
        let fmtStatus = AudioObjectGetPropertyData(aggID, &fmtAddr, 0, nil, &sz, &asbd)
        guard fmtStatus == noErr, asbd.mSampleRate > 0, asbd.mChannelsPerFrame > 0 else {
            cleanupProcessTap()
            throw RecorderError(message: "Не удалось получить формат Process Tap (OSStatus \(fmtStatus))")
        }
        tapFormat = asbd

        var proc: AudioDeviceIOProcID?
        let ioStatus = AudioDeviceCreateIOProcIDWithBlock(&proc, aggID, queue) { [weak self] _, inInputData, _, _, _ in
            self?.handleTapBuffer(inInputData)
        }
        guard ioStatus == noErr, let validProc = proc else {
            cleanupProcessTap()
            throw RecorderError(message: "Не удалось создать IOProc для Process Tap (OSStatus \(ioStatus))")
        }
        ioProcID = validProc

        let startStatus = AudioDeviceStart(aggID, validProc)
        guard startStatus == noErr else {
            cleanupProcessTap()
            throw RecorderError(message: "Не удалось запустить Process Tap (OSStatus \(startStatus))")
        }
        queue.sync {
            self.health.stopped = false
            self.health.error = nil
        }
    }

    private func handleTapBuffer(_ inData: UnsafePointer<AudioBufferList>) {
        guard !paused else { liveRms = 0; return }
        let list = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inData))
        guard let firstBuf = list.first, firstBuf.mData != nil, firstBuf.mDataByteSize > 0 else { return }

        let sr = tapFormat.mSampleRate
        let isFloat = (tapFormat.mFormatFlags & kAudioFormatFlagIsFloat) != 0
        guard isFloat, sr > 0 else { return }

        let isNonInterleaved = (tapFormat.mFormatFlags & kAudioFormatFlagIsNonInterleaved) != 0
        let channels = max(1, Int(tapFormat.mChannelsPerFrame))
        let frameCount: Int
        if isNonInterleaved {
            frameCount = Int(firstBuf.mDataByteSize) / MemoryLayout<Float>.size
        } else {
            frameCount = Int(firstBuf.mDataByteSize) / (MemoryLayout<Float>.size * channels)
        }
        guard frameCount > 0 else { return }

        guard let monoFmt = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: sr, channels: 1, interleaved: false),
              let monoBuf = AVAudioPCMBuffer(pcmFormat: monoFmt, frameCapacity: AVAudioFrameCount(frameCount)),
              let dst = monoBuf.floatChannelData?[0] else { return }
        monoBuf.frameLength = AVAudioFrameCount(frameCount)

        if isNonInterleaved {
            let chCount = min(channels, list.count)
            let inv = 1.0 / Float(max(1, chCount))
            memset(dst, 0, frameCount * MemoryLayout<Float>.size)
            for c in 0..<chCount {
                guard let raw = list[c].mData else { continue }
                let src = raw.assumingMemoryBound(to: Float.self)
                for i in 0..<frameCount { dst[i] += src[i] * inv }
            }
        } else {
            let src = firstBuf.mData!.assumingMemoryBound(to: Float.self)
            if channels == 1 {
                memcpy(dst, src, frameCount * MemoryLayout<Float>.size)
            } else {
                let inv = 1.0 / Float(channels)
                for i in 0..<frameCount {
                    var s: Float = 0
                    let base = i * channels
                    for c in 0..<channels { s += src[base + c] }
                    dst[i] = s * inv
                }
            }
        }
        writeMonoBuffer(monoBuf, pts: nil)
    }

    private func cleanupProcessTap() {
        if let proc = ioProcID, aggregateDeviceID != kAudioObjectUnknown {
            _ = AudioDeviceStop(aggregateDeviceID, proc)
            _ = AudioDeviceDestroyIOProcID(aggregateDeviceID, proc)
        }
        ioProcID = nil
        if aggregateDeviceID != kAudioObjectUnknown {
            _ = AudioHardwareDestroyAggregateDevice(aggregateDeviceID)
            aggregateDeviceID = AudioDeviceID(kAudioObjectUnknown)
        }
        if tapID != kAudioObjectUnknown {
            if #available(macOS 14.2, *) {
                _ = AudioHardwareDestroyProcessTap(tapID)
            }
            tapID = AudioObjectID(kAudioObjectUnknown)
        }
    }

    private func startScreenCaptureKit(target: String) async throws {
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
        cleanupProcessTap()
        queue.sync { self.resync = self.file != nil }
        try await start(target: target)
    }

    func setPaused(_ isPaused: Bool, pausedDuration: TimeInterval = 0) {
        queue.sync {
            self.paused = isPaused
            if isPaused {
                self.liveRms = 0
            } else {
                if let start = self.firstBufferTime, pausedDuration > 0 {
                    self.firstBufferTime = start.addingTimeInterval(pausedDuration)
                }
                self.resetPTSAfterPause = true
            }
        }
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sb.isValid, !paused,
              let asbd = sb.formatDescription?.audioStreamBasicDescription,
              let format = AVAudioFormat(standardFormatWithSampleRate: asbd.mSampleRate,
                                         channels: asbd.mChannelsPerFrame)
        else { return }

        let pts = CMSampleBufferGetPresentationTimeStamp(sb).seconds
        try? sb.withAudioBufferList { abl, _ in
            guard let buf = AVAudioPCMBuffer(pcmFormat: format, bufferListNoCopy: abl.unsafePointer) else { return }
            self.writeMonoBuffer(buf, pts: pts)
        }
    }

    private func writeMonoBuffer(_ buf: AVAudioPCMBuffer, pts: Double?) {
        let inFmt = buf.format
        if file == nil {
            firstBufferTime = Date()
            if let pts, pts.isFinite { firstPTS = pts }
            let mono = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: inFmt.sampleRate,
                                     channels: 1, interleaved: false)!
            fileFormat = mono
            file = try? AVAudioFile(
                forWriting: url,
                settings: wavSettings(sampleRate: mono.sampleRate, channels: 1),
                commonFormat: .pcmFormatFloat32,
                interleaved: false)
        }
        guard let file, let ff = fileFormat else { return }

        var out = buf
        if inFmt.sampleRate != ff.sampleRate || inFmt.channelCount != ff.channelCount {
            if converter == nil || converter?.inputFormat != inFmt || converter?.outputFormat != ff {
                converter = AVAudioConverter(from: inFmt, to: ff)
            }
            if let converter {
                let cap = AVAudioFrameCount(Double(buf.frameLength) * ff.sampleRate / inFmt.sampleRate) + 64
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
        }

        if resetPTSAfterPause {
            resetPTSAfterPause = false
            if let pts, pts.isFinite {
                firstPTS = pts - Double(framesWritten) / ff.sampleRate
            }
        } else if resync, let start = firstBufferTime {
            // После перезапуска (в том числе после сна) метки потока могли пойти заново — сверяемся с часами.
            resync = false
            let expected = AVAudioFramePosition(Date().timeIntervalSince(start) * ff.sampleRate)
            let gap = min(expected - framesWritten, AVAudioFramePosition(ff.sampleRate * 3600))
            if gap > AVAudioFramePosition(ff.sampleRate * 0.05) {
                framesWritten += writeSilence(frames: gap, format: ff, to: file)
            }
            if let pts, pts.isFinite { firstPTS = pts - Double(framesWritten) / ff.sampleRate }
        } else if let pts, pts.isFinite, let first = firstPTS {
            // Система не присылала звук (тишина в Zoom) — дописываем нули,
            // чтобы время в файле совпадало с реальным и с дорожкой микрофона.
            let expected = AVAudioFramePosition((pts - first) * ff.sampleRate)
            var gap = expected - framesWritten
            if gap > AVAudioFramePosition(ff.sampleRate * 0.05) {
                gap = min(gap, AVAudioFramePosition(ff.sampleRate * 3600))
                framesWritten += writeSilence(frames: gap, format: ff, to: file)
            }
        }
        try? file.write(from: out)
        framesWritten += AVAudioFramePosition(out.frameLength)
        let (peak, rms) = bufferLevels(out)
        health.lastBufferAt = Date()
        health.peakSinceCheck = max(health.peakSinceCheck, peak)
        liveRms = liveRms * 0.55 + rms * 0.45
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        fputs("Захват звука остановлен: \(error.localizedDescription)\n", stderr)
        queue.async { self.health.stopped = true; self.health.error = error.localizedDescription }
    }

    func takeHealth() -> TrackHealth {
        queue.sync { let h = self.health; self.health.peakSinceCheck = 0; return h }
    }

    func currentRms() -> Float {
        queue.sync { self.liveRms }
    }

    func stop() async {
        try? await stream?.stopCapture()
        stream = nil
        cleanupProcessTap()
        queue.sync { self.file = nil; self.liveRms = 0 }  // закрывает файл
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
    private var paused = false
    private var health = TrackHealth()
    private var liveRms: Float = 0
    private var url: URL?
    private var observer: NSObjectProtocol?
    private var wantEcho = false
    private(set) var echoCancel = false
    var skipContinuityMic = true
    private var preferredDeviceID: AudioDeviceID?
    private(set) var activeDeviceID = AudioDeviceID(kAudioObjectUnknown)
    private(set) var activeDeviceName = ""
    private(set) var continuityRejectedName: String?
    var onDeviceChange: (() -> Void)?

    /// echoCancel — Voice Processing macOS (подавление эха и шума). Не включилось — пишем как обычно.
    func start(url: URL, echoCancel: Bool, skipContinuityMic: Bool = true) throws {
        self.url = url
        self.wantEcho = echoCancel
        self.skipContinuityMic = skipContinuityMic
        try startEngine()
        observer = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: nil, queue: .main) { [weak self] note in
            guard let self, (note.object as? AVAudioEngine) === self.engine else { return }
            self.restart()
            self.onDeviceChange?()
        }
    }

    private func bindInputDeviceIfNeeded(on input: AVAudioInputNode) {
        let targetID: AudioDeviceID
        let targetName: String
        var rejected: String? = nil

        if let pref = preferredDeviceID, CoreAudioTools.isDeviceAlive(pref) {
            targetID = pref
            targetName = CoreAudioTools.stringProperty(pref, selector: kAudioObjectPropertyName)
        } else {
            preferredDeviceID = nil
            let resolved = CoreAudioTools.resolveInputDevice(skipContinuity: skipContinuityMic)
            targetID = resolved.id
            targetName = resolved.name
            rejected = resolved.rejectedContinuity
        }
        activeDeviceID = targetID
        activeDeviceName = targetName
        if let rejected { continuityRejectedName = rejected }

        let defID = CoreAudioTools.defaultInputDevice()
        if targetID != kAudioObjectUnknown && targetID != defID, let au = input.audioUnit {
            var dev = targetID
            let st = AudioUnitSetProperty(au, kAudioOutputUnitProperty_CurrentDevice,
                                          kAudioUnitScope_Global, 0, &dev,
                                          UInt32(MemoryLayout<AudioDeviceID>.size))
            if st != noErr {
                fputs("Не удалось переключить вход на \(targetName) (OSStatus \(st))\n", stderr)
            }
        }
    }

    private func startEngine() throws {
        let input = engine.inputNode
        bindInputDeviceIfNeeded(on: input)
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
        guard !paused, let file, let ff = fileFormat else {
            if paused { liveRms = 0 }
            return
        }
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
        let (peak, rms) = bufferLevels(out)
        health.lastBufferAt = Date()
        health.peakSinceCheck = max(health.peakSinceCheck, peak)
        liveRms = liveRms * 0.55 + rms * 0.45
    }

    func setPaused(_ isPaused: Bool, pausedDuration: TimeInterval = 0) {
        lock.lock(); defer { lock.unlock() }
        paused = isPaused
        if isPaused {
            liveRms = 0
        } else if let start = firstBufferTime, pausedDuration > 0 {
            firstBufferTime = start.addingTimeInterval(pausedDuration)
        }
    }

    /// Переключить запись на конкретный микрофон без остановки встречи.
    func switchToDevice(_ deviceID: AudioDeviceID) {
        preferredDeviceID = deviceID
        restart()
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

    func currentRms() -> Float {
        lock.lock(); defer { lock.unlock() }
        return liveRms
    }

    func stop() {
        if let observer { NotificationCenter.default.removeObserver(observer) }
        observer = nil
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        lock.lock(); file = nil; liveRms = 0; lock.unlock()
    }
}

// MARK: - Запись встречи целиком

/// Обе дорожки вместе: others.wav (собеседники), mic.wav (вы) и meta.json со временем начала каждой.
final class MeetingRecorder {
    let folder: URL
    let target: String
    private let appRec: AppAudioRecorder
    private let micRec = MicRecorder()
    private let muteMonitor = SpeakerMuteMonitor()
    private var checkpoint: DispatchSourceTimer?
    private var started = Date()
    private var pauseStartedAt: Date?
    private(set) var totalPausedSeconds: TimeInterval = 0
    private var pauses: [[Double]] = []
    private(set) var isPaused = false

    var echoCancel = false
    var skipContinuityMic = true
    var onDeviceChange: (() -> Void)? {
        get { micRec.onDeviceChange }
        set { micRec.onDeviceChange = newValue }
    }
    var onSpeakerMuteChange: ((Bool) -> Void)? {
        get { muteMonitor.onChange }
        set { muteMonitor.onChange = newValue }
    }
    var echoCancelActive: Bool { micRec.echoCancel }
    var captureMode: String { appRec.captureMode }
    var activeMicName: String { micRec.activeDeviceName }
    var activeMicID: AudioDeviceID { micRec.activeDeviceID }
    var continuityRejected: String? { micRec.continuityRejectedName }
    var isSpeakerMuted: Bool { muteMonitor.isMuted }

    init(folder: URL, target: String) {
        self.folder = folder
        self.target = target
        appRec = AppAudioRecorder(url: folder.appendingPathComponent("others.wav"))
    }

    private var lockURL: URL { folder.appendingPathComponent("recording.json") }

    func start() async throws {
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        started = Date()
        isPaused = false
        pauseStartedAt = nil
        totalPausedSeconds = 0
        pauses = []
        // Замок: пока процесс жив — запись идёт; если нет, сервер восстановит её при следующем запуске.
        writeAtomically(["pid": Int(getpid()), "started": started.timeIntervalSince1970, "target": target], to: lockURL)
        try micRec.start(url: folder.appendingPathComponent("mic.wav"), echoCancel: echoCancel,
                         skipContinuityMic: skipContinuityMic)
        do {
            try await appRec.start(target: target)
        } catch {
            micRec.stop()
            try? FileManager.default.removeItem(at: lockURL)
            throw error
        }
        muteMonitor.start(recordingStart: started)
        writeMeta(inProgress: true)
        let t = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
        t.schedule(deadline: .now() + 30, repeating: 30)
        t.setEventHandler { [weak self] in self?.writeMeta(inProgress: true) }
        t.resume()
        checkpoint = t
    }

    func pause() {
        guard !isPaused else { return }
        isPaused = true
        pauseStartedAt = Date()
        micRec.setPaused(true)
        appRec.setPaused(true)
        writeMeta(inProgress: true)
    }

    func resume() {
        guard isPaused else { return }
        let now = Date()
        let dt = pauseStartedAt.map { max(0, now.timeIntervalSince($0)) } ?? 0
        let audioAt = max(0, (pauseStartedAt ?? now).timeIntervalSince(started) - totalPausedSeconds)
        totalPausedSeconds += dt
        if dt >= 0.5 {
            pauses.append([round(audioAt * 10) / 10, round(dt * 10) / 10])
        }
        pauseStartedAt = nil
        isPaused = false
        micRec.setPaused(false, pausedDuration: dt)
        appRec.setPaused(false, pausedDuration: dt)
        writeMeta(inProgress: true)
    }

    /// Текущая накопленная длительность пауз (включая идущую прямо сейчас).
    func currentPausedSeconds() -> TimeInterval {
        let extra = (isPaused ? pauseStartedAt.map { max(0, Date().timeIntervalSince($0)) } : nil) ?? 0
        return totalPausedSeconds + extra
    }

    /// Здоровье дорожек с прошлой проверки: микрофон и собеседники.
    func health() -> (mic: TrackHealth, others: TrackHealth) { (micRec.takeHealth(), appRec.takeHealth()) }

    /// Текущий сглаженный RMS-уровень для живого индикатора (0…1).
    func levels() -> (mic: Float, others: Float) { (micRec.currentRms(), appRec.currentRms()) }

    func restartMic() { micRec.restart() }
    func switchMic(to deviceID: AudioDeviceID) { micRec.switchToDevice(deviceID) }
    func restartOthers() async throws { try await appRec.restart() }

    /// Поиск другого активного микрофона (выполнять на фоновой очереди).
    func probeOtherMic() -> (id: AudioDeviceID, name: String, peak: Float)? {
        CoreAudioTools.probeOtherActiveMic(excluding: micRec.activeDeviceID, skipContinuity: skipContinuityMic)
    }

    func stop() async {
        if isPaused { resume() }
        checkpoint?.cancel()
        checkpoint = nil
        muteMonitor.stop()
        await appRec.stop()
        micRec.stop()
        writeMeta(inProgress: false)
        try? FileManager.default.removeItem(at: lockURL)
    }

    private func writeMeta(inProgress: Bool) {
        var meta: [String: Any] = [
            "target": target,
            "started": started.timeIntervalSince1970,
            "echo_cancel": micRec.echoCancel,
            "capture_mode": appRec.captureMode,
        ]
        if !micRec.activeDeviceName.isEmpty { meta["mic_device"] = micRec.activeDeviceName }
        if let rej = micRec.continuityRejectedName { meta["continuity_rejected"] = rej }
        if let t = appRec.firstBufferTime { meta["others_start"] = t.timeIntervalSince1970 }
        if let t = micRec.firstBufferTime { meta["mic_start"] = t.timeIntervalSince1970 }
        if totalPausedSeconds > 0 { meta["paused_seconds"] = round(totalPausedSeconds * 10) / 10 }
        if !pauses.isEmpty { meta["pauses"] = pauses }
        let muted = muteMonitor.intervalsSnapshot()
        if !muted.isEmpty { meta["speaker_muted"] = muted }
        if inProgress {
            meta["in_progress"] = true
            meta["paused"] = isPaused
            meta["checkpoint"] = Date().timeIntervalSince1970
        }
        writeAtomically(meta, to: folder.appendingPathComponent("meta.json"))
    }
}
