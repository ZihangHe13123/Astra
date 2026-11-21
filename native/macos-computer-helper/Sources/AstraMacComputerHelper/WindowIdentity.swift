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
    // A sheet may report its parent's ID. Keep the independently proven sheet
    // ownership/focus path; this bridge is only for actual AXWindow objects.
    let role = AXNodeReader.stringAttribute(element, kAXRoleAttribute)
    guard role.status == .complete, role.value == kAXWindowRole else { return nil }
    return observedAXBackingWindowID(element)
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
