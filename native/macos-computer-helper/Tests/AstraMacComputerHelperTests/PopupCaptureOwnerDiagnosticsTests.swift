@testable import AstraMacComputerHelperCore
import CoreGraphics
import Testing

@Test func popupCaptureOwnerCandidatesRequireSamePIDAndStrictContainment() {
    let popup = PopupCaptureOwnerDiagnostics.Window(
        pid: 42, windowID: 1, frame: CGRect(x: 100, y: 100, width: 300, height: 150)
    )
    let parent = PopupCaptureOwnerDiagnostics.Window(
        pid: 42, windowID: 2, frame: CGRect(x: 50, y: 50, width: 600, height: 400)
    )
    let foreign = PopupCaptureOwnerDiagnostics.Window(
        pid: 43, windowID: 3, frame: parent.frame
    )
    let sameSize = PopupCaptureOwnerDiagnostics.Window(
        pid: 42, windowID: 4, frame: popup.frame
    )
    let overlapping = PopupCaptureOwnerDiagnostics.Window(
        pid: 42, windowID: 5, frame: CGRect(x: 200, y: 50, width: 600, height: 400)
    )
    #expect(PopupCaptureOwnerDiagnostics.containingCandidates(
        target: popup, windows: [popup, parent, foreign, sameSize, overlapping]
    ) == [parent])
}

@Test func popupCaptureOwnerCandidatesPreserveAmbiguousParents() {
    let popup = PopupCaptureOwnerDiagnostics.Window(
        pid: 42, windowID: 1, frame: CGRect(x: -100, y: 20, width: 300, height: 150)
    )
    let first = PopupCaptureOwnerDiagnostics.Window(
        pid: 42, windowID: 2, frame: CGRect(x: -200, y: -20, width: 800, height: 500)
    )
    let second = PopupCaptureOwnerDiagnostics.Window(
        pid: 42, windowID: 3, frame: first.frame
    )
    #expect(PopupCaptureOwnerDiagnostics.containingCandidates(
        target: popup, windows: [first, second]
    ) == [first, second])
}

@Test func popupCaptureParentChainStopsAtApplicationWithoutGuessing() {
    let parents = [1: 2, 2: 3, 3: 4]
    let chain = PopupCaptureOwnerDiagnostics.parentChain(
        from: 1, ownerPID: 42,
        same: ==, pid: { _ in 42 }, parent: { parents[$0] },
        isApplication: { $0 == 4 }
    )
    #expect(chain.parents == [2, 3, 4])
    #expect(chain.stop == .application)
}

@Test func popupCaptureParentChainRejectsForeignAndCyclicAncestry() {
    let foreign = PopupCaptureOwnerDiagnostics.parentChain(
        from: 1, ownerPID: 42,
        same: ==, pid: { $0 == 3 ? 43 : 42 },
        parent: { [1: 2, 2: 3][$0] }, isApplication: { _ in false }
    )
    #expect(foreign.parents == [2])
    #expect(foreign.stop == .foreignPID)
    let cyclic = PopupCaptureOwnerDiagnostics.parentChain(
        from: 1, ownerPID: 42,
        same: ==, pid: { _ in 42 },
        parent: { [1: 2, 2: 1][$0] }, isApplication: { _ in false }
    )
    #expect(cyclic.parents == [2])
    #expect(cyclic.stop == .cycle)
}

@Test func popupCaptureParentChainHasHardEightLevelLimit() {
    let chain = PopupCaptureOwnerDiagnostics.parentChain(
        from: 1, ownerPID: 42, maximumDepth: 100,
        same: ==, pid: { _ in 42 }, parent: { $0 + 1 },
        isApplication: { _ in false }
    )
    #expect(chain.parents == Array(2...9))
    #expect(chain.stop == .depthLimit)
}

@Test func popupCaptureOwnerMatchReportsItsEvidenceSource() {
    let frame = CGRect(x: -200, y: 30, width: 800, height: 500)
    let parent = PopupCaptureOwnerDiagnostics.Window(pid: 42, windowID: 2, frame: frame)
    let screen = PIDScreenCaptureWindowObservation(
        pid: 42, windowID: 2, bounds: frame, title: "Parent", isOnScreen: true
    )
    let wrongTitle = BoundedAXStringResult(value: "Different", status: .complete)
    let exact = PopupCaptureOwnerDiagnostics.matchedCandidate(
        pid: 42, frame: frame, title: wrongTitle,
        axWindowID: 2, screenWindows: [screen], candidates: [parent]
    )
    #expect(exact?.kind == .exactWindowID)
    let metadata = PopupCaptureOwnerDiagnostics.matchedCandidate(
        pid: 42, frame: frame,
        title: BoundedAXStringResult(value: "Parent", status: .complete),
        axWindowID: nil, screenWindows: [screen], candidates: [parent]
    )
    #expect(metadata?.kind == .metadataTitleFrame)
    #expect(PopupCaptureOwnerDiagnostics.matchedCandidate(
        pid: 42, frame: frame, title: wrongTitle,
        axWindowID: nil, screenWindows: [screen], candidates: [parent]
    ) == nil)
}

@Test func popupCaptureOwnerEvidenceRemainsInconclusiveForAmbiguityOrPartialReads() {
    let match = PopupCaptureOwnerDiagnostics.CandidateMatch(windowID: 2, kind: .metadataTitleFrame)
    #expect(PopupCaptureOwnerDiagnostics.ownerEvidence(
        candidateCount: 1, matches: [match], chainStop: .application,
        budgetFailed: false, budgetExpired: false
    ) == "positive_ax_parent")
    #expect(PopupCaptureOwnerDiagnostics.ownerEvidence(
        candidateCount: 2, matches: [match], chainStop: .application,
        budgetFailed: false, budgetExpired: false
    ) == "inconclusive")
    #expect(PopupCaptureOwnerDiagnostics.ownerEvidence(
        candidateCount: 1, matches: [match], chainStop: .missingParent,
        budgetFailed: false, budgetExpired: false
    ) == "inconclusive")
    #expect(PopupCaptureOwnerDiagnostics.ownerEvidence(
        candidateCount: 1, matches: [match], chainStop: .application,
        budgetFailed: false, budgetExpired: true
    ) == "inconclusive")
}
