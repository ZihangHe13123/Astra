import Darwin
import EventKit
import Foundation

// The person's calendars and reminders through EventKit: one JSON request on standard input, one JSON
// answer on standard output. `--calendar` starts itself again as `--calendar-child` with responsibility
// disclaimed, so macOS asks for, and lists, Calendars and Reminders access under this helper's own name
// and usage text rather than under whichever app started Astra.

let calendarRequestLimitBytes = 64 * 1024
let calendarNotesLimit = 1_000
let calendarItemsLimit = 500
let calendarAccessWaitSeconds = 120.0

public enum CalendarKind: String, Equatable {
    case events, reminders

    var entity: EKEntityType { self == .events ? .event : .reminder }
}

struct CalendarFailure: Error, Equatable {
    let code: String
    let message: String
}

struct NewCalendarEvent: Equatable {
    let title: String
    let start: Date
    /// For an all-day event, the start of its last day.
    let end: Date
    let allDay: Bool
    let location: String
    let notes: String
    let calendar: String
}

struct NewReminder: Equatable {
    let title: String
    let due: Date?
    let dueDate: DateComponents?
    let notes: String
    let list: String
}

enum CalendarRequest: Equatable {
    case status
    case lists(CalendarKind)
    case events(start: Date, end: Date, calendars: [String], limit: Int)
    case reminders(lists: [String], includeCompleted: Bool, limit: Int)
    case addEvent(NewCalendarEvent)
    case addReminder(NewReminder)
    /// The title is the one the person approved; a reminder with another title is left alone.
    case completeReminder(id: String, title: String)
}

struct CalendarCommand: Equatable {
    let request: CalendarRequest
    /// Whether an undecided permission may be asked now. Background jobs never ask.
    let mayAsk: Bool
}

private func invalid(_ message: String) -> CalendarFailure {
    CalendarFailure(code: "invalid_request", message: message)
}

private func text(_ object: [String: Any], _ key: String, limit: Int = 2_000) throws -> String {
    guard let value = object[key] else { return "" }
    guard let string = value as? String else { throw invalid("\(key) must be text") }
    guard string.count <= limit else { throw invalid("\(key) is longer than \(limit) characters") }
    return string
}

private func names(_ object: [String: Any], _ key: String) throws -> [String] {
    guard let value = object[key] else { return [] }
    guard let list = value as? [String], list.count <= 50 else { throw invalid("\(key) must be a list of names") }
    return list
}

private func flag(_ object: [String: Any], _ key: String) throws -> Bool {
    guard let value = object[key] else { return false }
    guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else {
        throw invalid("\(key) must be true or false")
    }
    return number.boolValue
}

private func instant(_ object: [String: Any], _ key: String) throws -> Date? {
    guard let value = object[key] else { return nil }
    guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
          number.doubleValue.isFinite, abs(number.doubleValue) < 1e11 else {
        throw invalid("\(key) must be seconds since 1970")
    }
    return Date(timeIntervalSince1970: number.doubleValue)
}

/// A day with no time ("2026-10-27"), read in the system calendar.
private func day(_ object: [String: Any], _ key: String) throws -> DateComponents? {
    let value = try text(object, key, limit: 10)
    guard !value.isEmpty else { return nil }
    let parts = value.split(separator: "-").compactMap { Int($0) }
    guard parts.count == 3, value.count == 10 else { throw invalid("\(key) must look like 2026-10-27") }
    let components = DateComponents(year: parts[0], month: parts[1], day: parts[2])
    guard let date = Calendar.current.date(from: components),
          Calendar.current.dateComponents([.year, .month, .day], from: date) == components else {
        throw invalid("\(key) is not a date")
    }
    return components
}

private func startOfDay(_ value: String) -> Double? {
    let parts = value.split(separator: "-").compactMap { Int($0) }
    guard parts.count == 3 else { return nil }
    return Calendar.current.date(from: DateComponents(year: parts[0], month: parts[1], day: parts[2]))?
        .timeIntervalSince1970
}

private func limit(_ object: [String: Any]) throws -> Int {
    guard let value = object["limit"] else { return 100 }
    guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
          (1...calendarItemsLimit).contains(number.intValue) else {
        throw invalid("limit must be 1 to \(calendarItemsLimit)")
    }
    return number.intValue
}

