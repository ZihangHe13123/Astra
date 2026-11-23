@preconcurrency import ApplicationServices
import AppKit
import Carbon
import CoreGraphics
import Darwin
import Foundation

fileprivate func actionFailureDiagnostic(_ reason: String) {
    let line = "[helper_failed] \(reason)\n"
    FileHandle.standardError.write(Data(line.utf8))
    let diagnosticPath = "/tmp/astra-target-gone-diagnostics.log"
    if let handle = FileHandle(forWritingAtPath: diagnosticPath) {
        defer { try? handle.close() }
        handle.seekToEndOfFile()
        handle.write(Data(line.utf8))
    } else {
        try? line.write(toFile: diagnosticPath, atomically: true, encoding: .utf8)
    }
}

let maximumNativeActions = 20
let maximumNativeTextCharacters = 20_000
let maximumNativeWaitMilliseconds = 10_000
let maximumForegroundExecutionSeconds: TimeInterval = 12

enum NativeActionKind: String, Equatable {
    case click
    case rightClick = "right_click"
    case doubleClick = "double_click"
    case type
    case keypress
    case scroll
    case drag
    case wait
}

struct NativeAction: Equatable {
    let kind: NativeActionKind
    let x: CGFloat?
    let y: CGFloat?
    let endX: CGFloat?
    let endY: CGFloat?
    let text: String?
    let key: String?
    let deltaX: CGFloat?
    let deltaY: CGFloat?
    let durationMS: Int?
    let elementRef: String?
    let targetElementRef: String?
    let elementIndex: Int?
    let modifiers: [String]
    let checked: Bool?
    let replace: Bool?
    let textMatch: AXTextMatch?
    let saveNewCopyTo: String?
    var supportsSelectedUnicodeEvent: Bool {
        guard textMatch != nil, let text, !text.isEmpty, text.utf16.count <= 20 else { return false }
        return !text.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains)
    }
    var isReplacement: Bool { replace == true || textMatch != nil }

    init(
        kind: NativeActionKind,
        x: CGFloat?,
        y: CGFloat?,
        endX: CGFloat?,
        endY: CGFloat?,
        text: String?,
        key: String?,
        deltaX: CGFloat?,
        deltaY: CGFloat?,
        durationMS: Int?,
        elementRef: String?,
        targetElementRef: String? = nil,
        elementIndex: Int? = nil,
        modifiers: [String],
        checked: Bool? = nil,
        replace: Bool? = nil,
        textMatch: AXTextMatch? = nil,
        saveNewCopyTo: String? = nil
    ) {
        self.kind = kind
        self.x = x
        self.y = y
        self.endX = endX
        self.endY = endY
        self.text = text
        self.key = key.map(canonicalComputerKey)
        self.deltaX = deltaX
        self.deltaY = deltaY
        self.durationMS = durationMS
        self.elementRef = elementRef
        self.targetElementRef = targetElementRef
        self.elementIndex = elementIndex
        self.modifiers = modifiers
        self.checked = checked
        self.replace = replace
        self.textMatch = textMatch
        self.saveNewCopyTo = saveNewCopyTo
    }

    static func click(x: CGFloat, y: CGFloat) -> Self { action(.click, x: x, y: y) }
    static func click(x: CGFloat, y: CGFloat, within elementRef: String) -> Self {
        action(.click, x: x, y: y, targetElementRef: elementRef)
    }
    static func click(elementRef: String) -> Self { action(.click, elementRef: elementRef) }
    static func rightClick(x: CGFloat, y: CGFloat) -> Self { action(.rightClick, x: x, y: y) }
    static func rightClick(x: CGFloat, y: CGFloat, within elementRef: String) -> Self {
        action(.rightClick, x: x, y: y, targetElementRef: elementRef)
    }
    static func rightClick(elementRef: String) -> Self { action(.rightClick, elementRef: elementRef) }
    static func doubleClick(x: CGFloat, y: CGFloat) -> Self { action(.doubleClick, x: x, y: y) }
    static func doubleClick(elementRef: String) -> Self { action(.doubleClick, elementRef: elementRef) }
    static func type(text: String, elementRef: String? = nil) -> Self { action(.type, text: text, elementRef: elementRef) }
    static func keypress(key: String, modifiers: [String] = [], elementRef: String? = nil) -> Self {
        action(.keypress, key: key, elementRef: elementRef, modifiers: modifiers)
    }
    static func scroll(
        deltaX: CGFloat = 0,
        deltaY: CGFloat = 0,
        x: CGFloat? = nil,
        y: CGFloat? = nil,
        elementRef: String? = nil,
        targetElementRef: String? = nil
    ) -> Self {
        action(
            .scroll,
            x: x,
            y: y,
            deltaX: deltaX,
            deltaY: deltaY,
            elementRef: elementRef,
            targetElementRef: targetElementRef
        )
    }
    static func drag(x: CGFloat, y: CGFloat, endX: CGFloat, endY: CGFloat, durationMS: Int? = nil) -> Self {
        action(.drag, x: x, y: y, endX: endX, endY: endY, durationMS: durationMS)
    }
    static func wait(durationMS: Int) -> Self { action(.wait, durationMS: durationMS) }

    func resolvingElementReference(_ reference: String) -> Self {
        Self(
            kind: kind,
            x: x,
            y: y,
            endX: endX,
            endY: endY,
            text: text,
            key: key,
            deltaX: deltaX,
            deltaY: deltaY,
            durationMS: durationMS,
            elementRef: reference,
            targetElementRef: nil,
            elementIndex: nil,
            modifiers: modifiers,
            checked: checked,
            replace: replace,
            textMatch: textMatch,
            saveNewCopyTo: saveNewCopyTo
        )
    }

    private static func action(
        _ kind: NativeActionKind,
        x: CGFloat? = nil,
        y: CGFloat? = nil,
        endX: CGFloat? = nil,
        endY: CGFloat? = nil,
        text: String? = nil,
        key: String? = nil,
        deltaX: CGFloat? = nil,
        deltaY: CGFloat? = nil,
        durationMS: Int? = nil,
        elementRef: String? = nil,
        targetElementRef: String? = nil,
        modifiers: [String] = []
    ) -> Self {
        Self(
            kind: kind,
            x: x,
            y: y,
            endX: endX,
            endY: endY,
            text: text,
            key: key,
            deltaX: deltaX,
            deltaY: deltaY,
            durationMS: durationMS,
            elementRef: elementRef,
            targetElementRef: targetElementRef,
            modifiers: modifiers
        )
    }

    static func parse(_ value: JSONValue) throws -> Self {
        guard case let .object(fields) = value,
              case let .string(type)? = fields["type"],
              let kind = NativeActionKind(rawValue: type),
              fields.keys.allSatisfy({ allowedFields.contains($0) })
        else { throw ActionExecutionError.invalidAction }
        if fields["replace"] != nil {
            guard fields["replace"] == .bool(true),
                  fields.keys.allSatisfy({ ["type", "text", "element_ref", "replace"].contains($0) })
            else { throw ActionExecutionError.invalidAction }
        }
        if fields["save_new_copy_to"] != nil {
            let allowed = kind == .click ? ["type","element_ref","save_new_copy_to"] : ["type","key","save_new_copy_to"]
            guard fields.keys.allSatisfy(allowed.contains) else { throw ActionExecutionError.invalidAction }
        }
        var textMatch: AXTextMatch?
        if fields["target_text"] != nil || fields["prefix"] != nil || fields["suffix"] != nil {
            guard fields.keys.allSatisfy({ ["type", "text", "element_ref", "target_text", "prefix", "suffix"].contains($0) }),
                  let needle = try optionalString(fields["target_text"], allowEmpty: false, maximumCharacters: maximumNativeTextCharacters)
            else { throw ActionExecutionError.invalidAction }
            textMatch = AXTextMatch(target: needle,
                prefix: try optionalString(fields["prefix"], allowEmpty: true, maximumCharacters: maximumNativeTextCharacters),
                suffix: try optionalString(fields["suffix"], allowEmpty: true, maximumCharacters: maximumNativeTextCharacters))
        }
        let action = Self(
            kind: kind,
            x: try optionalFinite(fields["x"]),
            y: try optionalFinite(fields["y"]),
            endX: try optionalFinite(fields["end_x"]),
            endY: try optionalFinite(fields["end_y"]),
            text: try optionalString(fields["text"], allowEmpty: true, maximumCharacters: maximumNativeTextCharacters),
            key: try optionalString(fields["key"], allowEmpty: false, maximumCharacters: 64),
            deltaX: try optionalFinite(fields["delta_x"]),
            deltaY: try optionalFinite(fields["delta_y"]),
            durationMS: try optionalInteger(fields["duration_ms"]),
            elementRef: try optionalString(fields["element_ref"], allowEmpty: false, maximumCharacters: 256),
            targetElementRef: try optionalString(fields["target_element_ref"], allowEmpty: false, maximumCharacters: 256),
            elementIndex: try optionalPositiveInteger(fields["element_index"]),
            modifiers: try modifierArray(fields["modifiers"]),
            checked: try optionalBool(fields["checked"]),
            replace: try optionalBool(fields["replace"]),
            textMatch: textMatch,
            saveNewCopyTo: try optionalString(fields["save_new_copy_to"], allowEmpty: false, maximumCharacters: 4096)
        )
        try action.validateShape()
        return action
    }

    private func validateShape() throws {
        if let path = saveNewCopyTo {
            guard savePanelRequestedURL(path) != nil, modifiers.isEmpty,
                  (kind == .click && elementRef != nil && x == nil && y == nil) ||
                  (kind == .keypress && key == "return" && elementRef == nil)
            else { throw ActionExecutionError.invalidAction }
        }
        if replace != nil || textMatch != nil {
            guard isReplacement, !(replace != nil && textMatch != nil), kind == .type, elementRef != nil, text != nil,
                  elementIndex == nil, targetElementRef == nil, checked == nil,
                  x == nil, y == nil, endX == nil, endY == nil, key == nil,
                  deltaX == nil, deltaY == nil, durationMS == nil, modifiers.isEmpty
            else { throw ActionExecutionError.invalidAction }
        }
        if checked != nil {
            guard kind == .click, elementRef != nil || elementIndex != nil,
                  x == nil, y == nil, modifiers.isEmpty else { throw ActionExecutionError.invalidAction }
        }
        if let durationMS, durationMS < 0 || durationMS > maximumNativeWaitMilliseconds { throw ActionExecutionError.invalidAction }
        guard elementRef == nil || targetElementRef == nil else { throw ActionExecutionError.invalidAction }
        if elementIndex != nil && (elementRef != nil || targetElementRef != nil) {
            throw ActionExecutionError.invalidAction
        }
        if elementIndex != nil && ![.click, .rightClick, .doubleClick, .scroll].contains(kind) {
            throw ActionExecutionError.invalidAction
        }
        switch kind {
        case .click, .rightClick, .doubleClick:
            if x != nil || y != nil {
                guard x != nil, y != nil, elementRef == nil, elementIndex == nil else {
                    throw ActionExecutionError.invalidAction
                }
            } else {
                guard (elementRef != nil) != (elementIndex != nil), targetElementRef == nil else {
                    throw ActionExecutionError.invalidAction
                }
            }
        case .type:
            guard text != nil, targetElementRef == nil else { throw ActionExecutionError.invalidAction }
        case .keypress:
            guard key != nil, targetElementRef == nil else { throw ActionExecutionError.invalidAction }
            guard let key, ApprovedKeyChord.supportsKeyName(key) else { throw ActionExecutionError.invalidAction }
            if key.lowercased() == "v", modifiers.contains("command") { throw ActionExecutionError.invalidAction }
        case .scroll:
            guard deltaX != nil || deltaY != nil else { throw ActionExecutionError.invalidAction }
            if x != nil || y != nil {
                guard x != nil, y != nil, elementRef == nil, elementIndex == nil else {
                    throw ActionExecutionError.invalidAction
                }
            } else {
                guard (elementRef != nil) != (elementIndex != nil), targetElementRef == nil else {
                    throw ActionExecutionError.invalidAction
                }
            }
        case .drag:
            guard x != nil, y != nil, endX != nil, endY != nil,
                  elementRef == nil, elementIndex == nil
            else {
                throw ActionExecutionError.invalidAction
            }
        case .wait:
            guard durationMS != nil, targetElementRef == nil else {
                throw ActionExecutionError.invalidAction
            }
        }
    }

    private static let allowedFields: Set<String> = [
        "type", "x", "y", "end_x", "end_y", "text", "key", "delta_x", "delta_y", "duration_ms", "element_ref", "target_element_ref", "element_index", "modifiers", "checked", "replace", "target_text", "prefix", "suffix", "save_new_copy_to",
    ]

    private static func optionalBool(_ value: JSONValue?) throws -> Bool? {
        guard let value else { return nil }
        guard case let .bool(boolean) = value else { throw ActionExecutionError.invalidAction }
        return boolean
    }

    private static func optionalPositiveInteger(_ value: JSONValue?) throws -> Int? {
        guard let value else { return nil }
        guard case let .number(number) = value, number.isFinite, number.rounded() == number, number >= 1, number <= Double(Int.max) else {
            throw ActionExecutionError.invalidAction
        }
        return Int(number)
    }

    private static func optionalFinite(_ value: JSONValue?) throws -> CGFloat? {
        guard let value else { return nil }
        guard case let .number(number) = value, number.isFinite, abs(number) <= 1_000_000 else { throw ActionExecutionError.invalidAction }
        return CGFloat(number)
    }

    private static func optionalInteger(_ value: JSONValue?) throws -> Int? {
        guard let value else { return nil }
        guard case let .number(number) = value, number.isFinite, number.rounded() == number, number >= 0, number <= Double(Int.max) else {
            throw ActionExecutionError.invalidAction
        }
        return Int(number)
    }

    private static func optionalString(_ value: JSONValue?, allowEmpty: Bool, maximumCharacters: Int) throws -> String? {
        guard let value else { return nil }
        guard case let .string(string) = value, (allowEmpty || !string.isEmpty), string.unicodeScalars.count <= maximumCharacters else {
            throw ActionExecutionError.invalidAction
        }
        return string
    }

    private static func modifierArray(_ value: JSONValue?) throws -> [String] {
        guard let value else { return [] }
        guard case let .array(items) = value, items.count <= 8 else { throw ActionExecutionError.invalidAction }
        let allowed: Set<String> = ["command", "control", "option", "shift", "function", "caps_lock"]
        let result = try items.map { item in
            guard case let .string(name) = item, allowed.contains(name) else { throw ActionExecutionError.invalidAction }
            return name
        }
        guard Set(result).count == result.count else { throw ActionExecutionError.invalidAction }
        return result
    }
}

