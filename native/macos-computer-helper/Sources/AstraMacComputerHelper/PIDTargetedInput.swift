import CoreGraphics
import Darwin
import Foundation

protocol PIDTargetedInputPosting: AnyObject {
    func preflight(targetPID: pid_t, marker: UInt64) -> Bool
    func post(_ event: SyntheticInputEvent, to targetPID: pid_t, marker: UInt64) throws
    func post(_ event: SyntheticInputEvent, to targetPID: pid_t, marker: UInt64, deadline: TimeInterval) throws
}

extension PIDTargetedInputPosting {
    func post(_ event: SyntheticInputEvent, to targetPID: pid_t, marker: UInt64, deadline: TimeInterval) throws {
        try post(event, to: targetPID, marker: marker)
    }
}

final class CGPIDTargetedInputPoster: PIDTargetedInputPosting {
    private let source: CGEventSource?
    private let preflightAccess: () -> Bool
    private let deliver: (CGEvent, pid_t) -> Void
    private let pointerLock = NSLock()
    private var lastPointer: (pid: pid_t, marker: UInt64, point: CGPoint)?

    init(
        preflightAccess: @escaping () -> Bool = { CGPreflightPostEventAccess() },
        deliver: @escaping (CGEvent, pid_t) -> Void = { event, pid in event.postToPid(pid) }
    ) {
        source = CGEventSource(stateID: .privateState)
        self.preflightAccess = preflightAccess
        self.deliver = deliver
    }

    func preflight(targetPID: pid_t, marker: UInt64) -> Bool {
        targetPID > 0 && marker != 0 && source != nil && preflightAccess()
    }

    func post(_ event: SyntheticInputEvent, to targetPID: pid_t, marker: UInt64) throws {
        guard targetPID > 0, marker != 0, let source else {
            throw SyntheticInputFailure(error: .helperFailed, inputStarted: false)
        }
        let value: CGEvent
        switch event {
        case let .mouseDown(point, clickCount, button):
            let mouseType: CGEventType = button == .right ? .rightMouseDown : .leftMouseDown
            let mouseButton: CGMouseButton = button == .right ? .right : .left
            guard let event = CGEvent(mouseEventSource: source, mouseType: mouseType, mouseCursorPosition: point, mouseButton: mouseButton) else {
                throw SyntheticInputFailure(error: .helperFailed, inputStarted: false)
            }
            event.setIntegerValueField(.mouseEventClickState, value: Int64(clickCount))
            value = event
        case let .mouseUp(point, clickCount, button):
            let mouseType: CGEventType = button == .right ? .rightMouseUp : .leftMouseUp
            let mouseButton: CGMouseButton = button == .right ? .right : .left
            guard let event = CGEvent(mouseEventSource: source, mouseType: mouseType, mouseCursorPosition: point, mouseButton: mouseButton) else {
                throw SyntheticInputFailure(error: .helperFailed, inputStarted: false)
            }
            event.setIntegerValueField(.mouseEventClickState, value: Int64(clickCount))
            value = event
        case let .mouseDragged(point):
            guard let event = CGEvent(mouseEventSource: source, mouseType: .leftMouseDragged, mouseCursorPosition: point, mouseButton: .left) else {
                throw SyntheticInputFailure(error: .helperFailed, inputStarted: false)
            }
            value = event
        case let .scroll(point, deltaX, deltaY):
            // Public deltas describe content offsets: positive means down/right,
            // matching AX increment and effect observation. CG wheel signs are
            // opposite, independent of the user's physical scroll preference.
            guard deltaX != Int32.min, deltaY != Int32.min else {
                throw SyntheticInputFailure(error: .invalidAction, inputStarted: false)
            }
            guard let event = CGEvent(
                scrollWheelEvent2Source: source,
                units: .pixel,
                wheelCount: 2,
                wheel1: -deltaY,
                wheel2: -deltaX,
                wheel3: 0
            ) else {
                throw SyntheticInputFailure(error: .helperFailed, inputStarted: false)
            }
            event.location = point
            value = event
        case let .unicodeKeyDown(units), let .unicodeKeyUp(units):
            let isDown: Bool
            if case .unicodeKeyDown = event { isDown = true } else { isDown = false }
            guard !units.isEmpty,
                  let keyboard = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: isDown)
            else { throw SyntheticInputFailure(error: .invalidAction, inputStarted: false) }
            // Plain text must not inherit a preceding shortcut's modifier state
            // (e.g. Command-A turning subsequent Unicode into more shortcuts).
            keyboard.flags = []
            keyboard.keyboardSetUnicodeString(stringLength: units.count, unicodeString: units)
            value = keyboard
        case let .virtualKeyDown(keyCode, flags), let .virtualKeyUp(keyCode, flags):
            let isDown: Bool
            if case .virtualKeyDown = event { isDown = true } else { isDown = false }
            guard let keyboard = CGEvent(keyboardEventSource: source, virtualKey: keyCode, keyDown: isDown) else {
                throw SyntheticInputFailure(error: .helperFailed, inputStarted: false)
            }
            keyboard.flags = flags
            value = keyboard
        }
        // CGEventCreateMouseEvent initializes relative motion to zero. Apps
        // that consume NSEvent.delta need the same motion as the absolute path.
        pointerLock.lock()
        switch event {
        case let .mouseDown(point, _, _): lastPointer = (targetPID, marker, point)
        case let .mouseDragged(point):
            if let previous = lastPointer, previous.pid == targetPID, previous.marker == marker {
                value.setDoubleValueField(.mouseEventDeltaX, value: point.x - previous.point.x)
                value.setDoubleValueField(.mouseEventDeltaY, value: point.y - previous.point.y)
                lastPointer = (targetPID, marker, point)
            }
        case .mouseUp:
            if lastPointer?.pid == targetPID, lastPointer?.marker == marker { lastPointer = nil }
        default: break
        }
        pointerLock.unlock()
        value.setIntegerValueField(.eventSourceUserData, value: Int64(bitPattern: marker))
        deliver(value, targetPID)
    }
}