func parseCalendarCommand(_ data: Data) throws -> CalendarCommand {
    guard data.count <= calendarRequestLimitBytes else { throw invalid("request exceeds 64 KiB") }
    guard let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
        throw invalid("request must be one JSON object")
    }
    let mayAsk = try flag(object, "may_ask")
    let operation = try text(object, "op", limit: 40)
    let request: CalendarRequest
    switch operation {
    case "status":
        request = .status
    case "lists":
        guard let kind = CalendarKind(rawValue: try text(object, "kind", limit: 20)) else {
            throw invalid("kind must be events or reminders")
        }
        request = .lists(kind)
    case "events":
        guard let start = try instant(object, "start"), let end = try instant(object, "end") else {
            throw invalid("events needs start and end")
        }
        guard start < end, end.timeIntervalSince(start) <= 366 * 86_400 else {
            throw invalid("events needs a start before its end, at most a year apart")
        }
        request = .events(start: start, end: end, calendars: try names(object, "calendars"), limit: try limit(object))
    case "reminders":
        request = .reminders(lists: try names(object, "lists"), includeCompleted: try flag(object, "include_completed"),
                             limit: try limit(object))
    case "add_event":
        let title = try text(object, "title", limit: 500).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty else { throw invalid("add_event needs a title") }
        let allDay = try flag(object, "all_day")
        let start: Date, end: Date
        if allDay {
            guard let first = try day(object, "date") else { throw invalid("an all-day event needs date") }
            let last = try day(object, "end_date") ?? first
            guard let from = Calendar.current.date(from: first), let through = Calendar.current.date(from: last),
                  from <= through else { throw invalid("end_date must not be before date") }
            start = from
            end = through
        } else {
            guard let from = try instant(object, "start") else { throw invalid("add_event needs start") }
            let until = try instant(object, "end") ?? from.addingTimeInterval(3_600)
            guard from < until, until.timeIntervalSince(from) <= 31 * 86_400 else {
                throw invalid("end must be after start, within 31 days")
            }
            start = from
            end = until
        }
        request = .addEvent(NewCalendarEvent(
            title: title, start: start, end: end, allDay: allDay, location: try text(object, "location", limit: 500),
            notes: try text(object, "notes"), calendar: try text(object, "calendar", limit: 200)))
    case "add_reminder":
        let title = try text(object, "title", limit: 500).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty else { throw invalid("add_reminder needs a title") }
        let due = try instant(object, "due")
        let dueDate = try day(object, "due_date")
        guard due == nil || dueDate == nil else { throw invalid("give due or due_date, not both") }
        request = .addReminder(NewReminder(title: title, due: due, dueDate: dueDate, notes: try text(object, "notes"),
                                           list: try text(object, "list", limit: 200)))
    case "complete_reminder":
        let id = try text(object, "id", limit: 300)
        let title = try text(object, "title", limit: 500).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !id.isEmpty, !title.isEmpty else { throw invalid("complete_reminder needs id and title") }
        request = .completeReminder(id: id, title: title)
    default:
        throw invalid("unknown op")
    }
    return CalendarCommand(request: request, mayAsk: mayAsk)
}

func authorizationName(_ status: EKAuthorizationStatus) -> String {
    switch status {
    case .notDetermined: return "not_determined"
    case .restricted: return "restricted"
    case .denied: return "denied"
    case .fullAccess: return "full"
    case .writeOnly: return "write_only"
    @unknown default: return "unknown"
    }
}

private func clipped(_ value: String?) -> String? {
    guard let value, !value.isEmpty else { return nil }
    return value.count > calendarNotesLimit ? String(value.prefix(calendarNotesLimit)) + "…(cut)" : value
}

private func dayText(_ date: Date) -> String {
    let parts = Calendar.current.dateComponents([.year, .month, .day], from: date)
    return String(format: "%04d-%02d-%02d", parts.year ?? 0, parts.month ?? 0, parts.day ?? 0)
}

