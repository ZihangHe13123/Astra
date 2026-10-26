import EventKit
import Foundation
import Testing

@testable import AstraMacComputerHelperCore

@Suite struct CalendarAccessTests {
    private func command(_ json: String) throws -> CalendarCommand {
        try parseCalendarCommand(Data(json.utf8))
    }

    private func failure(_ json: String) -> String? {
        do {
            _ = try command(json)
            return nil
        } catch let error as CalendarFailure {
            return error.code
        } catch {
            return "unexpected"
        }
    }

    @Test func calendarModesAreExact() {
        #expect(AppshotHelperMode(arguments: ["--calendar"]) == .calendar)
        #expect(AppshotHelperMode(arguments: ["--calendar-child"]) == .calendarChild)
        #expect(AppshotHelperMode(arguments: ["--calendar", "events"]) == .unsupported)
    }

    @Test func requestsReadTimesNamesAndWhetherToAsk() throws {
        let events = try command(#"{"op":"events","start":1800000000,"end":1800086400,"calendars":["Work"],"may_ask":true}"#)
        #expect(events == CalendarCommand(request: .events(
            start: Date(timeIntervalSince1970: 1_800_000_000), end: Date(timeIntervalSince1970: 1_800_086_400),
            calendars: ["Work"], limit: 100), mayAsk: true))
        #expect(try command(#"{"op":"reminders","include_completed":true,"limit":5}"#)
            == CalendarCommand(request: .reminders(lists: [], includeCompleted: true, limit: 5), mayAsk: false))
        let meeting = try command(#"{"op":"add_event","title":" Lab meeting ","start":1800000000}"#)
        guard case let .addEvent(event) = meeting.request else { Issue.record("not add_event"); return }
        #expect(event.title == "Lab meeting" && event.end.timeIntervalSince(event.start) == 3_600 && !event.allDay)
    }

    @Test func allDayEventsAndDatedRemindersUseWholeDays() throws {
        let trip = try command(#"{"op":"add_event","title":"Trip","all_day":true,"date":"2026-12-24","end_date":"2026-12-26"}"#)
        guard case let .addEvent(event) = trip.request else { Issue.record("not add_event"); return }
        #expect(event.allDay && event.start == Calendar.current.date(from: DateComponents(year: 2026, month: 12, day: 24)))
        #expect(event.end == Calendar.current.date(from: DateComponents(year: 2026, month: 12, day: 26)))
        let due = try command(#"{"op":"add_reminder","title":"Pay rent","due_date":"2026-11-01"}"#)
        #expect(due.request == .addReminder(NewReminder(
            title: "Pay rent", due: nil, dueDate: DateComponents(year: 2026, month: 11, day: 1), notes: "", list: "")))
    }

    @Test func malformedRequestsAreRefusedBeforeEventKit() {
        for json in [
            "[]", #"{"op":"delete_event"}"#, #"{"op":"events","start":1800000000}"#,
            #"{"op":"events","start":1800000000,"end":1700000000}"#,
            #"{"op":"events","start":1700000000,"end":1800000000}"#,
            #"{"op":"events","start":true,"end":1800000000}"#,
            #"{"op":"add_event","title":"  ","start":1800000000}"#,
            #"{"op":"add_event","title":"x","all_day":true,"date":"2026-02-30"}"#,
            #"{"op":"add_reminder","title":"x","due":1800000000,"due_date":"2026-11-01"}"#,
            #"{"op":"reminders","limit":0}"#, #"{"op":"status","may_ask":"yes"}"#,
            #"{"op":"complete_reminder"}"#, #"{"op":"complete_reminder","id":"x"}"#,
        ] {
            #expect(failure(json) == "invalid_request", "\(json)")
        }
        #expect(failure(String(repeating: " ", count: 70_000)) == "invalid_request")
    }

    @Test func itemsCarryWhatTheToolShows() {
        let store = EKEventStore()
        let event = EKEvent(eventStore: store)
        event.title = "Holiday"
        event.isAllDay = true
        event.startDate = Calendar.current.date(from: DateComponents(year: 2026, month: 12, day: 25))
        event.endDate = event.startDate
        event.notes = String(repeating: "n", count: 1_500)
        let item = calendarEventItem(event)
        #expect(item["date"] as? String == "2026-12-25" && item["end_date"] as? String == "2026-12-25")
        #expect((item["notes"] as? String)?.hasSuffix("…(cut)") == true)

        let open = ["title": "b", "completed": false, "due": 1_800_000_000.0] as [String: Any]
        let dated = ["title": "a", "completed": false, "due_date": "2027-01-01"] as [String: Any]
        let undated = ["title": "c", "completed": false] as [String: Any]
        let done = ["title": "d", "completed": true, "completed_at": 1_700_000_000.0] as [String: Any]
        let sorted = [done, undated, dated, open].sorted(by: reminderOrder).map { $0["title"] as? String }
        #expect(sorted == ["a", "b", "c", "d"])
    }

    @Test func answersAreJSONObjectsWithAnOutcome() {
        let refused = calendarAnswer(Data(#"{"op":"nope"}"#.utf8))
        #expect(refused["ok"] as? Bool == false && refused["error"] as? String == "invalid_request")
        let status = calendarAnswer(Data(#"{"op":"status"}"#.utf8))
        #expect(status["ok"] as? Bool == true && status["events"] is String && status["reminders"] is String)
    }
}
