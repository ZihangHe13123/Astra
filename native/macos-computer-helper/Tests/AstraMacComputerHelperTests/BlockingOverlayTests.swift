@testable import AstraMacComputerHelperCore
import ApplicationServices
import Foundation
import Testing

@Test func blockerEvidenceDistinguishesVisibleAXAndCorrelatedWindows() {
    let target = WindowTarget(appRef: "app", windowRef: "parent", pid: 42, windowID: 1,
                              bounds: CGRect(x: 0, y: 0, width: 800, height: 600), title: "Parent")
    let frame = CGRect(x: 10, y: 20, width: 272, height: 100)
    let visible = VisibleWindowRecord(pid: 42, windowID: 2, bounds: frame, layer: 3, alpha: 1, zOrder: 0)
    let foreign = VisibleWindowRecord(pid: 303, windowID: 3, bounds: frame, layer: 3, alpha: 1, zOrder: 0)
    let screens = [PIDScreenCaptureWindowObservation(pid: 42, windowID: 2, bounds: frame,
                                                    title: "Fixture", isOnScreen: true)]
    let child = ContainedBlockingElement(element: AXUIElementCreateApplication(42), window:
        BlockingOverlayWindow(windowID: 2, bounds: frame, layer: nil, title: "Sheet", isModal: true, source: "accessibility"))

    let visual = blockingOverlayEvidence(for: target, application: "Fixture", ax: .clear, contained: nil,
                                         offenders: [visible, foreign], screens: screens, axWindows: [])
    #expect(visual.visibleCount == 1 && visual.windows.count == 1)
    #expect(visual.windows[0].source == "window_server" && visual.windows[0].isModal == nil)
    #expect(visual.windows[0].title == "Fixture" && visual.windows[0].layer == 3)
    #expect(visual.png == nil && visual.imageStatus == "window_binding_unverified")

    let hidden = blockingOverlayEvidence(for: target, application: "Fixture", ax: .overlay, contained: child,
                                         offenders: [], screens: [], axWindows: [])
    #expect(hidden.visibleCount == 0 && hidden.windows.count == 1 && hidden.windows[0].isModal == true)
    #expect(hidden.png == nil && hidden.imageStatus == "no_visible_blocker")

    let correlated = blockingOverlayEvidence(for: target, application: "Fixture", ax: .overlay, contained: child,
                                             offenders: [visible], screens: screens, axWindows: [])
    #expect(correlated.windows.count == 1 && correlated.windows[0].source == "window_server_and_ax")
    #expect(correlated.windows[0].bounds == frame && correlated.windows[0].isModal == true)
    #expect(correlated.png == nil)
}
