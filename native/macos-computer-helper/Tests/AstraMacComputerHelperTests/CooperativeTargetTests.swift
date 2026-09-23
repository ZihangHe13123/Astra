@testable import AstraMacComputerHelperCore
@preconcurrency import ApplicationServices
import CoreGraphics
import Foundation
import Testing

@Test func backgroundSelectNeverActivatesRaisesOrChangesFocusedWindow() throws {
    let activation = TargetActivationSpy()
    let catalog = FixtureTargetCatalog()
    let controller = BackgroundTargetController(catalog: catalog, activation: activation)

    let target = try controller.select(appRef: "app", windowRef: "window")

    #expect(target.interactionMode == .background)
    #expect(target.axIdentity == 700)
    #expect(activation.activateCount == 0)
    #expect(activation.restoreCount == 0)
    #expect(activation.unhideCount == 0)
    #expect(activation.raiseCount == 0)
    #expect(activation.setFocusedWindowCount == 0)
    #expect(catalog.frontmostPID == 999)
}

@Test func backgroundSnapshotUsesExactSelectedAXWindowWhileSentinelRemainsFrontmost() throws {
    let activation = TargetActivationSpy()
    let catalog = FixtureTargetCatalog()
    let controller = BackgroundTargetController(catalog: catalog, activation: activation)
    let target = try controller.select(appRef: "app", windowRef: "window")

    let state = try controller.snapshotTargetState(target)
    let session = try BackgroundSnapshotSession(
        target: target,
        exactAXElement: target.axElement,
        frontmostPID: catalog.frontmostPID
    )
    let capture = SentinelCaptureProvider()
    _ = try session.capture { windowID in
        try captureExactWindowImage(windowID: windowID, provider: capture)
    }
    let serialized = session.serialize { exactAXElement in
        #expect(CFEqual(exactAXElement, target.axElement))
        return AXSerializer.serialize(
            AXNode(role: "AXWindow", title: "selected:\(state.axIdentity)", bounds: state.bounds),
            snapshotID: "sentinel"
        )
    }
    try session.verifyFrontmost(catalog.frontmostPID)

    #expect(state.pid == 42)
    #expect(state.windowID == 24)
    #expect(state.axIdentity == 700)
    #expect(state.bounds == catalog.initial.bounds)
    #expect(capture.windowIDs == [target.windowID])
    #expect(serialized.root.title == "selected:700")
    #expect(catalog.frontmostPID == 999)
    #expect(activation.activateCount == 0)
}

@Test func backgroundSnapshotRejectsUnavailableFrontmostSentinel() throws {
    let catalog = FixtureTargetCatalog()
    let target = try BackgroundTargetController(catalog: catalog).select(
        appRef: "app",
        windowRef: "window"
    )

    #expect(throws: WindowObservationError.self) {
        _ = try BackgroundSnapshotSession(
            target: target,
            exactAXElement: target.axElement,
            frontmostPID: nil
        )
    }
}

@Test func backgroundSnapshotRejectsUnavailableFinalFrontmostObservation() throws {
    let catalog = FixtureTargetCatalog()
    let target = try BackgroundTargetController(catalog: catalog).select(
        appRef: "app",
        windowRef: "window"
    )
    let session = try BackgroundSnapshotSession(
        target: target,
        exactAXElement: target.axElement,
        frontmostPID: 999
    )

    #expect(throws: WindowObservationError.self) {
        try session.verifyFrontmost(nil)
    }
}

@Test func backgroundSelectionRejectsContainedOverlayOutsideSelectedWindowIdentity() {
    let catalog = FixtureTargetCatalog()
    catalog.current = TargetCatalogRecord(
        appRef: catalog.initial.appRef,
        windowRef: catalog.initial.windowRef,
        pid: catalog.initial.pid,
        windowID: catalog.initial.windowID,
        bounds: catalog.initial.bounds,
        title: catalog.initial.title,
        axWindows: catalog.initial.axWindows,
        containsUnselectedOverlay: true
    )

    #expect(throws: WindowObservationError.self) {
        _ = try BackgroundTargetController(catalog: catalog).select(appRef: "app", windowRef: "window")
    }
}

