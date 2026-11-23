import ApplicationServices
import Foundation
import Testing
@testable import AstraMacComputerHelperCore

private let unnamed = BoundedAXStringResult(value: nil, status: .complete)
private func complete(_ value: String) -> BoundedAXStringResult {
    BoundedAXStringResult(value: value, status: .complete)
}

@Test func descriptionOnlySheetMatchesCatalogAndForegroundFocusWithoutLosingIdentity() {
    let element = AXUIElementCreateApplication(42)
    let bounds = CGRect(x: 236, y: 190, width: 880, height: 448)
    let name = accessibilityWindowName(element) { _, attribute in
        switch attribute {
        case kAXTitleAttribute: return unnamed
        case kAXRoleAttribute: return complete("AXSheet")
        case kAXDescriptionAttribute: return complete("Open fixture")
        default: Issue.record("unexpected attribute"); return unnamed
        }
    }
    let target = WindowTarget(appRef: "app", windowRef: "panel", pid: 42,
        windowID: 99, bounds: bounds, title: "Open fixture")
    let record = CatalogAXWindowRecord(element: element, bounds: bounds, title: name)
    let matched = matchingCatalogAXWindow(target: target, records: [record])
    #expect(matched.map { CFEqual($0, element) } == true)
    var mismatch: String?
    #expect(matchingCatalogAXWindow(target: target, records: [record, record], onMismatch: { mismatch = $0 }) == nil)
    #expect(mismatch == "multiple_ax_matches")
    let unnamedRecord = CatalogAXWindowRecord(element: element, bounds: bounds, title: complete(""))
    #expect(matchingCatalogAXWindow(target: target, records: [unnamedRecord], onMismatch: { mismatch = $0 }) == nil)
    #expect(mismatch == "ax_identity_metadata_mismatch")
    let independent = CatalogAXWindowRecord(element: element, bounds: bounds, title: complete(""), windowID: 99)
    #expect(matchingCatalogAXWindow(target: target, records: [independent]).map { CFEqual($0, element) } == true)
    let factory = ForegroundPIDActionStateFactory(
        focusedAXWindow: { PIDAXFocusedWindowObservation(pid: 42, bounds: bounds,
            axIdentity: CFHash(element), title: name.value) },
        screenCaptureWindows: { [PIDScreenCaptureWindowObservation(pid: 42,
            windowID: 99, bounds: bounds, title: "Open fixture", isOnScreen: true)] })
    let exact = ActionTargetState(pid: 42, windowID: 99, bounds: bounds,
        axIdentity: CFHash(element), focusedAXIdentity: CFHash(element), focusedAXBounds: bounds)
    #expect(factory.make(target: exact, snapshotID: "snap", frontmostPID: 42).isKeyWindow)
    let replaced = ActionTargetState(pid: 42, windowID: 100, bounds: bounds,
        axIdentity: CFHash(element), focusedAXIdentity: CFHash(element), focusedAXBounds: bounds)
    #expect(!factory.make(target: replaced, snapshotID: "snap", frontmostPID: 42).isKeyWindow)
}

@Test func windowNameNeverReplacesPresentFailedOrTruncatedTitle() {
    for title in [complete("Authoritative title"),
                  BoundedAXStringResult(value: nil, status: .failed),
                  BoundedAXStringResult(value: "Partial", status: .truncated)] {
        var calls: [String] = []
        let result = accessibilityWindowName(AXUIElementCreateApplication(42)) { _, attribute in
            calls.append(attribute)
            return title
        }
        #expect(result.value == title.value)
        #expect(result.status == title.status)
        #expect(calls == [kAXTitleAttribute])
    }
}

@Test func unnamedSheetFocusRequiresProvenOwnershipAndSoleExactCGIdentity() {
    let bounds = CGRect(x: 446, y: 306, width: 460, height: 215)
    let target = ActionTargetState(pid: 42, windowID: 99, bounds: bounds,
        axIdentity: 123, focusedAXIdentity: 123, focusedAXBounds: bounds)
    let screen = PIDScreenCaptureWindowObservation(pid: 42, windowID: 99,
        bounds: bounds, title: "", isOnScreen: true)
    func accepts(proven: Bool = true, title: String? = "", identity: CFHashCode = 123,
                 screens: [PIDScreenCaptureWindowObservation] = [screen]) -> Bool {
        ForegroundPIDActionStateFactory(focusedAXWindow: {
            PIDAXFocusedWindowObservation(pid: 42, bounds: bounds, axIdentity: identity,
                title: title, isProvenSheet: proven)
        }, screenCaptureWindows: { screens }).make(target: target, snapshotID: "snap", frontmostPID: 42).isKeyWindow
    }
    #expect(accepts())
    #expect(!accepts(proven: false))
    #expect(!accepts(title: nil))
    #expect(!accepts(identity: 456))
    #expect(!accepts(screens: [screen, screen]))
    #expect(!accepts(screens: [PIDScreenCaptureWindowObservation(pid: 42, windowID: 100,
        bounds: bounds, title: "", isOnScreen: true)]))
    #expect(!accepts(screens: [PIDScreenCaptureWindowObservation(pid: 42, windowID: 99,
        bounds: bounds, title: nil, isOnScreen: true)]))
}

@Test func onlyProvenSheetMayReadDescriptionAsWindowName() {
    for role in [complete("AXWindow"), complete("AXGroup"), unnamed,
                 BoundedAXStringResult(value: "AXSheet", status: .failed),
                 BoundedAXStringResult(value: "AXSheet", status: .truncated)] {
        var descriptionRead = false
        let result = accessibilityWindowName(AXUIElementCreateApplication(42)) { _, attribute in
            if attribute == kAXTitleAttribute { return unnamed }
            if attribute == kAXRoleAttribute { return role }
            descriptionRead = true
            return complete("Must not become a window title")
        }
        #expect(result.value == nil)
        #expect(!descriptionRead)
    }
}

