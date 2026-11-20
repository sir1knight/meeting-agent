// Участники из календаря macOS (EventKit): при начале записи ищем событие, которое идёт сейчас,
// и отдаём серверу его название и участников. Календари Exchange/Outlook, Google и iCloud видны,
// если аккаунт добавлен в «Календарь» (Системные настройки → Интернет-аккаунты).

import EventKit
import Foundation

final class CalendarLookup {
    private let store = EKEventStore()

    var authorized: Bool {
        let st = EKEventStore.authorizationStatus(for: .event)
        if #available(macOS 14.0, *) { return st == .fullAccess }
        return st == .authorized
    }

    /// Запрос доступа (показывает системный диалог один раз). true — доступ есть.
    func requestAccess(_ done: @escaping (Bool) -> Void) {
        if authorized { done(true); return }
        let finish: (Bool, Error?) -> Void = { ok, _ in DispatchQueue.main.async { done(ok) } }
        if #available(macOS 14.0, *) {
            store.requestFullAccessToEvents(completion: finish)
        } else {
            store.requestAccess(to: .event, completion: finish)
        }
    }

    /// Событие, которое идёт сейчас (или начнётся в ближайшие 10 минут): название и участники.
    func currentEvent(at now: Date = Date()) -> (title: String, attendees: [[String: String]])? {
        guard authorized else { return nil }
        let pred = store.predicateForEvents(withStart: now.addingTimeInterval(-6 * 3600),
                                            end: now.addingTimeInterval(600), calendars: nil)
        let events = store.events(matching: pred).filter { e in
            !e.isAllDay && e.startDate <= now.addingTimeInterval(600) && e.endDate >= now
                && e.status != .canceled && (e.endDate.timeIntervalSince(e.startDate) <= 8 * 3600)
        }
        // Встреча с участниками важнее личного напоминания; при равенстве — та, что началась ближе к «сейчас».
        let best = events.max { a, b in
            let pa = a.attendees?.isEmpty == false ? 1 : 0, pb = b.attendees?.isEmpty == false ? 1 : 0
            if pa != pb { return pa < pb }
            return abs(a.startDate.timeIntervalSince(now)) > abs(b.startDate.timeIntervalSince(now))
        }
        guard let e = best else { return nil }
        var people: [[String: String]] = []
        for p in e.attendees ?? [] where !p.isCurrentUser && p.participantType != .room && p.participantType != .resource {
            let email = p.url.absoluteString.replacingOccurrences(of: "mailto:", with: "")
            var name = p.name ?? ""
            if name.contains("@") { name = "" }
            people.append(["name": name, "email": email.contains("@") ? email : ""])
        }
        return (e.title ?? "", people)
    }
}
