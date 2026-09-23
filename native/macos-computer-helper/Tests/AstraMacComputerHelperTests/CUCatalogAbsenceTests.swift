import ApplicationServices
import Foundation
import Testing
@testable import AstraMacComputerHelperCore

@Test func cuCatalogAbsenceRequiresFullWindowServerInventoryNotVisibleCatalog() {
    var tracker = CatalogWindowAbsenceTracker()
    tracker.record(reference: "known-target", windowID: 10)
    tracker.record(reference: "known-witness", windowID: 11)
    // A minimized/truncated UI target is still present in WindowServer.
    #expect(tracker.confirmedAbsent(windowIDs: [10, 11]).isEmpty)
    #expect(tracker.confirmedAbsent(windowIDs: [11]) == ["known-target"])
    #expect(tracker.confirmedAbsent(windowIDs: nil).isEmpty)
    #expect(tracker.confirmedAbsent(windowIDs: []).isEmpty)
}

@Test func cuCatalogAbsenceRetainsKnownIdentityAcrossRepeatedCatalogRefreshes() {
    var tracker = CatalogWindowAbsenceTracker()
    tracker.record(reference: "known-menu", windowID: 10)
    tracker.record(reference: "known-parent", windowID: 11)
    #expect(tracker.confirmedAbsent(windowIDs: [11]) == ["known-menu"])
    tracker.record(reference: "known-parent", windowID: 11)
    #expect(tracker.confirmedAbsent(windowIDs: [11]) == ["known-menu"])
    // A reused WindowServer ID is not positive absence evidence.
    #expect(tracker.confirmedAbsent(windowIDs: [10, 11]).isEmpty)
    #expect(CatalogWindowAbsenceTracker().confirmedAbsent(windowIDs: []).isEmpty)
}

@Test func cuCatalogAbsenceHistoryIsBoundedAndConflictingIdentityFailsClosed() {
    var tracker = CatalogWindowAbsenceTracker(capacity: 2)
    tracker.record(reference: "old", windowID: 10)
    tracker.record(reference: "b", windowID: 20)
    tracker.record(reference: "c", windowID: 30)
    #expect(tracker.confirmedAbsent(windowIDs: [30]) == ["b"])
    tracker.record(reference: "b", windowID: 99)
    #expect(tracker.confirmedAbsent(windowIDs: []).isEmpty)
}

@Test func cuCatalogAbsenceRejectsIncompleteOrMalformedWindowServerRows() {
    #expect(CatalogWindowAbsenceTracker.validatedInventory(nil) == nil)
    #expect(CatalogWindowAbsenceTracker.validatedInventory([[:]]) == nil)
    #expect(CatalogWindowAbsenceTracker.validatedInventory([[kCGWindowNumber as String: true]]) == nil)
    #expect(CatalogWindowAbsenceTracker.validatedInventory([[kCGWindowNumber as String: 1.5]]) == nil)
    #expect(CatalogWindowAbsenceTracker.validatedInventory([[kCGWindowNumber as String: 0]]) == nil)
    #expect(CatalogWindowAbsenceTracker.validatedInventory([
        [kCGWindowNumber as String: 10], [kCGWindowNumber as String: 10],
    ]) == nil)
    #expect(CatalogWindowAbsenceTracker.validatedInventory([]) == nil)
    #expect(CatalogWindowAbsenceTracker.validatedInventory([
        [kCGWindowNumber as String: 10, kCGWindowIsOnscreen as String: false],
        [kCGWindowNumber as String: 11, kCGWindowIsOnscreen as String: true],
    ]) == Set<CGWindowID>([10, 11]))
}

// Live WPS 2026-09-23: a handoff was bound to a context menu. The user closed it, but Qt keeps one
// menu window and orders it out, so it never left the inventory and resume could not release.
@Test func cuCatalogAbsenceTreatsAnOrderedOutPopupAsClosed() {
    let rows: [[String: Any]] = [
        [kCGWindowNumber as String: 10023, kCGWindowLayer as String: 8, kCGWindowIsOnscreen as String: false],
        [kCGWindowNumber as String: 20, kCGWindowLayer as String: 101],
        [kCGWindowNumber as String: 11, kCGWindowLayer as String: 0, kCGWindowIsOnscreen as String: false],
        [kCGWindowNumber as String: 12, kCGWindowLayer as String: 8, kCGWindowIsOnscreen as String: true],
        [kCGWindowNumber as String: 13, kCGWindowLayer as String: true, kCGWindowIsOnscreen as String: false],
    ]
    // Minimized standard windows and anything with an unreadable layer stay present.
    let present = CatalogWindowAbsenceTracker.validatedInventory(rows)
    #expect(present == Set<CGWindowID>([11, 12, 13]))
    var tracker = CatalogWindowAbsenceTracker()
    tracker.record(reference: "identity-menu", windowID: 10023)
    tracker.record(reference: "identity-document", windowID: 11)
    #expect(tracker.confirmedAbsent(windowIDs: present) == ["identity-menu"])
    // The same menu window ordered back in is present again.
    let reopened = CatalogWindowAbsenceTracker.validatedInventory([
        [kCGWindowNumber as String: 10023, kCGWindowLayer as String: 8, kCGWindowIsOnscreen as String: true],
        [kCGWindowNumber as String: 11, kCGWindowLayer as String: 0],
    ])
    #expect(tracker.confirmedAbsent(windowIDs: reopened).isEmpty)
    // A duplicate hidden row is still malformed.
    #expect(CatalogWindowAbsenceTracker.validatedInventory([
        [kCGWindowNumber as String: 10023, kCGWindowLayer as String: 8],
        [kCGWindowNumber as String: 10023, kCGWindowLayer as String: 8],
    ]) == nil)
}

@Test func cuCatalogAbsenceProducerDistinguishesHiddenAndTrulyClosedTargets() throws {
    var visibleTarget = true
    var inventory: Set<CGWindowID>? = [10, 11]
    let observer = SystemWindowObserver(
        permissions: CUAbsencePermissions(),
        catalogBuilder: {
            let ids: [UInt32] = visibleTarget ? [10, 11] : [11]
            var targets: [String: WindowTarget] = [:]
            var windows: [JSONValue] = []
            for id in ids {
                let reference = "window-\(id)"
                targets[reference] = WindowTarget(
                    appRef: "app-proof", windowRef: reference, pid: 1234,
                    windowID: id, bounds: CGRect(x: 0, y: 0, width: 200, height: 100), title: "test"
                )
                windows.append(.object([
                    "window_ref": .string(reference), "bindable": .bool(true),
                    "window_identity_ref": .string("identity-\(id)"),
                ]))
            }
            return WindowCatalogObservation(targets: targets, apps: [.object([
                "app_ref": .string("app-proof"), "windows": .array(windows),
            ])])
        },
        windowServerInventory: { inventory }
    )
    func proof(_ value: JSONValue) -> [JSONValue] {
        guard case let .object(result) = value,
              case let .array(refs)? = result["confirmed_absent_window_identity_refs"] else { return [] }
        return refs
    }
    #expect(proof(try observer.apps()).isEmpty)
    visibleTarget = false
    #expect(proof(try observer.apps()).isEmpty) // minimized, not gone
    inventory = [11]
    #expect(proof(try observer.apps()) == [.string("identity-10")])
    #expect(proof(try observer.apps()) == [.string("identity-10")]) // resume's extra refresh
    inventory = nil
    #expect(proof(try observer.apps()).isEmpty)
}

private struct CUAbsencePermissions: PermissionStatusProviding {
    let accessibilityTrusted = true
    let screenRecordingAllowed = true
}
