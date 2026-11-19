// zoomrec — консольный рекордер для ./agent record и ./agent watch.
//
// Сборка:  swiftc -O recorder/Recorder.swift recorder/main.swift -o bin/zoomrec
// Запуск:  ./zoomrec <папка_для_записи> [bundle_id | all]
//          bundle_id по умолчанию us.zoom.xos; "all" — весь системный звук.
// Остановка: Ctrl+C или SIGTERM. На выходе: others.wav, mic.wav, meta.json

import Foundation

let args = CommandLine.arguments
guard args.count >= 2 else {
    fputs("Использование: zoomrec <папка> [bundle_id|all]\n", stderr)
    exit(1)
}
let outDir = URL(fileURLWithPath: (args[1] as NSString).expandingTildeInPath, isDirectory: true)
let recorder = MeetingRecorder(folder: outDir, target: args.count >= 3 ? args[2] : "us.zoom.xos")

func shutdown() {
    Task {
        await recorder.stop()
        print("STOPPED")
        exit(0)
    }
}

signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)
let signalSources: [DispatchSourceSignal] = [SIGINT, SIGTERM].map { sig in
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler { shutdown() }
    src.resume()
    return src
}

Task {
    do {
        try await recorder.start()
        print("RECORDING \(outDir.path)")
        fflush(stdout)
    } catch {
        fputs("Ошибка: \(error.localizedDescription)\n", stderr)
        exit(2)
    }
}

dispatchMain()
