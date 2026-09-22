import Carbon
import CoreGraphics
import Foundation

struct KeyboardFocusAuthority: Equatable {
    let identityToken: String
    let bounds: CGRect
    let role: String
    let subrole: String?

    var isSecure: Bool {
        role.localizedCaseInsensitiveContains("secure") ||
            (subrole?.localizedCaseInsensitiveContains("secure") ?? false)
    }
}

enum KeyboardFocusObservation: Equatable {
    case authority(KeyboardFocusAuthority)
    case secureOrIndeterminate
    // Explicit AX secure evidence must never enter missing-focus delegation.
    case secure
    case stale

    var authority: KeyboardFocusAuthority? {
        guard case let .authority(value) = self else { return nil }
        return value
    }
}

func makeKeyboardFocusAuthority(
    identityToken: String,
    bounds: CGRect,
    role: BoundedAXStringResult,
    subrole: BoundedAXStringResult,
    enabled: Bool?,
    containerBounds: CGRect
) -> KeyboardFocusAuthority? {
    guard keyboardFocusIdentityAndGeometryAreTrusted(
        identityToken: identityToken,
        bounds: bounds,
        containerBounds: containerBounds
    ),
          role.status == .complete,
          let roleValue = role.value,
          !roleValue.isEmpty,
          roleValue.unicodeScalars.count <= maximumAXStringCharacters,
          subrole.status == .complete,
          (subrole.value?.unicodeScalars.count ?? 0) <= maximumAXStringCharacters,
          enabled == true
    else { return nil }
    return KeyboardFocusAuthority(
        identityToken: identityToken,
        bounds: bounds,
        role: roleValue,
        subrole: subrole.value
    )
}

/// 接管态计划所需的键盘焦点权威。
///
/// `captureSnapshot` 只在 target 处于 foregroundTakeover 时才填 guard 的 keyboardFocus，因此
/// 一份后台观察里的该字段**按设计必为 nil**。计划期若把它原样抄进 modeGuard，就会自相锁死：
/// 派发闸以 "foregroundKeyboard requires keyboardFocus" 拒绝，执行期复验同样拿不到权威。
/// 这里在**已经请求接管**的模式下当场活读，既更新也符合"投递前焦点必须真实存在"的原意；
/// 后台态一律沿用来源 guard，绝不为了通过闸门去偷偷读焦点。活读回报 secure/stale 时返回 nil，
/// 让闸诚实拒绝，而不是伪造一份权威。
func planKeyboardFocusAuthority(
    source: KeyboardFocusAuthority?,
    interactionMode: InteractionMode,
    liveFocus: () -> KeyboardFocusObservation
) -> KeyboardFocusAuthority? {
    guard interactionMode == .foregroundTakeover else { return source }
    return liveFocus().authority
}

/// 投递前的键盘焦点权威。
///
/// macOS 只在 app 处于前台时才报 `kAXFocusedUIElement`（实测后台为 kAXErrorNoValue/-25212），
/// 而前台是接管的结果 ⇒ 唯一读得到权威的时机是**接管激活之后、按键投递之前**。begin_takeover
/// 原样继承了计划期那份（可能是 nil），沿用到底就会让投递前复验直接 stale_snapshot。
/// 纪律与计划期一致：已有权威不再白读，后台态一律不读，活读回报 secure/stale 就保持 nil
/// 让复验诚实拒绝。
func deliveryKeyboardFocusGuard(
    base: ActionGuard,
    liveFocus: () -> KeyboardFocusObservation
) -> ActionGuard {
    guard base.interactionMode == .foregroundTakeover, base.keyboardFocus == nil else { return base }
    return base.replacingKeyboardFocus(liveFocus().authority)
}

func keyboardFocusIsExplicitlySecure(role: String?, subrole: String?) -> Bool {
    (role?.localizedCaseInsensitiveContains("secure") ?? false) ||
        (subrole?.localizedCaseInsensitiveContains("secure") ?? false)
}