@Test func backgroundSelectionFailsClosedForAmbiguousContainedOverlay() {
    let catalog = FixtureTargetCatalog()
    catalog.current = TargetCatalogRecord(
        appRef: "app",
        windowRef: "window",
        pid: 42,
        windowID: 24,
        bounds: catalog.initial.bounds,
        title: "Document",
        axWindows: [
            TargetAXWindowRecord(windowID: 24, bounds: catalog.initial.bounds, identity: 700),
            TargetAXWindowRecord(
                windowID: nil,
                bounds: CGRect(x: 240, y: 180, width: 400, height: 240),
                identity: 701
            ),
        ]
    )

    #expect(throws: WindowObservationError.self) {
        _ = try BackgroundTargetController(catalog: catalog).select(appRef: "app", windowRef: "window")
    }
}

@Test func backgroundSelectionRejectsEqualBoundsCandidateWithoutIndependentWindowID() {
    let catalog = FixtureTargetCatalog()
    catalog.current = TargetCatalogRecord(
        appRef: "app",
        windowRef: "window",
        pid: 42,
        windowID: 24,
        bounds: catalog.initial.bounds,
        title: "Document",
        axWindows: [
            catalog.initial.axWindows[0],
            TargetAXWindowRecord(
                windowID: nil,
                bounds: catalog.initial.bounds,
                identity: 701,
                element: AXUIElementCreateApplication(43)
            ),
        ]
    )

    #expect(throws: WindowObservationError.self) {
        _ = try BackgroundTargetController(catalog: catalog).select(
            appRef: "app",
            windowRef: "window"
        )
    }
}

@Test func backgroundSelectionRejectsEqualBoundsSiblingWithDifferentMappedWindowID() {
    let catalog = FixtureTargetCatalog()
    catalog.current = TargetCatalogRecord(
        appRef: "app",
        windowRef: "window",
        pid: 42,
        windowID: 24,
        bounds: catalog.initial.bounds,
        title: "Document",
        axWindows: [
            catalog.initial.axWindows[0],
            TargetAXWindowRecord(
                windowID: 25,
                bounds: catalog.initial.bounds,
                identity: 701,
                element: AXUIElementCreateApplication(43),
                role: BoundedAXStringResult(value: "AXWindow", status: .complete),
                subrole: BoundedAXStringResult(value: "AXDialog", status: .complete),
                zOrder: 0,
                layer: 8,
                alpha: 1
            ),
        ]
    )

    #expect(throws: WindowObservationError.self) {
        _ = try BackgroundTargetController(catalog: catalog).select(
            appRef: "app",
            windowRef: "window"
        )
    }
}

@Test func backgroundSelectionAllowsOnlyProvenOrdinarySiblingBehind() throws {
    for subrole in ["AXStandardWindow", "AXDialog"] {
        for order in [0, 1, 2, Int.max] {
            let catalog = FixtureTargetCatalog()
            let selected = catalog.initial.axWindows[0]
            catalog.current = TargetCatalogRecord(appRef: "app", windowRef: "window", pid: 42,
                windowID: 24, bounds: catalog.initial.bounds, title: "Document", axWindows: [
                    TargetAXWindowRecord(windowID: 24, bounds: selected.bounds, identity: selected.identity,
                        element: selected.element, zOrder: 1, layer: 0, alpha: 1),
                    TargetAXWindowRecord(windowID: 25, bounds: selected.bounds, identity: 701,
                        element: AXUIElementCreateApplication(43),
                        role: BoundedAXStringResult(value: "AXWindow", status: .complete),
                        subrole: BoundedAXStringResult(value: subrole, status: .complete),
                        zOrder: order, layer: 0, alpha: 1),
                ])
            let controller = BackgroundTargetController(catalog: catalog)
            if subrole == "AXStandardWindow", order == 2 {
                #expect(try controller.select(appRef: "app", windowRef: "window").axIdentity == selected.identity)
            } else {
                #expect(throws: WindowObservationError.self) { _ = try controller.select(appRef: "app", windowRef: "window") }
            }
        }
    }
}

@Test func backgroundSelectionAllowsIndependentlyEnumeratedEqualBoundsTarget() throws {
    let catalog = FixtureTargetCatalog()

    let target = try BackgroundTargetController(catalog: catalog).select(
        appRef: "app",
        windowRef: "window"
    )

    #expect(target.windowID == 24)
    #expect(target.axIdentity == 700)
}