struct PIDActionTargetState {
    let target: ActionTargetState
    let snapshotID: String
    let isFrontmost: Bool
    let isKeyWindow: Bool
    var observedKeyboardFocus: KeyboardFocusObservation? = nil
}

protocol PIDActionGuardValidating: AnyObject {
    func revalidate(expected: ActionGuard, point: CGPoint?) throws
    func revalidate(expected: ActionGuard, point: CGPoint?, dragDisplacement: CGVector?) throws
    func revalidateBalancedRelease(expected: ActionGuard, point: CGPoint?, dragDisplacement: CGVector?) throws
    func revalidateAndObserveFocus(expected: ActionGuard, point: CGPoint?) throws -> KeyboardFocusObservation?
}

extension PIDActionGuardValidating {
    func revalidate(expected: ActionGuard, point: CGPoint?, dragDisplacement _: CGVector?) throws {
        try revalidate(expected: expected, point: point)
    }
    func revalidateBalancedRelease(expected: ActionGuard, point: CGPoint?, dragDisplacement: CGVector?) throws {
        try revalidate(expected: expected, point: point, dragDisplacement: dragDisplacement)
    }
    func revalidateAndObserveFocus(expected: ActionGuard, point: CGPoint?) throws -> KeyboardFocusObservation? {
        try revalidate(expected: expected, point: point)
        return nil
    }
}

final class ExactPIDActionGuardValidator: PIDActionGuardValidating {
    private let state: (CGVector?) throws -> PIDActionTargetState

    init(state: @escaping () throws -> PIDActionTargetState) { self.state = { _ in try state() } }

    init(stateForDrag: @escaping (CGVector?) throws -> PIDActionTargetState) { state = stateForDrag }

    func revalidate(expected: ActionGuard, point: CGPoint?) throws {
        _ = try revalidateAndObserveFocus(expected: expected, point: point)
    }

    func revalidate(expected: ActionGuard, point: CGPoint?, dragDisplacement: CGVector?) throws {
        _ = try validate(expected: expected, point: point, dragDisplacement: dragDisplacement)
    }

    func revalidateAndObserveFocus(expected: ActionGuard, point: CGPoint?) throws -> KeyboardFocusObservation? {
        try validate(expected: expected, point: point, dragDisplacement: nil)
    }

    private func validate(expected: ActionGuard, point: CGPoint?, dragDisplacement: CGVector?) throws -> KeyboardFocusObservation? {
        let current = try state(dragDisplacement)
        guard current.isFrontmost, current.isKeyWindow, current.target.pid == expected.pid else {
            logActionRejected("PID-GUARD focus frontmost=\(current.isFrontmost) key=\(current.isKeyWindow)")
            throw ActionExecutionError.targetNotFrontmost
        }
        let translation: CGVector
        let focusedTranslation: CGVector
        if let displacement = dragDisplacement {
            guard expected.interactionMode == .foregroundTakeover,
                  expected.focusedRootPreference == .selectedWindow,
                  displacement.dx.isFinite, displacement.dy.isFinite,
                  current.target.bounds.size == expected.bounds.size,
                  current.target.focusedAXBounds.size == expected.focusedAXBounds.size else { throw ActionExecutionError.staleSnapshot }
            translation = CGVector(dx: current.target.bounds.minX - expected.bounds.minX,
                                   dy: current.target.bounds.minY - expected.bounds.minY)
            focusedTranslation = CGVector(dx: current.target.focusedAXBounds.minX - expected.focusedAXBounds.minX,
                                          dy: current.target.focusedAXBounds.minY - expected.focusedAXBounds.minY)
            // A delivered drag may translate its captured window. OS delivery
            // can lag; it may not jump beyond the portion of the sealed path
            // already posted, resize, or substitute another window/root.
            guard (min(0, displacement.dx) - 2 ... max(0, displacement.dx) + 2).contains(translation.dx),
                  (min(0, displacement.dy) - 2 ... max(0, displacement.dy) + 2).contains(translation.dy),
                  (min(0, displacement.dx) - 2 ... max(0, displacement.dx) + 2).contains(focusedTranslation.dx),
                  (min(0, displacement.dy) - 2 ... max(0, displacement.dy) + 2).contains(focusedTranslation.dy)
            else { throw ActionExecutionError.staleSnapshot }
        } else { translation = .zero; focusedTranslation = .zero }
        let expectedBounds = expected.bounds.offsetBy(dx: translation.dx, dy: translation.dy)
        let expectedFocusedBounds = expected.focusedAXBounds.offsetBy(dx: focusedTranslation.dx, dy: focusedTranslation.dy)
        guard current.snapshotID == expected.snapshotID,
              current.target.windowID == expected.windowID,
              current.target.axIdentity == expected.axIdentity,
              current.target.focusedAXIdentity == expected.focusedAXIdentity,
              current.target.focusedRootPreference == expected.focusedRootPreference,
              pidFiniteRect(current.target.bounds),
              current.target.bounds == expectedBounds,
              pidFiniteRect(current.target.focusedAXBounds),
              current.target.focusedAXBounds == expectedFocusedBounds
        else {
            logActionRejected("PID-GUARD target-state-exact snapshotID=\(expected.snapshotID)")
            throw ActionExecutionError.staleSnapshot
        }
        if let point {
            guard point.x.isFinite, point.y.isFinite, (dragDisplacement == nil ? current.target.bounds : expected.bounds).contains(point) else {
                throw ActionExecutionError.outOfBounds
            }
        }
        return current.observedKeyboardFocus
    }
}

/// Used only for a sealed, single coordinate click on an AXDialog popup.
/// The popup can own the pointer while its parent remains the AX key window.
/// Every pointer event rechecks exact live window, visibility, and hit proof.
final class PopupPointerClickGuardValidator: PIDActionGuardValidating {
    private let sealed: ActionGuard
    private let authorizedPoint: CGPoint
    private let popupProof: () -> Bool

