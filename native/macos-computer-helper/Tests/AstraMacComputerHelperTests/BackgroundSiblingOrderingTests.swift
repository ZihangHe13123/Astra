@testable import AstraMacComputerHelperCore
@preconcurrency import ApplicationServices
import CoreGraphics
import Foundation
import Testing

private struct SiblingInventoryFixture {
    let pid: pid_t = 42
    var bounds = CGRect(x: 0, y: 33, width: 1512, height: 859)
    var ax: [TargetAXWindowRecord] = []
    var screen: [PIDScreenCaptureWindowObservation] = []
    var visual: [VisibleWindowRecord] = []

    init() {
        for index in 0..<9 {
            let element = AXUIElementCreateApplication(pid_t(1000 + index))
            let id = CGWindowID(100 + index)
            ax.append(TargetAXWindowRecord(windowID: index < 2 ? id : nil,
                bounds: bounds, identity: CFHash(element), element: element,
                role: BoundedAXStringResult(value: "AXWindow", status: .complete),
                subrole: BoundedAXStringResult(value: "AXStandardWindow", status: .complete),
                zOrder: index < 2 ? index + 1 : nil, layer: index < 2 ? 0 : nil,
                alpha: index < 2 ? 1 : nil, isModal: false))
            screen.append(PIDScreenCaptureWindowObservation(pid: pid, windowID: id,
                bounds: bounds, title: index == 0 ? "Unique target" : (index == 1 ? "Mapped sibling" : "file:///…/checkpoint.html"),
                isOnScreen: true))
            visual.append(VisibleWindowRecord(pid: pid, windowID: id, bounds: bounds,
                layer: 0, alpha: 1, zOrder: index + 1))
        }
    }

    func proof() -> BackgroundSiblingOrderingProof? {
        backgroundSiblingOrderingProof(targetPID: pid, targetWindowID: 100, targetBounds: bounds,
            axWindows: ax, screenWindows: screen, visibleWindows: visual)
    }

    func record(proof: BackgroundSiblingOrderingProof?) -> TargetCatalogRecord {
        TargetCatalogRecord(appRef: "app", windowRef: "window", pid: pid, windowID: 100,
            bounds: bounds, title: "Unique target", axWindows: ax,
            containsUnselectedOverlay: containedVisibleOverlayOrUncertain(targetPID: pid,
                targetWindowID: 100, targetBounds: bounds, records: visual), siblingOrdering: proof)
    }

    mutating func replaceAX(_ index: Int, windowID: CGWindowID? = nil,
                           role: BoundedAXStringResult? = nil, subrole: String = "AXStandardWindow",
                           modal: Bool? = false, element: AXUIElement? = nil, identity: CFHashCode? = nil) {
        let old = ax[index]
        ax[index] = TargetAXWindowRecord(windowID: windowID, bounds: old.bounds,
            identity: identity ?? old.identity, element: element ?? old.element,
            role: role ?? old.role, subrole: BoundedAXStringResult(value: subrole, status: .complete),
            zOrder: old.zOrder, layer: old.layer, alpha: old.alpha, isModal: modal)
    }
}

@Test(arguments: [false, true])
func completeSiblingOrderingAllowsNineStackedWindowsWithoutBindingSiblings(reverse: Bool) throws {
    var fixture = SiblingInventoryFixture()
    if reverse { fixture.ax.reverse(); fixture.screen.reverse(); fixture.visual.reverse() }
    let proof = try #require(fixture.proof())
    let record = fixture.record(proof: proof)
    let catalog = ClosureTargetCatalog(recordProvider: { _, _ in record }, currentProvider: { _ in record })
    let controller = BackgroundTargetController(catalog: catalog)
    let target = try controller.select(appRef: "app", windowRef: "window")
    #expect(target.windowID == 100)
    #expect(try controller.snapshotTargetState(target).windowID == 100)
    #expect(record.axWindows.filter { $0.windowID == nil }.count == 7)

    let unresolved = TargetCatalogRecord(appRef: "app", windowRef: "sibling", pid: fixture.pid,
        windowID: 102, bounds: fixture.bounds, title: "elided sibling", axWindows: fixture.ax,
        siblingOrdering: proof)
    let unresolvedCatalog = ClosureTargetCatalog(recordProvider: { _, _ in unresolved }, currentProvider: { _ in unresolved })
    #expect(throws: WindowObservationError.self) {
        _ = try BackgroundTargetController(catalog: unresolvedCatalog).select(appRef: "app", windowRef: "sibling")
    }
}