enum FocusedRootPreference: Equatable {
    case selectedWindow
    case containedOverlay
}

struct ActionGuard: Equatable {
    let pid: pid_t
    let windowID: CGWindowID
    let bounds: CGRect
    let axIdentity: CFHashCode
    let focusedAXIdentity: CFHashCode
    let focusedAXBounds: CGRect
    let focusedRootPreference: FocusedRootPreference
    let keyboardFocus: KeyboardFocusAuthority?
    let snapshotID: String
    let interactionMode: InteractionMode

    /// 只换键盘焦点权威的一份新 guard。计划期与投递期之间权威可能必须重取
    /// （见 deliveryKeyboardFocusGuard），而 guard 其余字段是已授权的身份凭证，不能动。
    func replacingKeyboardFocus(_ focus: KeyboardFocusAuthority?) -> ActionGuard {
        ActionGuard(
            pid: pid,
            windowID: windowID,
            bounds: bounds,
            axIdentity: axIdentity,
            focusedAXIdentity: focusedAXIdentity,
            focusedAXBounds: focusedAXBounds,
            focusedRootPreference: focusedRootPreference,
            keyboardFocus: focus,
            snapshotID: snapshotID,
            interactionMode: interactionMode
        )
    }

    init(
        pid: pid_t,
        windowID: CGWindowID,
        bounds: CGRect,
        axIdentity: CFHashCode,
        focusedAXIdentity: CFHashCode? = nil,
        focusedAXBounds: CGRect? = nil,
        focusedRootPreference: FocusedRootPreference = .selectedWindow,
        keyboardFocus: KeyboardFocusAuthority? = nil,
        snapshotID: String,
        interactionMode: InteractionMode = .foregroundTakeover
    ) {
        self.pid = pid
        self.windowID = windowID
        self.bounds = bounds
        self.axIdentity = axIdentity
        self.focusedAXIdentity = focusedAXIdentity ?? axIdentity
        self.focusedAXBounds = focusedAXBounds ?? bounds
        self.focusedRootPreference = focusedRootPreference
        self.keyboardFocus = keyboardFocus
        self.snapshotID = snapshotID
        self.interactionMode = interactionMode
    }
}

struct ActionTargetState: Equatable {
    let pid: pid_t
    let windowID: CGWindowID
    let bounds: CGRect
    let axIdentity: CFHashCode
    let focusedAXIdentity: CFHashCode
    let focusedAXBounds: CGRect
    let focusedRootPreference: FocusedRootPreference
    let keyboardFocus: KeyboardFocusAuthority?

    init(
        pid: pid_t,
        windowID: CGWindowID,
        bounds: CGRect,
        axIdentity: CFHashCode,
        focusedAXIdentity: CFHashCode? = nil,
        focusedAXBounds: CGRect? = nil,
        focusedRootPreference: FocusedRootPreference = .selectedWindow,
        keyboardFocus: KeyboardFocusAuthority? = nil
    ) {
        self.pid = pid
        self.windowID = windowID
        self.bounds = bounds
        self.axIdentity = axIdentity
        self.focusedAXIdentity = focusedAXIdentity ?? axIdentity
        self.focusedAXBounds = focusedAXBounds ?? bounds
        self.focusedRootPreference = focusedRootPreference
        self.keyboardFocus = keyboardFocus
    }
}

struct ActionElement {
    let element: AXUIElement?
    let identityToken: String
    let bounds: CGRect
    let roleResult: BoundedAXStringResult
    let subroleResult: BoundedAXStringResult
    let enabled: Bool?
    let actionNames: ActionNameResults

    init(
        element: AXUIElement?,
        identityToken: String,
        bounds: CGRect,
        roleResult: BoundedAXStringResult,
        subroleResult: BoundedAXStringResult,
        enabled: Bool?,
        actionNames: ActionNameResults
    ) {
        self.element = element
        self.identityToken = identityToken
        self.bounds = bounds
        self.roleResult = roleResult
        self.subroleResult = subroleResult
        self.enabled = enabled
        self.actionNames = actionNames
    }

    init(element: AXUIElement?, bounds: CGRect, role: String, subrole: String?, enabled: Bool? = true, actions: [String]) {
        self.init(
            element: element,
            identityToken: element.map { "ax:\(CFHash($0))" } ?? UUID().uuidString,
            bounds: bounds,
            roleResult: BoundedAXStringResult(value: role, status: .complete),
            subroleResult: BoundedAXStringResult(value: subrole, status: .complete),
            enabled: enabled,
            actionNames: .complete(actions)
        )
    }

    var isSecure: Bool {
        if roleResult.status == .complete, let role = roleResult.value,
           role.localizedCaseInsensitiveContains("secure") {
            return true
        }
        if subroleResult.status == .complete,
           let subrole = subroleResult.value,
           subrole.localizedCaseInsensitiveContains("secure") {
            return true
        }
        return false
    }

    var acceptsFocusedKeyboardInput: Bool {
        keyboardTargetRejection == nil
    }

    var keyboardTargetRejection: ActionExecutionError? {
        if isSecure { return .secureTarget }
        if enabled == false { return .controlDisabled }
        guard roleResult.status == .complete, let role = roleResult.value, !role.isEmpty,
              subroleResult.status == .complete else { return .keyboardFocusUnverified }
        return nil
    }

    var opensFilePanel: Bool {
        roleResult.status == .complete && roleResult.value == "AXButton"
            && subroleResult.status == .complete && subroleResult.value == "AXFileUploadButton"
    }

    var supportsAXSelectedTextWrite: Bool {
        guard roleResult.status == .complete, let role = roleResult.value else {
            return false
        }
        return ["AXTextField", "AXTextArea", "AXComboBox"].contains(role)
    }
}

struct AXScrollPressTarget {
    let owner: ActionElement
    let button: ActionElement
}

struct PointerSafeRegionAuthority: Equatable {
    let reference: String
    let identityToken: String
    let bounds: CGRect
}

struct ActionNameResults {
    let values: [BoundedAXStringResult]
    let status: BoundedAXStringStatus
    let error: AXError?

    init(values: [BoundedAXStringResult], status: BoundedAXStringStatus, error: AXError? = nil) {
        self.values = values
        self.status = status
        self.error = error
    }

    static func complete(_ names: [String]) -> Self {
        Self(values: names.map { BoundedAXStringResult(value: $0, status: .complete) }, status: .complete, error: nil)
    }

    var completeNames: [String]? {
        guard error == nil, status == .complete, values.allSatisfy({ $0.status == .complete && $0.value != nil }) else { return nil }
        return values.compactMap(\.value)
    }
}

enum ResolvedActionMethod: Equatable {
    case accessibilityPress
    case accessibilityIncrement
    case accessibilityDecrement
    case accessibilityText
    case pointerClick(count: Int, button: PointerButton)
    case unicodeText
    case keypress
    case scroll
    case drag
    case wait
}

enum PointerButton: Equatable {
    case left
    case right
}

struct ResolvedAction {
    let source: NativeAction
    let method: ResolvedActionMethod
    let screenPoint: CGPoint?
    let endScreenPoint: CGPoint?
    let element: AXUIElement?
    let verifiedElement: ActionElement?
    let effectElement: AXUIElement?
    let effectVerifiedElement: ActionElement?

    init(
        source: NativeAction,
        method: ResolvedActionMethod,
        screenPoint: CGPoint?,
        endScreenPoint: CGPoint?,
        element: AXUIElement?,
        verifiedElement: ActionElement? = nil,
        effectElement: AXUIElement? = nil,
        effectVerifiedElement: ActionElement? = nil
    ) {
        self.source = source
        self.method = method
        self.screenPoint = screenPoint
        self.endScreenPoint = endScreenPoint
        self.element = element
        self.verifiedElement = verifiedElement
        self.effectElement = effectElement
        self.effectVerifiedElement = effectVerifiedElement
    }
}

enum ActionExecutionError: String, Error, Equatable {
    case invalidAction = "protocol_mismatch"
    case permissionDenied = "permission_denied"
    case targetGone = "target_gone"
    case targetNotFrontmost = "target_not_frontmost"
    case staleSnapshot = "stale_snapshot"
    case outOfBounds = "out_of_bounds"
    case secureTarget = "secure_target"
    case secureInputActive = "secure_input_active"
    case saveDestinationUnverified = "save_destination_unverified"
    case saveDestinationMismatch = "save_destination_mismatch"
    case saveExtensionUnverified = "save_extension_unverified"
    case saveDestinationExists = "save_destination_exists"
    case textSelectionRequired = "text_selection_required"
    case textMatchNotFound = "text_match_not_found"
    case textMatchAmbiguous = "text_match_ambiguous"
    case textInputSourceUnverified = "text_input_source_unverified"
    case textCompositionActive = "text_composition_active"
    case textRangeUnsupported = "text_range_unsupported"
    case observationRequired = "observation_required"
    case keyboardFocusUnverified = "keyboard_focus_unverified"
    case controlDisabled = "control_disabled"
    case inputFocusRequired = "input_focus_required"
    case actionTimeout = "action_timeout"
    case helperFailed = "helper_failed"
    case unknownOutcome = "unknown_outcome"
    /// The application refused an accessibility action or attribute write before acting on it.
    case accessibilityActionRefused = "accessibility_action_refused"
}

struct ActionPerformance {
    let inputStarted: Bool
    var effectVerification: ActionEffectVerification? = nil
    var observationRequired: Bool = false
}

struct ActionPerformFailure: Error {
    let error: ActionExecutionError
    let inputStarted: Bool
}

enum ActionEffectVerification: String, Equatable {
    case verified
    case unverified
    case noop
}

enum AXEffectValue: Equatable, Sendable {
    case number(Double)
    case boolean(Bool)
    case text(String)
}

struct AXEffectObservation: Equatable, Sendable {
    let identityToken: String
    let value: AXEffectValue
}

protocol AXEffectReading: AnyObject {
    func read(
        _ element: AXUIElement,
        remainingBudget: TimeInterval
    ) -> AXEffectObservation?
}

final class SystemAXEffectReader: AXEffectReading {
    static let maximumTextCharacters = 128
    static let maximumTextUTF16Units = maximumTextCharacters * 2
    private let clock: () -> TimeInterval
    private let copyValue: (AXUIElement) -> (AXError, CFTypeRef?)
    private let setMessagingTimeout: (AXUIElement, Float) -> AXError

    init() {
        self.clock = { ProcessInfo.processInfo.systemUptime }
        self.copyValue = { element in
            var rawValue: CFTypeRef?
            let error = AXUIElementCopyAttributeValue(
                element,
                kAXValueAttribute as CFString,
                &rawValue
            )
            return (error, rawValue)
        }
        self.setMessagingTimeout = { element, timeout in
            AXUIElementSetMessagingTimeout(element, timeout)
        }
    }

    init(
        clock: @escaping () -> TimeInterval,
        copyValue: @escaping (AXUIElement) -> (AXError, CFTypeRef?),
        setMessagingTimeout: @escaping (AXUIElement, Float) -> AXError
    ) {
        self.clock = clock
        self.copyValue = copyValue
        self.setMessagingTimeout = setMessagingTimeout
    }

