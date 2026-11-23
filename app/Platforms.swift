// Какая встреча сейчас идёт: Zoom (по заголовку окна, а без «Записи экрана» — по микрофону),
// Teams, Яндекс Телемост, Slack, Discord, Webex, Telegram, WhatsApp и звонки в браузере
// (Google Meet, Телемост, Teams, Jitsi, SaluteJazz, Контур.Толк, Webex).
// Главный признак — программа сейчас пишет звук с микрофона (Core Audio, macOS 14.2+):
// так запись не начнётся от просто открытого окна и сама закончится, когда звонок завершится.

import AppKit
import CoreAudio
import CoreGraphics

enum MeetingPlatform: String, CaseIterable {
    case zoom, teams, telemost, meet, slack, discord, webex, telegram, whatsapp
    case jazz, ktalk, vkcalls, dion, trueconf

    var title: String {
        switch self {
        case .zoom: return "Zoom"
        case .teams: return "Microsoft Teams"
        case .telemost: return "Яндекс Телемост"
        case .meet: return "звонка в браузере"
        case .slack: return "Slack"
        case .discord: return "Discord"
        case .webex: return "Webex"
        case .telegram: return "Telegram"
        case .whatsapp: return "WhatsApp"
        case .jazz: return "SaluteJazz"
        case .ktalk: return "Контур.Толк"
        case .vkcalls: return "VK Звонки"
        case .dion: return "DION"
        case .trueconf: return "TrueConf"
        }
    }

    var shortTitle: String {
        switch self {
        case .zoom: return "Zoom"
        case .teams: return "Teams"
        case .telemost: return "Телемост"
        case .meet: return "браузер"
        case .slack: return "Slack"
        case .discord: return "Discord"
        case .webex: return "Webex"
        case .telegram: return "Telegram"
        case .whatsapp: return "WhatsApp"
        case .jazz: return "SaluteJazz"
        case .ktalk: return "Контур.Толк"
        case .vkcalls: return "VK Звонки"
        case .dion: return "DION"
        case .trueconf: return "TrueConf"
        }
    }

    var isCorp: Bool {
        switch self {
        case .jazz, .ktalk, .vkcalls, .dion, .trueconf: return true
        default: return false
        }
    }
}

enum Platforms {
    static let zoomBundles = ["us.zoom.xos"]
    static let teamsBundles = ["com.microsoft.teams"]                 // и com.microsoft.teams2, помощники
    static let telemostBundles = ["ru.yandex.desktop.telemost"]
    static let slackBundles = ["com.tinyspeck.slackmacgap"]
    static let discordBundles = ["com.hnc.discord"]
    static let webexBundles = ["com.cisco.webexmeetingsapp", "com.cisco.webex.meetings",
                               "com.webex.meetingmanager", "cisco-systems.spark"]
    static let telegramBundles = ["ru.keepcoder.telegram", "org.telegram.desktop"]
    static let whatsappBundles = ["net.whatsapp.whatsapp", "desktop.whatsapp"]
    static let jazzBundles = ["ru.sberdevices.jazz", "com.sberdevices.jazz", "ru.salutejazz"]
    static let ktalkBundles = ["ru.kontur.talk", "ru.kontur.ktalk"]
    static let vkCallsBundles = ["com.vk.calls", "ru.vk.calls", "com.vk.vkcalls"]
    static let dionBundles = ["vc.dion", "ru.dion", "com.dion"]
    static let trueconfBundles = ["com.trueconf", "ru.trueconf"]
    static let browserBundles = ["com.google.chrome", "com.apple.webkit", "com.apple.safari",
                                 "ru.yandex.desktop.yandex-browser", "com.microsoft.edgemac", "org.mozilla.firefox",
                                 "company.thebrowser", "com.operasoftware", "com.brave.browser", "org.chromium"]
    static let browserOwners = ["chrome", "safari", "yandex", "яндекс", "edge", "firefox", "arc", "opera", "brave", "chromium"]
    // Заголовок вкладки звонка в браузере (окно показывает заголовок активной вкладки).
    static let webCallTitles = ["meet.google.com", "google meet", "meet –", "meet -", "телемост", "telemost",
                                "microsoft teams", "teams.microsoft", "jitsi", "jazz", "salutejazz", "sberjazz",
                                "контур.толк", "ktalk", "talk.kontur", "vk звонки", "calls.vk", "мтс линк",
                                "mts-link", "webinar.ru", "сферум", "sferum", "pruffme", "trueconf", "dion",
                                "whereby", "webex", "huddle", "slack", "discord"]

