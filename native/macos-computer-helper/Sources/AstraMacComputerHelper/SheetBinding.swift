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

/// Capture the direct parent before delivery so a successor with a reused CG
/// number cannot stand in for the sheet's original parent.
func sheetDismissalWitness<Element>(
    sheet: Element, pid: pid_t, attachedParent: () -> Element?,
    same: @escaping (Element, Element) -> Bool,
    windowID: @escaping (Element) -> CGWindowID?,
    frontmostPID: @escaping () -> pid_t?,
    sheetIsInvalid: @escaping () -> Bool,
    currentSheets: @escaping (Element) -> [Element]?,
    focusedWindow: @escaping () -> Element?
) -> (() -> Bool)? {
    guard let parent = attachedParent(), let parentID = windowID(parent) else { return nil }
    return {
        guard frontmostPID() == pid,
              sheetIsInvalid() || currentSheets(parent).map({ sheets in
                  !sheets.contains(where: { same($0, sheet) })
              }) == true,
              let focused = focusedWindow(), same(focused, parent),
              windowID(focused) == parentID
        else { return false }
        return frontmostPID() == pid
    }
}

func sheetDismissalWitness(for sheet: AXUIElement, pid: pid_t) -> (() -> Bool)? {
    sheetDismissalWitness(sheet: sheet, pid: pid, attachedParent: {
        guard attachedSheetRoot(sheet, pid: pid) != nil,
              let parent = observedAXParent(sheet), observedAXPID(parent) == pid
        else { return nil }
        return parent
    }, same: { CFEqual($0, $1) }, windowID: observedAXWindowID, frontmostPID: liveFrontmostPID,
        sheetIsInvalid: { observationAXAttribute(sheet, kAXRoleAttribute).0 == .invalidUIElement },
        currentSheets: { completeAXElementArray($0, attribute: "AXSheets", maximum: maximumObservedWindows) },
        focusedWindow: {
            let (error, value) = observationAXAttribute(AXUIElementCreateApplication(pid), kAXFocusedWindowAttribute)
            guard error == .success, let focused = decodeAXElement(value), observedAXPID(focused) == pid
            else { return nil }
            return focusedAttachedSheet(in: focused, pid: pid) ?? focused
        })
}

func focusBelongsToExactAXRoot(app: AXUIElement, root: AXUIElement) -> Bool {
    guard let owner = observedAXPID(app) else { return false }
    let (error, value) = observationAXAttribute(app, kAXFocusedUIElementAttribute)
    guard error == .success, let focused = decodeAXElement(value) else { return false }
    return focusedElementBelongsToExactAXRoot(element: focused, root: root, pid: owner)
}

func focusedElementBelongsToExactAXRoot(element focused: AXUIElement, root: AXUIElement, pid owner: pid_t,
    ownerPID: (AXUIElement) -> pid_t? = observedAXPID, parent: (AXUIElement) -> AXUIElement? = observedAXParent,
    permitsDescendant: (AXUIElement) -> Bool = { _ in true }) -> Bool {
    guard ownerPID(root) == owner, ownerPID(focused) == owner else { return false }
    return CFEqual(focused, root) || focusedAncestorBranch(root: root, focused: focused,
        same: { CFEqual($0, $1) }, isOwned: { ownerPID($0) == owner && (CFEqual($0, root) || permitsDescendant($0)) },
        parent: parent) != nil
}

/// A sheet's field belongs to the sheet even when AX includes the document above it.
func fieldBelongsToNearestAXWindow(_ field: AXUIElement, root: AXUIElement, pid: pid_t,
    owner: (AXUIElement) -> pid_t? = observedAXPID, parent: (AXUIElement) -> AXUIElement? = observedAXParent,
    role: (AXUIElement) -> String? = {
        let r = AXNodeReader.stringAttribute($0, kAXRoleAttribute)
        return r.status == .complete ? r.value : nil
    }) -> Bool {
    guard let rootRole = role(root), rootRole == kAXWindowRole || rootRole == kAXSheetRole else { return false }
    return focusedElementBelongsToExactAXRoot(element: field, root: root, pid: pid, ownerPID: owner, parent: parent,
        permitsDescendant: { element in
            guard let r = role(element) else { return false }
            return r != kAXSheetRole && r != kAXWindowRole
        })
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
    let sheets = observedFocusedSheetChain(parents: parents, focused: focused, owner: owner,
                                          maximumSheets: maximumObservedWindows - windows.count)
    for sheet in sheets {
        if !result.contains(where: { CFEqual($0, sheet) }) { result.append(sheet) }
    }
    return result
}

/// Use the same exact AX ancestor and containment checks for both inventory
/// and activation when AXFocusedWindow still names a sheet's document.
func observedFocusedSheetChain(parents: [AXUIElement], focused: AXUIElement, owner: pid_t,
                               maximumSheets: Int = 8) -> [AXUIElement] {
    focusedSheetChain(windows: parents, focused: focused,
        maximumSheets: maximumSheets,
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
}

func focusedAttachedSheet(in window: AXUIElement, pid: pid_t) -> AXUIElement? {
    let (error, value) = observationAXAttribute(AXUIElementCreateApplication(pid), kAXFocusedUIElementAttribute)
    guard error == .success, let field = decodeAXElement(value) else { return nil }
    return observedFocusedSheetChain(parents: [window], focused: field, owner: pid).last
}