@Test(arguments: ["missing_ax", "missing_screen", "missing_visual", "duplicate_ax", "duplicate_screen",
    "duplicate_visual", "duplicate_mapping", "wrong_pid", "off_screen", "visual_id", "visual_geometry",
    "dialog", "modal", "unknown_modal", "unknown_role", "zero_identity", "unknown_order",
    "equal_order", "front_order", "front_layer", "missing_selected"])
func completeSiblingOrderingRejectsIncompleteOrUnsafeGroup(reason: String) {
    var f = SiblingInventoryFixture()
    let screen = f.screen[2]
    let visual = f.visual[2]
    switch reason {
    case "missing_ax": f.ax.removeLast()
    case "missing_screen": f.screen.removeLast()
    case "missing_visual": f.visual.removeLast()
    case "duplicate_ax": f.ax[3] = f.ax[2]
    case "duplicate_screen": f.screen[3] = f.screen[2]
    case "duplicate_visual": f.visual[3] = f.visual[2]
    case "duplicate_mapping": f.replaceAX(2, windowID: 101)
    case "wrong_pid": f.screen[2] = PIDScreenCaptureWindowObservation(pid: 99, windowID: screen.windowID,
        bounds: screen.bounds, title: screen.title, isOnScreen: true)
    case "off_screen": f.screen[2] = PIDScreenCaptureWindowObservation(pid: screen.pid, windowID: screen.windowID,
        bounds: screen.bounds, title: screen.title, isOnScreen: false)
    case "visual_id": f.visual[2] = VisibleWindowRecord(pid: visual.pid, windowID: 999, bounds: visual.bounds,
        layer: visual.layer, alpha: visual.alpha, zOrder: visual.zOrder)
    case "visual_geometry": f.visual[2] = VisibleWindowRecord(pid: visual.pid, windowID: visual.windowID,
        bounds: visual.bounds.offsetBy(dx: 0.5, dy: 0), layer: visual.layer, alpha: visual.alpha, zOrder: visual.zOrder)
    case "dialog": f.replaceAX(2, subrole: "AXDialog")
    case "modal": f.replaceAX(2, modal: true)
    case "unknown_modal": f.replaceAX(2, modal: nil)
    case "unknown_role": f.replaceAX(2, role: BoundedAXStringResult(value: "AXWindow", status: .failed))
    case "zero_identity": f.replaceAX(2, identity: 0)
    case "missing_selected": f.replaceAX(0)
    default:
        let order = reason == "unknown_order" ? Int.max : (reason == "equal_order" ? 1 : 0)
        f.visual[2] = VisibleWindowRecord(pid: visual.pid, windowID: visual.windowID, bounds: visual.bounds,
            layer: reason == "front_layer" ? 1 : 0, alpha: 1, zOrder: order)
    }
    #expect(f.proof() == nil)
}

@Test(arguments: ["sibling_front", "sibling_modal", "target_replaced", "inventory_incomplete"])
func completeSiblingOrderingRevalidatesBeforeSnapshot(change: String) throws {
    var fixture = SiblingInventoryFixture()
    let catalog = ClosureTargetCatalog(recordProvider: { _, _ in fixture.record(proof: fixture.proof()) },
        currentProvider: { _ in fixture.record(proof: fixture.proof()) })
    let controller = BackgroundTargetController(catalog: catalog)
    let target = try controller.select(appRef: "app", windowRef: "window")
    switch change {
    case "sibling_front":
        let old = fixture.visual[2]
        fixture.visual[2] = VisibleWindowRecord(pid: old.pid, windowID: old.windowID, bounds: old.bounds,
            layer: 0, alpha: 1, zOrder: 0)
    case "sibling_modal": fixture.replaceAX(2, modal: true)
    case "target_replaced": fixture.replaceAX(0, windowID: 100, element: AXUIElementCreateApplication(9999))
    default: fixture.ax.removeLast()
    }
    #expect(throws: WindowObservationError.self) { _ = try controller.snapshotTargetState(target) }
}

@Test func completeSiblingOrderingProofCannotMoveToAnotherTargetOrAXObject() throws {
    var fixture = SiblingInventoryFixture()
    let proof = try #require(fixture.proof())
    #expect(!proof.covers(targetPID: 99, targetWindowID: 100, targetBounds: fixture.bounds, selected: fixture.ax[0], sibling: fixture.ax[2]))
    #expect(!proof.covers(targetPID: fixture.pid, targetWindowID: 101, targetBounds: fixture.bounds, selected: fixture.ax[1], sibling: fixture.ax[2]))
    #expect(!proof.covers(targetPID: fixture.pid, targetWindowID: 100, targetBounds: fixture.bounds.offsetBy(dx: 1, dy: 0),
        selected: fixture.ax[0], sibling: fixture.ax[2]))
    fixture.replaceAX(2, element: AXUIElementCreateApplication(9999))
    #expect(!proof.covers(targetPID: fixture.pid, targetWindowID: 100, targetBounds: fixture.bounds, selected: fixture.ax[0], sibling: fixture.ax[2]))
}

