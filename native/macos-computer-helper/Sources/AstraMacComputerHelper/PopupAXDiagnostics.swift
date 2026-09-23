@preconcurrency import ApplicationServices
import CoreGraphics
import Foundation

/// Read-only evidence for a bound dialog whose ordinary AX tree reports no
/// children. It never supplies an action reference or changes input authority.
enum PopupAXDiagnostics {
    struct ChildrenRead: Equatable {
        let countError: AXError
        let reportedCount: Int?
        let copyError: AXError?
        let copiedCount: Int?

        var summary: String {
            "count_error=\(countError.rawValue),reported=\(reportedCount.map(String.init) ?? "unread")"
                + ",copy_error=\(copyError.map { String($0.rawValue) } ?? "not_requested")"
                + ",copied=\(copiedCount.map(String.init) ?? "unread")"
        }
    }

    struct ElementRead: Equatable {
        let role: String
        let samePID: Bool?
        let inExactWindow: Bool?
        let actionsStatus: BoundedAXStringStatus?
        let actionCount: Int?
        let hasAXPress: Bool?

        var summary: String {
            "role=\(role),same_pid=\(flag(samePID)),in_window=\(flag(inExactWindow))"
                + ",actions_status=\(actionsStatus.map { String(describing: $0) } ?? "unread")"
                + ",actions_count=\(actionCount.map(String.init) ?? "unread")"
                + ",ax_press=\(flag(hasAXPress))"
        }

        private func flag(_ value: Bool?) -> String {
            value.map { $0 ? "true" : "false" } ?? "unread"
        }
    }

    static func shouldProbe(role: String, subrole: String?, directChildren: Int, exactWindow: Bool) -> Bool {
        exactWindow && directChildren == 0 &&
            ((role == "AXWindow" && subrole == "AXDialog") || role == "AXDialog")
    }

    static func recordIfNeeded(root: AXUIElement, tree: AXNode, windowBounds: CGRect) {
        guard !tree.childrenTruncated,
              shouldProbe(
                  role: tree.role, subrole: tree.subrole,
                  directChildren: tree.children.count,
                  exactWindow: true
              ),
              AXObservationBudget.current?.available == true,
              let rootBounds = AXNodeReader.frameAttribute(root),
              screenCaptureBoundsMatchAXBounds(
                  screenCapture: windowBounds, accessibility: rootBounds
              )
        else { return }

        var pid: pid_t = 0
        let pidError = observationAXCall(element: root, fallback: AXError.cannotComplete) {
            AXUIElementGetPid(root, &pid)
        }
        let targetPID: pid_t? = pidError == .success && pid > 0 ? pid : nil
        let children = readChildren(root, attribute: kAXChildrenAttribute)
        let visible = readChildren(root, attribute: "AXVisibleChildren")

        let focused: ElementRead?
        if let targetPID {
            let app = AXUIElementCreateApplication(targetPID)
            let (error, value) = observationAXAttribute(app, kAXFocusedUIElementAttribute)
            focused = error == .success && value != nil
                ? describe(decodeAXElement(value), targetPID: targetPID, window: root)
                : nil
        } else { focused = nil }

        let system = AXUIElementCreateSystemWide()
        var hit: AXUIElement?
        let hitError = observationAXCall(element: system, fallback: AXError.cannotComplete) {
            AXUIElementCopyElementAtPosition(
                system, Float(windowBounds.midX), Float(windowBounds.midY), &hit
            )
        }
        let hitElement: ElementRead?
        if hitError == .success, let targetPID {
            hitElement = describe(hit, targetPID: targetPID, window: root)
        } else { hitElement = nil }
        // One line per incomplete exact dialog snapshot. No title, label, value,
        // document text or menu item text is read into this diagnostic.
        logActionRejected(
            "POPUP-AX-DIAG pid=\(targetPID.map(String.init) ?? "unread")"
                + " tree_truncated=\(tree.childrenTruncated)"
                + " children={\(children.summary)} visible_children={\(visible.summary)}"
                + " focused={\(focused?.summary ?? "unread")}"
                + " hit_center_error=\(hitError.rawValue) hit_center={\(hitElement?.summary ?? "unread")}"
        )
    }

    private static func readChildren(_ element: AXUIElement, attribute: String) -> ChildrenRead {
        var reported: CFIndex = 0
        let countError = observationAXCall(element: element, fallback: AXError.cannotComplete) {
            AXUIElementGetAttributeValueCount(element, attribute as CFString, &reported)
        }
        guard countError == .success, reported >= 0 else {
            return ChildrenRead(countError: countError, reportedCount: nil, copyError: nil, copiedCount: nil)
        }
        guard reported > 0 else {
            return ChildrenRead(countError: countError, reportedCount: 0, copyError: nil, copiedCount: 0)
        }
        var values: CFArray?
        let copyError = observationAXCall(element: element, fallback: AXError.cannotComplete) {
            AXUIElementCopyAttributeValues(
                element, attribute as CFString, 0, min(reported, 32), &values
            )
        }
        return ChildrenRead(
            countError: countError, reportedCount: reported,
            copyError: copyError,
            copiedCount: copyError == .success ? values.map(CFArrayGetCount) : nil
        )
    }

    private static func describe(
        _ element: AXUIElement?, targetPID: pid_t, window: AXUIElement
    ) -> ElementRead? {
        guard let element else { return nil }
        var owner: pid_t = 0
        let pidError = observationAXCall(element: element, fallback: AXError.cannotComplete) {
            AXUIElementGetPid(element, &owner)
        }
        let samePID: Bool? = pidError == .success ? owner == targetPID : nil
        let roleRead = AXNodeReader.stringAttribute(element, kAXRoleAttribute)
        let candidateRole = roleRead.value
        let role = roleRead.status == .complete &&
            (candidateRole?.hasPrefix("AX") == true) && (candidateRole?.count ?? 0) <= 64
            ? candidateRole ?? "unread" : "unread"
        let actionNames = AXNodeReader.actionNameResults(element)
        let names = actionNames.completeNames
        return ElementRead(
            role: role,
            samePID: samePID,
            inExactWindow: samePID == true
                ? axSubtreeBelongsToWindow(element, window: window, pid: targetPID) : nil,
            actionsStatus: actionNames.status,
            actionCount: names?.count,
            hasAXPress: names?.contains(kAXPressAction as String)
        )
    }
}
