import ApplicationServices
import Foundation

/// AXEnabled is optional on a standard NSTextView (including TextEdit). A failed read is
/// still unknown. Only an unsupported attribute can be supplemented with live evidence.
func actionTextAreaEnabled(_ element: AXUIElement) -> Bool? {
    let (error, raw) = observationAXAttribute(element, kAXEnabledAttribute)
    return actionTextAreaEnabled(error: error, value: raw as? Bool, standardTextArea: {
        let role = AXNodeReader.stringAttribute(element, kAXRoleAttribute)
        guard role.status == .complete, role.value == kAXTextAreaRole as String else { return false }
        let subrole = AXNodeReader.stringAttribute(element, kAXSubroleAttribute)
        return subrole.status == .complete && subrole.value == nil
    }, exactFocus: {
        guard AXNodeReader.boolAttribute(element, kAXFocusedAttribute) == true else { return false }
        var pid = pid_t()
        guard AXUIElementGetPid(element, &pid) == .success, pid > 0 else { return false }
        let (status, focused) = observationAXAttribute(AXUIElementCreateApplication(pid), kAXFocusedUIElementAttribute)
        guard status == .success, let live = decodeAXElement(focused) else { return false }
        return CFEqual(live, element)
    }, writableSelection: {
        let io = AXSelectionIO()
        return [kAXSelectedTextRangeAttribute, kAXSelectedTextAttribute].allSatisfy { attribute in
            let (status, writable) = io.settable(element, attribute as CFString)
            return status == .success && writable
        }
    })
}

func actionTextAreaEnabled(error: AXError, value: Bool?, standardTextArea: () -> Bool,
                           exactFocus: () -> Bool, writableSelection: () -> Bool) -> Bool? {
    if error == .success { return value }
    guard error == .attributeUnsupported, standardTextArea(), exactFocus(), writableSelection() else { return nil }
    return true
}

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

struct AXTextMatch: Equatable {
    let target: String
    let prefix: String?
    let suffix: String?

    /// AppKit accessibility text ranges count UTF-16 units. Literal matching keeps
    /// normalization and two otherwise identical occurrences distinct.
    func uniqueRange(in value: String) throws -> NSRange {
        guard !target.isEmpty else { throw ActionExecutionError.invalidAction }
        let source = value as NSString
        let pattern = (prefix ?? "") + target + (suffix ?? "")
        let search = NSRange(location: 0, length: source.length)
        let first = source.range(of: pattern, options: .literal, range: search)
        guard first.location != NSNotFound else { throw ActionExecutionError.textMatchNotFound }
        // Start one UTF-16 unit beyond the match's start to detect overlapping matches.
        let next = first.location + 1
        guard source.range(of: pattern, options: .literal,
                           range: NSRange(location: next, length: source.length - next)).location == NSNotFound
        else { throw ActionExecutionError.textMatchAmbiguous }
        let range = NSRange(location: first.location + (prefix?.utf16.count ?? 0), length: target.utf16.count)
        guard Range(range, in: value) != nil else { throw ActionExecutionError.textRangeUnsupported }
        return range
    }
}

/// Read-only witnesses for a single foreground Unicode event. No AX selection or text setter
/// runs on this path; the application's own Find UI can select the requested text beforehand.
struct AXKeyboardRangeWitness {
    let beforeInput: () throws -> Void
    let afterInput: () throws -> Bool
}

protocol AXTextValueReplacing {
    func keyboardWitness(for match: AXTextMatch, replacingWith text: String, in element: AXUIElement) throws -> AXKeyboardRangeWitness
    func preflightRange(to element: AXUIElement, matching match: AXTextMatch) -> AXTextMutationPreflight
    func replaceRange(_ match: AXTextMatch, with text: String, in element: AXUIElement,
                      validateBeforeMutation: () throws -> Void) throws -> TextReplacementResult
    func preflight(to element: AXUIElement) -> AXTextMutationPreflight
    func replace(_ text: String, to element: AXUIElement,
                 validateBeforeMutation: () throws -> Void) throws -> TextReplacementResult
}

extension AXTextValueReplacing {
    func keyboardWitness(for _: AXTextMatch, replacingWith _: String, in _: AXUIElement) throws -> AXKeyboardRangeWitness {
        throw ActionExecutionError.textRangeUnsupported
    }

    func preflightRange(to _: AXUIElement, matching _: AXTextMatch) -> AXTextMutationPreflight { .unsupported }
    func replaceRange(_: AXTextMatch, with _: String, in _: AXUIElement,
                      validateBeforeMutation _: () throws -> Void) throws -> TextReplacementResult {
        throw ActionPerformFailure(error: .invalidAction, inputStarted: false)
    }
}

