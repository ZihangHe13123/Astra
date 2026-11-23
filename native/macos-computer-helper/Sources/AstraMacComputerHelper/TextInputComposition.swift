@preconcurrency import ApplicationServices
import AppKit

/// Only the exact TextEdit editor supplies this witness. Values, text and offsets never leave the helper.
enum AXTextComposition {
    case unsupported, unavailable, unmarked, marked

    static func observe(_ element: ActionElement) -> Self {
        guard !element.isSecure, element.enabled == true,
              element.roleResult.value == kAXTextAreaRole as String, let ax = element.element
        else { return .unsupported }
        var pid: pid_t = 0
        guard AXUIElementGetPid(ax, &pid) == .success,
              let app = NSRunningApplication(processIdentifier: pid), app.bundleIdentifier == "com.apple.TextEdit",
              app.bundleURL?.standardizedFileURL.path == "/System/Applications/TextEdit.app"
        else { return .unsupported }
        var value: CFTypeRef?
        // AX wire name of NSAccessibilityTextInputMarkedRangeAttribute.
        let error = AXUIElementCopyAttributeValue(ax, "AXTextInputMarkedRange" as CFString, &value)
        return AXTextComposition.decode(error, value: value)
    }

    static func decode(_ error: AXError, value: CFTypeRef?) -> Self {
        guard error == .success, let value, CFGetTypeID(value) == AXValueGetTypeID() else { return .unavailable }
        let ax = unsafeBitCast(value, to: AXValue.self)
        var range = CFRange()
        guard AXValueGetType(ax) == .cfRange, AXValueGetValue(ax, .cfRange, &range),
              range.length >= 0, range.location >= 0 || range.location == kCFNotFound else { return .unavailable }
        if range.length == 0 { return .unmarked }
        guard range.location >= 0, range.location < Int.max - range.length else { return .unavailable }
        return .marked
    }
}
