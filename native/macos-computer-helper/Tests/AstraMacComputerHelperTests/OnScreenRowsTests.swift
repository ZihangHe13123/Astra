@testable import AstraMacComputerHelperCore
@preconcurrency import ApplicationServices
import CoreGraphics
import Foundation
import Testing

// Live Activity Monitor (2026-09-23): the process outline reported 584 rows with 19 on screen;
// reading them all took 69 s, so the 3 s content budget expired before the toolbar search field
// and the field had no reference. Only on-screen rows are read, and the toolbar is read first.
@Test func tableChildrenKeepOnlyOnScreenRowsInOrder() {
    let rows = Array(1...6)
    let children = [-1] + rows + [-2]  // header before the rows, a column after them
    let kept = onScreenChildren(children, rows: rows, visibleRows: [3, 4], hash: { $0 }, same: { $0 == $1 })
    #expect(kept == [-1, 3, 4, -2])
    // Colliding hashes still compare exactly.
    #expect(onScreenChildren(children, rows: rows, visibleRows: [3, 4], hash: { _ in 0 }, same: { $0 == $1 })
        == [-1, 3, 4, -2])
    // Nothing is filtered without a proper, consistent subset of visible rows.
    #expect(onScreenChildren(children, rows: rows, visibleRows: [], hash: { $0 }, same: { $0 == $1 }) == nil)
    #expect(onScreenChildren(children, rows: rows, visibleRows: rows, hash: { $0 }, same: { $0 == $1 }) == nil)
    #expect(onScreenChildren(children, rows: rows, visibleRows: [3, 9], hash: { $0 }, same: { $0 == $1 }) == nil)
}

@Test func windowToolbarIsReadBeforeContent() {
    let children = ["content", "status", "toolbar", "close"]
    #expect(prioritizeWindowToolbar(children, isToolbar: { $0 == "toolbar" }) == ["toolbar", "content", "status", "close"])
    #expect(prioritizeWindowToolbar(["content"], isToolbar: { $0 == "toolbar" }) == ["content"])
}

private final class RowsProvider: AXNodeAttributeProvider {
    let role: String
    let kids: [RowsProvider]
    let omitted: Bool
    init(_ role: String, _ kids: [RowsProvider] = [], omitted: Bool = false) {
        self.role = role; self.kids = kids; self.omitted = omitted
    }
    func stringValue(for attribute: String) -> BoundedAXStringResult {
        BoundedAXStringResult(value: attribute == kAXRoleAttribute ? role : nil, status: .complete)
    }
    func boolValue(for attribute: String) -> Bool? { nil }
    func bounds() -> CGRect { CGRect(x: 0, y: 0, width: 10, height: 10) }
    func actions() -> [BoundedAXStringResult] { [] }
    func children(remaining: Int) -> [any AXNodeAttributeProvider] { kids }
    func sourceElement() -> AXUIElement? { nil }
    var omittedOffscreenChildren: Bool { omitted }
}

@Test func readerMarksATableWithOffscreenRowsAsTruncated() {
    let table = RowsProvider("AXOutline", [RowsProvider("AXRow"), RowsProvider("AXRow")], omitted: true)
    let full = RowsProvider("AXOutline", [RowsProvider("AXRow")])
    let node = AXNodeReader.read(provider: RowsProvider("AXWindow", [table, full]), windowBounds: .zero)
    #expect(node.children.count == 2)
    #expect(node.children[0].childrenTruncated && node.children[0].children.count == 2)
    #expect(!node.children[1].childrenTruncated)
    #expect(!node.childrenTruncated)
}

// Live Outlook (2026-09-23): one window-list read failed as the app changed its windows, and the
// action was refused as a stale snapshot. The read is retried briefly and still fails closed.
@Test func transientReadIsRetriedBrieflyThenFailsClosed() {
    var calls = 0, pauses = 0
    let second: Int? = retryingTransientRead(pause: { pauses += 1 }) { calls += 1; return calls == 2 ? 7 : nil }
    #expect(second == 7 && calls == 2 && pauses == 1)
    calls = 0; pauses = 0
    let never: Int? = retryingTransientRead(pause: { pauses += 1 }) { calls += 1; return nil }
    #expect(never == nil && calls == 3 && pauses == 2)
    calls = 0; pauses = 0
    let first: Int? = retryingTransientRead(pause: { pauses += 1 }) { calls += 1; return 1 }
    #expect(first == 1 && calls == 1 && pauses == 0)
}