    init(sealed: ActionGuard, authorizedPoint: CGPoint, popupProof: @escaping () -> Bool) {
        self.sealed = sealed
        self.authorizedPoint = authorizedPoint
        self.popupProof = popupProof
    }

    func revalidate(expected: ActionGuard, point: CGPoint?) throws {
        try revalidate(expected: expected, point: point, dragDisplacement: nil)
    }

    func revalidate(expected: ActionGuard, point: CGPoint?, dragDisplacement: CGVector?) throws {
        try validateSealed(expected: expected, point: point, dragDisplacement: dragDisplacement)
        guard popupProof() else { throw ActionExecutionError.targetNotFrontmost }
    }

    /// Called only after a successful mouse-down. A popup may close as the
    /// action's immediate effect; releasing the held button must not wait on
    /// another screenshot or require that transient window to remain open.
    func revalidateBalancedRelease(expected: ActionGuard, point: CGPoint?, dragDisplacement: CGVector?) throws {
        try validateSealed(expected: expected, point: point, dragDisplacement: dragDisplacement)
    }

    private func validateSealed(expected: ActionGuard, point: CGPoint?, dragDisplacement: CGVector?) throws {
        guard dragDisplacement == nil, expected.interactionMode == .foregroundTakeover,
              expected.pid == sealed.pid, expected.windowID == sealed.windowID,
              expected.bounds == sealed.bounds, expected.axIdentity == sealed.axIdentity,
              expected.focusedAXIdentity == sealed.focusedAXIdentity,
              expected.focusedAXBounds == sealed.focusedAXBounds,
              expected.focusedRootPreference == sealed.focusedRootPreference,
              expected.snapshotID == sealed.snapshotID
        else { throw ActionExecutionError.staleSnapshot }
        if let point {
            guard point == authorizedPoint else { throw ActionExecutionError.outOfBounds }
        }
    }

    func revalidateAndObserveFocus(expected _: ActionGuard, point _: CGPoint?) throws -> KeyboardFocusObservation? {
        throw ActionExecutionError.invalidAction
    }
}

/// Validator for the background delivery family: the target window is
/// deliberately NOT frontmost. Only identity/snapshot stability is checked
/// (same window, same snapshot, same bounds); no focus or key-window
/// requirement, no real-cursor movement promised by the caller.
final class BackgroundPIDActionGuardValidator: PIDActionGuardValidating {
    private let state: () throws -> PIDActionTargetState
    /// Regions of the target that bind but must not receive pointer input, such as a waived
    /// status strip over the page.
    private let pointerExclusions: () -> [CGRect]

    init(state: @escaping () throws -> PIDActionTargetState, pointerExclusions: @escaping () -> [CGRect] = { [] }) {
        self.state = state
        self.pointerExclusions = pointerExclusions
    }

    func revalidate(expected: ActionGuard, point: CGPoint?) throws {
        let current = try state()
        guard current.target.pid == expected.pid else {
            throw ActionExecutionError.targetNotFrontmost
        }
        guard current.snapshotID == expected.snapshotID,
              current.target.windowID == expected.windowID,
              current.target.axIdentity == expected.axIdentity,
              current.target.focusedAXIdentity == expected.focusedAXIdentity,
              current.target.focusedRootPreference == expected.focusedRootPreference,
              pidFiniteRect(current.target.bounds),
              pidApproximatelyEqual(current.target.bounds, expected.bounds),
              pidFiniteRect(current.target.focusedAXBounds),
              pidApproximatelyEqual(current.target.focusedAXBounds, expected.focusedAXBounds)
        else {
            logActionRejected("PID-GUARD target-state-tolerant snapshotID=\(expected.snapshotID)")
            throw ActionExecutionError.staleSnapshot
        }
        if let point {
            guard point.x.isFinite, point.y.isFinite, current.target.bounds.contains(point),
                  !pointerExclusions().contains(where: { $0.contains(point) })
            else {
                throw ActionExecutionError.outOfBounds
            }
        }
    }
}

struct PIDTargetedActionResult {
    let outcomes: [ActionOutcome]
    let lastAcknowledgedAction: Int
    let error: ActionExecutionError?
    let cooperativeError: CooperativeErrorCode?
    let cleanupFailed: Bool

    init(
        outcomes: [ActionOutcome],
        lastAcknowledgedAction: Int,
        error: ActionExecutionError?,
        cooperativeError: CooperativeErrorCode?,
        cleanupFailed: Bool = false
    ) {
        self.outcomes = outcomes
        self.lastAcknowledgedAction = lastAcknowledgedAction
        self.error = error
        self.cooperativeError = cooperativeError
        self.cleanupFailed = cleanupFailed
    }
}

final class PIDTargetedActionExecutor {
    typealias ElementLookup = (String, String) -> ActionElement?
    typealias EvidenceCheck = (PlannedDispatchEntry, ActionGuard) -> Bool

    fileprivate struct PreparedAction {
        let entry: PlannedDispatchEntry
        let actionClass: DispatchActionClass
        let point: CGPoint
        let endPoint: CGPoint?
        let safeRegion: PointerSafeRegionAuthority
    }

    private let poster: any PIDTargetedInputPosting
    private let compatibility: PIDInputCompatibilityRegistry
    private let genericForegroundEnabled: Bool
    private let activity: any UserActivityMonitoring
    private let validator: any PIDActionGuardValidating
    private let heldInputs: HeldInputRegistry
    private let element: ElementLookup
    private let evidence: EvidenceCheck
    private let delay: (useconds_t) -> Void
    private let now: () -> TimeInterval

