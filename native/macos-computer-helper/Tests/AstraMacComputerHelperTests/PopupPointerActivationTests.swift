@testable import AstraMacComputerHelperCore
import CoreGraphics
import Testing

@Test func popupPointerGuardOnlyAuthorizesOneSealedClickPoint() throws {
    let bounds = CGRect(x: 530, y: 320, width: 164, height: 434)
    let point = CGPoint(x: 610, y: 362)
    let sealed = ActionGuard(
        pid: 547, windowID: 895, bounds: bounds, axIdentity: 1234,
        focusedAXIdentity: 1234, focusedAXBounds: bounds,
        snapshotID: "popup-fresh", interactionMode: .foregroundTakeover
    )
    var proof = true
    let validator = PopupPointerClickGuardValidator(
        sealed: sealed, authorizedPoint: point, popupProof: { proof }
    )
    try validator.revalidate(expected: sealed, point: point)
    try validator.revalidate(expected: sealed, point: nil)
    #expect(throws: ActionExecutionError.self) {
        try validator.revalidate(expected: sealed, point: CGPoint(x: point.x + 1, y: point.y))
    }
    #expect(throws: ActionExecutionError.self) {
        try validator.revalidate(expected: sealed, point: point, dragDisplacement: .zero)
    }
    #expect(throws: ActionExecutionError.self) {
        try validator.revalidateAndObserveFocus(expected: sealed, point: point)
    }
    let wrongPID = ActionGuard(
        pid: 999, windowID: 895, bounds: bounds, axIdentity: 1234,
        snapshotID: "popup-fresh", interactionMode: .foregroundTakeover
    )
    #expect(throws: ActionExecutionError.self) {
        try validator.revalidate(expected: wrongPID, point: point)
    }
    let oldSnapshot = ActionGuard(
        pid: 547, windowID: 895, bounds: bounds, axIdentity: 1234,
        snapshotID: "popup-old", interactionMode: .foregroundTakeover
    )
    #expect(throws: ActionExecutionError.self) {
        try validator.revalidate(expected: oldSnapshot, point: point)
    }
    proof = false
    #expect(throws: ActionExecutionError.self) {
        try validator.revalidate(expected: sealed, point: point)
    }
    // Only the executor's already-held balanced release uses this entry.
    // A vanished popup does not require another screenshot before mouse-up.
    try validator.revalidateBalancedRelease(expected: sealed, point: point, dragDisplacement: nil)
    #expect(throws: ActionExecutionError.self) {
        try validator.revalidateBalancedRelease(expected: sealed,
            point: CGPoint(x: point.x + 1, y: point.y), dragDisplacement: nil)
    }
}

@Test func popupPointerVisibilityNeedsOneExactTopmostDialog() {
    let bounds = CGRect(x: 530, y: 320, width: 164, height: 434)
    let point = CGPoint(x: 610, y: 362)
    let popup = VisibleWindowRecord(pid: 547, windowID: 895, bounds: bounds,
                                    layer: 8, alpha: 1, zOrder: 2)
    let parent = VisibleWindowRecord(pid: 547, windowID: 3878,
                                     bounds: CGRect(x: 0, y: 30, width: 1352, height: 768),
                                     layer: 0, alpha: 1, zOrder: 8)
    func permits(
        pid: pid_t = 547, frontmostPID: pid_t? = 547, windowID: CGWindowID = 895,
        selectedBounds: CGRect = bounds, click: CGPoint = point,
        role: String? = "AXWindow", subrole: String? = "AXDialog",
        records: [VisibleWindowRecord] = [popup, parent]
    ) -> Bool {
        popupPointerWindowIsTopmost(
            targetPID: pid, frontmostPID: frontmostPID, windowID: windowID,
            bounds: selectedBounds, point: click, role: role, subrole: subrole,
            visibleWindows: records
        )
    }

    #expect(permits())
    #expect(!permits(frontmostPID: 999))
    #expect(!permits(pid: 999))
    #expect(!permits(windowID: 894))
    #expect(!permits(windowID: 0))
    #expect(!permits(selectedBounds: bounds.offsetBy(dx: 2, dy: 0)))
    #expect(!permits(click: CGPoint(x: bounds.minX, y: point.y)))
    #expect(!permits(role: "AXSheet"))
    #expect(!permits(subrole: "AXStandardWindow"))
    #expect(!permits(records: [parent]))
    #expect(!permits(records: [popup, popup, parent]))
    #expect(!permits(records: [VisibleWindowRecord(pid: 547, windowID: 895,
        bounds: bounds, layer: 0, alpha: 1, zOrder: 2), parent]))
    #expect(!permits(records: [VisibleWindowRecord(pid: 547, windowID: 895,
        bounds: bounds, layer: 8, alpha: 0, zOrder: 2), parent]))
    let coveringOtherApp = VisibleWindowRecord(pid: 999, windowID: 901,
        bounds: CGRect(x: 600, y: 350, width: 30, height: 30), layer: 9, alpha: 1, zOrder: 1)
    #expect(!permits(records: [coveringOtherApp, popup, parent]))
    let coveringSameApp = VisibleWindowRecord(pid: 547, windowID: 902,
        bounds: CGRect(x: 600, y: 350, width: 30, height: 30), layer: 8, alpha: 1, zOrder: 1)
    #expect(!permits(records: [coveringSameApp, popup, parent]))
    let transparentFront = VisibleWindowRecord(pid: 547, windowID: 903,
        bounds: CGRect(x: 600, y: 350, width: 30, height: 30), layer: 8, alpha: 0, zOrder: 1)
    #expect(!permits(records: [transparentFront, popup, parent]))
    let transparentBehind = VisibleWindowRecord(pid: 547, windowID: 904,
        bounds: CGRect(x: 600, y: 350, width: 30, height: 30), layer: 8, alpha: 0, zOrder: 3)
    #expect(permits(records: [popup, transparentBehind, parent]))
}