private func recordWithMappedNonmodalDialog(
    _ catalog: FixtureTargetCatalog, reason: String = "behind"
) -> TargetCatalogRecord {
    let selected = catalog.initial.axWindows[0]
    let order: Int? = reason == "unknown_order" ? nil : (reason == "front" ? 0 : (reason == "equal" ? 1 : 2))
    return TargetCatalogRecord(appRef: "app", windowRef: "window", pid: 42,
        windowID: 24, bounds: selected.bounds, title: "Document", axWindows: [
            TargetAXWindowRecord(windowID: 24, bounds: selected.bounds, identity: selected.identity,
                element: selected.element, zOrder: 1, layer: 0, alpha: 1),
            TargetAXWindowRecord(windowID: reason == "unmapped" ? nil : 25,
                bounds: CGRect(x: selected.bounds.minX + 10, y: selected.bounds.minY + 11, width: 66, height: 20),
                identity: 701, element: AXUIElementCreateApplication(43),
                role: BoundedAXStringResult(value: reason == "sheet" ? "AXSheet" : "AXWindow",
                    status: reason == "unknown_role" ? .failed : .complete),
                subrole: BoundedAXStringResult(value: "AXDialog",
                    status: reason == "unknown_subrole" ? .failed : .complete),
                zOrder: order, layer: reason == "front_layer" ? 8 : 0, alpha: 1,
                isModal: reason == "unknown_modal" ? nil : reason == "modal"),
        ])
}

@Test(arguments: ["behind", "front", "equal", "front_layer", "unknown_order", "modal",
    "unknown_modal", "unknown_role", "unknown_subrole", "unmapped", "sheet"])
func backgroundSelectionRequiresMappedNonmodalDialogBehind(reason: String) throws {
    let catalog = FixtureTargetCatalog()
    let activation = TargetActivationSpy()
    catalog.current = recordWithMappedNonmodalDialog(catalog, reason: reason)
    let controller = BackgroundTargetController(catalog: catalog, activation: activation)
    if reason == "behind" {
        let target = try controller.select(appRef: "app", windowRef: "window")
        #expect(target.windowID == 24)
        #expect(target.interactionMode == .background)
        #expect(try controller.snapshotTargetState(target).axIdentity == catalog.initial.axWindows[0].identity)
    } else {
        #expect(throws: WindowObservationError.self) {
            _ = try controller.select(appRef: "app", windowRef: "window")
        }
    }
    #expect(activation.activateCount == 0)
    #expect(activation.raiseCount == 0)
    #expect(activation.setFocusedWindowCount == 0)
}

@Test(arguments: ["front", "modal", "unknown_modal", "unknown_order", "unmapped"])
func backgroundSnapshotRevalidatesMappedNonmodalDialog(reason: String) throws {
    let catalog = FixtureTargetCatalog()
    catalog.current = recordWithMappedNonmodalDialog(catalog)
    let controller = BackgroundTargetController(catalog: catalog)
    let target = try controller.select(appRef: "app", windowRef: "window")
    catalog.current = recordWithMappedNonmodalDialog(catalog, reason: reason)
    #expect(throws: WindowObservationError.self) { _ = try controller.snapshotTargetState(target) }
}

@Test func backgroundSelectionAcceptsOnePointScreenCaptureShadowExpansion() throws {
    let catalog = FixtureTargetCatalog()
    let screenCaptureBounds = CGRect(x: 99, y: 169, width: 719, height: 714)
    let accessibilityBounds = CGRect(x: 100, y: 170, width: 717, height: 712)
    let selectedElement = AXUIElementCreateApplication(42)
    catalog.current = TargetCatalogRecord(
        appRef: "app",
        windowRef: "window",
        pid: 42,
        windowID: 24,
        bounds: screenCaptureBounds,
        title: "Document",
        axWindows: [TargetAXWindowRecord(
            windowID: 24,
            bounds: accessibilityBounds,
            identity: 700,
            element: selectedElement
        )]
    )

    let target = try BackgroundTargetController(catalog: catalog).select(
        appRef: "app",
        windowRef: "window"
    )

    #expect(target.windowID == 24)
    #expect(target.bounds == screenCaptureBounds)
    #expect(target.axIdentity == 700)
    #expect(CFEqual(target.axElement, selectedElement))
}