    init(
        poster: any PIDTargetedInputPosting = CGPIDTargetedInputPoster(),
        compatibility: PIDInputCompatibilityRegistry = PIDInputCompatibilityRegistry(),
        genericForegroundEnabled: Bool = false,
        activity: any UserActivityMonitoring,
        validator: any PIDActionGuardValidating,
        heldInputs: HeldInputRegistry = .shared,
        element: @escaping ElementLookup,
        evidence: @escaping EvidenceCheck = { _, _ in false },
        delay: @escaping (useconds_t) -> Void = { usleep($0) },
        now: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }
    ) {
        self.poster = poster
        self.compatibility = compatibility
        self.genericForegroundEnabled = genericForegroundEnabled
        self.activity = activity
        self.validator = validator
        self.heldInputs = heldInputs
        self.element = element
        self.evidence = evidence
        self.delay = delay
        self.now = now
    }

    func run(
        expected: ActionGuard,
        application: PIDTargetApplication,
        lease: UserActivitySessionLease,
        actions: [PlannedDispatchEntry]
    ) -> PIDTargetedActionResult {
        guard actions.allSatisfy({ $0.backend == .pidPointer }) else {
            return stopped(error: .invalidAction)
        }
        let deadline = now() + maximumForegroundExecutionSeconds
        let plannedMilliseconds = actions.reduce(0) { $0 + ($1.source.kind == .drag ? ($1.source.durationMS ?? 300) : 0) }
        guard plannedMilliseconds <= maximumNativeWaitMilliseconds else { return stopped(error: .actionTimeout) }
        let prepared: PIDTargetedPreparedPlan
        do {
            prepared = try preflight(
                expected: expected,
                application: application,
                marker: lease.marker,
                entries: actions
            )
        } catch PIDTargetedActionFailure.compatibilityDisabled {
            return stopped(error: nil, cooperativeError: .backgroundActionUnsupported)
        } catch let error as ActionExecutionError {
            return stopped(error: error)
        } catch {
            return stopped(error: .helperFailed)
        }
        do {
            try activity.beginFragment(lease: lease)
        } catch is UserActivityMonitoringError {
            return stopped(error: nil, cooperativeError: .userActivityPaused)
        } catch {
            return stopped(error: .helperFailed)
        }
        defer { activity.endFragment(lease: lease) }

        var outcomes: [ActionOutcome] = []
        var acknowledged = -1
        for entry in actions {
            let result = executePrepared(
                sourceIndex: entry.sourceIndex,
                from: prepared,
                expected: expected,
                lease: lease,
                batchDeadline: deadline
            )
            outcomes.append(contentsOf: result.outcomes)
            if result.error != nil || result.cooperativeError != nil {
                return PIDTargetedActionResult(
                    outcomes: outcomes,
                    lastAcknowledgedAction: acknowledged,
                    error: result.error,
                    cooperativeError: result.cooperativeError,
                    cleanupFailed: result.cleanupFailed
                )
            }
            acknowledged = entry.sourceIndex
            if result.outcomes.last?.observationRequired == true {
                return PIDTargetedActionResult(
                    outcomes: outcomes, lastAcknowledgedAction: acknowledged, error: nil,
                    cooperativeError: entry.sourceIndex == actions.last?.sourceIndex ? nil : .observationRequired
                )
            }
        }
        return PIDTargetedActionResult(outcomes: outcomes, lastAcknowledgedAction: acknowledged, error: nil, cooperativeError: nil)
    }

    func preflight(
        expected: ActionGuard,
        application: PIDTargetApplication,
        marker: UInt64,
        entries: [PlannedDispatchEntry],
        allowBackgroundDelivery: Bool = false
    ) throws -> PIDTargetedPreparedPlan {
        guard expected.interactionMode == .foregroundTakeover || allowBackgroundDelivery else {
            throw PIDTargetedActionFailure.compatibilityDisabled
        }
        guard entries.count <= maximumNativeActions else { throw ActionExecutionError.invalidAction }
        let pidEntries = entries.filter { [.pidPointer, .foregroundPointer].contains($0.backend) }
        if pidEntries.contains(where: { $0.backend == .foregroundPointer }) {
            guard genericForegroundEnabled, expected.interactionMode == .foregroundTakeover,
                  !application.bundleIdentifier.isEmpty, !application.version.isEmpty
            else { throw PIDTargetedActionFailure.compatibilityDisabled }
        }
        guard Set(pidEntries.map(\.sourceIndex)).count == pidEntries.count else {
            throw ActionExecutionError.invalidAction
        }
        var validated: [(entry: PlannedDispatchEntry, action: PreparedAction)] = []
        for entry in pidEntries {
            guard let actionClass = entry.actionClass else { throw ActionExecutionError.invalidAction }
            validated.append((
                entry: entry,
                action: try prepare(entry, actionClass: actionClass, expected: expected)
            ))
        }
        guard validated.allSatisfy({
            $0.entry.backend == .foregroundPointer || compatibility.allows(application: application, action: $0.action.actionClass)
        }) else {
            throw PIDTargetedActionFailure.compatibilityDisabled
        }
        let prepared = Dictionary(uniqueKeysWithValues: validated.map { ($0.entry.sourceIndex, $0.action) })
        if !pidEntries.isEmpty,
           !poster.preflight(targetPID: expected.pid, marker: marker) {
            throw ActionExecutionError.permissionDenied
        }
        return PIDTargetedPreparedPlan(actions: prepared)
    }

    func executePrepared(
        sourceIndex: Int,
        from plan: PIDTargetedPreparedPlan,
        expected: ActionGuard,
        lease: UserActivitySessionLease,
        batchDeadline: TimeInterval? = nil
    ) -> PIDTargetedActionResult {
        guard let action = plan.actions[sourceIndex] else { return stopped(error: .invalidAction) }
        let deadline = min(now() + maximumForegroundExecutionSeconds, batchDeadline ?? .infinity)
        do {
            try activity.assertNotPaused(lease: lease)
            try perform(action, expected: expected, marker: lease.marker, lease: lease, deadline: deadline)
            let evidenceConfirmed: Bool
            do {
                try activity.assertNotPaused(lease: lease)
                if genericForegroundEnabled, action.actionClass == .drag,
                   action.safeRegion.reference == foregroundWindowRegionReference,
                   let end = action.endPoint {
                    evidenceConfirmed = (try? validator.revalidate(expected: expected, point: nil,
                        dragDisplacement: CGVector(dx: end.x - action.point.x, dy: end.y - action.point.y))) != nil
                } else {
                    evidenceConfirmed = evidence(action.entry, expected)
                }
                try activity.assertNotPaused(lease: lease)
            } catch is UserActivityMonitoringError {
                throw PIDUserActivityPauseFailure(inputStarted: true, cleanupFailed: false)
            }
            // Background delivery has no cooperative checkpoint contract yet.
            // Keep its existing fail-closed receipt rather than running a suffix
            // or emitting a checkpoint that its batch consumer cannot represent.
            if !evidenceConfirmed, expected.interactionMode != .foregroundTakeover {
                return PIDTargetedActionResult(
                    outcomes: [ActionOutcome(index: sourceIndex, ok: false, error: .unknownOutcome)],
                    lastAcknowledgedAction: -1, error: .unknownOutcome, cooperativeError: nil
                )
            }
            // perform returned only after all guarded events and the matching
            // release completed. Losing post-action state requires observation,
            // not replay or a claim about the application's effect. Posting,
            // release, and user-activity failures still take the catch paths.
            return PIDTargetedActionResult(
                outcomes: [ActionOutcome(index: sourceIndex, ok: true, error: nil,
                    observationRequired: !evidenceConfirmed)],
                lastAcknowledgedAction: sourceIndex,
                error: nil,
                cooperativeError: nil
            )
        } catch let pause as PIDUserActivityPauseFailure {
            let actionError = pause.inputStarted ? ActionExecutionError.unknownOutcome : nil
            return PIDTargetedActionResult(
                outcomes: [ActionOutcome(index: sourceIndex, ok: false, error: actionError)],
                lastAcknowledgedAction: -1,
                error: actionError,
                cooperativeError: .userActivityPaused,
                cleanupFailed: pause.cleanupFailed
            )
        } catch is UserActivityMonitoringError {
            return PIDTargetedActionResult(
                outcomes: [],
                lastAcknowledgedAction: -1,
                error: nil,
                cooperativeError: .userActivityPaused
            )
        } catch let failure as PIDActionPerformFailure {
            let reported = failure.inputStarted ? ActionExecutionError.unknownOutcome : failure.error
            return PIDTargetedActionResult(
                outcomes: [ActionOutcome(index: sourceIndex, ok: false, error: reported)],
                lastAcknowledgedAction: -1,
                error: reported,
                cooperativeError: nil,
                cleanupFailed: failure.cleanupFailed
            )
        } catch let failure as SyntheticInputFailure {
            let reported = failure.inputStarted ? ActionExecutionError.unknownOutcome : failure.error
            return PIDTargetedActionResult(
                outcomes: [ActionOutcome(index: sourceIndex, ok: false, error: reported)],
                lastAcknowledgedAction: -1,
                error: reported,
                cooperativeError: nil
            )
        } catch let error as ActionExecutionError {
            return PIDTargetedActionResult(
                outcomes: [ActionOutcome(index: sourceIndex, ok: false, error: error)],
                lastAcknowledgedAction: -1,
                error: error,
                cooperativeError: nil
            )
        } catch {
            return PIDTargetedActionResult(
                outcomes: [ActionOutcome(index: sourceIndex, ok: false, error: .helperFailed)],
                lastAcknowledgedAction: -1,
                error: .helperFailed,
                cooperativeError: nil
            )
        }
    }

    private func stopped(
        error: ActionExecutionError?,
        cooperativeError: CooperativeErrorCode? = nil
    ) -> PIDTargetedActionResult {
        PIDTargetedActionResult(outcomes: [], lastAcknowledgedAction: -1, error: error, cooperativeError: cooperativeError)
    }

    private func prepare(
        _ entry: PlannedDispatchEntry,
        actionClass: DispatchActionClass,
        expected: ActionGuard
    ) throws -> PreparedAction {
        let source = entry.source
        let safeRegion = try prepareSafeRegion(entry, expected: expected)
        switch actionClass {
        case .click, .doubleClick:
            let point = try pointerPoint(source, safeRegion: safeRegion, expected: expected)
            return PreparedAction(entry: entry, actionClass: actionClass, point: point, endPoint: nil, safeRegion: safeRegion)
        case .scroll:
            let point = try pointerPoint(source, safeRegion: safeRegion, expected: expected)
            return PreparedAction(entry: entry, actionClass: actionClass, point: point, endPoint: nil, safeRegion: safeRegion)
        case .drag:
            guard let x = source.x, let y = source.y, let endX = source.endX, let endY = source.endY else {
                throw ActionExecutionError.invalidAction
            }
            return PreparedAction(
                entry: entry,
                actionClass: actionClass,
                point: try screenPoint(local: CGPoint(x: x, y: y), expected: expected),
                endPoint: try screenPoint(local: CGPoint(x: endX, y: endY), expected: expected),
                safeRegion: safeRegion
            )
        case .press, .text:
            throw PIDTargetedActionFailure.compatibilityDisabled
        }
    }

    private func pointerPoint(
        _ action: NativeAction,
        safeRegion: PointerSafeRegionAuthority,
        expected: ActionGuard
    ) throws -> CGPoint {
        let localPoint: CGPoint
        if let x = action.x, let y = action.y {
            localPoint = CGPoint(x: x, y: y)
        } else {
            localPoint = CGPoint(x: safeRegion.bounds.midX, y: safeRegion.bounds.midY)
        }
        if !pidStrictlyContains(safeRegion.bounds, point: localPoint) {
            throw ActionExecutionError.outOfBounds
        }
        return try screenPoint(local: localPoint, expected: expected)
    }

    private func prepareSafeRegion(
        _ entry: PlannedDispatchEntry,
        expected: ActionGuard
    ) throws -> PointerSafeRegionAuthority {
        let sourceReference = pidLocalPoints(entry.source).isEmpty
            ? entry.source.elementRef
            : entry.source.targetElementRef
        guard let planned = entry.pointerSafeRegion else { throw ActionExecutionError.invalidAction }
        if entry.backend == .foregroundPointer, sourceReference == nil {
            let localBounds = CGRect(origin: .zero, size: expected.bounds.size)
            guard genericForegroundEnabled, expected.interactionMode == .foregroundTakeover,
                  planned.reference == foregroundWindowRegionReference,
                  planned.identityToken == expected.snapshotID, planned.bounds == localBounds,
                  !pidLocalPoints(entry.source).isEmpty,
                  pidLocalPoints(entry.source).allSatisfy({ pidStrictlyContains(localBounds, point: $0) })
            else { throw ActionExecutionError.staleSnapshot }
            return planned
        }
        guard sourceReference == planned.reference else {
            logActionRejected("PID-GUARD safe-ref-mismatch snapshotID=\(expected.snapshotID)")
            throw ActionExecutionError.staleSnapshot
        }
        guard let current = element(planned.reference, expected.snapshotID) else {
            logActionRejected("PID-GUARD safe-element-gone snapshotID=\(expected.snapshotID)")
            throw ActionExecutionError.staleSnapshot
        }
        guard current.enabled != false, !current.isSecure else {
            logActionRejected("PID-GUARD safe-element-blocked snapshotID=\(expected.snapshotID)")
            throw ActionExecutionError.staleSnapshot
        }
        guard pidValidLocalRect(current.bounds, within: expected.bounds.size) else {
            throw ActionExecutionError.outOfBounds
        }
        guard planned.identityToken == current.identityToken,
              planned.bounds == current.bounds
        else {
            logActionRejected("PID-GUARD safe-region-moved snapshotID=\(expected.snapshotID)")
            throw ActionExecutionError.staleSnapshot
        }
        for point in pidLocalPoints(entry.source) {
            guard pidStrictlyContains(planned.bounds, point: point) else {
                throw ActionExecutionError.outOfBounds
            }
        }
        return planned
    }

    private func screenPoint(local: CGPoint, expected: ActionGuard) throws -> CGPoint {
        guard local.x.isFinite, local.y.isFinite,
              local.x >= 0, local.y >= 0,
              local.x < expected.bounds.width, local.y < expected.bounds.height
        else { throw ActionExecutionError.outOfBounds }
        let point = CGPoint(x: expected.bounds.minX + local.x, y: expected.bounds.minY + local.y)
        guard expected.bounds.contains(point) else { throw ActionExecutionError.outOfBounds }
        return point
    }

    private func perform(
        _ action: PreparedAction,
        expected: ActionGuard,
        marker: UInt64,
        lease: UserActivitySessionLease,
        deadline: TimeInterval
    ) throws {
        switch action.actionClass {
        case .click:
            try postClicks(count: 1, button: action.entry.source.kind == .rightClick ? .right : .left, point: action.point, safeRegion: action.safeRegion, expected: expected, marker: marker, lease: lease, deadline: deadline)
        case .doubleClick:
            try postClicks(count: 2, point: action.point, safeRegion: action.safeRegion, expected: expected, marker: marker, lease: lease, deadline: deadline)
        case .scroll:
            let dx = try pidBoundedInt32(action.entry.source.deltaX ?? 0)
            let dy = try pidBoundedInt32(action.entry.source.deltaY ?? 0)
            try checkedPost(
                .scroll(point: action.point, deltaX: dx, deltaY: dy),
                point: action.point,
                safeRegion: action.safeRegion,
                expected: expected,
                marker: marker,
                lease: lease,
                deadline: deadline
            )
        case .drag:
            guard let end = action.endPoint else { throw ActionExecutionError.invalidAction }
            try postDrag(
                from: action.point,
                to: end,
                durationMS: action.entry.source.durationMS ?? 300,
                safeRegion: action.safeRegion,
                expected: expected,
                marker: marker,
                lease: lease,
                deadline: deadline
            )
        case .press, .text:
            throw PIDTargetedActionFailure.compatibilityDisabled
        }
    }

    private func postClicks(
        count: Int,
        button: PointerButton = .left,
        point: CGPoint,
        safeRegion: PointerSafeRegionAuthority,
        expected: ActionGuard,
        marker: UInt64,
        lease: UserActivitySessionLease,
        deadline: TimeInterval
    ) throws {
        var inputStarted = false
        for clickState in 1 ... count {
            do {
                try postBalanced(
                    down: .mouseDown(point: point, clickCount: clickState, button: button),
                    release: .mouseUp(point: point, clickCount: clickState, button: button),
                    intermediate: [],
                    safeRegion: safeRegion,
                    expected: expected,
                    marker: marker,
                    lease: lease,
                    deadline: deadline
                )
                inputStarted = true
            } catch let failure as PIDActionPerformFailure {
                throw PIDActionPerformFailure(
                    error: failure.error,
                    inputStarted: inputStarted || failure.inputStarted,
                    cleanupFailed: failure.cleanupFailed
                )
            } catch let pause as PIDUserActivityPauseFailure {
                throw PIDUserActivityPauseFailure(
                    inputStarted: inputStarted || pause.inputStarted,
                    cleanupFailed: pause.cleanupFailed
                )
            }
        }
    }

    private func postDrag(
        from start: CGPoint,
        to end: CGPoint,
        durationMS: Int,
        safeRegion: PointerSafeRegionAuthority,
        expected: ActionGuard,
        marker: UInt64,
        lease: UserActivitySessionLease,
        deadline: TimeInterval
    ) throws {
        guard durationMS >= 0, durationMS <= maximumNativeWaitMilliseconds else {
            throw ActionExecutionError.actionTimeout
        }
        let steps = max(1, min(120, durationMS / 10))
        let intermediate = (1 ... steps).map { step -> SyntheticInputEvent in
            let fraction = CGFloat(step) / CGFloat(steps)
            return .mouseDragged(point: CGPoint(
                x: start.x + (end.x - start.x) * fraction,
                y: start.y + (end.y - start.y) * fraction
            ))
        }
        try postBalanced(
            down: .mouseDown(point: start, clickCount: 1),
            release: .mouseUp(point: end, clickCount: 1),
            intermediate: intermediate,
            safeRegion: safeRegion,
            expected: expected,
            marker: marker,
            lease: lease,
            deadline: deadline,
            delayMicroseconds: durationMS > 0 ? useconds_t(durationMS * 1_000 / steps) : 0
        )
    }

    private func postBalanced(
        down: SyntheticInputEvent,
        release: SyntheticInputEvent,
        intermediate: [SyntheticInputEvent],
        safeRegion: PointerSafeRegionAuthority,
        expected: ActionGuard,
        marker: UInt64,
        lease: UserActivitySessionLease,
        deadline: TimeInterval,
        delayMicroseconds: useconds_t = 0
    ) throws {
        let token = UUID()
        let cleanupRelease = PIDCleanupReleaseState(release: release, initialPoint: down.point)
        let scope: HeldInputScope
        do {
            scope = try activity.heldInputScope(lease: lease)
        } catch is UserActivityMonitoringError {
            throw PIDUserActivityPauseFailure(inputStarted: false, cleanupFailed: false)
        }
        let started = now()
        var dragDisplacement: CGVector? = genericForegroundEnabled &&
            expected.interactionMode == .foregroundTakeover &&
            safeRegion.reference == foregroundWindowRegionReference && !intermediate.isEmpty ? .zero : nil
        var inputStarted = false
        do {
            try activity.performPIDEvent(
                lease: lease,
                validate: { try self.revalidate(expected: expected, point: down.point, safeRegion: safeRegion, deadline: deadline) },
                mutation: {
                    try heldInputs.begin(
                        token: token,
                        scope: scope,
                        postDown: { try self.poster.post(down, to: expected.pid, marker: marker, deadline: deadline) },
                        release: { try self.poster.post(release, to: expected.pid, marker: marker) },
                        cleanupRelease: { try self.poster.post(cleanupRelease.event, to: expected.pid, marker: marker) }
                    )
                }
            )
            inputStarted = true
            for (index, event) in intermediate.enumerated() {
                try checkedPost(
                    event,
                    point: event.point,
                    safeRegion: safeRegion,
                    expected: expected,
                    marker: marker,
                    lease: lease,
                    deadline: deadline,
                    dragDisplacement: dragDisplacement,
                    afterPosting: {
                        cleanupRelease.advance(afterPosting: event)
                        if dragDisplacement != nil, let point = event.point, let start = down.point {
                            dragDisplacement = CGVector(dx: point.x - start.x, dy: point.y - start.y)
                        }
                    }
                )
                if delayMicroseconds > 0 {
                    let due = started + Double(index + 1) * Double(delayMicroseconds) / 1_000_000
                    let remaining = max(0, due - now())
                    if remaining > 0 { delay(useconds_t(min(remaining * 1_000_000, Double(delayMicroseconds)))) }
                }
            }
            try activity.performPIDEvent(
                lease: lease,
                validate: { try self.revalidate(expected: expected, point: release.point, safeRegion: safeRegion,
                    deadline: deadline, dragDisplacement: dragDisplacement, balancedRelease: true) },
                mutation: { try heldInputs.finish(token: token) }
            )
        } catch is UserActivityMonitoringError {
            throw PIDUserActivityPauseFailure(
                inputStarted: inputStarted,
                cleanupFailed: cleanupHeld(token: token, scope: scope, lease: lease, inputStarted: inputStarted)
            )
        } catch let failure as SyntheticInputFailure {
            throw PIDActionPerformFailure(
                error: failure.error,
                inputStarted: inputStarted || failure.inputStarted,
                cleanupFailed: cleanupHeld(
                    token: token,
                    scope: scope,
                    lease: lease,
                    inputStarted: inputStarted || failure.inputStarted
                )
            )
        } catch let error as ActionExecutionError {
            throw PIDActionPerformFailure(
                error: error,
                inputStarted: inputStarted,
                cleanupFailed: cleanupHeld(token: token, scope: scope, lease: lease, inputStarted: inputStarted)
            )
        } catch {
            throw PIDActionPerformFailure(
                error: .helperFailed,
                inputStarted: inputStarted,
                cleanupFailed: cleanupHeld(token: token, scope: scope, lease: lease, inputStarted: inputStarted)
            )
        }
    }

    private func checkedPost(
        _ event: SyntheticInputEvent,
        point: CGPoint?,
        safeRegion: PointerSafeRegionAuthority,
        expected: ActionGuard,
        marker: UInt64,
        lease: UserActivitySessionLease,
        deadline: TimeInterval,
        dragDisplacement: CGVector? = nil,
        afterPosting: () -> Void = {}
    ) throws {
        try activity.performPIDEvent(
            lease: lease,
            validate: { try self.revalidate(expected: expected, point: point, safeRegion: safeRegion, deadline: deadline, dragDisplacement: dragDisplacement) },
            mutation: {
                try poster.post(event, to: expected.pid, marker: marker, deadline: deadline)
                afterPosting()
            }
        )
    }

    private func revalidate(
        expected: ActionGuard,
        point: CGPoint?,
        safeRegion: PointerSafeRegionAuthority,
        deadline: TimeInterval,
        dragDisplacement: CGVector? = nil,
        balancedRelease: Bool = false
    ) throws {
        guard now() <= deadline else { throw ActionExecutionError.actionTimeout }
        do {
            if balancedRelease {
                try validator.revalidateBalancedRelease(
                    expected: expected, point: point, dragDisplacement: dragDisplacement
                )
            } else {
                try validator.revalidate(expected: expected, point: point, dragDisplacement: dragDisplacement)
            }
        } catch {
            logActionRejected("PID-GUARD observation error=\(error) drag=\(dragDisplacement != nil)")
            throw error
        }
        if genericForegroundEnabled, expected.interactionMode == .foregroundTakeover,
           safeRegion.reference == foregroundWindowRegionReference,
           safeRegion.identityToken == expected.snapshotID {
            let localBounds = CGRect(origin: .zero, size: expected.bounds.size)
            guard safeRegion.bounds == localBounds else { throw ActionExecutionError.staleSnapshot }
            if let point {
                let local = CGPoint(x: point.x - expected.bounds.minX, y: point.y - expected.bounds.minY)
                guard pidStrictlyContains(localBounds, point: local) else { throw ActionExecutionError.outOfBounds }
            }
            guard now() <= deadline else { throw ActionExecutionError.actionTimeout }
            return
        }
        if balancedRelease {
            // The press reached the proven element; this release completes it at the same point.
            // Controls change as they are pressed (live Outlook: the search box expands and opens
            // its suggestions), and cleanup would post this same release anyway. The window checks
            // above still apply; only the element is not proven again.
            guard now() <= deadline else { throw ActionExecutionError.actionTimeout }
            return
        }
        guard let current = element(safeRegion.reference, expected.snapshotID),
              current.enabled != false,
              !current.isSecure,
              current.identityToken == safeRegion.identityToken,
              pidApproximatelyEqual(current.bounds, safeRegion.bounds)
        else {
            logActionRejected("PID-GUARD safe-region-recheck snapshotID=\(expected.snapshotID)")
            throw ActionExecutionError.staleSnapshot
        }
        guard pidValidLocalRect(current.bounds, within: expected.bounds.size) else {
            throw ActionExecutionError.outOfBounds
        }
        if let point {
            let local = CGPoint(x: point.x - expected.bounds.minX, y: point.y - expected.bounds.minY)
            guard pidStrictlyContains(current.bounds, point: local) else {
                throw ActionExecutionError.outOfBounds
            }
        }
        guard now() <= deadline else { throw ActionExecutionError.actionTimeout }
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

}