    func read(
        _ element: AXUIElement,
        remainingBudget: TimeInterval
    ) -> AXEffectObservation? {
        let started = clock()
        let deadline = started + remainingBudget
        guard started.isFinite,
              deadline.isFinite,
              let timeout = boundedAXEffectMessagingTimeout(remainingBudget),
              setMessagingTimeout(element, timeout) == .success
        else { return nil }
        guard clock() < deadline else {
            _ = setMessagingTimeout(element, 0)
            return nil
        }

        let (error, rawValue) = copyValue(element)
        guard setMessagingTimeout(element, 0) == .success,
              clock() <= deadline,
              error == .success,
              let rawValue,
              let value = Self.typedValue(rawValue),
              clock() <= deadline
        else { return nil }
        return AXEffectObservation(
            identityToken: "ax:\(CFHash(element))",
            value: value
        )
    }

    private static func typedValue(_ rawValue: CFTypeRef) -> AXEffectValue? {
        if CFGetTypeID(rawValue) == CFBooleanGetTypeID(),
           let value = rawValue as? Bool {
            return .boolean(value)
        }
        if CFGetTypeID(rawValue) == CFNumberGetTypeID(),
           let number = rawValue as? NSNumber {
            let value = number.doubleValue
            return value.isFinite ? .number(value) : nil
        }
        if CFGetTypeID(rawValue) == CFStringGetTypeID() {
            let string = unsafeBitCast(rawValue, to: CFString.self)
            guard CFStringGetLength(string) <= maximumTextUTF16Units,
                  let value = rawValue as? String,
                  value.unicodeScalars.count <= maximumTextCharacters
            else { return nil }
            return .text(value)
        }
        return nil
    }
}

private func boundedAXEffectMessagingTimeout(_ remaining: TimeInterval) -> Float? {
    guard remaining.isFinite, remaining > 0 else { return nil }
    var timeout = Float(remaining)
    guard timeout.isFinite, timeout > 0 else { return nil }
    if TimeInterval(timeout) > remaining { timeout = timeout.nextDown }
    return timeout > 0 ? timeout : nil
}

enum KeyboardFailureStage: String {
    case beforeKeyDown = "before_key_down"
    case keyDown = "key_down"
    case beforeKeyUp = "before_key_up"
    case keyUp = "key_up"
    case afterKeyUp = "after_key_up"
}

/// Fixed execution evidence only. This is not application-effect verification.
/// `inputMayHaveStarted` includes earlier characters in a text action.
struct KeyboardFailureDiagnostics: Equatable {
    let stage: KeyboardFailureStage
    let cause: ActionExecutionError?
    let userActivityPaused: Bool
    let inputMayHaveStarted: Bool
    let cleanupFailed: Bool

    func asJSON() -> JSONValue {
        .object([
            "stage": .string(stage.rawValue),
            "cause": .string(userActivityPaused ? "user_activity_paused" : (cause ?? .helperFailed).rawValue),
            "input_may_have_started": .bool(inputMayHaveStarted),
            "cleanup_failed": .bool(cleanupFailed),
        ])
    }
}

struct ActionOutcome: Equatable {
    let index: Int
    let ok: Bool
    let error: ActionExecutionError?
    let effectVerification: ActionEffectVerification?
    let inputDiagnostics: KeyboardFailureDiagnostics?
    let observationRequired: Bool

    init(
        index: Int,
        ok: Bool,
        error: ActionExecutionError?,
        effectVerification: ActionEffectVerification? = nil,
        inputDiagnostics: KeyboardFailureDiagnostics? = nil,
        observationRequired: Bool = false
    ) {
        self.index = index
        self.ok = ok
        self.error = error
        self.effectVerification = effectVerification
        self.inputDiagnostics = inputDiagnostics
        self.observationRequired = observationRequired
    }

    func asJSON() -> JSONValue {
        var fields: [String: JSONValue] = ["index": .number(Double(index)), "ok": .bool(ok)]
        if let error {
            fields["error_code"] = .string(error.rawValue)
        } else if !ok, inputDiagnostics?.userActivityPaused == true {
            fields["error_code"] = .string(CooperativeErrorCode.userActivityPaused.rawValue)
        }
        if let effectVerification {
            fields["effect_verification"] = .string(effectVerification.rawValue)
        }
        if let inputDiagnostics { fields["input_diagnostics"] = inputDiagnostics.asJSON() }
        if observationRequired { fields["observation_required"] = .bool(true) }
        return .object(fields)
    }
}

struct ActionBatchResult {
    let outcomes: [ActionOutcome]
    let lastAcknowledgedAction: Int
    let error: ActionExecutionError?

    func asJSON() -> JSONValue {
        .object([
            "outcomes": .array(outcomes.map { $0.asJSON() }),
            "last_acknowledged_action": .number(Double(lastAcknowledgedAction)),
        ])
    }
}

protocol ActionProviding: AnyObject {
    func validateSaveDestination(_ action: NativeAction) throws
    func currentTargetState() throws -> ActionTargetState
    func element(reference: String, snapshotID: String) -> ActionElement?
    func pointerElement(reference: String, snapshotID: String) -> ActionElement?
    func scrollPressTarget(
        reference: String,
        snapshotID: String,
        direction: AXScrollDirection
    ) -> AXScrollPressTarget?
    /// Observation only, including when the production AX focus setter is installed.
    func focusedKeyboardElement(matching expected: ActionElement?) throws -> ActionElement
    func acquireOrVerifyKeyboardFocus(matching expected: ActionElement?, validateFocusMutation: () throws -> Void) throws -> ActionElement
    func isVisibleOnScreen(_ point: CGPoint) -> Bool
    func preflightAXTextMutation(_ element: ActionElement) -> AXTextMutationPreflight
    func noteAXTextWriteUnavailable(_ element: ActionElement)
    func preflightAXTextReplacement(_ element: ActionElement) -> AXTextMutationPreflight
    func preflightAXRangeReplacement(_ element: ActionElement, matching match: AXTextMatch) -> AXTextMutationPreflight
    func textComposition(of element: ActionElement) -> AXTextComposition
    func permitsIMEAXWrite(to element: ActionElement) -> Bool
    func validateBackgroundReplacementTarget(_ element: ActionElement) throws
    func selectedUnicodeWitness(for action: NativeAction, element: ActionElement) throws -> AXKeyboardRangeWitness
    func perform(_ action: ResolvedAction) throws -> ActionPerformance
    func perform(_ action: ResolvedAction, validateMutation: () throws -> Void) throws -> ActionPerformance
    func performReplacement(_ action: ResolvedAction, validateMutation: () throws -> Void) throws -> ActionPerformance
}

extension ActionProviding {
    func validateSaveDestination(_ action: NativeAction) throws {
        if action.saveNewCopyTo != nil { throw ActionExecutionError.saveDestinationUnverified }
    }
    func pointerElement(reference _: String, snapshotID _: String) -> ActionElement? { nil }
    func scrollPressTarget(
        reference _: String,
        snapshotID _: String,
        direction _: AXScrollDirection
    ) -> AXScrollPressTarget? { nil }
    func isVisibleOnScreen(_: CGPoint) -> Bool { true }
    func focusedKeyboardElement(matching _: ActionElement?) throws -> ActionElement { throw ActionExecutionError.targetNotFrontmost }
    func acquireOrVerifyKeyboardFocus(matching expected: ActionElement?, validateFocusMutation: () throws -> Void) throws -> ActionElement {
        try validateFocusMutation()
        return try focusedKeyboardElement(matching: expected)
    }
    func preflightAXTextMutation(_: ActionElement) -> AXTextMutationPreflight { .unsupported }
    func noteAXTextWriteUnavailable(_: ActionElement) {}
    func preflightAXTextReplacement(_: ActionElement) -> AXTextMutationPreflight { .unsupported }
    func preflightAXRangeReplacement(_: ActionElement, matching _: AXTextMatch) -> AXTextMutationPreflight { .unsupported }
    func textComposition(of _: ActionElement) -> AXTextComposition { .unsupported }
    func permitsIMEAXWrite(to _: ActionElement) -> Bool { true }

    func allowsAXText(_ element: ActionElement, source: BackgroundTextInputSafety?) throws -> Bool {
        // Check app/field delivery capability before the last potentially blocking AX read.
        let imeCanWrite = source == .imeOrCandidate && permitsIMEAXWrite(to: element)
        switch textComposition(of: element) {
        case .marked: throw ActionExecutionError.textCompositionActive
        case .unmarked: return source == .safeASCIIKeyboardLayout || imeCanWrite
        case .unavailable: return false
        case .unsupported: return source == .safeASCIIKeyboardLayout
        }
    }

    func validateBackgroundReplacementTarget(_ element: ActionElement) throws {
        _ = try focusedKeyboardElement(matching: element)
    }
    func selectedUnicodeWitness(for _: NativeAction, element _: ActionElement) throws -> AXKeyboardRangeWitness {
        throw ActionExecutionError.textInputSourceUnverified
    }
    func perform(_ action: ResolvedAction, validateMutation: () throws -> Void) throws -> ActionPerformance {
        try validateMutation()
        return try perform(action)
    }
    func performReplacement(_: ResolvedAction, validateMutation: () throws -> Void) throws -> ActionPerformance {
        throw ActionPerformFailure(error: .invalidAction, inputStarted: false)
    }
}

final class ActionExecutor {
    private let provider: any ActionProviding
    private let settlePropagation: () -> Void

    init(provider: any ActionProviding, settlePropagation: (() -> Void)? = nil) {
        self.provider = provider
        // AX mutations reach the target application's event loop
        // asynchronously. Without a bounded settle wait, the observation
        // captured right after an acknowledged batch reflects the pre-action
        // tree, so every follow-up snapshot lags one action behind reality.
        self.settlePropagation = settlePropagation ?? { Thread.sleep(forTimeInterval: 0.25) }
    }

    func run(expected: ActionGuard, actions: [NativeAction]) -> ActionBatchResult {
        guard actions.count <= maximumNativeActions else {
            return ActionBatchResult(outcomes: [], lastAcknowledgedAction: -1, error: .invalidAction)
        }
        var outcomes: [ActionOutcome] = []
        var acknowledged = -1
        for (index, action) in actions.enumerated() {
            do {
                try verify(expected: expected, current: provider.currentTargetState())
                let resolved = try resolve(action, expected: expected)
                do {
                    _ = try provider.perform(resolved)
                } catch let failure as ActionPerformFailure {
                    let reported = failure.inputStarted ? ActionExecutionError.unknownOutcome : failure.error
                    outcomes.append(ActionOutcome(index: index, ok: false, error: reported))
                    return ActionBatchResult(outcomes: outcomes, lastAcknowledgedAction: acknowledged, error: reported)
                } catch {
                    outcomes.append(ActionOutcome(index: index, ok: false, error: .unknownOutcome))
                    return ActionBatchResult(outcomes: outcomes, lastAcknowledgedAction: acknowledged, error: .unknownOutcome)
                }
                acknowledged = index
                outcomes.append(ActionOutcome(index: index, ok: true, error: nil))
            } catch {
                let typed = actionError(error)
                outcomes.append(ActionOutcome(index: index, ok: false, error: typed))
                return ActionBatchResult(outcomes: outcomes, lastAcknowledgedAction: acknowledged, error: typed)
            }
        }
        if acknowledged >= 0 {
            settlePropagation()
        }
        return ActionBatchResult(outcomes: outcomes, lastAcknowledgedAction: acknowledged, error: nil)
    }

    private func helperActionDiagnostic(_ reason: String) {
        let line = "[helper_failed] \(reason)\n"
        FileHandle.standardError.write(Data(line.utf8))
        let diagnosticPath = "/tmp/astra-target-gone-diagnostics.log"
        if let handle = FileHandle(forWritingAtPath: diagnosticPath) {
            defer { try? handle.close() }
            handle.seekToEndOfFile()
            handle.write(Data(line.utf8))
        } else {
            try? line.write(toFile: diagnosticPath, atomically: true, encoding: .utf8)
        }
    }

    private func verify(expected: ActionGuard, current: ActionTargetState) throws {
        guard current.pid == expected.pid else { throw ActionExecutionError.targetNotFrontmost }
        guard current.windowID == expected.windowID, current.axIdentity == expected.axIdentity else {
            logActionRejected("PLAN-GUARD verify window-identity snapshotID=\(expected.snapshotID)")
            throw ActionExecutionError.staleSnapshot
        }
        guard finiteRect(current.bounds), approximatelyEqualActionRect(current.bounds, expected.bounds) else {
            logActionRejected("PLAN-GUARD verify window-bounds snapshotID=\(expected.snapshotID)")
            throw ActionExecutionError.staleSnapshot
        }
        guard current.focusedAXIdentity == expected.focusedAXIdentity,
              finiteRect(current.focusedAXBounds),
              approximatelyEqualActionRect(current.focusedAXBounds, expected.focusedAXBounds),
              current.focusedRootPreference == expected.focusedRootPreference
        else {
            logActionRejected("PLAN-GUARD verify focused-element snapshotID=\(expected.snapshotID)")
            throw ActionExecutionError.staleSnapshot
        }
    }