@Test func backgroundSnapshotRejectsSameHashNonEqualAXReplacement() throws {
    let catalog = FixtureTargetCatalog()
    let controller = BackgroundTargetController(catalog: catalog)
    let target = try controller.select(appRef: "app", windowRef: "window")
    catalog.current = TargetCatalogRecord(
        appRef: "app",
        windowRef: "window",
        pid: 42,
        windowID: 24,
        bounds: catalog.initial.bounds,
        title: "Document",
        axWindows: [TargetAXWindowRecord(
            windowID: 24,
            bounds: catalog.initial.bounds,
            identity: 700,
            element: AXUIElementCreateApplication(43)
        )]
    )

    #expect(throws: WindowObservationError.self) {
        _ = try controller.snapshotTargetState(target)
    }
}

@Test func backgroundSnapshotRejectsMovedWindow() throws {
    let catalog = FixtureTargetCatalog()
    let controller = BackgroundTargetController(catalog: catalog)
    let target = try controller.select(appRef: "app", windowRef: "window")
    catalog.current = TargetCatalogRecord(
        appRef: "app",
        windowRef: "window",
        pid: 42,
        windowID: 24,
        bounds: catalog.initial.bounds.offsetBy(dx: 20, dy: 0),
        title: "Document",
        axWindows: [TargetAXWindowRecord(
            windowID: 24,
            bounds: catalog.initial.bounds.offsetBy(dx: 20, dy: 0),
            identity: 700
        )]
    )

    #expect(throws: WindowObservationError.self) {
        _ = try controller.snapshotTargetState(target)
    }
}

@Test func backgroundSelectionRejectsMissingWindowID() {
    let catalog = FixtureTargetCatalog()
    catalog.current = TargetCatalogRecord(
        appRef: "app",
        windowRef: "window",
        pid: 42,
        windowID: nil,
        bounds: catalog.initial.bounds,
        title: "Document",
        axWindows: [TargetAXWindowRecord(windowID: nil, bounds: catalog.initial.bounds, identity: 700)]
    )

    #expect(throws: WindowObservationError.self) {
        _ = try BackgroundTargetController(catalog: catalog).select(appRef: "app", windowRef: "window")
    }
}

@Test func backgroundSnapshotRejectsChangedAXIdentity() throws {
    let catalog = FixtureTargetCatalog()
    let controller = BackgroundTargetController(catalog: catalog)
    let target = try controller.select(appRef: "app", windowRef: "window")
    catalog.current = TargetCatalogRecord(
        appRef: "app",
        windowRef: "window",
        pid: 42,
        windowID: 24,
        bounds: catalog.initial.bounds,
        title: "Document",
        axWindows: [TargetAXWindowRecord(windowID: 24, bounds: catalog.initial.bounds, identity: 701)]
    )

    #expect(throws: WindowObservationError.self) {
        _ = try controller.snapshotTargetState(target)
    }
}

private final class FixtureTargetCatalog: TargetCataloging {
    let initial: TargetCatalogRecord
    var current: TargetCatalogRecord
    var frontmostPID: pid_t? = 999

    init() {
        let bounds = CGRect(x: 100, y: 80, width: 800, height: 600)
        let selectedElement = AXUIElementCreateApplication(42)
        let record = TargetCatalogRecord(
            appRef: "app",
            windowRef: "window",
            pid: 42,
            windowID: 24,
            bounds: bounds,
            title: "Document",
            axWindows: [TargetAXWindowRecord(
                windowID: 24,
                bounds: bounds,
                identity: 700,
                element: selectedElement
            )]
        )
        initial = record
        current = record
    }

    func record(appRef: String, windowRef: String) throws -> TargetCatalogRecord? {
        guard appRef == initial.appRef, windowRef == initial.windowRef else { return nil }
        return current
    }

    func currentRecord(for target: WindowTarget) throws -> TargetCatalogRecord? {
        current
    }
}

private final class TargetActivationSpy: ApplicationActivationControlling {
    private(set) var activateCount = 0
    private(set) var restoreCount = 0
    private(set) var unhideCount = 0
    private(set) var raiseCount = 0
    private(set) var setFocusedWindowCount = 0

    func activate(_ target: WindowTarget) throws { activateCount += 1 }
    func restore(pid: pid_t) throws { restoreCount += 1 }
}

