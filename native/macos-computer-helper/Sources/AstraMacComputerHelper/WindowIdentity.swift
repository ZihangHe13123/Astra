import ApplicationServices
import Darwin
import Foundation

// macOS has no public AX-to-CG window identity bridge. Resolve this optional
// read-only SPI at runtime, as window managers do; an unavailable symbol must
// leave the existing unique metadata match in place, never guess an identity.
private typealias AXWindowIDFunction = @convention(c) (AXUIElement, UnsafeMutablePointer<CGWindowID>) -> AXError
private let axWindowIDFunction: AXWindowIDFunction? = {
    guard let symbol = dlsym(UnsafeMutableRawPointer(bitPattern: -2), "_AXUIElementGetWindow") else { return nil }
    return unsafeBitCast(symbol, to: AXWindowIDFunction.self)
}()

func observedAXWindowID(_ element: AXUIElement) -> CGWindowID? {
    let role = AXNodeReader.stringAttribute(element, kAXRoleAttribute)
    guard role.status == .complete else { return nil }
    if role.value == kAXWindowRole { return observedAXBackingWindowID(element) }
    // AppKit sometimes assigns a sheet its own CG surface while leaving AXTitle empty.
    // A composited sheet can instead return an ancestor's number.
    guard role.value == kAXSheetRole, let pid = observedAXPID(element),
          let id = observedAXBackingWindowID(element),
          attachedSheetRoot(element, pid: pid, excludingBackingID: id) != nil
    else { return nil }
    return id
}

// Use a sheet's raw backing surface only with independent exact AX focus/attachment proof.
// A composited sheet can return its parent window number, not a unique sheet window number.
func observedAXBackingWindowID(_ element: AXUIElement) -> CGWindowID? {
    guard let read = axWindowIDFunction else { return nil }
    return observationAXCall(element: element, fallback: nil as CGWindowID?) {
        var id: CGWindowID = 0
        return read(element, &id) == .success && id != 0 ? id : nil
    }
}

/// A relation between two current observations, distinct from an AX parent or a read grant.
/// Both native objects must resolve to exact stable identities from the same live catalog.
func focusedDialogDocumentRelation(
    pid: pid_t,
    catalogIdentity: (AXUIElement) -> String?,
    appWindow: (AXUIElement, String) -> AXUIElement? = { app, key in
        var value: CFTypeRef?
        guard observationAXCall(element: app, fallback: AXError.cannotComplete, {
            AXUIElementCopyAttributeValue(app, key as CFString, &value)
        }) == .success else { return nil }
        return decodeAXElement(value)
    },
    windowSubrole: (AXUIElement) -> String? = { element in
        let role = AXNodeReader.stringAttribute(element, kAXRoleAttribute)
        guard role.status == .complete, role.value == kAXWindowRole as String else { return nil }
        let subrole = AXNodeReader.stringAttribute(element, kAXSubroleAttribute)
        return subrole.status == .complete ? subrole.value : nil
    }
) -> (dialog: String, document: String)? {
    let app = AXUIElementCreateApplication(pid)
    guard let dialog = appWindow(app, kAXFocusedWindowAttribute),
          windowSubrole(dialog) == kAXDialogSubrole as String,
          let main = appWindow(app, kAXMainWindowAttribute), !CFEqual(dialog, main),
          windowSubrole(main) == kAXStandardWindowSubrole as String,
          let dialogRef = catalogIdentity(dialog), let mainRef = catalogIdentity(main), dialogRef != mainRef,
          let stillMain = appWindow(app, kAXMainWindowAttribute), CFEqual(main, stillMain),
          let stillFocused = appWindow(app, kAXFocusedWindowAttribute), CFEqual(dialog, stillFocused)
    else { return nil }
    return (dialogRef, mainRef)
}

func matchingScreenWindow(
    pid: pid_t, bounds: CGRect, title: BoundedAXStringResult,
    windowID: CGWindowID?, windows: [PIDScreenCaptureWindowObservation]
) -> PIDScreenCaptureWindowObservation? {
    let owned = windows.filter { $0.pid == pid && $0.isOnScreen }
    if let windowID {
        // An explicit identity mismatch cannot fall back to a sibling's title.
        let exact = owned.filter { $0.windowID == windowID }
        guard windowID != 0, exact.count == 1,
              screenCaptureBoundsMatchAXBounds(screenCapture: exact[0].bounds, accessibility: bounds)
        else { return nil }
        return exact[0]
    }
    guard title.status == .complete else { return nil }
    let matches = owned.filter {
        screenCaptureBoundsMatchAXBounds(screenCapture: $0.bounds, accessibility: bounds) &&
            windowTitlesMatch(screenCaptureTitle: $0.title ?? "", accessibilityTitle: title.value ?? "")
    }
    return matches.count == 1 ? matches[0] : nil
}

// File panels can expose their visible name as AXDescription instead of AXTitle.
// This is matching metadata, never a replacement for PID, geometry, uniqueness,
// retained AX identity or current-focus validation.
func accessibilityWindowName(
    _ element: AXUIElement,
    read: (AXUIElement, String) -> BoundedAXStringResult = AXNodeReader.stringAttribute
) -> BoundedAXStringResult {
    let title = read(element, kAXTitleAttribute)
    guard title.status == .complete,
          title.value?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty != false
    else { return title }
    let role = read(element, kAXRoleAttribute)
    guard role.status == .complete, role.value == kAXSheetRole else { return title }
    let description = read(element, kAXDescriptionAttribute)
    guard description.status == .complete else { return description }
    return description.value?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false
        ? description : title
}
