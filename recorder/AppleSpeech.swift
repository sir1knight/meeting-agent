import AVFoundation
import Foundation
import Speech

struct SegmentOut: Codable {
    let start: Double
    let end: Double
    let text: String
}

struct ResponseOut: Codable {
    let engine: String
    let segments: [SegmentOut]
    let error: String?
}

func emit(_ resp: ResponseOut, exitCode: Int32 = 0) -> Never {
    let enc = JSONEncoder()
    enc.outputFormatting = [.withoutEscapingSlashes]
    if let data = try? enc.encode(resp), let s = String(data: data, encoding: .utf8) {
        print(s)
    } else {
        print("{\"engine\":\"error\",\"segments\":[],\"error\":\"encode failed\"}")
    }
    exit(exitCode)
}

@available(macOS 26.0, *)
func transcribeModern(url: URL, locale: Locale) async throws -> (String, [SegmentOut]) {
    let langCode = locale.language.languageCode?.identifier ?? "ru"
    let stLocales = await SpeechTranscriber.supportedLocales
    let useSpeechTranscriber = stLocales.contains(where: { $0.language.languageCode?.identifier == langCode })

    let module: any SpeechModule
    let resultsStream: AsyncThrowingStream<SegmentOut, Error>
    let engineName: String

    if useSpeechTranscriber {
        let t = SpeechTranscriber(
            locale: locale,
            transcriptionOptions: [],
            reportingOptions: [],
            attributeOptions: []
        )
        module = t
        engineName = "speech_transcriber"
        resultsStream = AsyncThrowingStream { cont in
            Task {
                do {
                    for try await r in t.results {
                        let txt = String(r.text.characters).trimmingCharacters(in: .whitespacesAndNewlines)
                        if txt.isEmpty { continue }
                        let s0 = max(0.0, CMTimeGetSeconds(r.range.start))
                        let d = CMTimeGetSeconds(r.range.duration)
                        let s1 = d.isFinite ? max(s0, s0 + d) : s0
                        cont.yield(SegmentOut(start: (s0 * 100).rounded() / 100, end: (s1 * 100).rounded() / 100, text: txt))
                    }
                    cont.finish()
                } catch {
                    cont.finish(throwing: error)
                }
            }
        }
    } else {
        let d = DictationTranscriber(locale: locale, preset: .longDictation)
        module = d
        engineName = "dictation_transcriber"
        resultsStream = AsyncThrowingStream { cont in
            Task {
                do {
                    for try await r in d.results {
                        let txt = String(r.text.characters).trimmingCharacters(in: .whitespacesAndNewlines)
                        if txt.isEmpty { continue }
                        let s0 = max(0.0, CMTimeGetSeconds(r.range.start))
                        let dur = CMTimeGetSeconds(r.range.duration)
                        let s1 = dur.isFinite ? max(s0, s0 + dur) : s0
                        cont.yield(SegmentOut(start: (s0 * 100).rounded() / 100, end: (s1 * 100).rounded() / 100, text: txt))
                    }
                    cont.finish()
                } catch {
                    cont.finish(throwing: error)
                }
            }
        }
    }

    if let req = try await AssetInventory.assetInstallationRequest(supporting: [module]) {
        try await req.downloadAndInstall()
    }
    let audioFile = try AVAudioFile(forReading: url)
    async let collectTask: [SegmentOut] = {
        var out: [SegmentOut] = []
        for try await seg in resultsStream { out.append(seg) }
        return out
    }()
    let analyzer = try await SpeechAnalyzer(
        inputAudioFile: audioFile,
        modules: [module],
        finishAfterFile: true
    )
    let out = try await collectTask
    _ = analyzer
    return (engineName, out)
}

