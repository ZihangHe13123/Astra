@testable import AstraMacComputerHelperCore
@preconcurrency import ApplicationServices
import Testing

@Test func popupAXDiagnosticsOnlyProbeAnEmptyExactDialog() {
    #expect(PopupAXDiagnostics.shouldProbe(
        role: "AXWindow", subrole: "AXDialog", directChildren: 0, exactWindow: true
    ))
    #expect(PopupAXDiagnostics.shouldProbe(
        role: "AXDialog", subrole: nil, directChildren: 0, exactWindow: true
    ))
    #expect(!PopupAXDiagnostics.shouldProbe(
        role: "AXWindow", subrole: "AXStandardWindow", directChildren: 0, exactWindow: true
    ))
    #expect(!PopupAXDiagnostics.shouldProbe(
        role: "AXWindow", subrole: "AXDialog", directChildren: 1, exactWindow: true
    ))
    #expect(!PopupAXDiagnostics.shouldProbe(
        role: "AXWindow", subrole: "AXDialog", directChildren: 0, exactWindow: false
    ))
}

@Test func popupAXDiagnosticsDistinguishEmptyChildrenFromFailedReads() {
    let empty = PopupAXDiagnostics.ChildrenRead(
        countError: .success, reportedCount: 0, copyError: nil, copiedCount: 0
    )
    let failed = PopupAXDiagnostics.ChildrenRead(
        countError: .cannotComplete, reportedCount: nil, copyError: nil, copiedCount: nil
    )
    #expect(empty.summary.contains("reported=0"))
    #expect(failed.summary.contains("reported=unread"))
    #expect(empty.summary != failed.summary)
}

@Test func popupAXDiagnosticsExposePressCapabilityWithoutMenuText() {
    let hit = PopupAXDiagnostics.ElementRead(
        role: "AXMenuItem", samePID: true, inExactWindow: true,
        actionsStatus: .complete, actionCount: 1, hasAXPress: true
    )
    #expect(hit.summary.contains("role=AXMenuItem"))
    #expect(hit.summary.contains("same_pid=true,in_window=true"))
    #expect(hit.summary.contains("actions_count=1,ax_press=true"))
}
