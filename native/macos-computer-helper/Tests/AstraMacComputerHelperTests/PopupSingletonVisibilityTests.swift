@testable import AstraMacComputerHelperCore
import CoreGraphics
import Testing

private let popupVisibilityBounds = CGRect(x: 89, y: 70, width: 1038, height: 199)
private let popupVisibleTarget = VisibleWindowRecord(
    pid: 42, windowID: 24, bounds: popupVisibilityBounds,
    layer: 3, alpha: 1, zOrder: 1
)
private let popupVisibleParent = VisibleWindowRecord(
    pid: 42, windowID: 23,
    bounds: CGRect(x: 0, y: 0, width: 1400, height: 900),
    layer: 0, alpha: 1, zOrder: 2
)

private func popupVisibility(
    _ records: [VisibleWindowRecord]?
) -> PopupSingletonVisibilityProof {
    popupSingletonVisibilityProof(
        targetPID: 42, targetWindowID: 24,
        targetBounds: popupVisibilityBounds, records: records
    )
}

@Test func popupSingletonCaptureRoleRequiresCompleteExplicitAXWindowSubrole() {
    let window = BoundedAXStringResult(value: "AXWindow", status: .complete)
    for subrole in ["AXDialog", "AXUnknown"] {
        #expect(popupSingletonAXRoleAllowed(
            role: window,
            subrole: BoundedAXStringResult(value: subrole, status: .complete)
        ))
    }

    for subrole in ["AXStandardWindow", "AXSystemDialog", "", nil] {
        #expect(!popupSingletonAXRoleAllowed(
            role: window,
            subrole: BoundedAXStringResult(value: subrole, status: .complete)
        ))
    }
    for status: BoundedAXStringStatus in [.failed, .truncated] {
        #expect(!popupSingletonAXRoleAllowed(
            role: window,
            subrole: BoundedAXStringResult(value: "AXUnknown", status: status)
        ))
        #expect(!popupSingletonAXRoleAllowed(
            role: BoundedAXStringResult(value: "AXWindow", status: status),
            subrole: BoundedAXStringResult(value: "AXUnknown", status: .complete)
        ))
    }
    for role in ["AXSheet", "AXGroup", "AXUnknown", "", nil] {
        #expect(!popupSingletonAXRoleAllowed(
            role: BoundedAXStringResult(value: role, status: .complete),
            subrole: BoundedAXStringResult(value: "AXUnknown", status: .complete)
        ))
    }
}

@Test func popupSingletonVisibilityAllowsContainingParentOnlyWhenBehind() {
    #expect(popupVisibility([popupVisibleTarget, popupVisibleParent]) == .proven)

    let frontParent = VisibleWindowRecord(
        pid: 42, windowID: 23, bounds: popupVisibleParent.bounds,
        layer: 4, alpha: 1, zOrder: 0
    )
    #expect(popupVisibility([popupVisibleTarget, frontParent]) == .intersectingWindowNotBehind)
}

@Test func popupSingletonVisibilityRejectsAnyPIDPartialCoverAndUnknownOrder() {
    let edgeCover = CGRect(
        x: popupVisibilityBounds.maxX - 10, y: popupVisibilityBounds.maxY - 10,
        width: 50, height: 50
    )
    let foreignFront = VisibleWindowRecord(
        pid: 99, windowID: 25, bounds: edgeCover,
        layer: 4, alpha: 1, zOrder: 0
    )
    #expect(popupVisibility([popupVisibleTarget, popupVisibleParent, foreignFront]) ==
        .intersectingWindowNotBehind)

    let unknownOrder = VisibleWindowRecord(
        pid: 99, windowID: 25, bounds: edgeCover,
        layer: 3, alpha: 1, zOrder: .max
    )
    #expect(popupVisibility([popupVisibleTarget, popupVisibleParent, unknownOrder]) ==
        .intersectingWindowNotBehind)
}

@Test func popupSingletonVisibilityDoesNotExemptTransparentFrontWindow() {
    let transparentFront = VisibleWindowRecord(
        pid: 99, windowID: 25,
        bounds: CGRect(x: 90, y: 71, width: 1, height: 1),
        layer: 4, alpha: 0, zOrder: 0
    )
    #expect(popupVisibility([popupVisibleTarget, popupVisibleParent, transparentFront]) ==
        .intersectingWindowNotBehind)
}

@Test func popupSingletonVisibilityUsesGlobalCoordinatesOnShiftedDisplay() {
    let shiftedBounds = popupVisibilityBounds.offsetBy(dx: -1500, dy: -300)
    let shiftedTarget = VisibleWindowRecord(
        pid: 42, windowID: 24, bounds: shiftedBounds,
        layer: 3, alpha: 1, zOrder: 1
    )
    let shiftedParent = VisibleWindowRecord(
        pid: 42, windowID: 23,
        bounds: popupVisibleParent.bounds.offsetBy(dx: -1500, dy: -300),
        layer: 0, alpha: 1, zOrder: 2
    )
    #expect(popupSingletonVisibilityProof(
        targetPID: 42, targetWindowID: 24, targetBounds: shiftedBounds,
        records: [shiftedTarget, shiftedParent]
    ) == .proven)
}

@Test func popupSingletonVisibilityFailsClosedWithoutExactCGTarget() {
    #expect(popupVisibility(nil) == .inventoryUnavailable)
    #expect(popupVisibility([popupVisibleParent]) == .targetNotUnique)
    #expect(popupVisibility([popupVisibleTarget, popupVisibleTarget]) == .targetNotUnique)

    let movedTarget = VisibleWindowRecord(
        pid: 42, windowID: 24,
        bounds: popupVisibilityBounds.offsetBy(dx: 1, dy: 0),
        layer: 3, alpha: 1, zOrder: 1
    )
    #expect(popupVisibility([movedTarget, popupVisibleParent]) == .targetIdentityMismatch)

    let invisibleTarget = VisibleWindowRecord(
        pid: 42, windowID: 24, bounds: popupVisibilityBounds,
        layer: 3, alpha: 0, zOrder: 1
    )
    #expect(popupVisibility([invisibleTarget, popupVisibleParent]) == .targetNotVisible)
}