func transcribeLegacy(url: URL, locale: Locale, contextual: [String]) async throws -> [SegmentOut] {
    let auth = await withCheckedContinuation { cont in
        SFSpeechRecognizer.requestAuthorization { status in
            cont.resume(returning: status)
        }
    }
    guard auth == .authorized else {
        throw NSError(domain: "AppleSpeech", code: 1, userInfo: [
            NSLocalizedDescriptionKey: "Нет разрешения «Распознавание речи» в Настройках macOS"
        ])
    }
    guard let rec = SFSpeechRecognizer(locale: locale), rec.isAvailable else {
        throw NSError(domain: "AppleSpeech", code: 2, userInfo: [
            NSLocalizedDescriptionKey: "Встроенное распознавание речи macOS недоступно для \(locale.identifier)"
        ])
    }
    let req = SFSpeechURLRecognitionRequest(url: url)
    req.shouldReportPartialResults = false
    if rec.supportsOnDeviceRecognition {
        req.requiresOnDeviceRecognition = true
    }
    if !contextual.isEmpty {
        req.contextualStrings = contextual
    }
    return try await withCheckedThrowingContinuation { cont in
        var resumed = false
        rec.recognitionTask(with: req) { result, err in
            if resumed { return }
            if let err {
                resumed = true
                cont.resume(throwing: err)
                return
            }
            guard let result, result.isFinal else { return }
            resumed = true
            let text = result.bestTranscription.formattedString.trimmingCharacters(in: .whitespacesAndNewlines)
            if text.isEmpty {
                cont.resume(returning: [])
                return
            }
            let segs = result.bestTranscription.segments
            let s0 = segs.first?.timestamp ?? 0.0
            let s1 = (segs.last.map { $0.timestamp + $0.duration }) ?? s0
            cont.resume(returning: [SegmentOut(
                start: (s0 * 100).rounded() / 100,
                end: (s1 * 100).rounded() / 100,
                text: text
            )])
        }
    }
}

@main
struct AppleSpeechCLI {
    static func main() async {
        let args = Array(CommandLine.arguments.dropFirst())
        if args.contains("--check") {
            let lang = args.first(where: { $0.hasPrefix("--lang=") }).map { String($0.dropFirst(7)) } ?? "ru-RU"
            let loc = Locale(identifier: lang)
            let code = loc.language.languageCode?.identifier ?? "ru"
            if #available(macOS 26.0, *) {
                let st = await SpeechTranscriber.supportedLocales
                let dt = await DictationTranscriber.supportedLocales
                let ok = st.contains(where: { $0.language.languageCode?.identifier == code })
                    || dt.contains(where: { $0.language.languageCode?.identifier == code })
                emit(ResponseOut(engine: "speech_analyzer", segments: [], error: ok ? nil : "Язык \(lang) не поддерживается"))
            } else {
                let ok = SFSpeechRecognizer(locale: loc)?.isAvailable ?? false
                emit(ResponseOut(engine: "sf_speech", segments: [], error: ok ? nil : "Язык \(lang) недоступен"))
            }
        }
        guard let path = args.first(where: { !$0.hasPrefix("-") }) else {
            emit(ResponseOut(engine: "error", segments: [], error: "Укажите путь к аудиофайлу"), exitCode: 2)
        }
        let lang = args.first(where: { $0.hasPrefix("--lang=") }).map { String($0.dropFirst(7)) } ?? "ru-RU"
        let hints = args.first(where: { $0.hasPrefix("--hints=") })
            .map { String($0.dropFirst(8)).split(separator: ",").map(String.init) } ?? []
        let url = URL(fileURLWithPath: path)
        let locale = Locale(identifier: lang)
        do {
            if #available(macOS 26.0, *) {
                let (eng, segs) = try await transcribeModern(url: url, locale: locale)
                emit(ResponseOut(engine: eng, segments: segs, error: nil))
            } else {
                let segs = try await transcribeLegacy(url: url, locale: locale, contextual: hints)
                emit(ResponseOut(engine: "sf_speech", segments: segs, error: nil))
            }
        } catch {
            emit(ResponseOut(engine: "error", segments: [], error: error.localizedDescription), exitCode: 1)
        }
    }
}