    private func resolve(_ action: NativeAction, expected: ActionGuard) throws -> ResolvedAction {
        let element = action.elementRef.flatMap { provider.element(reference: $0, snapshotID: expected.snapshotID) }
        if action.elementRef != nil && element == nil {
            logActionRejected("PLAN-GUARD resolve element-ref snapshotID=\(expected.snapshotID)")
            throw ActionExecutionError.staleSnapshot
        }
        if let element, element.isSecure, action.kind == .click || action.kind == .doubleClick {
            throw ActionExecutionError.secureTarget
        }
        switch action.kind {
        case .click, .rightClick, .doubleClick:
            if let element {
                guard element.enabled == true else {
                    helperActionDiagnostic("resolve click: element enabled=\(String(describing: element.enabled))")
                    throw ActionExecutionError.helperFailed
                }
                guard let names = element.actionNames.completeNames else {
                    if let error = element.actionNames.error { throw mapAXActionError(error) }
                    helperActionDiagnostic("resolve click: actionNames incomplete (no AX error)")
                    throw ActionExecutionError.helperFailed
                }
                // Right-click has no AXPress semantics; only plain click may
                // use the accessibility press path.
                if action.kind == .click, names.contains(kAXPressAction as String) {
                    return ResolvedAction(source: action, method: .accessibilityPress, screenPoint: nil, endScreenPoint: nil, element: element.element, verifiedElement: element)
                }
            }
            let localPoint: CGPoint
            if let element {
                guard validLocalRect(element.bounds, within: expected.bounds.size) else {
                    throw ActionExecutionError.outOfBounds
                }
                localPoint = CGPoint(x: element.bounds.midX, y: element.bounds.midY)
            } else {
                guard let x = action.x, let y = action.y else { throw ActionExecutionError.invalidAction }
                localPoint = CGPoint(x: x, y: y)
            }
            let screenPoint = try convert(localPoint, expected: expected)
            let count = action.kind == .doubleClick ? 2 : 1
            return ResolvedAction(source: action, method: .pointerClick(count: count, button: action.kind == .rightClick ? .right : .left), screenPoint: screenPoint, endScreenPoint: nil, element: element?.element, verifiedElement: element)
        case .type:
            let focused = try provider.focusedKeyboardElement(matching: element)
            if let rejection = focused.keyboardTargetRejection { throw rejection }
            return ResolvedAction(source: action, method: .unicodeText, screenPoint: nil, endScreenPoint: nil, element: focused.element, verifiedElement: focused)
        case .keypress:
            guard action.key?.lowercased() != "v" || !action.modifiers.contains("command") else {
                throw ActionExecutionError.invalidAction
            }
            let focused = try provider.focusedKeyboardElement(matching: element)
            if let rejection = focused.keyboardTargetRejection { throw rejection }
            return ResolvedAction(source: action, method: .keypress, screenPoint: nil, endScreenPoint: nil, element: focused.element, verifiedElement: focused)
        case .scroll:
            let local = CGPoint(x: action.x ?? expected.bounds.width / 2, y: action.y ?? expected.bounds.height / 2)
            return ResolvedAction(source: action, method: .scroll, screenPoint: try convert(local, expected: expected), endScreenPoint: nil, element: nil)
        case .drag:
            guard let x = action.x, let y = action.y, let endX = action.endX, let endY = action.endY else { throw ActionExecutionError.invalidAction }
            return ResolvedAction(source: action, method: .drag, screenPoint: try convert(CGPoint(x: x, y: y), expected: expected), endScreenPoint: try convert(CGPoint(x: endX, y: endY), expected: expected), element: nil)
        case .wait:
            return ResolvedAction(source: action, method: .wait, screenPoint: nil, endScreenPoint: nil, element: nil)
        }
    }

    private func convert(_ local: CGPoint, expected: ActionGuard) throws -> CGPoint {
        guard local.x.isFinite, local.y.isFinite,
              local.x >= 0, local.y >= 0, local.x < expected.bounds.width, local.y < expected.bounds.height
        else { throw ActionExecutionError.outOfBounds }
        let screen = CGPoint(x: expected.bounds.minX + local.x, y: expected.bounds.minY + local.y)
        guard screen.x.isFinite, screen.y.isFinite, expected.bounds.contains(screen), provider.isVisibleOnScreen(screen) else {
            throw ActionExecutionError.outOfBounds
        }
        return screen
    }
}

private func actionError(_ error: Error) -> ActionExecutionError {
    if let error = error as? ActionExecutionError { return error }
    return .helperFailed
}

private func finiteRect(_ rect: CGRect) -> Bool {
    rect.origin.x.isFinite && rect.origin.y.isFinite && rect.width.isFinite && rect.height.isFinite && rect.width > 0 && rect.height > 0
}

private func validLocalRect(_ rect: CGRect, within size: CGSize) -> Bool {
    finiteRect(rect) && rect.minX >= 0 && rect.minY >= 0 && rect.maxX <= size.width && rect.maxY <= size.height
}

private func approximatelyEqualActionRect(_ lhs: CGRect, _ rhs: CGRect) -> Bool {
    abs(lhs.origin.x - rhs.origin.x) <= 1 && abs(lhs.origin.y - rhs.origin.y) <= 1 && abs(lhs.width - rhs.width) <= 1 && abs(lhs.height - rhs.height) <= 1
}

enum AXSelectedTextWriteResult: Equatable {
    case written
    case unsupported
    case failed(ActionExecutionError, inputStarted: Bool)
    /// The application accepted the write, yet a readable value stayed unchanged for the whole
    /// settle window (live: Edge web fields). Nothing was typed; callers deliver by keyboard.
    case ineffective
}

/// Readback used to tell a real AXSelectedText write from one the application silently ignores.
struct AXTextWriteEffectProbe {
    let readValue: (AXUIElement) -> String?
    let readSelectedText: (AXUIElement) -> String?
    let settleMilliseconds: Int
    let pollMilliseconds: Int
    let sleepMilliseconds: (Int) -> Void

    static let live = AXTextWriteEffectProbe(
        readValue: { completeAXString($0, kAXValueAttribute) },
        readSelectedText: { completeAXString($0, kAXSelectedTextAttribute) },
        settleMilliseconds: 300, pollMilliseconds: 25,
        sleepMilliseconds: { usleep(useconds_t(max(0, $0)) * 1_000) })
}

/// Truncated or failed reads are unknown, never evidence that a value is unchanged.
private func completeAXString(_ element: AXUIElement, _ attribute: String) -> String? {
    let result = AXNodeReader.stringAttribute(element, attribute)
    guard result.status == .complete else { return nil }
    return result.value
}

/// Processes where an AXSelectedText write was ignored or became unavailable in this helper
/// lifetime. Their later text is delivered by keyboard instead of repeating the unreliable path.
final class AXTextWriteEffectMemory: @unchecked Sendable {
    static let shared = AXTextWriteEffectMemory()
    private let lock = NSLock()
    private var ignoringPIDs: Set<pid_t> = []

    func recordIgnoredSelectedTextWrite(pid: pid_t) {
        lock.lock(); defer { lock.unlock() }
        ignoringPIDs.insert(pid)
    }

    func ignoresSelectedTextWrites(pid: pid_t) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return ignoringPIDs.contains(pid)
    }
}