struct PIDTargetedPreparedPlan {
    fileprivate let actions: [Int: PIDTargetedActionExecutor.PreparedAction]

    var sourceIndexes: [Int] {
        actions.keys.sorted()
    }
}

enum PIDTargetedActionFailure: Error {
    case compatibilityDisabled
}

private struct PIDUserActivityPauseFailure: Error {
    let inputStarted: Bool
    let cleanupFailed: Bool
}

private struct PIDActionPerformFailure: Error {
    let error: ActionExecutionError
    let inputStarted: Bool
    let cleanupFailed: Bool
}

private final class PIDCleanupReleaseState {
    private let lock = NSLock()
    private let template: SyntheticInputEvent
    private var lastPoint: CGPoint?

    init(release: SyntheticInputEvent, initialPoint: CGPoint?) {
        template = release
        lastPoint = initialPoint
    }

    var event: SyntheticInputEvent {
        lock.lock()
        defer { lock.unlock() }
        guard let lastPoint else { return template }
        return template.replacingPoint(with: lastPoint)
    }

    func advance(afterPosting event: SyntheticInputEvent) {
        guard let point = event.point else { return }
        lock.lock()
        lastPoint = point
        lock.unlock()
    }
}

private extension SyntheticInputEvent {
    var point: CGPoint? {
        switch self {
        case let .mouseDown(point, _, _), let .mouseUp(point, _, _), let .mouseDragged(point), let .scroll(point, _, _): point
        case .unicodeKeyDown, .unicodeKeyUp, .virtualKeyDown, .virtualKeyUp: nil
        }
    }

