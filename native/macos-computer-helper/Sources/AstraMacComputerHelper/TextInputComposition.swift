@preconcurrency import ApplicationServices
import AppKit

/// A composition witness belongs to one retained, eligible editing control. No text is read.
enum AXTextComposition {
    case unsupported, unavailable, unmarked, marked

    static func observe(_ element: ActionElement) -> Self {
        guard element.supportsAXSelectedTextWrite, element.acceptsFocusedKeyboardInput,
              element.enabled == true, let ax = element.element else { return .unsupported }
        var pid: pid_t = 0
        guard AXUIElementGetPid(ax, &pid) == .success,
              let app = NSRunningApplication(processIdentifier: pid), !app.isTerminated else { return .unsupported }
        var value: CFTypeRef?
        // AX wire name of NSAccessibilityTextInputMarkedRangeAttribute.
        let error = AXUIElementCopyAttributeValue(ax, "AXTextInputMarkedRange" as CFString, &value)
        return AXTextComposition.decode(error, value: value)
    }

    static func decode(_ error: AXError, value: CFTypeRef?) -> Self {
        if error == .attributeUnsupported { return .unsupported }
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