@Test func popupPointerProofStagesDistinguishStaleIdentityAndCoveringWindows() {
    let bounds = CGRect(x: 530, y: 320, width: 164, height: 434)
    let point = CGPoint(x: 610, y: 362)
    let popup = VisibleWindowRecord(pid: 547, windowID: 895, bounds: bounds,
                                    layer: 8, alpha: 1, zOrder: 2)
    let parent = VisibleWindowRecord(pid: 547, windowID: 3878,
                                     bounds: CGRect(x: 0, y: 30, width: 1352, height: 768),
                                     layer: 0, alpha: 1, zOrder: 8)
    func stage(
        pid: pid_t = 547, frontmost: pid_t? = 547,
        id: CGWindowID = 895, selectedBounds: CGRect = bounds,
        click: CGPoint = point, role: String? = "AXWindow", subrole: String? = "AXDialog",
        records: [VisibleWindowRecord] = [popup, parent]
    ) -> PopupPointerProofFailureStage? {
        popupPointerWindowTopmostFailure(
            targetPID: pid, frontmostPID: frontmost, windowID: id,
            bounds: selectedBounds, point: click, role: role, subrole: subrole,
            visibleWindows: records
        )
    }

    #expect(stage() == nil)
    #expect(stage(click: CGPoint(x: bounds.minX, y: point.y)) == .invalidTarget)
    #expect(stage(frontmost: 999) == .notFrontmost)
    #expect(stage(subrole: "AXStandardWindow") == .axRole)
    #expect(stage(records: [parent]) == .selectedWindowCount)
    #expect(stage(records: [popup, popup, parent]) == .selectedWindowCount)
    #expect(stage(selectedBounds: bounds.offsetBy(dx: 1, dy: 0)) == .selectedWindowIdentity)
    let hidden = VisibleWindowRecord(pid: 547, windowID: 895, bounds: bounds,
                                     layer: 8, alpha: 0, zOrder: 2)
    #expect(stage(records: [hidden, parent]) == .selectedWindowVisibility)
    let covering = VisibleWindowRecord(pid: 999, windowID: 901,
        bounds: CGRect(x: 600, y: 350, width: 30, height: 30), layer: 9, alpha: 1, zOrder: 1)
    #expect(stage(records: [covering, popup, parent]) == .coveringWindow)
    #expect(PopupPointerProofFailureStage.coveringWindow.rawValue == "covering_window")
}

@Test func popupPointerHitProofReportsInvalidInputWithoutInspectingAX() {
    var stages: [PopupPointerProofFailureStage] = []
    #expect(!foregroundPointBelongsToWindow(
        CGPoint(x: CGFloat.nan, y: 362), pid: 547, window: nil,
        onFailure: { stages.append($0) }
    ))
    #expect(stages == [.hitInvalidInput])
}