@Test func incompleteSheetDescriptionCannotMatchEvenAnUntitledScreenWindow() {
    let element = AXUIElementCreateApplication(42)
    let bounds = CGRect(x: 0, y: 0, width: 300, height: 200)
    for description in [BoundedAXStringResult(value: nil, status: .failed),
                        BoundedAXStringResult(value: "Open", status: .truncated)] {
        let name = accessibilityWindowName(element) { _, attribute in
            if attribute == kAXTitleAttribute { return complete("") }
            if attribute == kAXRoleAttribute { return complete("AXSheet") }
            return description
        }
        let record = CatalogAXWindowRecord(element: element, bounds: bounds, title: name)
        let target = WindowTarget(appRef: "app", windowRef: "window", pid: 42,
            windowID: 1, bounds: bounds, title: "")
        #expect(matchingCatalogAXWindow(target: target, records: [record]) == nil)
    }
}

@Test func presentButUnmappedWindowIsNotReportedAsGone() throws {
    let bounds = CGRect(x: 0, y: 0, width: 300, height: 200)
    let element = AXUIElementCreateApplication(42)
    for candidates in [[], [TargetAXWindowRecord(windowID: nil, bounds: bounds,
        identity: CFHash(element), element: element)]] {
        let record = TargetCatalogRecord(appRef: "app", windowRef: "window", pid: 42,
            windowID: 1, bounds: bounds, title: "Present", axWindows: candidates)
        let catalog = ClosureTargetCatalog(recordProvider: { _, _ in record }, currentProvider: { _ in record })
        do {
            _ = try BackgroundTargetController(catalog: catalog).select(appRef: "app", windowRef: "window")
            Issue.record("unmatched window was accepted")
        } catch WindowObservationError.axWindowUnmatched {
            // Present CG identity, no matching AX identity.
        }
    }
    let absent = ClosureTargetCatalog(recordProvider: { _, _ in nil }, currentProvider: { _ in nil })
    do {
        _ = try BackgroundTargetController(catalog: absent).select(appRef: "app", windowRef: "window")
        Issue.record("missing window was accepted")
    } catch WindowObservationError.targetGone {
        // A missing catalog target remains a separate condition.
    }
}

@Test func catalogRelationsAreReturnedOnlyToClientsThatRequestThem() throws {
    let catalog = JSONValue.object(["apps": .array([.object(["windows": .array([
        .object(["window_ref": .string("sheet"), "parent_window_identity_ref": .string("parent")]),
        .object(["window_ref": .string("unbound"), "binding_reason": .string("no_ax_candidate")]),
        .object(["window_ref": .string("find"), "focused_dialog_document_identity_ref": .string("document")])
    ])])])])
    func records(_ payload: JSONValue) throws -> [[String: String]] {
        let data = try JSONEncoder().encode(catalogForClient(catalog, payload: payload))
        let object = try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
        let apps = try #require(object["apps"] as? [[String: Any]])
        return try #require(apps.first?["windows"] as? [[String: String]])
    }
    let legacy = try records(.object([:]))
    #expect(legacy == [["window_ref": "sheet"], ["window_ref": "unbound"], ["window_ref": "find"]])
    let current = try records(.object(["window_catalog_relations": .bool(true)]))
    #expect(current[0]["parent_window_identity_ref"] == "parent")
    #expect(current[1]["binding_reason"] == "no_ax_candidate")
    #expect(current[2]["focused_dialog_document_identity_ref"] == nil)
    let extended = try records(.object(["window_catalog_relations": .bool(true), "focused_dialog_relations": .bool(true)]))
    #expect(extended[2]["focused_dialog_document_identity_ref"] == "document")
}

@Test func focusedDialogRelationRequiresStableMainAndFocusAndTwoBoundWindows() throws {
    let doc = AXUIElementCreateApplication(100), panel = AXUIElementCreateApplication(200)
    let other = AXUIElementCreateApplication(300)
    func observe(focused: [AXUIElement], main: [AXUIElement], omitPanel: Bool = false,
                 panelSubrole: String = "AXDialog") -> (dialog: String, document: String)? {
        var focusReads = focused, mainReads = main
        return focusedDialogDocumentRelation(pid: 100, catalogIdentity: { element in
            if CFEqual(element, doc) { return "document" }
            return CFEqual(element, panel) && !omitPanel ? "panel" : nil
        }, appWindow: { _, attribute in
            if attribute == kAXFocusedWindowAttribute {
                return focusReads.isEmpty ? nil : focusReads.removeFirst()
            }
            return mainReads.isEmpty ? nil : mainReads.removeFirst()
        }, windowSubrole: { CFEqual($0, panel) ? panelSubrole : "AXStandardWindow" })
    }
    let relation = try #require(observe(focused: [panel, panel], main: [doc, doc]))
    #expect(relation.dialog == "panel" && relation.document == "document")
    #expect(observe(focused: [panel, panel], main: [doc, other]) == nil)
    #expect(observe(focused: [panel, other], main: [doc, doc]) == nil)
    #expect(observe(focused: [panel, panel], main: [panel, panel]) == nil)
    #expect(observe(focused: [panel, panel], main: [doc, doc], omitPanel: true) == nil)
    #expect(observe(focused: [panel, panel], main: [doc, doc], panelSubrole: "AXFloatingWindow") == nil)
}
