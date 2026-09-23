import AppKit
import ApplicationServices
import CoreGraphics

/// The only production global-event emitter. Constructed only after exact
/// foreground takeover consumption. Per-event target/window guards remain in
/// the executors; this final check also prevents delivery to an inactive PID.
final class CGForegroundInputPoster: PIDTargetedInputPosting {
    private let encoder: CGPIDTargetedInputPoster
    private let frontmostPID: () -> pid_t?
    private let pointIsInTargetWindow: (CGPoint, pid_t) -> Bool
    private let deliver: (CGEvent) -> Void
    private let now: () -> TimeInterval
    private let captureLock = NSLock()
    private var pointerCapture: (pid: pid_t, marker: UInt64)?

    init(
        preflightAccess: @escaping () -> Bool = { CGPreflightPostEventAccess() },
        frontmostPID: @escaping () -> pid_t? = { liveFrontmostPID() },
        pointIsInTargetWindow: @escaping (CGPoint, pid_t) -> Bool = { _, _ in false },
        deliver: @escaping (CGEvent) -> Void = { $0.post(tap: .cghidEventTap) },
        now: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }
    ) {
        self.frontmostPID = frontmostPID
        self.pointIsInTargetWindow = pointIsInTargetWindow
        self.deliver = deliver
        self.now = now
        encoder = CGPIDTargetedInputPoster(preflightAccess: preflightAccess, deliver: { event, _ in deliver(event) })
    }

    func preflight(targetPID: pid_t, marker: UInt64) -> Bool {
        frontmostPID() == targetPID && encoder.preflight(targetPID: targetPID, marker: marker)
    }

    func post(_ event: SyntheticInputEvent, to targetPID: pid_t, marker: UInt64) throws {
        try post(event, to: targetPID, marker: marker, deadline: .infinity)
    }

    func post(_ event: SyntheticInputEvent, to targetPID: pid_t, marker: UInt64, deadline: TimeInterval) throws {
        var inputStarted = false
        guard targetPID > 0, marker != 0 else {
            throw SyntheticInputFailure(error: .invalidAction, inputStarted: inputStarted)
        }
        // Up events are also used by HeldInputRegistry on interruption. They
        // must release the physical state even if focus moved after the down.
        let isRelease: Bool
        switch event {
        case .mouseUp, .unicodeKeyUp, .virtualKeyUp: isRelease = true
        default: isRelease = false
        }
        guard isRelease || frontmostPID() == targetPID else {
            throw SyntheticInputFailure(error: .targetNotFrontmost, inputStarted: inputStarted)
        }
        let point: CGPoint?
        switch event {
        case let .mouseDown(value, _, _), let .scroll(value, _, _): point = value
        default: point = nil
        }
        if case .mouseDragged = event {
            captureLock.lock()
            let captured = pointerCapture?.pid == targetPID && pointerCapture?.marker == marker
            captureLock.unlock()
            guard captured else { throw SyntheticInputFailure(error: .staleSnapshot, inputStarted: false) }
        }
        if let point {
            guard pointIsInTargetWindow(point, targetPID) else {
                throw SyntheticInputFailure(error: .staleSnapshot, inputStarted: inputStarted)
            }
            guard now() <= deadline else {
                throw SyntheticInputFailure(error: .actionTimeout, inputStarted: inputStarted)
            }
            guard frontmostPID() == targetPID else {
                throw SyntheticInputFailure(error: .targetNotFrontmost, inputStarted: inputStarted)
            }
            if case .scroll = event {
                // Wheel dispatch follows the real cursor, not merely the wheel
                // event's location field. Mark our move so it does not pause us.
                guard let movement = CGEvent(mouseEventSource: nil, mouseType: .mouseMoved,
                    mouseCursorPosition: point, mouseButton: .left) else {
                    throw SyntheticInputFailure(error: .helperFailed, inputStarted: inputStarted)
                }
                movement.setIntegerValueField(.eventSourceUserData, value: Int64(bitPattern: marker))
                deliver(movement)
                inputStarted = true
                guard frontmostPID() == targetPID, pointIsInTargetWindow(point, targetPID), now() <= deadline else {
                    throw SyntheticInputFailure(error: .staleSnapshot, inputStarted: true)
                }
            }
        }
        guard isRelease || now() <= deadline else {
            throw SyntheticInputFailure(error: .actionTimeout, inputStarted: inputStarted)
        }
        guard isRelease || frontmostPID() == targetPID else {
            throw SyntheticInputFailure(error: .targetNotFrontmost, inputStarted: inputStarted)
        }
        // macOS captures a drag to its original mouse-down window. Re-hit-testing
        // a moving titlebar can select a different surface between its frames.
        // The executor still checks exact identity, bounded translation, focus,
        // user activity, and deadline for every point on the sealed drag path.
        captureLock.lock()
        if case .mouseDown(_, _, .left) = event { pointerCapture = (targetPID, marker) }
        if case .mouseUp = event { pointerCapture = nil }
        captureLock.unlock()
        do {
            try encoder.post(event, to: targetPID, marker: marker)
        } catch let failure as SyntheticInputFailure {
            captureLock.lock()
            pointerCapture = nil
            captureLock.unlock()
            throw SyntheticInputFailure(error: failure.error, inputStarted: inputStarted || failure.inputStarted)
        }
    }
}

/// Internal authority, never an AX element reference supplied by the caller.
let foregroundWindowRegionReference = "__astra_foreground_window_region__"


/// Accessibility hit testing resolves click-through system surfaces (e.g. the
/// Dock's full-screen bookkeeping window) without exempting a whole app/layer.
/// Require the exact retained AX window, not just a matching foreground PID.
func foregroundPointBelongsToWindow(
    _ point: CGPoint, pid: pid_t, window: AXUIElement?,
    onFailure: ((PopupPointerProofFailureStage) -> Void)? = nil
) -> Bool {
    func reject(_ stage: PopupPointerProofFailureStage) -> Bool {
        onFailure?(stage)
        return false
    }
    guard point.x.isFinite, point.y.isFinite, let window else { return reject(.hitInvalidInput) }
    let system = AXUIElementCreateSystemWide()
    guard AXUIElementSetMessagingTimeout(system, 0.2) == .success else { return reject(.hitSystemTimeout) }
    var hit: AXUIElement?
    guard AXUIElementCopyElementAtPosition(system, Float(point.x), Float(point.y), &hit) == .success,
          var current = hit else { return reject(.hitRead) }
    var owner: pid_t = 0
    guard AXUIElementGetPid(current, &owner) == .success, owner == pid else { return reject(.hitPID) }
    for _ in 0..<12 {
        if CFEqual(current, window) { return true }
        guard AXUIElementSetMessagingTimeout(current, 0.2) == .success else {
            return reject(.hitAncestorTimeout)
        }
        var containing: CFTypeRef?
        if AXUIElementCopyAttributeValue(current, kAXWindowAttribute as CFString, &containing) == .success,
           let containing, CFGetTypeID(containing) == AXUIElementGetTypeID() {
            return CFEqual(containing, window) || reject(.hitWindowMismatch)
        }
        var parent: CFTypeRef?
        guard AXUIElementCopyAttributeValue(current, kAXParentAttribute as CFString, &parent) == .success,
              let parent, CFGetTypeID(parent) == AXUIElementGetTypeID() else { return reject(.hitParentRead) }
        current = unsafeBitCast(parent, to: AXUIElement.self)
    }
    return reject(.hitDepth)
}