    func replacingPoint(with point: CGPoint) -> SyntheticInputEvent {
        switch self {
        case let .mouseDown(_, clickCount, button): .mouseDown(point: point, clickCount: clickCount, button: button)
        case let .mouseUp(_, clickCount, button): .mouseUp(point: point, clickCount: clickCount, button: button)
        case .mouseDragged: .mouseDragged(point: point)
        case let .scroll(_, deltaX, deltaY): .scroll(point: point, deltaX: deltaX, deltaY: deltaY)
        case .unicodeKeyDown, .unicodeKeyUp, .virtualKeyDown, .virtualKeyUp: self
        }
    }
}

private func pidBoundedInt32(_ value: CGFloat) throws -> Int32 {
    guard value.isFinite, abs(value) <= 1_000_000 else { throw ActionExecutionError.outOfBounds }
    return Int32(value.rounded())
}

private func pidFiniteRect(_ rect: CGRect) -> Bool {
    rect.origin.x.isFinite && rect.origin.y.isFinite && rect.width.isFinite && rect.height.isFinite && rect.width > 0 && rect.height > 0
}

private func pidApproximatelyEqual(_ lhs: CGRect, _ rhs: CGRect) -> Bool {
    abs(lhs.origin.x - rhs.origin.x) <= 1 &&
        abs(lhs.origin.y - rhs.origin.y) <= 1 &&
        abs(lhs.width - rhs.width) <= 1 &&
        abs(lhs.height - rhs.height) <= 1
}

private func pidValidLocalRect(_ rect: CGRect, within size: CGSize) -> Bool {
    pidFiniteRect(rect) && rect.minX >= 0 && rect.minY >= 0 && rect.maxX <= size.width && rect.maxY <= size.height
}

private func pidLocalPoints(_ action: NativeAction) -> [CGPoint] {
    var points: [CGPoint] = []
    if let x = action.x, let y = action.y { points.append(CGPoint(x: x, y: y)) }
    if let endX = action.endX, let endY = action.endY { points.append(CGPoint(x: endX, y: endY)) }
    return points
}

private func pidStrictlyContains(_ rect: CGRect, point: CGPoint) -> Bool {
    point.x.isFinite && point.y.isFinite &&
        point.x > rect.minX && point.x < rect.maxX && point.y > rect.minY && point.y < rect.maxY
}
