@preconcurrency import ApplicationServices
import Foundation

// Keep feature negotiation inside the extensible AX tree so older protocol-v4
// clients still accept ordinary snapshots. New requests require these features.
func snapshotAXTreeJSON(_ tree: SerializedAXTree, subtree: Bool) -> JSONValue {
    guard case var .object(fields) = tree.asJSON() else { return tree.asJSON() }
    fields["observation_capabilities"] = .array([.string("subtree_v1"), .string("checked_click_v1"), .string("auto_takeover_v1"), .string("replace_text_v1")])
    fields["observation_scope"] = .string(subtree ? "native_subtree" : "window")
    return .object(fields)
}

func axSubtreeBelongsToWindow(_ element: AXUIElement, window: AXUIElement, pid: pid_t) -> Bool {
    var ownerPID: pid_t = 0
    guard AXUIElementGetPid(element, &ownerPID) == .success, ownerPID == pid else { return false }
    if CFEqual(element, window) { return true }
    let (error, ownerValue) = observationAXAttribute(element, kAXWindowAttribute)
    if error == .success, let owner = decodeAXElement(ownerValue) { return CFEqual(owner, window) }
    var current = element
    var seen = Set<CFHashCode>()
    for _ in 0..<maximumAXTraversalDepth {
        guard seen.insert(CFHash(current)).inserted else { return false }
        let (error, parentValue) = observationAXAttribute(current, kAXParentAttribute)
        guard error == .success, let parent = decodeAXElement(parentValue) else { return false }
        if CFEqual(parent, window) { return true }
        current = parent
    }
    return false
}