func makeKeyboardFocusObservation(
    expectedPID: pid_t,
    actualPID: pid_t,
    identityToken: String,
    bounds: CGRect,
    role: BoundedAXStringResult,
    subrole: BoundedAXStringResult,
    enabled: Bool?,
    containerBounds: CGRect
) -> KeyboardFocusObservation {
    if expectedPID > 0, actualPID == expectedPID,
       keyboardFocusIsExplicitlySecure(role: role.value, subrole: subrole.value) {
        return .secure
    }
    guard expectedPID > 0, actualPID == expectedPID,
          keyboardFocusIdentityAndGeometryAreTrusted(
              identityToken: identityToken,
              bounds: bounds,
              containerBounds: containerBounds
          )
    else { return .stale }
    guard role.status == .complete,
          let roleValue = role.value,
          !roleValue.isEmpty,
          roleValue.unicodeScalars.count <= maximumAXStringCharacters,
          subrole.status == .complete,
          (subrole.value?.unicodeScalars.count ?? 0) <= maximumAXStringCharacters,
          enabled == true
    else { return .secureOrIndeterminate }
    let authority = KeyboardFocusAuthority(
        identityToken: identityToken,
        bounds: bounds,
        role: roleValue,
        subrole: subrole.value
    )
    return .authority(authority)
}

protocol SecureInputDetecting {
    func isSecureInputEnabled() -> Bool
}

struct SystemSecureInputDetector: SecureInputDetecting {
    func isSecureInputEnabled() -> Bool { IsSecureEventInputEnabled() }
}

enum ForegroundKeyboardFailure: Error, Equatable {
    case compatibilityDisabled
}

struct ForegroundKeyboardPreparedPlan {
    fileprivate let actions: [Int: ForegroundKeyboardExecutor.PreparedAction]
    fileprivate let application: PIDTargetApplication
    fileprivate let deadline: TimeInterval
}

final class ForegroundKeyboardExecutor {
    typealias FocusLookup = () throws -> KeyboardFocusObservation
    typealias ContinuingTextFocusLookup = (ActionGuard, KeyboardFocusAuthority) throws -> KeyboardTextContinuationObservation

    fileprivate enum PreparedPayload {
        case key(down: SyntheticInputEvent, up: SyntheticInputEvent)
        case text([[UInt16]])
    }

    fileprivate struct PreparedAction {
        let entry: PlannedDispatchEntry
        let intent: SyntheticInputIntent
        let payload: PreparedPayload
        let targetKeyboardFocus: KeyboardFocusAuthority?
    }

    private let poster: any PIDTargetedInputPosting
    private let compatibility: PIDInputCompatibilityRegistry
    private let genericForegroundEnabled: Bool
    private let activity: any UserActivityMonitoring
    private let validator: any PIDActionGuardValidating
    private let secureInput: any SecureInputDetecting
    private let heldInputs: HeldInputRegistry
    private let focus: FocusLookup
    private let continuingTextFocus: ContinuingTextFocusLookup?
    private let now: () -> TimeInterval
    private let actionTimeout: TimeInterval
    private let experimentalUnicodeChunkGraphemes: Int

    init(
        poster: any PIDTargetedInputPosting = CGPIDTargetedInputPoster(),
        compatibility: PIDInputCompatibilityRegistry = PIDInputCompatibilityRegistry(),
        genericForegroundEnabled: Bool = false,
        activity: any UserActivityMonitoring,
        validator: any PIDActionGuardValidating,
        secureInput: any SecureInputDetecting = SystemSecureInputDetector(),
        heldInputs: HeldInputRegistry = .shared,
        focus: @escaping FocusLookup,
        continuingTextFocus: ContinuingTextFocusLookup? = nil,
        now: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
        actionTimeout: TimeInterval = 10,
        experimentalUnicodeChunkGraphemes: Int = 8
    ) {
        self.poster = poster
        self.compatibility = compatibility
        self.genericForegroundEnabled = genericForegroundEnabled
        self.activity = activity
        self.validator = validator
        self.secureInput = secureInput
        self.heldInputs = heldInputs
        self.focus = focus
        self.continuingTextFocus = continuingTextFocus
        self.now = now
        self.actionTimeout = actionTimeout
        self.experimentalUnicodeChunkGraphemes = min(8, max(1, experimentalUnicodeChunkGraphemes))
    }