private final class SentinelCaptureProvider: ExactWindowImageProviding {
    private(set) var windowIDs: [CGWindowID] = []

    func primaryImage() throws -> CGImage { throw PrimaryWindowCaptureError.timedOut }

    func fallbackImage(for windowID: CGWindowID) -> CGImage? {
        windowIDs.append(windowID)
        let colorSpace = CGColorSpaceCreateDeviceRGB()
        let context = CGContext(
            data: nil,
            width: 1,
            height: 1,
            bitsPerComponent: 8,
            bytesPerRow: 4,
            space: colorSpace,
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        )
        return context?.makeImage()
    }
}

@Test func overlayFailureClearsOnlyAfterOverlayStateChanges() throws {
    let catalog = FixtureTargetCatalog()
    catalog.current = TargetCatalogRecord(
        appRef: catalog.initial.appRef, windowRef: catalog.initial.windowRef,
        pid: catalog.initial.pid, windowID: catalog.initial.windowID,
        bounds: catalog.initial.bounds, title: catalog.initial.title,
        axWindows: catalog.initial.axWindows, containsUnselectedOverlay: true
    )
    let controller = BackgroundTargetController(catalog: catalog)
    for _ in 0..<2 {
        do {
            _ = try controller.select(appRef: "app", windowRef: "window")
            Issue.record("overlay must prevent binding")
        } catch WindowObservationError.overlayBlocked {
            // Repeated binding does not evade the overlay check.
        }
    }
    catalog.current = catalog.initial
    let target = try controller.select(appRef: "app", windowRef: "window")
    #expect(target.windowID == catalog.initial.windowID)
}

private final class SequencedTargetCatalog: TargetCataloging {
    private var records: [TargetCatalogRecord]
    private(set) var reads = 0

    init(_ records: [TargetCatalogRecord]) { self.records = records }

    private func next() -> TargetCatalogRecord? {
        reads += 1
        return records.count > 1 ? records.removeFirst() : records.first
    }

    func record(appRef: String, windowRef: String) throws -> TargetCatalogRecord? { next() }
    func currentRecord(for target: WindowTarget) throws -> TargetCatalogRecord? { next() }
}

private func overlaid(_ base: TargetCatalogRecord, transient: Bool) -> TargetCatalogRecord {
    TargetCatalogRecord(appRef: base.appRef, windowRef: base.windowRef, pid: base.pid, windowID: base.windowID,
        bounds: base.bounds, title: base.title, axWindows: base.axWindows, containsUnselectedOverlay: true,
        overlayMayBeTransient: transient)
}

private func expectOverlayBlocked(_ body: () throws -> Void) {
    do {
        try body()
        Issue.record("expected overlay_blocked")
    } catch WindowObservationError.overlayBlocked {
    } catch {
        Issue.record("unexpected error: \(error)")
    }
}

// Live Edge 153: after an input-source switch macOS draws an 84×77 layer-3 indicator at the
// caret for about 1.5 s, owned by the application itself.
@Test func backgroundSelectionWaitsOutASmallTransientAppOverlay() throws {
    let clear = FixtureTargetCatalog().initial
    let catalog = SequencedTargetCatalog([overlaid(clear, transient: true), overlaid(clear, transient: true), clear])
    var slept = 0
    let target = try BackgroundTargetController(catalog: catalog, sleepMilliseconds: { slept += $0 })
        .select(appRef: "app", windowRef: "window")
    #expect(target.windowID == 24)
    #expect(catalog.reads == 3 && slept == 200)
}

@Test func backgroundSelectionStillBlocksPersistentOrNonTransientOverlays() {
    let clear = FixtureTargetCatalog().initial
    var slept = 0
    expectOverlayBlocked {
        _ = try BackgroundTargetController(catalog: SequencedTargetCatalog([overlaid(clear, transient: true)]),
            sleepMilliseconds: { slept += $0 }).select(appRef: "app", windowRef: "window")
    }
    #expect(slept == 2_000)
    slept = 0
    let menu = SequencedTargetCatalog([overlaid(clear, transient: false), clear])
    expectOverlayBlocked {
        _ = try BackgroundTargetController(catalog: menu, sleepMilliseconds: { slept += $0 })
            .select(appRef: "app", windowRef: "window")
    }
    #expect(slept == 0 && menu.reads == 1)
}
