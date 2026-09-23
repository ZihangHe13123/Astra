import ApplicationServices
import Foundation

func focusedSheetCandidate<Element>(
    windows: [Element], focused: Element,
    same: (Element, Element) -> Bool, isOwned: (Element) -> Bool,
    parent: (Element) -> Element?, isSheet: (Element) -> Bool,
    contained: (Element, Element) -> Bool
) -> Element? {
    let matches = windows.compactMap { window -> Element? in
        guard let child = focusedAncestorBranch(root: window, focused: focused,
            same: same, isOwned: isOwned, parent: parent),
            isSheet(child), contained(window, child) else { return nil }
        return child
    }
    return matches.count == 1 ? matches[0] : nil
}

func focusedSheetChain<Element>(
    windows: [Element], focused: Element, maximumSheets: Int = 8,
    same: (Element, Element) -> Bool, isOwned: (Element) -> Bool,
    parent: (Element) -> Element?, isSheet: (Element) -> Bool,
    contained: (Element, Element) -> Bool
) -> [Element] {
    var result: [Element] = []
    var parents = windows
    for _ in 0..<max(0, min(maximumSheets, 8)) {
        guard let sheet = focusedSheetCandidate(windows: parents, focused: focused,
            same: same, isOwned: isOwned, parent: parent, isSheet: isSheet, contained: contained),
            !result.contains(where: { same($0, sheet) }) else { break }
        result.append(sheet)
        parents = [sheet]
    }
    return result
}

func observedAXPID(_ element: AXUIElement) -> pid_t? {
    observationAXCall(element: element, fallback: nil as pid_t?) {
        var pid: pid_t = 0
        return AXUIElementGetPid(element, &pid) == .success && pid > 0 ? pid : nil
    }
}

func observedAXParent(_ element: AXUIElement) -> AXUIElement? {
    let (error, value) = observationAXAttribute(element, kAXParentAttribute)
    return error == .success ? decodeAXElement(value) : nil
}

func focusBelongsToExactAXRoot(app: AXUIElement, root: AXUIElement) -> Bool {
    guard let owner = observedAXPID(app) else { return false }
    let (error, value) = observationAXAttribute(app, kAXFocusedUIElementAttribute)
    guard error == .success, let focused = decodeAXElement(value) else { return false }
    return focusedElementBelongsToExactAXRoot(element: focused, root: root, pid: owner)
}

func focusedElementBelongsToExactAXRoot(element focused: AXUIElement, root: AXUIElement, pid owner: pid_t) -> Bool {
    guard observedAXPID(root) == owner, observedAXPID(focused) == owner else { return false }
    return CFEqual(focused, root) || focusedAncestorBranch(root: root, focused: focused,
        same: { CFEqual($0, $1) }, isOwned: { observedAXPID($0) == owner },
        parent: observedAXParent) != nil
}

// Reconstruct sheet membership on every inventory; focus loss removes the candidate.
// CG matching and stored AX identity checks remain the caller's responsibility.
/// A read that fails when its source changes under it: an app's window list is counted, then
/// copied, and a tooltip, popup or tab opening in between fails the copy (live Edge and Outlook).
/// Read again after a short pause before failing closed.
func retryingTransientRead<Value>(
    attempts: Int = 3, pause: () -> Void = { usleep(50_000) }, _ read: () -> Value?
) -> Value? {
    for attempt in 0..<max(1, attempts) {
        if let value = read() { return value }
        if attempt + 1 < attempts { pause() }
    }
    return nil
}

func completeObservedAXWindows(_ app: AXUIElement) -> [AXUIElement]? {
    guard let windows = retryingTransientRead({
        completeAXElementArray(app, attribute: kAXWindowsAttribute, maximum: maximumObservedWindows)
    }) else { return nil }
    guard windows.count < maximumObservedWindows, let owner = observedAXPID(app) else { return windows }
    let (error, value) = observationAXAttribute(app, kAXFocusedUIElementAttribute)
    guard error == .success, let focused = decodeAXElement(value) else { return windows }
    var result = windows
    // Recover a chain, never an arbitrary descendant. Each level must be a
    // direct owned sheet of the uniquely proven previous window/sheet.
    let parents = windows.filter {
        let role = AXNodeReader.stringAttribute($0, kAXRoleAttribute)
        return role.status == .complete && role.value == kAXWindowRole
    }
    let sheets = focusedSheetChain(windows: parents, focused: focused,
        maximumSheets: maximumObservedWindows - windows.count,
        same: { CFEqual($0, $1) }, isOwned: { observedAXPID($0) == owner },
        parent: observedAXParent,
        isSheet: {
            let role = AXNodeReader.stringAttribute($0, kAXRoleAttribute)
            return role.status == .complete && role.value == kAXSheetRole
        },
        contained: { window, child in
            guard let outer = AXNodeReader.frameAttribute(window),
                  let inner = AXNodeReader.frameAttribute(child) else { return false }
            return trustedFocusedWindow(expectedIdentity: 1, targetBounds: outer,
                focusedIdentity: 2, focusedBounds: inner)
        })
    for sheet in sheets {
        if !result.contains(where: { CFEqual($0, sheet) }) { result.append(sheet) }
    }
    return result
}