    func preflight(
        expected: ActionGuard,
        application: PIDTargetApplication,
        marker: UInt64,
        entries: [PlannedDispatchEntry]
    ) throws -> ForegroundKeyboardPreparedPlan {
        guard expected.interactionMode == .foregroundTakeover else {
            throw ForegroundKeyboardFailure.compatibilityDisabled
        }
        guard !application.bundleIdentifier.isEmpty, !application.version.isEmpty else {
            throw ForegroundKeyboardFailure.compatibilityDisabled
        }
        guard entries.count <= maximumNativeActions else { throw ActionExecutionError.invalidAction }
        let keyboardEntries = entries.filter {
            $0.backend == .foregroundKeyboard || $0.backend == .pidKeyboard
        }
        guard Set(keyboardEntries.map(\.sourceIndex)).count == keyboardEntries.count else {
            throw ActionExecutionError.invalidAction
        }
        var prepared: [Int: PreparedAction] = [:]
        for entry in keyboardEntries {
            prepared[entry.sourceIndex] = try prepare(entry, application: application)
        }
        if !keyboardEntries.isEmpty {
            guard poster.preflight(targetPID: expected.pid, marker: marker) else {
                throw ActionExecutionError.permissionDenied
            }
            _ = try validateEnvironment(expected: expected, application: application, actions: Array(prepared.values))
        }
        return ForegroundKeyboardPreparedPlan(
            actions: prepared,
            application: application,
            deadline: now() + maximumForegroundExecutionSeconds
        )
    }

    func executePrepared(
        sourceIndex: Int,
        from plan: ForegroundKeyboardPreparedPlan,
        expected: ActionGuard,
        lease: UserActivitySessionLease,
        batchDeadline: TimeInterval? = nil
    ) -> PIDTargetedActionResult {
        let deadline = min(now() + max(0, actionTimeout), batchDeadline ?? plan.deadline)
        guard let action = plan.actions[sourceIndex] else { return stopped(error: .invalidAction) }
        do {
            var observationRequired = false
            switch action.payload {
            case let .key(down, up):
                observationRequired = try postBalanced(
                    down: down,
                    up: up,
                    action: action,
                    application: plan.application,
                    expected: expected,
                    deadline: deadline,
                    lease: lease
                )
            case let .text(characters):
                var inputStarted = false
                for units in characters {
                    do {
                        // Chromium editors ignore a standalone Unicode LF.
                        // Shift-Return inserts a line break without selecting
                        // the ordinary Return/default-button action.
                        let lineBreak = units == [10] || units == [13] || units == [13, 10]
                        let needsObservation = try postBalanced(
                            down: lineBreak ? .virtualKeyDown(36, .maskShift) : .unicodeKeyDown(units),
                            up: lineBreak ? .virtualKeyUp(36, .maskShift) : .unicodeKeyUp(units),
                            action: action,
                            application: plan.application,
                            expected: expected,
                            deadline: deadline,
                            lease: lease,
                            textInputStarted: inputStarted
                        )
                        observationRequired = observationRequired || needsObservation
                        inputStarted = true
                    } catch let failure as KeyboardPerformFailure {
                        throw KeyboardPerformFailure(
                            error: failure.error,
                            inputStarted: inputStarted || failure.inputStarted,
                            cleanupFailed: failure.cleanupFailed,
                            stage: failure.stage
                        )
                    } catch let pause as KeyboardPauseFailure {
                        throw KeyboardPauseFailure(
                            inputStarted: inputStarted || pause.inputStarted,
                            cleanupFailed: pause.cleanupFailed,
                            stage: pause.stage
                        )
                    }
                }
            }
            return PIDTargetedActionResult(
                outcomes: [ActionOutcome(index: sourceIndex, ok: true, error: nil,
                    observationRequired: observationRequired)],
                lastAcknowledgedAction: sourceIndex,
                error: nil,
                cooperativeError: nil
            )
        } catch let pause as KeyboardPauseFailure {
            let error = pause.inputStarted ? ActionExecutionError.unknownOutcome : nil
            return PIDTargetedActionResult(
                outcomes: [ActionOutcome(index: sourceIndex, ok: false, error: error,
                    inputDiagnostics: KeyboardFailureDiagnostics(
                        stage: pause.stage, cause: nil, userActivityPaused: true,
                        inputMayHaveStarted: pause.inputStarted, cleanupFailed: pause.cleanupFailed
                    ))],
                lastAcknowledgedAction: -1,
                error: error,
                cooperativeError: .userActivityPaused,
                cleanupFailed: pause.cleanupFailed
            )
        } catch is UserActivityMonitoringError {
            return stopped(cooperativeError: .userActivityPaused)
        } catch let failure as KeyboardPerformFailure {
            let reported = failure.inputStarted ? ActionExecutionError.unknownOutcome : failure.error
            return PIDTargetedActionResult(
                outcomes: [ActionOutcome(index: sourceIndex, ok: false, error: reported,
                    inputDiagnostics: KeyboardFailureDiagnostics(
                        stage: failure.stage, cause: failure.error, userActivityPaused: false,
                        inputMayHaveStarted: failure.inputStarted, cleanupFailed: failure.cleanupFailed
                    ))],
                lastAcknowledgedAction: -1,
                error: reported,
                cooperativeError: nil,
                cleanupFailed: failure.cleanupFailed
            )
        } catch let error as ActionExecutionError {
            return PIDTargetedActionResult(
                outcomes: [ActionOutcome(index: sourceIndex, ok: false, error: error)],
                lastAcknowledgedAction: -1,
                error: error,
                cooperativeError: nil
            )
        } catch {
            return stopped(error: .helperFailed)
        }
    }

