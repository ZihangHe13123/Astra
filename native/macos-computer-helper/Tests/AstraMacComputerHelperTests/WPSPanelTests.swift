@testable import AstraMacComputerHelperCore
import ApplicationServices
import Foundation
import Testing

private func node(_ index: Int, _ role: String, title: String? = nil, value: String? = nil,
                  children: [SerializedAXNode] = []) -> SerializedAXNode {
    SerializedAXNode(index: index, role: role, subrole: role == "AXWindow" ? "AXDialog" : nil, label: nil,
        title: title, help: nil, value: value, enabled: true, focused: false, actions: [], bounds: .zero,
        elementRef: ElementReference(snapshotID: "snapshot", ordinal: index), children: children,
        sourceElement: AXUIElementCreateApplication(pid_t(10_000 + index)))
}

@Test func wpsPanelSnapshotUsesDistinctLabelledFieldsAndFailsClosedOnAmbiguity() throws {
    for lastLabel in ["替换为(I)", "查找内容(N)"] {
        let first = node(3, "AXTextField"), second = node(6, "AXTextField")
        let group = node(7, "AXGroup", children: [
            node(1, "AXStaticText", value: "查找内容(N)"), node(2, "AXPopUpButton", children: [first]),
            node(4, "AXStaticText", value: lastLabel), node(5, "AXPopUpButton", children: [second])
        ])
        let root = node(8, "AXWindow", title: "查找和替换", children: [group])
        let target = WindowTarget(appRef: "app", windowRef: "dialog", pid: 42, windowID: 77,
                                  bounds: .zero, title: "查找和替换", axElement: root.sourceElement)
        let tree = SerializedAXTree(root: root, nodeCount: 8, jsonByteCount: 1000)
        let snapshot = try #require(WPSPanelSnapshot.capture(tree: tree, target: target, front: 18,
                                                             identifier: { _ in nil }, readValue: { _ in "" }))
        #expect(snapshot.boundField(reference: first.elementRef.wireValue) == (lastLabel == "替换为(I)" ? .find : nil))
        #expect(snapshot.boundField(reference: second.elementRef.wireValue) == (lastLabel == "替换为(I)" ? .replacement : nil))
        #expect(throws: ActionExecutionError.staleSnapshot) { try snapshot.validateFront(42) }
        #expect(throws: ActionExecutionError.staleSnapshot) { try snapshot.validateFront(19) }
        try snapshot.validateFront(18)
        #expect(WPSPanelSnapshot.capture(tree: tree, target: target, front: 42, identifier: { _ in nil }, readValue: { _ in "" }) == nil)
        let changed = try #require(WPSPanelSnapshot.capture(tree: tree, target: target, front: 18,
                                                            identifier: { _ in nil }, readValue: { _ in "new value" }))
        #expect(changed.boundField(reference: first.elementRef.wireValue) == nil)
    }
}

@Test func wpsPanelRetainedControlCannotMoveOrBeReplacedAtItsOldPosition() {
    let group = AXUIElementCreateApplication(20), first = AXUIElementCreateApplication(21)
    let desired = AXUIElementCreateApplication(22), other = AXUIElementCreateApplication(23)
    let path = WPSPanelPath(edges: [.init(parent: group, index: 1, child: desired)])
    #expect(path.matches { _ in [first, desired] })
    #expect(!path.matches { _ in [desired, first] })
    #expect(!path.matches { _ in [first, other] })
    #expect(!path.matches { _ in [desired, desired] })
}

@Test func wpsPanelRegistryDoesNotEnableOtherApplicationsVersionsOrSyntheticDelivery() {
    let bytes = Data(#"{"schema_version":2,"applications":[{"bundle_id":"com.kingsoft.wpsoffice.mac","app_version":"12.1.26026","capabilities":[{"backend":"wps_panel_ax","enabled_actions":["text","press"]}]}]}"#.utf8)
    let registry = PIDInputCompatibilityRegistry(bytes: bytes)
    let wps = PIDTargetApplication(bundleIdentifier: "com.kingsoft.wpsoffice.mac", version: "12.1.26026")
    #expect(registry.allows(application: wps, backend: .axSelectedText, action: .text))
    #expect(registry.allows(application: wps, backend: .axPress, action: .press))
    #expect(!registry.allowsTextEntry(application: wps))
    for app in [PIDTargetApplication(bundleIdentifier: wps.bundleIdentifier, version: "12.1.26027"),
                PIDTargetApplication(bundleIdentifier: "com.apple.TextEdit", version: wps.version)] {
        #expect(!registry.allows(application: app, backend: .axSelectedText, action: .text))
    }
}
