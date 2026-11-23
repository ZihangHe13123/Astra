@preconcurrency import ApplicationServices
import Foundation

// Keep feature negotiation inside the extensible AX tree so older protocol-v4
// clients still accept ordinary snapshots. New requests require these features.
func snapshotAXTreeJSON(_ tree: SerializedAXTree, subtree: Bool, standardSavePanel: JSONValue? = nil) -> JSONValue {
    guard case var .object(fields) = tree.asJSON() else { return tree.asJSON() }
    fields["observation_capabilities"] = .array([
        .string("wps_panel_ax_value_v1"), .string("save_panel_destination_v1"), .string("subtree_v1"), .string("checked_click_v1"), .string("auto_takeover_v1"),
        .string("replace_text_v1"), .string("replace_text_range_v1"), .string("focus_loan_v3"), .string("focus_loan_sheet_v1"),
        .string("focus_presentation_v2"), .string("focus_consent_v1"),
    ])
    if let standardSavePanel, !subtree { fields["standard_save_panel"] = standardSavePanel }
    fields["observation_scope"] = .string(subtree ? "native_subtree" : "window")
    return .object(fields)
}

func applicationMenuRole(_ role: String?) -> Bool {
    role == kAXMenuBarRole || role == kAXMenuBarItemRole || role == kAXMenuRole || role == kAXMenuItemRole
}

/// The app's menu bar is a sibling of its document windows. Menu commands act on
/// the current document, so both main and focused window must still be the selected one.
func applicationMenuDocumentMatch(_ element: AXUIElement, window: AXUIElement, pid: pid_t,
    appAttribute: (AXUIElement, String) -> AXUIElement? = { app, attribute in
        let (error, value) = observationAXAttribute(app, attribute)
        return error == .success ? decodeAXElement(value) : nil
    }, role: (AXUIElement) -> String? = { AXNodeReader.stringAttribute($0, kAXRoleAttribute).value },
    owner: (AXUIElement) -> pid_t? = observedAXPID, parent: (AXUIElement) -> AXUIElement? = observedAXParent
) -> Bool? {
    guard pid > 0, owner(window) == pid, let itemRole = role(element), applicationMenuRole(itemRole) else { return nil }
    let app = AXUIElementCreateApplication(pid)
    guard let bar = appAttribute(app, kAXMenuBarAttribute), role(bar) == kAXMenuBarRole,
          focusedElementBelongsToExactAXRoot(element: element, root: bar, pid: pid,
            ownerPID: owner, parent: parent, permitsDescendant: { applicationMenuRole(role($0)) })
    else {
        // Context menus can belong to the window directly; a menu-bar entry cannot.
        return itemRole == kAXMenuBarRole || itemRole == kAXMenuBarItemRole ? false : nil
    }
    guard let main = appAttribute(app, kAXMainWindowAttribute), CFEqual(main, window),
          let focused = appAttribute(app, kAXFocusedWindowAttribute), CFEqual(focused, window)
    else { return false }
    return true
}

func axSubtreeBelongsToWindow(_ element: AXUIElement, window: AXUIElement, pid: pid_t) -> Bool {
    var ownerPID: pid_t = 0
    guard AXUIElementGetPid(element, &ownerPID) == .success, ownerPID == pid else { return false }
    if CFEqual(element, window) { return true }
    let (error, ownerValue) = observationAXAttribute(element, kAXWindowAttribute)
    let explicitOwner = error == .success ? decodeAXElement(ownerValue) : nil
    if let explicitOwner, !CFEqual(explicitOwner, window) { return false }
    if let menuMatch = applicationMenuDocumentMatch(element, window: window, pid: pid) { return menuMatch }
    if explicitOwner != nil { return true }
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