    private func prepare(
        _ entry: PlannedDispatchEntry,
        application: PIDTargetApplication
    ) throws -> PreparedAction {
        guard entry.resolved == nil,
              entry.actionClass == .text || entry.actionClass == .press
        else {
            throw ActionExecutionError.invalidAction
        }
        guard entry.source.elementRef == nil || entry.targetKeyboardFocus != nil else {
            throw ActionExecutionError.inputFocusRequired
        }
        switch entry.source.kind {
        case .keypress:
            guard let chord = ApprovedKeyChord(action: entry.source),
                  (genericForegroundEnabled || compatibility.allows(application: application, backend: .foregroundKeyboard, action: .text)),
                  (genericForegroundEnabled || compatibility.allows(application: application, keyChord: chord)),
                  let key = entry.source.key,
                  let keyCode = foregroundKeyboardKeyCodes[key.lowercased()]
            else { throw ForegroundKeyboardFailure.compatibilityDisabled }
            let flags = foregroundKeyboardFlags(entry.source.modifiers)
            return PreparedAction(
                entry: entry,
                intent: .keyChord(chord),
                payload: .key(
                    down: .virtualKeyDown(keyCode, flags),
                    up: .virtualKeyUp(keyCode, flags)
                ),
                targetKeyboardFocus: entry.targetKeyboardFocus
            )
        case .type:
            guard let text = entry.source.text,
                  text.unicodeScalars.count <= maximumNativeTextCharacters,
                  (genericForegroundEnabled || compatibility.allows(application: application, backend: .foregroundKeyboard, action: .text)),
                  (genericForegroundEnabled || compatibility.allowsTextEntry(application: application))
            else { throw ForegroundKeyboardFailure.compatibilityDisabled }
            return PreparedAction(
                entry: entry,
                intent: .textEntry,
                payload: .text(unicodeChunks(text)),
                targetKeyboardFocus: entry.targetKeyboardFocus
            )
        case .click, .rightClick, .doubleClick, .scroll, .drag, .wait:
            throw ActionExecutionError.invalidAction
        }
    }

