import ApplicationServices
import Foundation

func validateReplacementField(expected: ActionElement, current: ActionElement?,
                              belongs: (AXUIElement) -> Bool) throws {
    guard let current, let ax = current.element, let retained = expected.element,
          CFEqual(ax, retained), current.identityToken == expected.identityToken,
          current.bounds == expected.bounds, current.roleResult == expected.roleResult,
          current.subroleResult == expected.subroleResult, current.enabled == true,
          !current.isSecure, current.supportsAXSelectedTextWrite, belongs(ax)
    else { throw ActionExecutionError.staleSnapshot }
}

struct TextReplacementResult {
    let inputStarted: Bool
    let effectVerification: ActionEffectVerification
    let observationRequired: Bool
}

protocol AXTextValueReplacing {
    func preflight(to element: AXUIElement) -> AXTextMutationPreflight
    func replace(_ text: String, to element: AXUIElement,
                 validateBeforeMutation: () throws -> Void) throws -> TextReplacementResult
}

/// Full-value replacement only. No keyboard or selected-text fallback is possible.
final class SystemAXTextValueReplacer: AXTextValueReplacing {
    private let clock: () -> TimeInterval
    private let setMessagingTimeout: (AXUIElement, Float) -> AXError
    private let isSettable: (AXUIElement) -> (AXError, Bool)
    private let copyValue: (AXUIElement) -> (AXError, CFTypeRef?)
    private let setValue: (AXUIElement, CFString) -> AXError
    private let budget: TimeInterval = 1

    init(
        clock: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
        setMessagingTimeout: @escaping (AXUIElement, Float) -> AXError = AXUIElementSetMessagingTimeout,
        isSettable: @escaping (AXUIElement) -> (AXError, Bool) = { element in
            var settable = DarwinBoolean(false)
            let error = AXUIElementIsAttributeSettable(element, kAXValueAttribute as CFString, &settable)
            return (error, settable.boolValue)
        },
        copyValue: @escaping (AXUIElement) -> (AXError, CFTypeRef?) = { element in
            var raw: CFTypeRef?
            let error = AXUIElementCopyAttributeValue(element, kAXValueAttribute as CFString, &raw)
            return (error, raw)
        },
        setValue: @escaping (AXUIElement, CFString) -> AXError = { element, text in
            AXUIElementSetAttributeValue(element, kAXValueAttribute as CFString, text)
        }
    ) {
        self.clock = clock
        self.setMessagingTimeout = setMessagingTimeout
        self.isSettable = isSettable
        self.copyValue = copyValue
        self.setValue = setValue
    }

    func preflight(to element: AXUIElement) -> AXTextMutationPreflight {
        do { return try preflight(to: element, deadline: clock() + budget) }
        catch let error as ActionExecutionError { return .failed(error) }
        catch { return .failed(.helperFailed) }
    }

    func replace(_ text: String, to element: AXUIElement,
                 validateBeforeMutation: () throws -> Void) throws -> TextReplacementResult {
        var inputStarted = false
        let deadline = clock() + budget
        do {
            guard text.unicodeScalars.count <= maximumNativeTextCharacters else {
                throw ActionExecutionError.invalidAction
            }
            try validateBeforeMutation()
            switch try preflight(to: element, deadline: deadline) {
            case .settable: break
            case .unsupported: throw ActionExecutionError.helperFailed
            case let .failed(error): throw error
            }
            let before = try read(element, deadline: deadline)
            try validateBeforeMutation()
            try checkDeadline(deadline)
            if let before, before.utf8.elementsEqual(text.utf8) {
                return TextReplacementResult(inputStarted: false, effectVerification: .noop, observationRequired: false)
            }
            try bounded(element, deadline: deadline) {
                try validateBeforeMutation()
                try self.checkDeadline(deadline)
                inputStarted = true // AX cannot prove that an error implies zero mutation.
                let error = self.setValue(element, text as CFString)
                guard error == .success else { throw mapAXActionError(error) }
            }
            let after = try read(element, deadline: deadline)
            try validateBeforeMutation()
            try checkDeadline(deadline)
            let verified = after.map { $0.utf8.elementsEqual(text.utf8) } ?? false
            return TextReplacementResult(inputStarted: true,
                effectVerification: verified ? .verified : .unverified, observationRequired: !verified)
        } catch let failure as ActionPerformFailure {
            throw ActionPerformFailure(error: failure.error, inputStarted: inputStarted || failure.inputStarted)
        } catch {
            throw ActionPerformFailure(error: (error as? ActionExecutionError) ?? .helperFailed, inputStarted: inputStarted)
        }
    }

    private func preflight(to element: AXUIElement, deadline: TimeInterval) throws -> AXTextMutationPreflight {
        try bounded(element, deadline: deadline) {
            let (error, settable) = self.isSettable(element)
            if error == .attributeUnsupported || error == .notImplemented { return .unsupported }
            guard error == .success else { return .failed(mapAXActionError(error)) }
            return settable ? .settable : .unsupported
        }
    }

    private func read(_ element: AXUIElement, deadline: TimeInterval) throws -> String? {
        try bounded(element, deadline: deadline) {
            let (error, raw) = self.copyValue(element)
            guard error == .success, let raw, CFGetTypeID(raw) == CFStringGetTypeID() else { return nil }
            let cf = unsafeBitCast(raw, to: CFString.self)
            let length = CFStringGetLength(cf)
            guard length <= maximumNativeTextCharacters * 2, let value = raw as? String,
                  value.unicodeScalars.count <= maximumNativeTextCharacters else { return nil }
            // Refuse lossy bridging of malformed UTF-16 as well as oversized values.
            var units = [UniChar](repeating: 0, count: length)
            CFStringGetCharacters(cf, CFRange(location: 0, length: length), &units)
            guard units.elementsEqual(value.utf16) else { return nil }
            return value
        }
    }

    private func checkDeadline(_ deadline: TimeInterval) throws {
        let now = clock()
        guard now.isFinite, deadline.isFinite, now < deadline else { throw ActionExecutionError.actionTimeout }
    }

    private func bounded<T>(_ element: AXUIElement, deadline: TimeInterval, _ operation: () throws -> T) throws -> T {
        try checkDeadline(deadline)
        let remaining = deadline - clock()
        var timeout = Float(remaining)
        if TimeInterval(timeout) > remaining { timeout = timeout.nextDown }
        guard timeout.isFinite, timeout > 0 else { throw ActionExecutionError.actionTimeout }
        guard setMessagingTimeout(element, timeout) == .success else { throw ActionExecutionError.helperFailed }
        let result: T
        do {
            try checkDeadline(deadline)
            result = try operation()
        } catch {
            _ = setMessagingTimeout(element, 0)
            throw error
        }
        guard setMessagingTimeout(element, 0) == .success else { throw ActionExecutionError.helperFailed }
        try checkDeadline(deadline)
        return result
    }
}