func calendarEventItem(_ event: EKEvent) -> [String: Any] {
    var item: [String: Any] = [
        "title": event.title ?? "",
        "start": event.startDate.timeIntervalSince1970,
        "end": event.endDate.timeIntervalSince1970,
        "all_day": event.isAllDay,
        "calendar": event.calendar?.title ?? "",
        "recurring": event.hasRecurrenceRules,
    ]
    if event.isAllDay {
        // All-day events are floating days; the dates say which ones, whatever time zone reads them.
        item["date"] = dayText(event.startDate)
        item["end_date"] = dayText(event.endDate.addingTimeInterval(-1))
    }
    if let location = event.location, !location.isEmpty { item["location"] = location }
    if let notes = clipped(event.notes) { item["notes"] = notes }
    if let url = event.url { item["url"] = url.absoluteString }
    if let organizer = event.organizer, !organizer.isCurrentUser, let name = organizer.name { item["organizer"] = name }
    return item
}

func calendarReminderItem(_ reminder: EKReminder) -> [String: Any] {
    var item: [String: Any] = [
        "id": reminder.calendarItemIdentifier,
        "title": reminder.title ?? "",
        "list": reminder.calendar?.title ?? "",
        "completed": reminder.isCompleted,
        "priority": reminder.priority,
    ]
    if let components = reminder.dueDateComponents {
        var calendar = components.calendar ?? Calendar(identifier: .gregorian)
        calendar.timeZone = components.timeZone ?? .current
        if components.hour == nil {
            item["due_date"] = String(format: "%04d-%02d-%02d", components.year ?? 0, components.month ?? 0,
                                      components.day ?? 0)
        } else if let due = calendar.date(from: components) {
            item["due"] = due.timeIntervalSince1970
        }
    }
    if let completed = reminder.completionDate { item["completed_at"] = completed.timeIntervalSince1970 }
    if let notes = clipped(reminder.notes) { item["notes"] = notes }
    return item
}

private final class Waiting<Value>: @unchecked Sendable {
    var value: Value
    let done = DispatchSemaphore(value: 0)

    init(_ value: Value) { self.value = value }
}

final class CalendarAccess {
    private var store = EKEventStore()

    func status(_ kind: CalendarKind) -> EKAuthorizationStatus {
        EKEventStore.authorizationStatus(for: kind.entity)
    }

    private func ask(_ kind: CalendarKind) {
        let waiting = Waiting(false)
        let finish: EKEventStoreRequestAccessCompletionHandler = { granted, _ in
            waiting.value = granted
            waiting.done.signal()
        }
        switch kind {
        case .events: store.requestFullAccessToEvents(completion: finish)
        case .reminders: store.requestFullAccessToReminders(completion: finish)
        }
        _ = waiting.done.wait(timeout: .now() + calendarAccessWaitSeconds)
        // A store made before the answer keeps seeing no calendars.
        store = EKEventStore()
    }

    private func ensure(_ kind: CalendarKind, mayAsk: Bool, reading: Bool) throws {
        if status(kind) == .notDetermined && mayAsk { ask(kind) }
        let current = status(kind)
        if current == .fullAccess || (!reading && current == .writeOnly) { return }
        throw CalendarFailure(code: current == .notDetermined ? "not_determined" : "access_denied",
                              message: "\(kind.rawValue) access is \(authorizationName(current))")
    }

    private func pick(_ kind: CalendarKind, _ wanted: [String]) throws -> [EKCalendar]? {
        guard !wanted.isEmpty else { return nil }
        let all = store.calendars(for: kind.entity)
        let keys = Set(wanted.map { $0.lowercased() })
        let found = all.filter { keys.contains($0.title.lowercased()) }
        guard !found.isEmpty else {
            throw CalendarFailure(code: "not_found", message: "no \(kind == .events ? "calendar" : "list") named "
                                  + "\(wanted.joined(separator: ", ")); there are: "
                                  + all.map(\.title).joined(separator: ", "))
        }
        return found
    }

    private func writable(_ kind: CalendarKind, _ name: String) throws -> EKCalendar? {
        guard !name.isEmpty else {
            return kind == .events ? store.defaultCalendarForNewEvents : store.defaultCalendarForNewReminders()
        }
        guard let calendar = try pick(kind, [name])?.first(where: \.allowsContentModifications) else {
            throw CalendarFailure(code: "not_found", message: "\(name) cannot be changed")
        }
        return calendar
    }