/// Readback-verified AX edits. Full-value and selected-range writes stay separate.
final class SystemAXTextValueReplacer: AXTextValueReplacing {
    private let clock: () -> TimeInterval
    private let setMessagingTimeout: (AXUIElement, Float) -> AXError
    private let isSettable: (AXUIElement) -> (AXError, Bool)
    private let copyValue: (AXUIElement) -> (AXError, CFTypeRef?)
    private let setValue: (AXUIElement, CFString) -> AXError
    private let rangeIO: AXSelectionIO
    private let budget: TimeInterval = 1

    init(
        rangeIO: AXSelectionIO = AXSelectionIO(),
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
        self.rangeIO = rangeIO
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
        try replace(text, to: element, expectedValue: nil, validateBeforeMutation: validateBeforeMutation)
    }

    func replace(_ text: String, to element: AXUIElement, expectedValue: String?,
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
            if let expectedValue, before?.utf8.elementsEqual(expectedValue.utf8) != true { throw ActionExecutionError.staleSnapshot }
            try validateBeforeMutation()
            try checkDeadline(deadline)
            if let before, before.utf8.elementsEqual(text.utf8) {
                return TextReplacementResult(inputStarted: false, effectVerification: .noop, observationRequired: false)
            }
            try bounded(element, deadline: deadline) {
                try validateBeforeMutation()
                try self.checkDeadline(deadline)
                if let expectedValue {
                    guard try read(element, deadline: deadline)?.utf8.elementsEqual(expectedValue.utf8) == true else {
                        throw ActionExecutionError.staleSnapshot
                    }
                    try validateBeforeMutation()
                }
                inputStarted = true // AX cannot prove that an error implies zero mutation.
                let error = self.setValue(element, text as CFString)
                guard error == .success else { throw mapAXActionError(error) }
            }
            let after = try read(element, deadline: deadline)
            try validateBeforeMutation()
            try checkDeadline(deadline)
            let verified = after.map { $0.utf8.elementsEqual(text.utf8) } ?? false
            if expectedValue != nil && !verified { throw ActionExecutionError.unknownOutcome }
            return TextReplacementResult(inputStarted: true,
                effectVerification: verified ? .verified : .unverified, observationRequired: !verified)
        } catch let failure as ActionPerformFailure {
            throw ActionPerformFailure(error: failure.error, inputStarted: inputStarted || failure.inputStarted)
        } catch let pause as UserActivityMonitoringError {
            if inputStarted { throw ActionPerformFailure(error: .unknownOutcome, inputStarted: true) }
            throw pause
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

    private func read(_ element: AXUIElement, deadline: TimeInterval, limit: Int = maximumNativeTextCharacters) throws -> String? {
        try bounded(element, deadline: deadline) {
            let (error, raw) = self.copyValue(element)
            guard error == .success, let raw, CFGetTypeID(raw) == CFStringGetTypeID() else { return nil }
            let cf = unsafeBitCast(raw, to: CFString.self)
            let length = CFStringGetLength(cf)
            guard length <= limit * 2, let value = raw as? String,
                  value.unicodeScalars.count <= limit else { return nil }
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

/// Attribute-level adapters are injected in tests so the same real write/read transaction is exercised.
struct AXSelectionIO {
    var settable: (AXUIElement, CFString) -> (AXError, Bool) = { element, attribute in
        var flag = DarwinBoolean(false)
        let status = AXUIElementIsAttributeSettable(element, attribute, &flag)
        return (status, flag.boolValue)
    }
    var select: (AXUIElement, NSRange) -> AXError = { element, range in
        var span = CFRange(location: range.location, length: range.length)
        guard let value = AXValueCreate(.cfRange, &span) else { return .illegalArgument }
        return AXUIElementSetAttributeValue(element, kAXSelectedTextRangeAttribute as CFString, value)
    }
    var selection: (AXUIElement) -> NSRange? = { element in
        var raw: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXSelectedTextRangeAttribute as CFString, &raw) == .success,
              let raw, CFGetTypeID(raw) == AXValueGetTypeID() else { return nil }
        var range = CFRange()
        let value = unsafeBitCast(raw, to: AXValue.self)
        guard AXValueGetType(value) == .cfRange, AXValueGetValue(value, .cfRange, &range) else { return nil }
        return NSRange(location: range.location, length: range.length)
    }
    var text: (AXUIElement) -> String? = { element in
        var raw: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXSelectedTextAttribute as CFString, &raw) == .success else { return nil }
        return raw as? String
    }
    var insert: (AXUIElement, String) -> AXError = { element, text in
        AXUIElementSetAttributeValue(element, kAXSelectedTextAttribute as CFString, text as CFString)
    }
}

extension SystemAXTextValueReplacer {
    private static let rangeDocumentLimit = 1_000_000

    func preflightRange(to element: AXUIElement, matching match: AXTextMatch) -> AXTextMutationPreflight {
        do {
            let deadline = clock() + budget
            for attribute in [kAXSelectedTextRangeAttribute, kAXSelectedTextAttribute] {
                let (status, writable) = try bounded(element, deadline: deadline) {
                    self.rangeIO.settable(element, attribute as CFString)
                }
                if status == .attributeUnsupported || status == .notImplemented { return .unsupported }
                guard status == .success else { return .failed(mapAXActionError(status)) }
                guard writable else { return .unsupported }
            }
            guard let value = try read(element, deadline: deadline, limit: Self.rangeDocumentLimit) else {
                return .failed(.textRangeUnsupported)
            }
            _ = try match.uniqueRange(in: value)
            return .settable
        } catch let error as ActionExecutionError { return .failed(error) }
        catch { return .failed(.helperFailed) }
    }

    func keyboardWitness(for match: AXTextMatch, replacingWith text: String, in element: AXUIElement) throws -> AXKeyboardRangeWitness {
        func document() throws -> String {
            guard let value = try read(element, deadline: clock() + budget, limit: Self.rangeDocumentLimit) else {
                throw ActionExecutionError.textRangeUnsupported
            }
            return value
        }
        let before = try document()
        let range = try match.uniqueRange(in: before)
        let expected = (before as NSString).replacingCharacters(in: range, with: text)
        func requireSelection() throws {
            guard (try document()).utf16.elementsEqual(before.utf16) else { throw ActionExecutionError.staleSnapshot }
            try bounded(element, deadline: clock() + budget) {
                guard self.rangeIO.selection(element) == range,
                      self.rangeIO.text(element)?.utf16.elementsEqual(match.target.utf16) == true
                else { throw ActionExecutionError.textSelectionRequired }
            }
        }
        try requireSelection()
        return AXKeyboardRangeWitness(beforeInput: requireSelection, afterInput: {
            try document().utf16.elementsEqual(expected.utf16)
        })
    }

    func replaceRange(_ match: AXTextMatch, with text: String, in element: AXUIElement,
                      validateBeforeMutation: () throws -> Void) throws -> TextReplacementResult {
        let deadline = clock() + budget
        var inputStarted = false
        // Bound inspection of an entire document independently of the string carried on the wire.
        let documentLimit = Self.rangeDocumentLimit
        do {
            try validateBeforeMutation()
            guard let before = try read(element, deadline: deadline, limit: documentLimit) else {
                throw ActionExecutionError.textRangeUnsupported
            }
            let range = try match.uniqueRange(in: before)
            let expected = (before as NSString).replacingCharacters(in: range, with: text)
            if before.utf8.elementsEqual(expected.utf8) {
                return TextReplacementResult(inputStarted: false, effectVerification: .noop, observationRequired: false)
            }
            try bounded(element, deadline: deadline) {
                try validateBeforeMutation()
                try self.checkDeadline(deadline)
                inputStarted = true // A failed AX selection may already have moved the caret.
                let status = self.rangeIO.select(element, range)
                guard status == .success else { throw mapAXActionError(status) }
            }
            // If a concurrent edit moved the match or the app ignored our selection, stop
            // before any text write. A repeated word at a different offset is insufficient.
            guard let current = try read(element, deadline: deadline, limit: documentLimit),
                  before.utf8.elementsEqual(current.utf8) else { throw ActionExecutionError.staleSnapshot }
            try bounded(element, deadline: deadline) {
                try validateBeforeMutation()
                guard self.rangeIO.selection(element) == range,
                      self.rangeIO.text(element)?.utf16.elementsEqual(match.target.utf16) == true
                else { throw ActionExecutionError.staleSnapshot }
                try validateBeforeMutation()
                try self.checkDeadline(deadline)
                let status = self.rangeIO.insert(element, text)
                guard status == .success else { throw mapAXActionError(status) }
            }
            let after = try read(element, deadline: deadline, limit: documentLimit + maximumNativeTextCharacters)
            try validateBeforeMutation()
            let verified = after?.utf8.elementsEqual(expected.utf8) == true
            return TextReplacementResult(inputStarted: true, effectVerification: verified ? .verified : .unverified,
                                         observationRequired: !verified)
        } catch let failure as ActionPerformFailure {
            throw ActionPerformFailure(error: failure.error, inputStarted: inputStarted || failure.inputStarted)
        } catch let pause as UserActivityMonitoringError {
            if inputStarted { throw ActionPerformFailure(error: .unknownOutcome, inputStarted: true) }
            throw pause
        } catch {
            throw ActionPerformFailure(error: (error as? ActionExecutionError) ?? .helperFailed, inputStarted: inputStarted)
        }
    }
}