    private func unicodeChunks(_ text: String) -> [[UInt16]] {
        var chunks: [[UInt16]] = []
        var current: [UInt16] = []
        var count = 0
        for character in text {
            let units = Array(String(character).utf16)
            // Editors interpret line breaks as editing commands. Combining
            // them with printable text in one CGEvent can drop that text or
            // duplicate breaks; retain the single-grapheme delivery boundary.
            if character == "\n" || character == "\r" || character == "\r\n" || character == "\t" {
                if !current.isEmpty { chunks.append(current) }
                chunks.append(units)
                current = []
                count = 0
                continue
            }
            // Keep CGEvent payloads small without splitting a grapheme (emoji,
            // combining marks). Revalidate focus and user activity per chunk.
            if !current.isEmpty && current.count + units.count > 20 {
                chunks.append(current)
                current = []
                count = 0
            }
            current.append(contentsOf: units)
            count += 1
            if count == experimentalUnicodeChunkGraphemes {
                chunks.append(current)
                current = []
                count = 0
            }
        }
        if !current.isEmpty { chunks.append(current) }
        return chunks
    }

    private func postBalanced(
        down: SyntheticInputEvent,
        up: SyntheticInputEvent,
        action: PreparedAction,
        application: PIDTargetApplication,
        expected: ActionGuard,
        deadline: TimeInterval,
        lease: UserActivitySessionLease,
        textInputStarted: Bool = false
    ) throws -> Bool {
        let token = UUID()
        let scope: HeldInputScope
        do {
            scope = try activity.heldInputScope(lease: lease)
        } catch is UserActivityMonitoringError {
            throw KeyboardPauseFailure(inputStarted: false, cleanupFailed: false)
        }
        var inputStarted = false
        var focusChanged = false
        var stage: KeyboardFailureStage = .beforeKeyDown
        do {
            try activity.performPIDEvent(
                lease: lease,
                validate: {
                    focusChanged = try self.validateEvent(
                        action: action,
                        application: application,
                        expected: expected,
                        deadline: deadline,
                        allowTextGeometryTransition: textInputStarted
                    )
                },
                mutation: {
                    stage = .keyDown
                    try self.heldInputs.begin(
                        token: token,
                        scope: scope,
                        postDown: { try self.poster.post(down, to: expected.pid, marker: lease.marker, deadline: deadline) },
                        release: { try self.poster.post(up, to: expected.pid, marker: lease.marker) },
                        cleanupRelease: { try self.poster.post(up, to: expected.pid, marker: lease.marker) }
                    )
                }
            )
            inputStarted = true
            stage = .beforeKeyUp
            // A matching release completes existing input. It is authorized by
            // the held token and activity generation, not by the old AX focus.
            try activity.performPIDCleanup(lease: lease) {
                stage = .keyUp
                try self.heldInputs.finish(token: token)
            }
            stage = .afterKeyUp
            try activity.performPIDEvent(
                lease: lease,
                validate: {
                    guard self.now() <= deadline else { throw ActionExecutionError.actionTimeout }
                    let afterReleaseChanged = try self.validateEnvironment(
                        expected: expected, application: application, actions: [action],
                        allowOrdinaryKeyFocusTransition: true,
                        allowTextGeometryTransition: true
                    )
                    focusChanged = focusChanged || afterReleaseChanged
                    guard self.now() <= deadline else { throw ActionExecutionError.actionTimeout }
                },
                mutation: {}
            )
            return focusChanged
        } catch is UserActivityMonitoringError {
            throw KeyboardPauseFailure(
                inputStarted: inputStarted,
                cleanupFailed: cleanupHeld(token: token, scope: scope, lease: lease, inputStarted: inputStarted),
                stage: stage
            )
        } catch let failure as SyntheticInputFailure {
            throw KeyboardPerformFailure(
                error: failure.error,
                inputStarted: inputStarted || failure.inputStarted,
                cleanupFailed: cleanupHeld(
                    token: token,
                    scope: scope,
                    lease: lease,
                    inputStarted: inputStarted || failure.inputStarted
                ),
                stage: stage
            )
        } catch let error as ActionExecutionError {
            throw KeyboardPerformFailure(
                error: error,
                inputStarted: inputStarted,
                cleanupFailed: cleanupHeld(token: token, scope: scope, lease: lease, inputStarted: inputStarted),
                stage: stage
            )
        } catch {
            throw KeyboardPerformFailure(
                error: .helperFailed,
                inputStarted: inputStarted,
                cleanupFailed: cleanupHeld(token: token, scope: scope, lease: lease, inputStarted: inputStarted),
                stage: stage
            )
        }
    }