    /// Bundle ID программ, у которых сейчас активен входной (микрофон) или выходной аудиопоток (macOS 14.2+).
    /// nil — macOS старше 14.2: узнать нельзя.
    private static func audioUsers(selector: AudioObjectPropertySelector) -> [String]? {
        guard #available(macOS 14.2, *) else { return nil }
        let system = AudioObjectID(kAudioObjectSystemObject)
        var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyProcessObjectList,
                                              mScope: kAudioObjectPropertyScopeGlobal,
                                              mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(system, &addr, 0, nil, &size) == noErr, size > 0 else { return [] }
        var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
        guard AudioObjectGetPropertyData(system, &addr, 0, nil, &size, &ids) == noErr else { return nil }
        let me = getpid()
        var out: [String] = []
        for id in ids {
            var running: UInt32 = 0
            var sz = UInt32(MemoryLayout<UInt32>.size)
            var a = AudioObjectPropertyAddress(mSelector: selector,
                                               mScope: kAudioObjectPropertyScopeGlobal,
                                               mElement: kAudioObjectPropertyElementMain)
            guard AudioObjectGetPropertyData(id, &a, 0, nil, &sz, &running) == noErr, running != 0 else { continue }
            var pid: pid_t = 0
            sz = UInt32(MemoryLayout<pid_t>.size)
            a.mSelector = kAudioProcessPropertyPID
            if AudioObjectGetPropertyData(id, &a, 0, nil, &sz, &pid) == noErr, pid == me { continue }
            var bundle: Unmanaged<CFString>?
            sz = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
            a.mSelector = kAudioProcessPropertyBundleID
            if AudioObjectGetPropertyData(id, &a, 0, nil, &sz, &bundle) == noErr, let b = bundle?.takeRetainedValue() {
                out.append((b as String).lowercased())
            } else if let app = NSRunningApplication(processIdentifier: pid), let b = app.bundleIdentifier {
                out.append(b.lowercased())
            }
        }
        return out
    }

    /// Bundle ID программ, которые сейчас пишут звук с микрофона (кроме самого Meeting Agent).
    static func micUsers() -> [String]? {
        guard #available(macOS 14.2, *) else { return nil }
        return audioUsers(selector: kAudioProcessPropertyIsRunningInput)
    }

    /// Bundle ID программ, которые сейчас выводят звук (помогает удержать запись при переходе в Breakout Room).
    static func outputUsers() -> [String]? {
        guard #available(macOS 14.2, *) else { return nil }
        return audioUsers(selector: kAudioProcessPropertyIsRunningOutput)
    }

    /// Запущен ли Zoom (учитывается при переходе в сессионный зал / Breakout Room).
    static func isZoomRunning(bundleId: String = "us.zoom.xos") -> Bool {
        !NSRunningApplication.runningApplications(withBundleIdentifier: bundleId).isEmpty
    }

    /// Микрофон где-то работает (запасной признак для macOS 13–14.1).
    static func micRunningSomewhere() -> Bool {
        var device = AudioObjectID(0)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultInputDevice,
                                              mScope: kAudioObjectPropertyScopeGlobal,
                                              mElement: kAudioObjectPropertyElementMain)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &device) == noErr
        else { return false }
        var running: UInt32 = 0
        size = UInt32(MemoryLayout<UInt32>.size)
        addr.mSelector = kAudioDevicePropertyDeviceIsRunningSomewhere
        return AudioObjectGetPropertyData(device, &addr, 0, nil, &size, &running) == noErr && running != 0
    }

    static func uses(_ users: [String], _ prefixes: [String]) -> Bool {
        users.contains { u in prefixes.contains { u.hasPrefix($0) } }
    }

    /// (владелец, заголовок) всех окон в нижнем регистре.
    static func windows() -> [(owner: String, name: String)] {
        guard let list = CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as? [[String: Any]] else { return [] }
        return list.map { w in
            ((w[kCGWindowOwnerName as String] as? String ?? "").lowercased(),
             (w[kCGWindowName as String] as? String ?? "").lowercased())
        }
    }

    static func browserCallWindow(_ wins: [(owner: String, name: String)]) -> Bool {
        wins.contains { w in
            browserOwners.contains { w.owner.contains($0) } && webCallTitles.contains { w.name.contains($0) }
        }
    }

    static func appWindow(_ wins: [(owner: String, name: String)], _ owners: [String]) -> Bool {
        wins.contains { w in owners.contains { w.owner.contains($0) } }
    }
}