/// Chromium browsers and Electron apps ship "(Renderer)" helper apps. Their native text fields
/// can take an AXSelectedText write that reads back exactly while the app's own input model
/// never sees it (an address bar then ignores Return). Detect by bundle layout, not by name.
func applicationBundleShipsChromiumRenderer(_ bundle: URL, fileManager: FileManager = .default) -> Bool {
    func entries(_ directory: URL, limit: Int) -> ArraySlice<URL> {
        ((try? fileManager.contentsOfDirectory(at: directory, includingPropertiesForKeys: [.isDirectoryKey],
            options: [.skipsHiddenFiles])) ?? []).prefix(limit)
    }
    func isRendererApp(_ url: URL) -> Bool {
        url.lastPathComponent.hasSuffix("(Renderer).app") &&
            (try? url.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true
    }
    for entry in entries(bundle.appendingPathComponent("Contents/Frameworks", isDirectory: true), limit: 256) {
        // Electron keeps the helper beside its framework; Chromium browsers keep it inside
        // the versioned framework.
        if isRendererApp(entry) { return true }
        guard entry.pathExtension == "framework" else { continue }
        for version in entries(entry.appendingPathComponent("Versions", isDirectory: true), limit: 8)
        where entries(version.appendingPathComponent("Helpers", isDirectory: true), limit: 64)
            .contains(where: isRendererApp) {
            return true
        }
    }
    return false
}

/// One bundle-layout probe per application bundle in this helper lifetime.
final class ChromiumProcessClassifier: @unchecked Sendable {
    static let shared = ChromiumProcessClassifier()
    private let lock = NSLock()
    private var bundles: [String: Bool] = [:]
    private let bundleURL: (pid_t) -> URL?
    private let bundleShipsRenderer: (URL) -> Bool

    init(
        bundleURL: @escaping (pid_t) -> URL? = { NSRunningApplication(processIdentifier: $0)?.bundleURL },
        bundleShipsRenderer: @escaping (URL) -> Bool = { applicationBundleShipsChromiumRenderer($0) }
    ) {
        self.bundleURL = bundleURL
        self.bundleShipsRenderer = bundleShipsRenderer
    }

    func usesChromiumRenderer(pid: pid_t) -> Bool {
        guard pid > 0, let bundle = bundleURL(pid) else { return false }
        let key = bundle.standardizedFileURL.path
        lock.lock()
        if let known = bundles[key] {
            lock.unlock()
            return known
        }
        lock.unlock()
        let result = bundleShipsRenderer(bundle)
        lock.lock()
        bundles[key] = result
        lock.unlock()
        if result { logActionRejected("TEXT-ROUTE chromium_family pid=\(pid) text uses keyboard delivery") }
        return result
    }
}

/// Web content text fields never take AXSelectedText writes usefully: WebKit rejects them and
/// Chromium accepts them without changing the page. Detect by structure, not by app name.
func axElementIsInsideWebArea(
    _ element: AXUIElement,
    parent: (AXUIElement) -> AXUIElement? = { decodeAXElement(observationAXAttribute($0, kAXParentAttribute).1) },
    role: (AXUIElement) -> String? = { completeAXString($0, kAXRoleAttribute) },
    maximumDepth: Int = 64
) -> Bool {
    var current = parent(element)
    var depth = 0
    while let node = current, depth < maximumDepth {
        if role(node) == "AXWebArea" { return true }
        current = parent(node)
        depth += 1
    }
    return false
}

enum AXTextMutationPreflight: Equatable {
    case settable
    case unsupported
    case failed(ActionExecutionError)
}

enum BackgroundTextInputSafety: Equatable {
    case safeASCIIKeyboardLayout
    case imeOrCandidate
    case unknown
}

protocol BackgroundTextInputSafetyDetecting {
    func detect() -> BackgroundTextInputSafety
}

struct BackgroundTextInputSourceSnapshot: Equatable {
    let category: String?
    let sourceType: String?
    let isASCIICapable: Bool?
}

/// 诊断用：helper **自己进程内**看到的当前键盘输入源 ID。
/// TIS 的进程内缓存须先处理输入源变更通知；外部 CLI 可作对照，不能替代动作许可。
func currentKeyboardInputSourceIDForDiagnostics(
    readGate: KeyboardInputSourceReadGate = .shared,
    readIdentifier: (() -> String?)? = nil
) -> String {
    let read = readIdentifier ?? {
        guard let unmanaged = TISCopyCurrentKeyboardInputSource() else { return "nil-source" }
        let source = unmanaged.takeRetainedValue()
        guard let raw = TISGetInputSourceProperty(source, kTISPropertyInputSourceID) else { return "nil-id" }
        return Unmanaged<CFString>.fromOpaque(raw).takeUnretainedValue() as String
    }
    return readGate.read(read) ?? "unavailable-source"
}

struct SystemBackgroundTextInputSafetyDetector: BackgroundTextInputSafetyDetecting {
    private let snapshot: () -> BackgroundTextInputSourceSnapshot?

    init(
        snapshot: (() -> BackgroundTextInputSourceSnapshot?)? = nil,
        readGate: KeyboardInputSourceReadGate = .shared
    ) {
        let readSnapshot = snapshot ?? Self.currentSnapshot
        self.snapshot = { readGate.read(readSnapshot) }
    }

    func detect() -> BackgroundTextInputSafety {
        guard let snapshot = snapshot(),
              let category = snapshot.category,
              let sourceType = snapshot.sourceType,
              let isASCIICapable = snapshot.isASCIICapable
        else { return .unknown }
        guard category == kTISCategoryKeyboardInputSource as String,
              sourceType == kTISTypeKeyboardLayout as String,
              isASCIICapable
        else { return .imeOrCandidate }
        return .safeASCIIKeyboardLayout
    }

    private static func currentSnapshot() -> BackgroundTextInputSourceSnapshot? {
        guard let unmanaged = TISCopyCurrentKeyboardInputSource() else { return nil }
        let source = unmanaged.takeRetainedValue()
        return BackgroundTextInputSourceSnapshot(
            category: stringProperty(source, key: kTISPropertyInputSourceCategory),
            sourceType: stringProperty(source, key: kTISPropertyInputSourceType),
            isASCIICapable: boolProperty(source, key: kTISPropertyInputSourceIsASCIICapable)
        )
    }

    private static func stringProperty(_ source: TISInputSource, key: CFString) -> String? {
        guard let pointer = TISGetInputSourceProperty(source, key) else { return nil }
        return Unmanaged<CFString>.fromOpaque(pointer).takeUnretainedValue() as String
    }

    private static func boolProperty(_ source: TISInputSource, key: CFString) -> Bool? {
        guard let pointer = TISGetInputSourceProperty(source, key) else { return nil }
        return CFBooleanGetValue(Unmanaged<CFBoolean>.fromOpaque(pointer).takeUnretainedValue())
    }
}

protocol AXSelectedTextWriting {
    func preflightSelectedText(to element: AXUIElement) -> AXTextMutationPreflight
    func writeSelectedText(_ text: String, to element: AXUIElement) -> AXSelectedTextWriteResult
    func writeSelectedText(
        _ text: String,
        to element: AXUIElement,
        validateBeforeMutation: () throws -> Void
    ) throws -> AXSelectedTextWriteResult
}

extension AXSelectedTextWriting {
    func preflightSelectedText(to _: AXUIElement) -> AXTextMutationPreflight { .unsupported }
    func writeSelectedText(
        _ text: String,
        to element: AXUIElement,
        validateBeforeMutation: () throws -> Void
    ) throws -> AXSelectedTextWriteResult {
        try validateBeforeMutation()
        return writeSelectedText(text, to: element)
    }
}

struct SystemAXSelectedTextWriter: AXSelectedTextWriting {
    private let isSettable: (AXUIElement) -> (AXError, Bool)
    private let setValue: (AXUIElement, String) -> AXError
    /// Effect readback. Default nil = legacy "API success means written"; production injects
    /// the live probe in `makeActionPerformer`.
    private let effectProbe: AXTextWriteEffectProbe?

    init(
        isSettable: ((AXUIElement) -> (AXError, Bool))? = nil,
        setValue: ((AXUIElement, String) -> AXError)? = nil,
        effectProbe: AXTextWriteEffectProbe? = nil
    ) {
        self.effectProbe = effectProbe
        self.isSettable = isSettable ?? { element in
            var flag = DarwinBoolean(false)
            let error = AXUIElementIsAttributeSettable(
                element,
                kAXSelectedTextAttribute as CFString,
                &flag
            )
            return (error, flag.boolValue)
        }
        self.setValue = setValue ?? { element, text in
            AXUIElementSetAttributeValue(
                element,
                kAXSelectedTextAttribute as CFString,
                text as CFString
            )
        }
    }

    func writeSelectedText(_ text: String, to element: AXUIElement) -> AXSelectedTextWriteResult {
        do {
            return try writeSelectedText(text, to: element, validateBeforeMutation: {})
        } catch {
            return .failed(.helperFailed, inputStarted: false)
        }
    }

    func writeSelectedText(
        _ text: String,
        to element: AXUIElement,
        validateBeforeMutation: () throws -> Void
    ) throws -> AXSelectedTextWriteResult {
        switch preflightSelectedText(to: element) {
        case .settable:
            break
        case .unsupported:
            return .unsupported
        case let .failed(error):
            return .failed(error, inputStarted: false)
        }
        try validateBeforeMutation()
        let before = effectProbe?.readValue(element)
        let selectedBefore = effectProbe?.readSelectedText(element)
        // AX reads may block while the input source or focus changes. Recheck after
        // them so the final guard sits immediately before the write.
        try validateBeforeMutation()
        let setError = setValue(element, text)
        guard setError == .success else {
            // A refused write changed nothing, like an unsupported one; callers then use the
            // keyboard or report that nothing was sent instead of an uncertain outcome.
            if axErrorRefusedBeforeActing(setError) {
                logActionRejected("AX-MAP raw=\(setError.rawValue) refused")
                return .unsupported
            }
            return .failed(mapAXActionError(setError), inputStarted: true)
        }
        // Only a readable, unchanged value across the settle window proves the write was
        // ignored. Retyping the current selection legitimately leaves the value as it was.
        guard let effectProbe, let before, !text.isEmpty, selectedBefore != text else { return .written }
        var waited = 0
        while true {
            guard let after = effectProbe.readValue(element), after == before else { return .written }
            if waited >= effectProbe.settleMilliseconds { return .ineffective }
            effectProbe.sleepMilliseconds(effectProbe.pollMilliseconds)
            waited += effectProbe.pollMilliseconds
        }
    }

    func preflightSelectedText(to element: AXUIElement) -> AXTextMutationPreflight {
        let (queryError, settable) = isSettable(element)
        if queryError == .notImplemented || queryError == .attributeUnsupported {
            return .unsupported
        }
        guard queryError == .success else {
            return .failed(mapAXActionError(queryError))
        }
        return settable ? .settable : .unsupported
    }
}

/// 写 `AXFocused=true` 把键盘焦点带到**本次就要打字进去**的那个文本元素，然后**重新读取**
/// `AXFocusedUIElement` 复核身份；任一步不如实则返回 nil（调用方照旧 inputFocusRequired）。
/// 角色/安全/禁用判定在调用点（keyboardFocusAcquisitionAllowed），这里不重复放宽。
/// 能力只在装配处显式传入（见 Windows.swift 的 makeActionPerformer），不作为隐式默认。
func acquireKeyboardFocusViaAX(
    state: ActionTargetState,
    expected: ActionElement
) -> ActionElement? {
    acquireKeyboardFocus(
        expected: expected,
        setFocused: {
            AXUIElementSetAttributeValue($0, kAXFocusedAttribute as CFString, kCFBooleanTrue) == .success
        },
        readFocused: {
            var value: CFTypeRef?
            guard AXUIElementCopyAttributeValue(
                AXUIElementCreateApplication(state.pid),
                kAXFocusedUIElementAttribute as CFString,
                &value
            ) == .success else { return nil }
            return decodeAXElement(value)
        },
        identity: { focused in
            readActionElementIdentity(
                provider: AXActionIdentityProvider(element: focused),
                identityToken: "ax:\(CFHash(focused))",
                windowBounds: .zero
            )
        }
    )
}

/// Chromium applies an AX focus request asynchronously (live: Edge right after a page button
/// press), so poll briefly for the requested element before judging the previous focus. The
/// caller still compares the returned identity and refuses any other element.
func acquireKeyboardFocus(
    expected: ActionElement,
    setFocused: (AXUIElement) -> Bool,
    readFocused: () -> AXUIElement?,
    identity: (AXUIElement) -> ActionElement?,
    settleMilliseconds: Int = 300,
    pollMilliseconds: Int = 25,
    sleepMilliseconds: (Int) -> Void = { usleep(useconds_t(max(0, $0)) * 1_000) }
) -> ActionElement? {
    guard let element = expected.element, setFocused(element) else { return nil }
    var focused = readFocused()
    var waited = 0
    while !(focused.map { CFEqual($0, element) } ?? false), waited < settleMilliseconds {
        sleepMilliseconds(pollMilliseconds)
        waited += pollMilliseconds
        focused = readFocused()
    }
    guard let focused else { return nil }
    return identity(focused)
}

final class SystemActionPerformer: ActionProviding {
    private let state: () throws -> ActionTargetState
    private let lookup: (String, String) -> ActionElement?
    private let pointerLookup: ((String, String) -> ActionElement?)?
    private let scrollPressLookup: ((String, String, AXScrollDirection) -> AXScrollPressTarget?)?
    private let inputPoster: any SyntheticInputPosting
    private let selectedTextWriter: any AXSelectedTextWriting
    private let savePanelGuard: ((NativeAction) throws -> Void)?
    private let textValueReplacer: any AXTextValueReplacing
    private let keyboardTargetValidation: ((ActionElement) throws -> Void)?
    private let validateMenuPress: ((AXUIElement) throws -> Void)?
    private let performAXAction: (AXUIElement, CFString) -> AXError
    private let heldInputs: HeldInputRegistry
    private let injectedKeyboardFocus: ((ActionElement?) throws -> ActionElement)?
    /// 键盘焦点获取能力。**默认 nil ＝ 永不获取**：能力必须在装配处显式开启，
    /// 既让单测不必碰真实 AX，也保证这不是一个隐式全局行为。
    private let focusAcquisition: ((ActionTargetState, ActionElement) -> ActionElement?)?
    /// Production reads the live keyboard source and the exact editor's composition at every AX mutation.
    private let textInputSafety: (() -> BackgroundTextInputSafety)?
    private let textCompositionProbe: (ActionElement) -> AXTextComposition
    /// Web content detection, ignored-write memory and the Chromium-family process probe.
    /// Default nil = no extra routing (legacy unit paths unchanged); production injects all
    /// three in `makeActionPerformer`.
    private let webContentProbe: ((AXUIElement) -> Bool)?
    private let axTextWriteMemory: AXTextWriteEffectMemory?
    private let keyboardOnlyTextProcess: ((pid_t) -> Bool)?

    init(
        state: @escaping () throws -> ActionTargetState,
        lookup: @escaping (String, String) -> ActionElement?,
        pointerLookup: ((String, String) -> ActionElement?)? = nil,
        scrollPressLookup: ((String, String, AXScrollDirection) -> AXScrollPressTarget?)? = nil,
        inputPoster: any SyntheticInputPosting = UnavailableSyntheticInputPoster(),
        selectedTextWriter: any AXSelectedTextWriting = SystemAXSelectedTextWriter(),
        textValueReplacer: any AXTextValueReplacing = SystemAXTextValueReplacer(),
        savePanelGuard: ((NativeAction) throws -> Void)? = nil,
        keyboardTargetValidation: ((ActionElement) throws -> Void)? = nil,
        validateMenuPress: ((AXUIElement) throws -> Void)? = nil,
        performAXAction: ((AXUIElement, CFString) -> AXError)? = nil,
        heldInputs: HeldInputRegistry = .shared,
        focusedKeyboard: ((ActionElement?) throws -> ActionElement)? = nil,
        focusAcquisition: ((ActionTargetState, ActionElement) -> ActionElement?)? = nil
        ,textInputSafety: (() -> BackgroundTextInputSafety)? = nil,
        textCompositionProbe: @escaping (ActionElement) -> AXTextComposition = AXTextComposition.observe,
        webContentProbe: ((AXUIElement) -> Bool)? = nil,
        axTextWriteMemory: AXTextWriteEffectMemory? = nil,
        keyboardOnlyTextProcess: ((pid_t) -> Bool)? = nil
    ) {
        self.webContentProbe = webContentProbe
        self.axTextWriteMemory = axTextWriteMemory
        self.keyboardOnlyTextProcess = keyboardOnlyTextProcess
        self.state = state
        self.lookup = lookup
        self.pointerLookup = pointerLookup
        self.scrollPressLookup = scrollPressLookup
        self.inputPoster = inputPoster
        self.selectedTextWriter = selectedTextWriter
        self.textValueReplacer = textValueReplacer
        self.savePanelGuard = savePanelGuard
        self.keyboardTargetValidation = keyboardTargetValidation
        self.validateMenuPress = validateMenuPress
        self.performAXAction = performAXAction ?? AXUIElementPerformAction
        self.heldInputs = heldInputs
        injectedKeyboardFocus = focusedKeyboard
        self.focusAcquisition = focusAcquisition
        self.textInputSafety = textInputSafety
        self.textCompositionProbe = textCompositionProbe
    }

    func currentTargetState() throws -> ActionTargetState { try state() }
    func element(reference: String, snapshotID: String) -> ActionElement? { lookup(reference, snapshotID) }
    func pointerElement(reference: String, snapshotID: String) -> ActionElement? {
        pointerLookup?(reference, snapshotID)
    }
    func scrollPressTarget(
        reference: String,
        snapshotID: String,
        direction: AXScrollDirection
    ) -> AXScrollPressTarget? {
        scrollPressLookup?(reference, snapshotID, direction)
    }
    func isVisibleOnScreen(_ point: CGPoint) -> Bool { activeDisplayBounds().contains { $0.contains(point) } }

    func validateSaveDestination(_ action: NativeAction) throws {
        guard action.saveNewCopyTo != nil else { return }
        guard let savePanelGuard else { throw ActionExecutionError.saveDestinationUnverified }
        try savePanelGuard(action)
    }

    func preflightAXTextMutation(_ element: ActionElement) -> AXTextMutationPreflight {
        guard !element.isSecure, element.enabled == true, element.supportsAXSelectedTextWrite,
              let axElement = element.element, axTextWriteMayLand(axElement)
        else { return .unsupported }
        return selectedTextWriter.preflightSelectedText(to: axElement)
    }

    /// False routes the text to keyboard delivery: web content, a Chromium-family process
    /// whose own input model ignores AX-inserted text, or a process that already ignored an
    /// AXSelectedText write in this helper lifetime.
    private func axTextWriteMayLand(_ element: AXUIElement) -> Bool {
        if webContentProbe?(element) == true || inKeyboardOnlyTextProcess(element) { return false }
        guard let axTextWriteMemory else { return true }
        var pid: pid_t = 0
        guard AXUIElementGetPid(element, &pid) == .success else { return true }
        return !axTextWriteMemory.ignoresSelectedTextWrites(pid: pid)
    }

    private func inKeyboardOnlyTextProcess(_ element: AXUIElement) -> Bool {
        guard let keyboardOnlyTextProcess else { return false }
        var pid: pid_t = 0
        return AXUIElementGetPid(element, &pid) == .success && keyboardOnlyTextProcess(pid)
    }

    private func rememberIgnoredAXTextWrite(_ element: AXUIElement) {
        var pid: pid_t = 0
        guard AXUIElementGetPid(element, &pid) == .success else { return }
        axTextWriteMemory?.recordIgnoredSelectedTextWrite(pid: pid)
        logActionRejected("AX-TEXT-INEFFECTIVE pid=\(pid) value unchanged after an accepted AXSelectedText write")
    }

    func noteAXTextWriteUnavailable(_ element: ActionElement) {
        guard !element.isSecure, element.enabled == true,
              element.supportsAXSelectedTextWrite, let axElement = element.element,
              webContentProbe?(axElement) != true
        else { return }
        rememberUnavailableAXTextWrite(axElement)
    }

    private func rememberUnavailableAXTextWrite(_ element: AXUIElement) {
        var pid: pid_t = 0
        guard AXUIElementGetPid(element, &pid) == .success else { return }
        axTextWriteMemory?.recordIgnoredSelectedTextWrite(pid: pid)
        logActionRejected("AX-TEXT-UNAVAILABLE pid=\(pid) selected-text write refused before mutation")
    }

    func preflightAXTextReplacement(_ element: ActionElement) -> AXTextMutationPreflight {
        // A Chromium-family field can read back the replaced value while the app's own
        // input model never saw it; refuse before input so keyboard typing is chosen instead.
        guard keyboardTargetValidation != nil, !element.isSecure, element.enabled == true,
              element.supportsAXSelectedTextWrite, let ax = element.element,
              !inKeyboardOnlyTextProcess(ax) else { return .unsupported }
        return textValueReplacer.preflight(to: ax)
    }

    func preflightAXRangeReplacement(_ element: ActionElement, matching match: AXTextMatch) -> AXTextMutationPreflight {
        if IsSecureEventInputEnabled() { return .failed(.secureInputActive) }
        guard keyboardTargetValidation != nil, let ax = element.element, element.acceptsFocusedKeyboardInput,
              element.enabled == true, element.supportsAXSelectedTextWrite, axTextWriteMayLand(ax)
        else { return .unsupported }
        return textValueReplacer.preflightRange(to: ax, matching: match)
    }

    func textComposition(of element: ActionElement) -> AXTextComposition { textCompositionProbe(element) }

    func permitsIMEAXWrite(to element: ActionElement) -> Bool {
        guard element.supportsAXSelectedTextWrite, element.acceptsFocusedKeyboardInput,
              element.enabled == true, let field = element.element else { return false }
        return axTextWriteMayLand(field)
    }

    private func validateAXTextInput(_ target: ActionElement) throws {
        do {
            guard try allowsAXText(target, source: textInputSafety?() ?? .safeASCIIKeyboardLayout) else {
                throw ActionExecutionError.textInputSourceUnverified
            }
        } catch let error as ActionExecutionError {
            throw ActionPerformFailure(error: error, inputStarted: false)
        }
    }


    func validateBackgroundReplacementTarget(_ element: ActionElement) throws {
        guard let keyboardTargetValidation else { throw ActionExecutionError.keyboardFocusUnverified }
        // The application can retain its focused editor behind Terminal. Verify the same
        // exact frontmost document and field required at mutation, without acquiring focus.
        try keyboardTargetValidation(element)
        _ = try focusedKeyboardElement(matching: element)
    }

    func selectedUnicodeWitness(for action: NativeAction, element: ActionElement) throws -> AXKeyboardRangeWitness {
        guard action.supportsSelectedUnicodeEvent, let match = action.textMatch, let text = action.text, let ax = element.element,
              let keyboardTargetValidation, element.supportsAXSelectedTextWrite else { throw ActionExecutionError.textRangeUnsupported }
        func validate() throws {
            try keyboardTargetValidation(element)
            if IsSecureEventInputEnabled() { throw ActionExecutionError.secureInputActive }
            _ = try self.focusedKeyboardElement(matching: element)
        }
        try validate()
        let witness = try textValueReplacer.keyboardWitness(for: match, replacingWith: text, in: ax)
        return AXKeyboardRangeWitness(beforeInput: {
            try validate()
            try witness.beforeInput()
        }, afterInput: {
            try validate()
            return try witness.afterInput()
        })
    }

    /// 焦点只允许被带到**本次就要往里打字**的文本元素上（与既有写入白名单同源），
    /// 安全字段与被禁用的一律拒绝。放在调用点，注入闭包也绕不过它。
    static func keyboardFocusAcquisitionAllowed(for expected: ActionElement) -> Bool {
        expected.supportsAXSelectedTextWrite && expected.acceptsFocusedKeyboardInput && expected.enabled == true
    }

    func focusedKeyboardElement(matching expected: ActionElement?) throws -> ActionElement {
        try readKeyboardFocus(matching: expected, selected: state())
    }

    func acquireOrVerifyKeyboardFocus(matching expected: ActionElement?,
        validateFocusMutation: () throws -> Void
    ) throws -> ActionElement {
        let selected = try state()
        let observed: ActionElement
        do {
            observed = try readKeyboardFocus(matching: expected, selected: selected)
        } catch let error as ActionExecutionError where error == .inputFocusRequired || error == .targetNotFrontmost {
            guard let expected, let focusAcquisition,
                  Self.keyboardFocusAcquisitionAllowed(for: expected) else { throw error }
            // A granted lease is not proof that this window is still frontmost, or that
            // a field retained across a wait is still safe and belongs to this exact root.
            guard let keyboardTargetValidation else { throw ActionExecutionError.staleSnapshot }
            try keyboardTargetValidation(expected)
            try validateFocusMutation()
            guard let acquired = focusAcquisition(selected, expected), let live = acquired.element,
                  let retained = expected.element, CFEqual(live, retained),
                  Self.keyboardFocusAcquisitionAllowed(for: acquired),
                  acquired.roleResult == expected.roleResult, acquired.subroleResult == expected.subroleResult
            else { throw ActionExecutionError.inputFocusRequired }
            return acquired
        }
        try validateFocusMutation()
        return observed
    }

    private func readKeyboardFocus(matching expected: ActionElement?, selected: ActionTargetState) throws -> ActionElement {
        let requiresFrontmostWindowContract = expected == nil
        if requiresFrontmostWindowContract {
            guard liveFrontmostPID() == selected.pid else {
                throw ActionExecutionError.targetNotFrontmost
            }
        }
        let focusedIdentity: ActionElement
        if let injectedKeyboardFocus {
            focusedIdentity = try injectedKeyboardFocus(expected)
        } else {
            let application = AXUIElementCreateApplication(selected.pid)
            var value: CFTypeRef?
            let error = AXUIElementCopyAttributeValue(
                application,
                kAXFocusedUIElementAttribute as CFString,
                &value
            )
            if error == .noValue, expected != nil { throw ActionExecutionError.inputFocusRequired }
            guard error == .success else { throw mapAXActionError(error) }
            guard let focused = decodeAXElement(value) else {
                throw ActionExecutionError.targetNotFrontmost
            }
            focusedIdentity = readActionElementIdentity(
                provider: AXActionIdentityProvider(element: focused),
                identityToken: "ax:\(CFHash(focused))",
                windowBounds: .zero
            )
        }
        if let rejection = focusedIdentity.keyboardTargetRejection { throw rejection }
        guard focusedIdentity.enabled == true else { throw ActionExecutionError.keyboardFocusUnverified }
        if let expected {
            guard let expectedElement = expected.element,
                  let focusedElement = focusedIdentity.element
            else {
                logActionRejected("PERFORM-TARGET element-nil")
                throw ActionExecutionError.staleSnapshot
            }
            guard CFEqual(focusedElement, expectedElement) else {
                throw ActionExecutionError.inputFocusRequired
            }
        }
        if requiresFrontmostWindowContract {
            guard liveFrontmostPID() == selected.pid else {
                throw ActionExecutionError.targetNotFrontmost
            }
        }
        return focusedIdentity
    }

    func perform(_ action: ResolvedAction) throws -> ActionPerformance {
        // Background execution can write to a still-focused field, never focus it.
        try perform(action, validateMutation: {}, keyboardFocus: focusedKeyboardElement(matching:))
    }

    func perform(_ action: ResolvedAction, validateMutation: () throws -> Void) throws -> ActionPerformance {
        try perform(action, validateMutation: validateMutation, keyboardFocus: {
            try self.acquireOrVerifyKeyboardFocus(matching: $0, validateFocusMutation: validateMutation)
        })
    }

    private func perform(_ action: ResolvedAction, validateMutation: () throws -> Void,
        keyboardFocus: (ActionElement?) throws -> ActionElement
    ) throws -> ActionPerformance {
        guard action.source.replace != true else {
            throw ActionPerformFailure(error: .invalidAction, inputStarted: false)
        }
        guard let checked = action.source.checked else {
            return try performUnverified(action, validateMutation: validateMutation, keyboardFocus: keyboardFocus)
        }
        guard let element = action.element,
              let role = action.verifiedElement?.roleResult.value,
              role == "AXRadioButton" || role == "AXCheckBox"
        else { throw ActionPerformFailure(error: .invalidAction, inputStarted: false) }
        let reader = SystemAXEffectReader()
        return try performCheckedAction(expected: checked, radio: role == "AXRadioButton", read: {
            guard let value = reader.read(element, remainingBudget: 0.05)?.value else { return nil }
            switch value {
            case let .boolean(value): return value
            case .number(0), .text("0"), .text("false"): return false
            case .number(1), .text("1"), .text("true"): return true
            default: return nil
            }
        }, perform: { try self.performUnverified(action, validateMutation: validateMutation, keyboardFocus: keyboardFocus) })
    }

    func performReplacement(_ action: ResolvedAction, validateMutation: () throws -> Void) throws -> ActionPerformance {
        guard action.source.isReplacement, action.source.checked == nil else {
            throw ActionPerformFailure(error: .invalidAction, inputStarted: false)
        }
        return try performUnverified(action, validateMutation: validateMutation, keyboardFocus: {
            try self.acquireOrVerifyKeyboardFocus(matching: $0, validateFocusMutation: validateMutation)
        })
    }

    private func performUnverified(_ action: ResolvedAction, validateMutation: () throws -> Void,
        keyboardFocus: (ActionElement?) throws -> ActionElement
    ) throws -> ActionPerformance {
        func revalidateKeyboardTarget(_ expected: ActionElement?, observeOnly: Bool = false) throws -> ActionElement {
            do {
                let current = try observeOnly ? self.focusedKeyboardElement(matching: expected) : keyboardFocus(expected)
                if let rejection = current.keyboardTargetRejection { throw rejection }
                return current
            } catch let error as ActionExecutionError {
                throw ActionPerformFailure(error: error, inputStarted: false)
            } catch let error as UserActivityMonitoringError {
                throw error
            } catch {
                throw ActionPerformFailure(error: .helperFailed, inputStarted: false)
            }
        }
        func validateTextSetter(_ field: ActionElement) throws {
            guard field.enabled == true, let expected = action.verifiedElement,
                  field.roleResult == expected.roleResult, field.subroleResult == expected.subroleResult
            else { throw ActionPerformFailure(error: .keyboardFocusUnverified, inputStarted: false) }
            try validateMutation()
            // The marked-range read is the last AX query. A later focus query could itself
            // start composition. After this point the production guards only check HID
            // activity, the loan deadline and secure input before the caller invokes the setter.
            try self.validateAXTextInput(field)
            if IsSecureEventInputEnabled() { throw ActionPerformFailure(error: .secureInputActive, inputStarted: false) }
            try validateMutation()
        }
        if action.source.isReplacement {
            guard action.source.kind == .type,
                  action.source.elementRef != nil, action.method == .accessibilityText
            else { throw ActionPerformFailure(error: .invalidAction, inputStarted: false) }
        }
        do { _ = try state() }
        catch let error as ActionExecutionError {
            logActionRejected("PERFORM-STATE error=\(error)")
            throw ActionPerformFailure(error: error, inputStarted: false)
        }
        catch {
            logActionRejected("PERFORM-STATE-OTHER")
            throw ActionPerformFailure(error: .helperFailed, inputStarted: false)
        }
        switch action.method {
        case .accessibilityPress:
            guard let element = action.element else {
                logActionRejected("PERFORM element-nil method=\(action.method)")
                throw ActionPerformFailure(error: .staleSnapshot, inputStarted: false)
            }
            if applicationMenuRole(action.verifiedElement?.roleResult.value) {
                guard let validateMenuPress else { throw ActionPerformFailure(error: .staleSnapshot, inputStarted: false) }
                do { try validateMenuPress(element) }
                catch { throw ActionPerformFailure(error: .staleSnapshot, inputStarted: false) }
            }
            if action.source.saveNewCopyTo != nil {
                do { try validateSaveDestination(action.source) }
                catch let error as ActionExecutionError { throw ActionPerformFailure(error: error, inputStarted: false) }
            }
            try validateMutation()
            let error = performAXAction(element, kAXPressAction as CFString)
            guard error == .success else {
                actionFailureDiagnostic("AXPress failed: AXError.rawValue=\(error.rawValue) role=\(action.verifiedElement?.roleResult.value ?? "?")")
                throw axMutationFailure(error)
            }
            return ActionPerformance(inputStarted: true)
        case .accessibilityIncrement:
            guard let element = action.element else {
                logActionRejected("PERFORM element-nil method=\(action.method)")
                throw ActionPerformFailure(error: .staleSnapshot, inputStarted: false)
            }
            try validateMutation()
            let error = performAXAction(element, kAXIncrementAction as CFString)
            guard error == .success else { throw axMutationFailure(error) }
            return ActionPerformance(inputStarted: true)
        case .accessibilityDecrement:
            guard let element = action.element else {
                logActionRejected("PERFORM element-nil method=\(action.method)")
                throw ActionPerformFailure(error: .staleSnapshot, inputStarted: false)
            }
            try validateMutation()
            let error = performAXAction(element, kAXDecrementAction as CFString)
            guard error == .success else { throw axMutationFailure(error) }
            return ActionPerformance(inputStarted: true)
        case .accessibilityText:
            guard let element = action.element else {
                logActionRejected("PERFORM element-nil method=\(action.method)")
                throw ActionPerformFailure(error: .staleSnapshot, inputStarted: false)
            }
            if action.source.isReplacement {
                guard let expected = action.verifiedElement, let keyboardTargetValidation else {
                    throw ActionPerformFailure(error: .invalidAction, inputStarted: false)
                }
                var validatedFocus = false
                func validateTextMutation() throws {
                    try validateMutation()
                    _ = try self.state()
                    let current = try revalidateKeyboardTarget(expected, observeOnly: validatedFocus)
                    guard current.supportsAXSelectedTextWrite, current.enabled == true,
                          current.roleResult == expected.roleResult, current.subroleResult == expected.subroleResult
                    else { throw ActionExecutionError.keyboardFocusUnverified }
                    try keyboardTargetValidation(expected)
                    try validateTextSetter(current)
                    validatedFocus = true
                }
                let replacement = action.source.text ?? ""
                let result: TextReplacementResult
                if let match = action.source.textMatch {
                    result = try textValueReplacer.replaceRange(match, with: replacement, in: element,
                        validateBeforeMutation: validateTextMutation)
                } else {
                    result = try textValueReplacer.replace(replacement, to: element, validateBeforeMutation: validateTextMutation)
                }
                return ActionPerformance(inputStarted: result.inputStarted, effectVerification: result.effectVerification,
                    observationRequired: result.observationRequired)
            }
            let exactTarget = try revalidateKeyboardTarget(action.verifiedElement)
            try validateAXTextInput(exactTarget)
            guard axTextWriteMayLand(element) else {
                throw ActionPerformFailure(error: .staleSnapshot, inputStarted: false)
            }
            switch selectedTextWriter.preflightSelectedText(to: element) {
            case .settable:
                break
            case .unsupported:
                rememberUnavailableAXTextWrite(element)
                throw ActionPerformFailure(error: .staleSnapshot, inputStarted: false)
            case let .failed(error):
                throw ActionPerformFailure(error: error, inputStarted: false)
            }
            let selectedTextResult = try selectedTextWriter.writeSelectedText(
                action.source.text ?? "",
                to: element,
                validateBeforeMutation: {
                    guard self.axTextWriteMayLand(element)
                    else { throw ActionPerformFailure(error: .staleSnapshot, inputStarted: false) }
                    let exactTarget = try revalidateKeyboardTarget(action.verifiedElement, observeOnly: true)
                    try validateTextSetter(exactTarget)
                }
            )
            switch selectedTextResult {
            case .written:
                return ActionPerformance(inputStarted: true)
            case .unsupported:
                rememberUnavailableAXTextWrite(element)
                throw ActionPerformFailure(error: .staleSnapshot, inputStarted: false)
            case .ineffective:
                // A background plan cannot switch to keyboard mid-action. Report that nothing
                // was sent; the remembered process routes the retry to keyboard delivery.
                rememberIgnoredAXTextWrite(element)
                throw ActionPerformFailure(error: .inputFocusRequired, inputStarted: false)
            case let .failed(error, inputStarted):
                throw ActionPerformFailure(error: error, inputStarted: inputStarted)
            }
        case let .pointerClick(count, button):
            guard let point = action.screenPoint else { throw ActionPerformFailure(error: .invalidAction, inputStarted: false) }
            try postClick(at: point, count: count, button: button)
            return ActionPerformance(inputStarted: true)
        case .unicodeText:
            let text = action.source.text ?? ""
            guard text.unicodeScalars.count <= maximumNativeTextCharacters else {
                throw ActionPerformFailure(error: .invalidAction, inputStarted: false)
            }
            let current = try revalidateKeyboardTarget(action.verifiedElement)
            // 输入法不安全时绝不碰 AX 文本写（plan 侧同做双保险，两处都不许漏）。
            let layoutAllowsTextMutation = textInputSafety.map { $0() == .safeASCIIKeyboardLayout } ?? true
            if layoutAllowsTextMutation, current.supportsAXSelectedTextWrite, let element = current.element,
               axTextWriteMayLand(element) {
                let selectedTextResult = try selectedTextWriter.writeSelectedText(
                    text,
                    to: element,
                    validateBeforeMutation: {
                        _ = try revalidateKeyboardTarget(action.verifiedElement)
                    }
                )
                switch selectedTextResult {
                case .written:
                    return ActionPerformance(inputStarted: true)
                case .unsupported:
                    break
                case .ineffective:
                    rememberIgnoredAXTextWrite(element)
                case let .failed(error, inputStarted):
                    throw ActionPerformFailure(error: error, inputStarted: inputStarted)
                }
            }
            // 走到这一行意味着**文本只能靠键盘通道投递**：role 不在 AX 写白名单、元素不可得，
            // 或刚刚实测 AXSelectedText 返回 .unsupported（WebKit 正是最后一类 —— role 在白名单里
            // 但写不进去）。所以事件形态必须带真实按键码；`postUnicode` 内部对查不到键位的字符
            // 逐字回退 unicode，中文/CJK 不受影响。
            // 回退 1322aad：它把形态改成"必投真实按键码"，理由（WebKit 忽略 virtualKey 0 软事件）
            // 已被实测反证 —— SCIM 拼音激活下，virtualKey 0 + unicode 串的 postToPid 事件把
            // 「测试」与「abc」都送进了 Safari 网页输入框（AX value 可见、发送按钮由灰变黑）。
            // 真实按键码反而会被输入法截走候选，故形态仍由 role 白名单决定（在白名单 ⇒ unicode）。
            // 另见 `ActionTests` 的 unsafeInputMethodSkipsAXWriteAndPostsUnicodeText。
            try postUnicode(text, preferPhysicalKeys: !current.supportsAXSelectedTextWrite)
            return ActionPerformance(inputStarted: true)
        case .keypress:
            _ = try revalidateKeyboardTarget(action.verifiedElement)
            try postKey(action.source.key ?? "", modifiers: action.source.modifiers)
            return ActionPerformance(inputStarted: true)
        case .scroll:
            guard let point = action.screenPoint else { throw ActionPerformFailure(error: .invalidAction, inputStarted: false) }
            try requireSyntheticInputAccess()
            let dx = try boundedInt32(action.source.deltaX ?? 0)
            let dy = try boundedInt32(action.source.deltaY ?? 0)
            do {
                try inputPoster.post(.scroll(point: point, deltaX: dx, deltaY: dy))
            } catch let failure as SyntheticInputFailure {
                throw ActionPerformFailure(error: failure.error, inputStarted: failure.inputStarted)
            } catch {
                throw ActionPerformFailure(error: .helperFailed, inputStarted: false)
            }
            return ActionPerformance(inputStarted: true)
        case .drag:
            guard let start = action.screenPoint, let end = action.endScreenPoint else {
                throw ActionPerformFailure(error: .invalidAction, inputStarted: false)
            }
            try postDrag(from: start, to: end, durationMS: action.source.durationMS ?? 300)
            return ActionPerformance(inputStarted: true)
        case .wait:
            let duration = action.source.durationMS ?? 0
            guard duration <= maximumNativeWaitMilliseconds else { throw ActionPerformFailure(error: .actionTimeout, inputStarted: false) }
            do { try sleepMilliseconds(duration) }
            catch { throw ActionPerformFailure(error: actionError(error), inputStarted: false) }
            return ActionPerformance(inputStarted: false)
        }
    }

    private func requireSyntheticInputAccess() throws {
        guard inputPoster.preflight() else {
            throw ActionPerformFailure(error: .permissionDenied, inputStarted: false)
        }
    }


    private func postClick(at point: CGPoint, count: Int, button: PointerButton) throws {
        try requireSyntheticInputAccess()
        var anyStarted = false
        for clickState in 1...count {
            do {
                try postBalanced(
                    down: .mouseDown(point: point, clickCount: clickState, button: button),
                    release: .mouseUp(point: point, clickCount: clickState, button: button),
                    intermediate: []
                )
                anyStarted = true
            } catch let failure as ActionPerformFailure {
                throw ActionPerformFailure(error: failure.error, inputStarted: anyStarted || failure.inputStarted)
            }
        }
    }

    private func postUnicode(_ text: String, preferPhysicalKeys: Bool) throws {
        guard text.unicodeScalars.count <= maximumNativeTextCharacters else {
            throw ActionPerformFailure(error: .invalidAction, inputStarted: false)
        }
        try requireSyntheticInputAccess()
        if !preferPhysicalKeys {
            let units = Array(text.utf16)
            try postBalanced(
                down: .unicodeKeyDown(units),
                release: .unicodeKeyUp(units),
                intermediate: []
            )
            return
        }
        var anyStarted = false
        for character in text {
            let units = Array(String(character).utf16)
            let events: (SyntheticInputEvent, SyntheticInputEvent)
            if let (keyCode, flags) = physicalKey(for: character) {
                events = (
                    .virtualKeyDown(keyCode, flags),
                    .virtualKeyUp(keyCode, flags)
                )
            } else {
                events = (.unicodeKeyDown(units), .unicodeKeyUp(units))
            }
            do {
                try postBalanced(
                    down: events.0,
                    release: events.1,
                    intermediate: []
                )
                anyStarted = true
            } catch let failure as ActionPerformFailure {
                throw ActionPerformFailure(
                    error: failure.error,
                    inputStarted: anyStarted || failure.inputStarted
                )
            }
        }
    }

    private func physicalKey(for character: Character) -> (CGKeyCode, CGEventFlags)? {
        let value = String(character)
        guard value.utf8.count == 1, let byte = value.utf8.first else { return nil }
        if byte >= Character("a").asciiValue!, byte <= Character("z").asciiValue!,
           let keyCode = keyCodes[value]
        {
            return (keyCode, [])
        }
        if byte >= Character("A").asciiValue!, byte <= Character("Z").asciiValue!,
           let keyCode = keyCodes[value.lowercased()]
        {
            return (keyCode, .maskShift)
        }
        if (byte >= Character("0").asciiValue! && byte <= Character("9").asciiValue!) || value == " " {
            return keyCodes[value == " " ? "space" : value].map { ($0, []) }
        }
        if let keyCode = keyCodes[value] { return (keyCode, []) }
        let shifted: [String: String] = [
            "!": "1", "@": "2", "#": "3", "$": "4", "%": "5",
            "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
            "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\",
            ":": ";", "\"": "'", "<": ",", ">": ".", "?": "/",
        ]
        return shifted[value].flatMap { keyCodes[$0] }.map { ($0, .maskShift) }
    }

    private func postKey(_ key: String, modifiers: [String]) throws {
        guard key.lowercased() != "v" || !modifiers.contains("command") else {
            throw ActionPerformFailure(error: .invalidAction, inputStarted: false)
        }
        guard let keyCode = keyCodes[key.lowercased()] else { throw ActionPerformFailure(error: .invalidAction, inputStarted: false) }
        let flags = modifiers.reduce(CGEventFlags()) { result, name in
            switch name {
            case "command": return result.union(.maskCommand)
            case "control": return result.union(.maskControl)
            case "option": return result.union(.maskAlternate)
            case "shift": return result.union(.maskShift)
            case "function": return result.union(.maskSecondaryFn)
            case "caps_lock": return result.union(.maskAlphaShift)
            default: return result
            }
        }
        try requireSyntheticInputAccess()
        try postBalanced(down: .virtualKeyDown(keyCode, flags), release: .virtualKeyUp(keyCode, flags), intermediate: [])
    }

    private func postDrag(from start: CGPoint, to end: CGPoint, durationMS: Int) throws {
        guard durationMS >= 0, durationMS <= maximumNativeWaitMilliseconds else {
            throw ActionPerformFailure(error: .actionTimeout, inputStarted: false)
        }
        try requireSyntheticInputAccess()
        let steps = max(1, min(120, durationMS / 10))
        var dragEvents: [SyntheticInputEvent] = []
        for step in 1...steps {
            let fraction = CGFloat(step) / CGFloat(steps)
            let point = CGPoint(x: start.x + (end.x - start.x) * fraction, y: start.y + (end.y - start.y) * fraction)
            dragEvents.append(.mouseDragged(point: point))
        }
        try postBalanced(
            down: .mouseDown(point: start, clickCount: 1),
            release: .mouseUp(point: end, clickCount: 1),
            intermediate: dragEvents,
            delayMicroseconds: durationMS > 0 ? useconds_t(durationMS * 1_000 / steps) : 0
        )
    }

    private func postBalanced(
        down: SyntheticInputEvent,
        release: SyntheticInputEvent,
        intermediate: [SyntheticInputEvent],
        delayMicroseconds: useconds_t = 0
    ) throws {
        let token = UUID()
        do {
            try heldInputs.begin(token: token, poster: inputPoster, down: down, release: release)
        } catch let failure as SyntheticInputFailure {
            _ = heldInputs.cleanup(token: token)
            throw ActionPerformFailure(error: failure.error, inputStarted: failure.inputStarted)
        } catch {
            throw ActionPerformFailure(error: .helperFailed, inputStarted: false)
        }
        do {
            for event in intermediate {
                try inputPoster.post(event)
                if delayMicroseconds > 0 { usleep(delayMicroseconds) }
            }
            try heldInputs.finish(token: token)
        } catch let failure as SyntheticInputFailure {
            _ = heldInputs.cleanup(token: token)
            throw ActionPerformFailure(error: failure.error, inputStarted: true)
        } catch {
            _ = heldInputs.cleanup(token: token)
            throw ActionPerformFailure(error: .helperFailed, inputStarted: true)
        }
    }

    private func boundedInt32(_ value: CGFloat) throws -> Int32 {
        guard value.isFinite, abs(value) <= 1_000_000 else { throw ActionPerformFailure(error: .outOfBounds, inputStarted: false) }
        return Int32(value.rounded())
    }

    private func sleepMilliseconds(_ duration: Int) throws {
        var request = timespec(tv_sec: duration / 1_000, tv_nsec: (duration % 1_000) * 1_000_000)
        while true {
            var remaining = timespec()
            if nanosleep(&request, &remaining) == 0 { return }
            if errno == EINTR {
                request = remaining
                continue
            }
            throw ActionExecutionError.helperFailed
        }
    }

    private let keyCodes: [String: CGKeyCode] = [
        "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
        "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
        "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28,
        "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "return": 36,
        "enter": 36, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44,
        "n": 45, "m": 46, ".": 47, "tab": 48, "space": 49, "delete": 51, "escape": 53,
        "left": 123, "right": 124, "down": 125, "up": 126,
    ]
}

func secureActionIdentity(role: BoundedAXStringResult, subrole: BoundedAXStringResult) -> Bool {
    guard role.status == .complete, let roleValue = role.value, !roleValue.isEmpty, subrole.status == .complete else {
        return true
    }
    return roleValue.localizedCaseInsensitiveContains("secure") ||
        (subrole.value?.localizedCaseInsensitiveContains("secure") ?? false)
}

func readActionElementIdentity(
    provider: any AXNodeAttributeProvider,
    identityToken: String,
    windowBounds: CGRect
) -> ActionElement {
    let global = provider.bounds()
    return ActionElement(
        element: provider.sourceElement(),
        identityToken: identityToken,
        bounds: CGRect(x: global.minX - windowBounds.minX, y: global.minY - windowBounds.minY, width: global.width, height: global.height),
        roleResult: provider.stringValue(for: kAXRoleAttribute),
        subroleResult: provider.stringValue(for: kAXSubroleAttribute),
        enabled: provider.boolValue(for: kAXEnabledAttribute),
        actionNames: .complete([])
    )
}

private final class AXActionIdentityProvider: AXNodeAttributeProvider {
    private let element: AXUIElement
    init(element: AXUIElement) { self.element = element }
    func stringValue(for attribute: String) -> BoundedAXStringResult { AXNodeReader.stringAttribute(element, attribute) }
    func boolValue(for attribute: String) -> Bool? {
        if attribute == kAXEnabledAttribute { return actionTextAreaEnabled(element) }
        return AXNodeReader.boolAttribute(element, attribute)
    }
    func bounds() -> CGRect { AXNodeReader.frameAttribute(element) ?? .zero }
    func actions() -> [BoundedAXStringResult] { [] }
    func children(remaining _: Int) -> [any AXNodeAttributeProvider] { [] }
    func sourceElement() -> AXUIElement? { element }
}

/// Errors with which the target application refused an AX mutation before acting on it. Live
/// Finder answered AXPress on a search-suggestion item with kAXErrorAttributeUnsupported.
func axErrorRefusedBeforeActing(_ error: AXError) -> Bool {
    switch error {
    case .actionUnsupported, .attributeUnsupported, .parameterizedAttributeUnsupported,
         .notImplemented, .illegalArgument:
        true
    default:
        false
    }
}

/// A failed AX press, increment or decrement. Refusals, a vanished element and disabled
/// accessibility prove nothing happened; a timeout or generic failure may already have acted.
func axMutationFailure(_ error: AXError) -> ActionPerformFailure {
    if axErrorRefusedBeforeActing(error) {
        logActionRejected("AX-MAP raw=\(error.rawValue) refused")
        return ActionPerformFailure(error: .accessibilityActionRefused, inputStarted: false)
    }
    switch error {
    case .invalidUIElement, .invalidUIElementObserver, .apiDisabled:
        return ActionPerformFailure(error: mapAXActionError(error), inputStarted: false)
    default:
        return ActionPerformFailure(error: mapAXActionError(error), inputStarted: true)
    }
}

func mapAXActionError(_ error: AXError) -> ActionExecutionError {
    logActionRejected("AX-MAP raw=\(error.rawValue)")
    switch error {
    case .apiDisabled:
        return .permissionDenied
    case .invalidUIElement, .invalidUIElementObserver:
        return .staleSnapshot
    case .cannotComplete:
        return .actionTimeout
    case .notImplemented, .illegalArgument, .attributeUnsupported, .actionUnsupported, .notificationUnsupported,
         .notificationAlreadyRegistered, .notificationNotRegistered, .failure, .noValue,
         .parameterizedAttributeUnsupported, .notEnoughPrecision:
        return .helperFailed
    case .success:
        return .helperFailed
    @unknown default:
        return .helperFailed
    }
}

func activeDisplayBounds() -> [CGRect] {
    var count: UInt32 = 0
    guard CGGetActiveDisplayList(0, nil, &count) == .success, count > 0 else { return [] }
    var displays = [CGDirectDisplayID](repeating: 0, count: Int(count))
    guard CGGetActiveDisplayList(count, &displays, &count) == .success else { return [] }
    return displays.prefix(Int(count)).map(CGDisplayBounds)
}

/// Narrow local AX value predicates shared by background and foreground delivery.
final class AXActionEffectVerifier {
    private let effectReader: any AXEffectReading
    private let effectClock: () -> TimeInterval
    private let effectSleeper: (TimeInterval) -> Void
    private let effectPollInterval: TimeInterval

    init(reader: any AXEffectReading = SystemAXEffectReader(),
         clock: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
         sleeper: @escaping (TimeInterval) -> Void = { Thread.sleep(forTimeInterval: $0) },
         pollInterval: TimeInterval = 0.02) {
        effectReader = reader
        effectClock = clock
        effectSleeper = sleeper
        effectPollInterval = min(max(pollInterval, 0.001), 0.05)
    }
    enum EffectPredicate {
        case increasingNumber
        case decreasingNumber
        case changedValue
    }

    struct EffectProbe {
        let element: AXUIElement
        let identityToken: String
        let before: AXEffectValue
        let predicate: EffectPredicate
        let deadline: TimeInterval
    }

    func makeProbe(
        _ entry: PlannedDispatchEntry,
        currentElement: ActionElement?,
        deadline: TimeInterval
    ) -> EffectProbe? {
        guard let currentElement,
              let resolved = entry.resolved,
              let retainedElement = resolved.effectElement ?? resolved.element
        else { return nil }
        let predicate: EffectPredicate
        switch entry.backend {
        case .axIncrement:
            predicate = .increasingNumber
        case .axDecrement:
            predicate = .decreasingNumber
        case .axPress:
            if entry.actionClass == .scroll {
                guard let deltaY = entry.source.deltaY, deltaY != 0 else { return nil }
                predicate = deltaY > 0 ? .increasingNumber : .decreasingNumber
            } else {
                guard currentElement.roleResult.status == .complete,
                      let role = currentElement.roleResult.value,
                      ["AXCheckBox", "AXRadioButton", "AXDisclosureTriangle"].contains(role)
                else { return nil }
                predicate = .changedValue
            }
        case .axSelectedText, .pidPointer, .foregroundPointer, .pidKeyboard, .foregroundKeyboard, .wait:
            return nil
        }
        guard let remainingBudget = remainingEffectBudget(until: deadline),
              let before = effectReader.read(
                  retainedElement,
                  remainingBudget: remainingBudget
              ),
              effectClock() <= deadline,
              before.identityToken == currentElement.identityToken
        else { return nil }
        switch predicate {
        case .increasingNumber, .decreasingNumber:
            guard case .number = before.value else { return nil }
        case .changedValue:
            break
        }
        return EffectProbe(
            element: retainedElement,
            identityToken: currentElement.identityToken,
            before: before.value,
            predicate: predicate,
            deadline: deadline
        )
    }

    func verify(_ probe: EffectProbe?) -> ActionEffectVerification {
        guard let probe else { return .unverified }
        var observedComparableAfterAction = false
        for _ in 0 ..< 64 {
            guard let remainingBudget = remainingEffectBudget(until: probe.deadline)
            else { return observedComparableAfterAction ? .noop : .unverified }
            guard let current = effectReader.read(
                probe.element,
                remainingBudget: remainingBudget
            ),
                  effectClock() <= probe.deadline,
                  current.identityToken == probe.identityToken
            else { return .unverified }
            if effectMatches(probe.predicate, before: probe.before, after: current.value) {
                return .verified
            }
            if current.value != probe.before { return .unverified }
            observedComparableAfterAction = true
            let now = effectClock()
            if now >= probe.deadline { return .noop }
            effectSleeper(min(effectPollInterval, probe.deadline - now))
        }
        return .unverified
    }

    private func remainingEffectBudget(until deadline: TimeInterval) -> TimeInterval? {
        let remaining = deadline - effectClock()
        return remaining.isFinite && remaining > 0 ? remaining : nil
    }

    private func effectMatches(
        _ predicate: EffectPredicate,
        before: AXEffectValue,
        after: AXEffectValue
    ) -> Bool {
        switch (predicate, before, after) {
        case let (.increasingNumber, .number(old), .number(new)):
            return new > old
        case let (.decreasingNumber, .number(old), .number(new)):
            return new < old
        case (.changedValue, _, _):
            return before != after
        default:
            return false
        }
    }

}