    private func validateEvent(
        action: PreparedAction,
        application: PIDTargetApplication,
        expected: ActionGuard,
        deadline: TimeInterval,
        allowTextGeometryTransition: Bool = false
    ) throws -> Bool {
        guard now() <= deadline else { throw ActionExecutionError.actionTimeout }
        let changed = try validateEnvironment(expected: expected, application: application, actions: [action],
                                              allowTextGeometryTransition: allowTextGeometryTransition)
        guard now() <= deadline else { throw ActionExecutionError.actionTimeout }
        return changed
    }

    private func validateEnvironment(
        expected: ActionGuard,
        application: PIDTargetApplication,
        actions: [PreparedAction],
        allowOrdinaryKeyFocusTransition: Bool = false,
        allowTextGeometryTransition: Bool = false
    ) throws -> Bool {
        var focusChanged = false
        let observedFocus: KeyboardFocusObservation?
        if allowTextGeometryTransition, expected.interactionMode == .foregroundTakeover,
           actions.count == 1, case .text = actions[0].payload,
           let wanted = actions[0].targetKeyboardFocus ?? expected.keyboardFocus,
           let continuingTextFocus {
            // This observer must perform the full live window/field validation;
            // it is never used for preflight, first down, keys or blind typing.
            let observed = try continuingTextFocus(expected, wanted)
            observedFocus = observed.focus
            focusChanged = observed.observationRequired
        } else {
            observedFocus = try validator.revalidateAndObserveFocus(expected: expected, point: nil)
        }
        // Reuse only the live observation from this exact guard invocation.
        let currentObservation = { try observedFocus ?? self.focus() }
        // takeover 盲打委托：guard 无焦点权威 == 计划层双闸已放行的 OS 焦点委托形态
        //（WPS 等无 AX 焦点应用）。保留不可读焦点的委托，仅拒绝明确安全字段。
        // 每次投递前复核，避免 preflight 后焦点进入安全字段；不要求焦点 identity 匹配。
        if expected.keyboardFocus == nil, expected.interactionMode == .foregroundTakeover {
            guard !secureInput.isSecureInputEnabled() else {
                throw ActionExecutionError.secureTarget
            }
            switch try currentObservation() {
            case .secure:
                throw ActionExecutionError.secureTarget
            case let .authority(value) where value.isSecure:
                throw ActionExecutionError.secureTarget
            default:
                break
            }
            for action in actions where action.targetKeyboardFocus != nil {
                throw ActionExecutionError.inputFocusRequired
            }
        } else {
            guard let expectedFocus = expected.keyboardFocus else { throw ActionExecutionError.staleSnapshot }
            guard !expectedFocus.isSecure, !secureInput.isSecureInputEnabled() else {
                throw ActionExecutionError.secureTarget
            }
            let currentFocus: KeyboardFocusAuthority
            switch try currentObservation() {
            case let .authority(value):
                guard !value.isSecure else { throw ActionExecutionError.secureTarget }
                currentFocus = value
            case .secure, .secureOrIndeterminate:
                throw ActionExecutionError.secureTarget
            case .stale:
                throw ActionExecutionError.staleSnapshot
            }
            for action in actions {
                // Typing may scroll/reflow the same editor. Only after this
                // action starts, accept geometry changes for that exact AX
                // identity and role inside the still-validated window.
                func matches(_ wanted: KeyboardFocusAuthority) -> Bool {
                    if wanted == currentFocus { return true }
                    guard allowTextGeometryTransition, case .text = action.payload else { return false }
                    return wanted.identityToken == currentFocus.identityToken &&
                        wanted.role == currentFocus.role && wanted.subrole == currentFocus.subrole &&
                        keyboardFocusIdentityAndGeometryAreTrusted(
                            identityToken: currentFocus.identityToken, bounds: currentFocus.bounds,
                            containerBounds: expected.focusedAXBounds
                        )
                }
                if let targetKeyboardFocus = action.targetKeyboardFocus {
                    guard matches(targetKeyboardFocus) else {
                        throw ActionExecutionError.inputFocusRequired
                    }
                } else {
                    if !matches(expectedFocus) {
                        if allowOrdinaryKeyFocusTransition, case .key = action.payload {
                            guard !currentFocus.role.isEmpty,
                                  currentFocus.role.lowercased() != "axunknown",
                                  currentFocus.subrole?.lowercased() != "axunknown"
                            else { throw ActionExecutionError.secureTarget }
                            focusChanged = true
                        } else {
                            throw ActionExecutionError.staleSnapshot
                        }
                    }
                }
            }
        }
        for action in actions {
            let allowed: Bool
            switch action.intent {
            case let .keyChord(chord):
                allowed = (genericForegroundEnabled || compatibility.allows(application: application, backend: .foregroundKeyboard, action: .text)) &&
                    (genericForegroundEnabled || compatibility.allows(application: application, keyChord: chord))
            case .textEntry:
                allowed = (genericForegroundEnabled || compatibility.allows(application: application, backend: .foregroundKeyboard, action: .text)) &&
                    (genericForegroundEnabled || compatibility.allowsTextEntry(application: application))
            case .pointer:
                allowed = false
            }
            guard allowed else { throw ForegroundKeyboardFailure.compatibilityDisabled }
        }
        return focusChanged
    }