// Live Edge 152 (2026-09-23): the hovered link's address strip is also an AX window of the app.
// Its bottom-edge strip geometry, judged with its live layer, must not block binding, while a
// same-app suggestion window of ordinary size still does.
@Test(arguments: [false, true])
func appStatusStripAXWindowDoesNotBlockBackgroundBinding(dropdown: Bool) throws {
    let bounds = CGRect(x: 25, y: 30, width: 1319, height: 768)
    let targetElement = AXUIElementCreateApplication(2001)
    let stripElement = AXUIElementCreateApplication(2002)
    func window(_ element: AXUIElement, id: CGWindowID, frame: CGRect, subrole: String, z: Int) -> TargetAXWindowRecord {
        TargetAXWindowRecord(windowID: id, bounds: frame, identity: CFHash(element), element: element,
            role: BoundedAXStringResult(value: "AXWindow", status: .complete),
            subrole: BoundedAXStringResult(value: subrole, status: .complete),
            zOrder: z, layer: 0, alpha: 1, isModal: false)
    }
    let overlayFrame = dropdown ? CGRect(x: 89, y: 70, width: 1038, height: 199)
        : CGRect(x: 28, y: 771, width: 249, height: 24)
    let record = TargetCatalogRecord(appRef: "app", windowRef: "window", pid: 42, windowID: 273,
        bounds: bounds, title: "Astra CU Text Fixture",
        axWindows: [window(targetElement, id: 273, frame: bounds, subrole: "AXStandardWindow", z: 16),
                    window(stripElement, id: 9, frame: overlayFrame, subrole: "AXUnknown", z: 14)],
        containsUnselectedOverlay: false)
    let catalog = ClosureTargetCatalog(recordProvider: { _, _ in record }, currentProvider: { _ in record })
    let controller = BackgroundTargetController(catalog: catalog)
    if dropdown {
        do {
            _ = try controller.select(appRef: "app", windowRef: "window")
            Issue.record("a same-app suggestion window must keep blocking binding")
        } catch let error as WindowObservationError {
            guard case .overlayBlocked = error else {
                Issue.record("unexpected observation error: \(error)")
                return
            }
        }
    } else {
        let target = try controller.select(appRef: "app", windowRef: "window")
        #expect(try controller.snapshotTargetState(target).windowID == 273)
    }
}

// The focused address field's suggestion list is an AX window of the app as well. It binds only
// when the catalog proved it from live focus and geometry; an unproven window keeps blocking.
@Test(arguments: [false, true])
func focusedFieldSuggestionListAXWindowBindsOnlyWithCatalogProof(proven: Bool) throws {
    let bounds = CGRect(x: 25, y: 30, width: 1319, height: 768)
    let targetElement = AXUIElementCreateApplication(2001)
    let popupElement = AXUIElementCreateApplication(2003)
    func window(_ element: AXUIElement, id: CGWindowID, frame: CGRect, subrole: String, z: Int) -> TargetAXWindowRecord {
        TargetAXWindowRecord(windowID: id, bounds: frame, identity: CFHash(element), element: element,
            role: BoundedAXStringResult(value: "AXWindow", status: .complete),
            subrole: BoundedAXStringResult(value: subrole, status: .complete),
            zOrder: z, layer: 0, alpha: 1, isModal: false)
    }
    let record = TargetCatalogRecord(appRef: "app", windowRef: "window", pid: 42, windowID: 273,
        bounds: bounds, title: "Astra CU Text Fixture",
        axWindows: [window(targetElement, id: 273, frame: bounds, subrole: "AXStandardWindow", z: 16),
                    window(popupElement, id: 10, frame: CGRect(x: 89, y: 70, width: 1038, height: 199),
                           subrole: "AXUnknown", z: 15)],
        containsUnselectedOverlay: false,
        suggestionPopupWindowIDs: proven ? [10] : [11])
    let catalog = ClosureTargetCatalog(recordProvider: { _, _ in record }, currentProvider: { _ in record })
    let controller = BackgroundTargetController(catalog: catalog)
    if proven {
        let target = try controller.select(appRef: "app", windowRef: "window")
        #expect(try controller.snapshotTargetState(target).windowID == 273)
    } else {
        do {
            _ = try controller.select(appRef: "app", windowRef: "window")
            Issue.record("an unproven same-app window must keep blocking binding")
        } catch let error as WindowObservationError {
            guard case .overlayBlocked = error else {
                Issue.record("unexpected observation error: \(error)")
                return
            }
        }
    }
}