    func run(_ command: CalendarCommand) throws -> [String: Any] {
        switch command.request {
        case .status:
            return ["events": authorizationName(status(.events)), "reminders": authorizationName(status(.reminders))]
        case let .lists(kind):
            try ensure(kind, mayAsk: command.mayAsk, reading: true)
            let defaultID = (kind == .events ? store.defaultCalendarForNewEvents
                             : store.defaultCalendarForNewReminders())?.calendarIdentifier
            return ["items": store.calendars(for: kind.entity).map { calendar in
                ["title": calendar.title, "writable": calendar.allowsContentModifications,
                 "account": calendar.source?.title ?? "", "default": calendar.calendarIdentifier == defaultID]
            }]
        case let .events(start, end, calendars, limit):
            try ensure(.events, mayAsk: command.mayAsk, reading: true)
            let predicate = store.predicateForEvents(withStart: start, end: end, calendars: try pick(.events, calendars))
            let found = store.events(matching: predicate).sorted { left, right in
                (left.startDate, left.title ?? "") < (right.startDate, right.title ?? "")
            }
            return ["items": found.prefix(limit).map(calendarEventItem), "total": found.count]
        case let .reminders(lists, includeCompleted, limit):
            try ensure(.reminders, mayAsk: command.mayAsk, reading: true)
            let calendars = try pick(.reminders, lists)
            let predicate = includeCompleted ? store.predicateForReminders(in: calendars)
                : store.predicateForIncompleteReminders(withDueDateStarting: nil, ending: nil, calendars: calendars)
            let waiting = Waiting<[EKReminder]>([])
            store.fetchReminders(matching: predicate) { reminders in
                waiting.value = reminders ?? []
                waiting.done.signal()
            }
            guard waiting.done.wait(timeout: .now() + 30) == .success else {
                throw CalendarFailure(code: "timeout", message: "reminders did not answer within 30 seconds")
            }
            let items = waiting.value.map(calendarReminderItem).sorted(by: reminderOrder)
            return ["items": Array(items.prefix(limit)), "total": items.count]
        case let .addEvent(new):
            try ensure(.events, mayAsk: command.mayAsk, reading: false)
            let event = EKEvent(eventStore: store)
            event.title = new.title
            // All-day first: EventKit then stretches the end to the close of its day.
            event.isAllDay = new.allDay
            event.startDate = new.start
            event.endDate = new.end
            if !new.location.isEmpty { event.location = new.location }
            if !new.notes.isEmpty { event.notes = new.notes }
            guard let calendar = try writable(.events, new.calendar) else {
                throw CalendarFailure(code: "not_found", message: "there is no calendar for new events")
            }
            event.calendar = calendar
            try save { try store.save(event, span: .thisEvent, commit: true) }
            return ["item": calendarEventItem(event)]
        case let .addReminder(new):
            try ensure(.reminders, mayAsk: command.mayAsk, reading: false)
            let reminder = EKReminder(eventStore: store)
            reminder.title = new.title
            if !new.notes.isEmpty { reminder.notes = new.notes }
            guard let list = try writable(.reminders, new.list) else {
                throw CalendarFailure(code: "not_found", message: "there is no list for new reminders")
            }
            reminder.calendar = list
            if let due = new.due {
                var components = Calendar.current.dateComponents([.year, .month, .day, .hour, .minute], from: due)
                components.timeZone = .current
                reminder.dueDateComponents = components
                // An alarm is what makes Reminders notify at that time, also on the person's phone.
                reminder.addAlarm(EKAlarm(absoluteDate: due))
            } else if let dueDate = new.dueDate {
                reminder.dueDateComponents = dueDate
            }
            try save { try store.save(reminder, commit: true) }
            return ["item": calendarReminderItem(reminder)]
        case let .completeReminder(id, title):
            try ensure(.reminders, mayAsk: command.mayAsk, reading: true)
            guard let reminder = store.calendarItem(withIdentifier: id) as? EKReminder else {
                throw CalendarFailure(code: "not_found", message: "no reminder with that id")
            }
            guard (reminder.title ?? "").trimmingCharacters(in: .whitespacesAndNewlines) == title else {
                throw CalendarFailure(code: "not_found", message: "that reminder is titled \(reminder.title ?? "")")
            }
            reminder.isCompleted = true
            try save { try store.save(reminder, commit: true) }
            return ["item": calendarReminderItem(reminder)]
        }
    }