    private func cleanupHeld(
        token: UUID,
        scope: HeldInputScope,
        lease: UserActivitySessionLease,
        inputStarted: Bool
    ) -> Bool {
        var result: HeldInputCleanupResult?
        do {
            try activity.performPIDCleanup(lease: lease) {
                result = heldInputs.cleanup(token: token, scope: scope)
            }
            return (result?.failed ?? 0) > 0
        } catch {
            return inputStarted
        }
    }

    private func stopped(
        error: ActionExecutionError? = nil,
        cooperativeError: CooperativeErrorCode? = nil
    ) -> PIDTargetedActionResult {
        PIDTargetedActionResult(
            outcomes: [],
            lastAcknowledgedAction: -1,
            error: error,
            cooperativeError: cooperativeError
        )
    }
}

private struct KeyboardPauseFailure: Error {
    let inputStarted: Bool
    let cleanupFailed: Bool
    var stage: KeyboardFailureStage = .beforeKeyDown
}

private struct KeyboardPerformFailure: Error {
    let error: ActionExecutionError
    let inputStarted: Bool
    let cleanupFailed: Bool
    var stage: KeyboardFailureStage = .beforeKeyDown
}

private func keyboardFiniteRect(_ rect: CGRect) -> Bool {
    rect.origin.x.isFinite && rect.origin.y.isFinite &&
        rect.width.isFinite && rect.height.isFinite &&
        rect.minX.isFinite && rect.maxX.isFinite &&
        rect.minY.isFinite && rect.maxY.isFinite &&
        rect.width > 0 && rect.height > 0
}

func keyboardFocusIdentityAndGeometryAreTrusted(
    identityToken: String,
    bounds: CGRect,
    containerBounds: CGRect
) -> Bool {
    guard !identityToken.isEmpty,
          identityToken.unicodeScalars.count <= maximumAXStringCharacters,
          keyboardFiniteRect(bounds),
          keyboardFiniteRect(containerBounds)
    else { return false }
    return bounds.minX > containerBounds.minX && bounds.maxX < containerBounds.maxX &&
        bounds.minY > containerBounds.minY && bounds.maxY < containerBounds.maxY
}

private func foregroundKeyboardFlags(_ modifiers: [String]) -> CGEventFlags {
    modifiers.reduce(CGEventFlags()) { result, name in
        switch name {
        case "command": result.union(.maskCommand)
        case "control": result.union(.maskControl)
        case "option": result.union(.maskAlternate)
        case "shift": result.union(.maskShift)
        case "function": result.union(.maskSecondaryFn)
        case "caps_lock": result.union(.maskAlphaShift)
        default: result
        }
    }
}

private let foregroundKeyboardKeyCodes: [String: CGKeyCode] = [
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
    "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28,
    "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "return": 36,
    "enter": 36, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44,
    "n": 45, "m": 46, ".": 47, "tab": 48, "space": 49, "delete": 51, "escape": 53,
    "left": 123, "right": 124, "down": 125, "up": 126,
]