@Test func popupPointerCoveringRequiresVisualAgreementAndExactAXHit() {
    let bounds = CGRect(x: 530, y: 320, width: 164, height: 434)
    let point = CGPoint(x: 610, y: 362)
    let popup = VisibleWindowRecord(pid: 547, windowID: 895, bounds: bounds,
                                    layer: 8, alpha: 1, zOrder: 2)
    // WPS exposes a same-PID, full-screen compositor window above its menu.
    // Its PID and layer alone cannot be used to allow input.
    let covering = VisibleWindowRecord(pid: 547, windowID: 8734,
        bounds: CGRect(x: 0, y: 0, width: 1512, height: 982),
        layer: 2_147_483_630, alpha: 0, zOrder: 0)
    let failure = popupPointerWindowTopmostFailure(
        targetPID: 547, frontmostPID: 547, windowID: 895,
        bounds: bounds, point: point, role: "AXWindow", subrole: "AXDialog",
        visibleWindows: [covering, popup]
    )
    #expect(failure == .coveringWindow)

    var calls: [String] = []
    var rejected: [PopupPointerProofFailureStage] = []
    func check(visual: Bool, hit: Bool) -> Bool {
        popupPointerProofAllowsDispatch(
            geometryFailure: failure,
            visualAgreement: { calls.append("visual"); return visual },
            exactAXHit: { calls.append("hit"); return hit },
            onGeometryFailure: { rejected.append($0) }
        )
    }
    #expect(check(visual: true, hit: true))
    #expect(calls == ["visual", "hit"])
    #expect(rejected.isEmpty)

    calls.removeAll()
    #expect(!check(visual: false, hit: true))
    #expect(calls == ["visual"])
    #expect(rejected == [.coveringWindow])

    calls.removeAll()
    #expect(!check(visual: true, hit: false))
    #expect(calls == ["visual", "hit"])
    #expect(rejected == [.coveringWindow])
}

@Test func popupPointerOtherGeometryFailuresNeverInvokeVisualOrHitProof() {
    for failure in PopupPointerProofFailureStage.allCases where failure != .coveringWindow {
        var calls: [String] = []
        var rejected: [PopupPointerProofFailureStage] = []
        #expect(!popupPointerProofAllowsDispatch(
            geometryFailure: failure,
            visualAgreement: { calls.append("visual"); return true },
            exactAXHit: { calls.append("hit"); return true },
            onGeometryFailure: { rejected.append($0) }
        ))
        #expect(calls.isEmpty)
        #expect(rejected == [failure])
    }

    var calls: [String] = []
    #expect(popupPointerProofAllowsDispatch(
        geometryFailure: nil,
        visualAgreement: { calls.append("visual"); return false },
        exactAXHit: { calls.append("hit"); return true },
        onGeometryFailure: { _ in calls.append("rejected") }
    ))
    #expect(calls == ["hit"])
}

@Test func popupPointerCoveringDiagnosticNamesOnlyFirstBlockingWindow() {
    let bounds = CGRect(x: 530, y: 320, width: 164, height: 434)
    let point = CGPoint(x: 610, y: 362)
    let selected = VisibleWindowRecord(pid: 547, windowID: 895, bounds: bounds,
                                       layer: 8, alpha: 1, zOrder: 2)
    let first = VisibleWindowRecord(pid: 999, windowID: 901,
        bounds: CGRect(x: 600, y: 350, width: 30, height: 30),
        layer: 9, alpha: 0, zOrder: 1)
    let second = VisibleWindowRecord(pid: 998, windowID: 902,
        bounds: CGRect(x: 605, y: 355, width: 30, height: 30),
        layer: 9, alpha: 1, zOrder: 0)
    var captures: [String] = []
    let failure = popupPointerWindowTopmostFailure(
        targetPID: 547, frontmostPID: 547, windowID: 895,
        bounds: bounds, point: point, role: "AXWindow", subrole: "AXDialog",
        visibleWindows: [first, second, selected],
        onCoveringWindow: { selected, candidate in
            captures.append(popupPointerCoveringWindowDiagnostic(selected: selected, candidate: candidate))
        }
    )
    #expect(failure == .coveringWindow)
    #expect(captures == [
        "selected_layer=8 selected_z=2 selected_alpha=1.00"
            + " candidate_pid=999 candidate_windowID=901 candidate_layer=9 candidate_z=1"
            + " candidate_alpha=0.00 dx=70.00 dy=30.00 w=30.00 h=30.00"
    ])
    captures.removeAll()
    #expect(popupPointerWindowTopmostFailure(
        targetPID: 547, frontmostPID: 547, windowID: 895,
        bounds: bounds, point: point, role: "AXWindow", subrole: "AXDialog",
        visibleWindows: [selected], onCoveringWindow: { _, _ in captures.append("unexpected") }
    ) == nil)
    #expect(captures.isEmpty)

    let far = VisibleWindowRecord(pid: 999, windowID: 901,
        bounds: CGRect(x: 200_000, y: 350, width: 30, height: 30),
        layer: 9, alpha: 100, zOrder: 1)
    let bounded = popupPointerCoveringWindowDiagnostic(selected: selected, candidate: far)
    #expect(bounded.contains("candidate_alpha=unavailable"))
    #expect(bounded.contains("dx=unavailable"))
}
