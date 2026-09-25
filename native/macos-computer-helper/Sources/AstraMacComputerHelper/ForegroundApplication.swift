import AppKit
import ApplicationServices

// NSWorkspace.frontmostApplication is updated by the main run loop. The CU
// helper serves synchronous stdin requests, so its cached value can still name
// yesterday's foreground app. Never use that cache as input-delivery authority.
func liveFrontmostPID() -> pid_t? {
    if let pid = accessibilityFrontmostPID() {
        frontmostFallbackEpisode.end()
        return pid
    }
    let pid = launchServicesFrontmostPID(candidates: onScreenWindowOwnerPIDs())
    frontmostFallbackEpisode.record(pid)
    return pid
}

/// One diagnostics line per stretch of failed system-wide queries, not one per poll.
private let frontmostFallbackEpisode = FrontmostFallbackEpisode()

private final class FrontmostFallbackEpisode {
    private let lock = NSLock()
    private var logged: pid_t??

    func record(_ pid: pid_t?) {
        lock.lock()
        defer { lock.unlock() }
        guard logged != .some(pid) else { return }
        logged = .some(pid)
        logActionRejected("FRONTMOST-FALLBACK launchServicesPID=\(pid.map(String.init) ?? "unknown")")
    }

    func end() {
        lock.lock()
        logged = nil
        lock.unlock()
    }
}

private func accessibilityFrontmostPID() -> pid_t? {
    let system = AXUIElementCreateSystemWide()
    guard AXUIElementSetMessagingTimeout(system, 0.2) == .success else { return nil }
    // Respect any enclosing observation deadline, but bypass its attribute
    // cache: focus is revalidated at every activation/input/cleanup boundary.
    return observationAXCall(element: system, fallback: nil as pid_t?) {
        var value: CFTypeRef?
        let error = AXUIElementCopyAttributeValue(
            system, kAXFocusedApplicationAttribute as CFString, &value
        )
        return focusedApplicationPID(error: error, value: value)
    }
}

func focusedApplicationPID(error: AXError, value: CFTypeRef?) -> pid_t? {
    guard error == .success, let application = decodeAXElement(value) else { return nil }
    var pid: pid_t = 0
    guard AXUIElementGetPid(application, &pid) == .success, pid > 0 else { return nil }
    return pid
}

/// The system-wide query above is answered by the front application itself, so
/// an application that does not serve it fails the query while plainly in front
/// (live: WeChat 4.1, a system alert, a process without an AppKit run loop).
/// LaunchServices still knows. A new NSRunningApplication reads it now, unlike
/// the workspace's instances; exactly one active window owner must answer.
func launchServicesFrontmostPID(
    candidates: [pid_t],
    isActive: (pid_t) -> Bool = { NSRunningApplication(processIdentifier: $0)?.isActive == true }
) -> pid_t? {
    let active = Set(candidates.filter { $0 > 0 }).filter(isActive)
    return active.count == 1 ? active.first : nil
}

func onScreenWindowOwnerPIDs() -> [pid_t] {
    let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] ?? []
    return windows.compactMap { ($0[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value }
}