    private func save(_ write: () throws -> Void) throws {
        do { try write() } catch {
            throw CalendarFailure(code: "save_failed", message: error.localizedDescription)
        }
    }
}

/// Open reminders by due time, undated ones after them; finished ones last, the latest first.
func reminderOrder(_ left: [String: Any], _ right: [String: Any]) -> Bool {
    func key(_ item: [String: Any]) -> (Int, Double, String) {
        let title = item["title"] as? String ?? ""
        if item["completed"] as? Bool == true { return (2, -(item["completed_at"] as? Double ?? 0), title) }
        if let due = item["due"] as? Double { return (0, due, title) }
        if let day = item["due_date"] as? String, let start = startOfDay(day) { return (0, start, title) }
        return (1, 0, title)
    }
    return key(left) < key(right)
}

func calendarAnswer(_ data: Data, access: () -> CalendarAccess = { CalendarAccess() }) -> [String: Any] {
    do {
        var answer = try access().run(try parseCalendarCommand(data))
        answer["ok"] = true
        return answer
    } catch let failure as CalendarFailure {
        return ["ok": false, "error": failure.code, "message": failure.message]
    } catch {
        return ["ok": false, "error": "failed", "message": error.localizedDescription]
    }
}

private typealias DisclaimFunction = @convention(c) (UnsafeMutablePointer<posix_spawnattr_t?>, Bool) -> Int32

/// Runs this executable again as its own responsible process and returns its exit status, or nil when
/// that is not possible here (then the caller answers in this process instead).
private func runDisclaimed(arguments: [String]) -> Int32? {
    guard let symbol = dlsym(UnsafeMutableRawPointer(bitPattern: -2), "responsibility_spawnattrs_setdisclaim"),
          let executable = Bundle.main.executablePath else { return nil }
    let disclaim = unsafeBitCast(symbol, to: DisclaimFunction.self)
    var attributes: posix_spawnattr_t?
    guard posix_spawnattr_init(&attributes) == 0 else { return nil }
    defer { posix_spawnattr_destroy(&attributes) }
    guard disclaim(&attributes, true) == 0 else { return nil }
    let argv = ([executable] + arguments).map { strdup($0) } + [nil]
    defer { argv.forEach { free($0) } }
    var pid = pid_t()
    guard posix_spawn(&pid, executable, nil, &attributes, argv, environ) == 0 else { return nil }
    var status: Int32 = 0
    while waitpid(pid, &status, 0) == -1 {
        if errno != EINTR { return 70 }
    }
    let signal = status & 0x7f
    return signal == 0 ? (status >> 8) & 0xff : 128 + signal
}

public func runCalendarCommand(child: Bool) -> Int32 {
    if !child, let status = runDisclaimed(arguments: ["--calendar-child"]) { return status }
    var input = Data()
    var chunk = [UInt8](repeating: 0, count: 16 * 1024)
    while input.count <= calendarRequestLimitBytes {
        let count = chunk.withUnsafeMutableBytes { Darwin.read(STDIN_FILENO, $0.baseAddress, $0.count) }
        if count > 0 { input.append(contentsOf: chunk[..<count]) } else if count == 0 || errno != EINTR { break }
    }
    // EventKit may answer on the main queue, so the work runs elsewhere while the main queue stays free.
    DispatchQueue.global(qos: .userInitiated).async {
        let answer = calendarAnswer(input)
        let data = (try? JSONSerialization.data(withJSONObject: answer, options: [.sortedKeys]))
            ?? Data(#"{"error":"failed","message":"answer could not be encoded","ok":false}"#.utf8)
        FileHandle.standardOutput.write(data + Data([10]))
        exit(0)
    }
    dispatchMain()
}
