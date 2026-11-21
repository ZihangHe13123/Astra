import CoreGraphics
import Foundation

enum UserActivityReason: String, Equatable {
    case pointerMotion = "pointer_motion"
    case pointerButton = "pointer_button"
    case scroll
    case keyboard
    case monitorDisabled = "monitor_disabled"
}

enum UserActivityEvent: Equatable {
    case pointerMotion(deltaX: Double, deltaY: Double, marker: UInt64)
    case pointerButton(marker: UInt64)
    case scroll(deltaX: Double, deltaY: Double, marker: UInt64)
    case keyDown(keyCode: Int64, marker: UInt64)
    case modifierFlagsChanged(marker: UInt64)

    var marker: UInt64 {
        switch self {
        case let .pointerMotion(_, _, marker), let .scroll(_, _, marker): marker
        case let .pointerButton(marker), let .keyDown(_, marker), let .modifierFlagsChanged(marker): marker
        }
    }
}

enum UserActivityClassification: Equatable {
    case ignoredSynthetic
    case ignoredModifier
    case jitter
    case pause(UserActivityReason)
}

final class UserActivityClassifier {
    private struct MotionSample {
        let timestamp: TimeInterval
        let distance: Double
    }

    private let marker: UInt64
    private let clock: () -> TimeInterval
    let motionSampleCapacity = 16_384
    let motionExpirationBudget = 256
    private var motion: [MotionSample?]
    private var motionHead = 0
    private(set) var bufferedMotionSampleCount = 0
    private(set) var lastMotionExpirationCount = 0
    private var motionDistance = 0.0

    init(marker: UInt64, clock: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }) {
        self.marker = marker
        self.clock = clock
        motion = Array(repeating: nil, count: motionSampleCapacity)
    }

    func consume(_ event: UserActivityEvent) -> UserActivityClassification {
        guard event.marker != marker else { return .ignoredSynthetic }
        switch event {
        case let .pointerMotion(deltaX, deltaY, _):
            let now = clock()
            guard expireMotion(before: now - 0.150) else { return .pause(.pointerMotion) }
            let distance = hypot(deltaX, deltaY)
            guard distance.isFinite else { return .jitter }
            guard distance > 0 else { return .jitter }
            guard bufferedMotionSampleCount < motionSampleCapacity else { return .pause(.pointerMotion) }
            appendMotion(MotionSample(timestamp: now, distance: distance))
            return motionDistance > 12 ? .pause(.pointerMotion) : .jitter
        case .pointerButton:
            return .pause(.pointerButton)
        case let .scroll(deltaX, deltaY, _):
            return deltaX != 0 || deltaY != 0 ? .pause(.scroll) : .jitter
        case .keyDown:
            return .pause(.keyboard)
        case .modifierFlagsChanged:
            return .ignoredModifier
        }
    }

    private func expireMotion(before cutoff: TimeInterval) -> Bool {
        lastMotionExpirationCount = 0
        while bufferedMotionSampleCount > 0, let oldest = motion[motionHead], oldest.timestamp < cutoff {
            guard lastMotionExpirationCount < motionExpirationBudget else { return false }
            motionDistance -= oldest.distance
            motion[motionHead] = nil
            motionHead = (motionHead + 1) % motionSampleCapacity
            bufferedMotionSampleCount -= 1
            lastMotionExpirationCount += 1
        }
        if bufferedMotionSampleCount == 0 { motionDistance = 0 }
        return true
    }

    private func appendMotion(_ sample: MotionSample) {
        let insertion = (motionHead + bufferedMotionSampleCount) % motionSampleCapacity
        motion[insertion] = sample
        bufferedMotionSampleCount += 1
        motionDistance += sample.distance
    }
}

enum UserActivityMonitoringError: Error, Equatable {
    case alreadyArmed
    case eventTapUnavailable
    case eventTapDisabled
    case startupTimedOut
    case notArmed
    case paused
}

final class UserActivitySessionLease {
    // This object owns teardown authority for the session. Fragment one-use state
    // is tracked separately by the arbiter and never consumes that authority.
    // Authority is the arbiter-issued object identity, never these readable marker bytes.
    let marker: UInt64

    init(marker: UInt64) { self.marker = marker }
}

protocol UserActivityMonitoring: AnyObject {
    var paused: Bool { get }
    func arm(marker: UInt64, notification: UserActivityPauseSignal) throws -> UserActivitySessionLease
    func beginFragment(lease: UserActivitySessionLease) throws
    func endFragment(lease: UserActivitySessionLease)
    func assertNotPaused(lease: UserActivitySessionLease) throws
    func heldInputScope(lease: UserActivitySessionLease) throws -> HeldInputScope
    func performPIDEvent(
        lease: UserActivitySessionLease,
        validate: () throws -> Void,
        mutation: () throws -> Void
    ) throws
    func performPIDCleanup(lease: UserActivitySessionLease, _ cleanup: () throws -> Void) throws
    func cleanupHeldInputs(lease: UserActivitySessionLease) throws -> HeldInputCleanupResult
    @discardableResult
    func disarm(lease: UserActivitySessionLease) throws -> Bool
}

protocol HelperSessionActivityMonitoring: UserActivityMonitoring {
    func requestTerminalStop()
}

final class UserActivityPauseSignal {
    private let condition = NSCondition()
    private let onOffer: (() -> Void)?
    private var pending: UserActivityReason?
    private var offered = false

    init(onOffer: (() -> Void)? = nil) {
        self.onOffer = onOffer
    }

    func offer(_ reason: UserActivityReason) {
        condition.lock()
        guard !offered else {
            condition.unlock()
            return
        }
        offered = true
        pending = reason
        condition.signal()
        condition.unlock()
        if let onOffer {
            DispatchQueue.global(qos: .userInitiated).async(execute: onOffer)
        }
    }

    func poll() -> UserActivityReason? {
        condition.lock()
        defer { condition.unlock() }
        defer { pending = nil }
        return pending
    }

    func wait(timeout: TimeInterval) -> UserActivityReason? {
        let deadline = Date(timeIntervalSinceNow: max(0, timeout))
        condition.lock()
        defer { condition.unlock() }
        while pending == nil, condition.wait(until: deadline) {}
        defer { pending = nil }
        return pending
    }
}

final class UserActivityPauseLatch {
    private let lock = NSLock()
    private var pauseReason: UserActivityReason?

    var paused: Bool {
        lock.lock()
        defer { lock.unlock() }
        return pauseReason != nil
    }

    @discardableResult
    func pause(reason: UserActivityReason) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard pauseReason == nil else { return false }
        pauseReason = reason
        return true
    }

}

enum AstraEventMarker {
    static func random() -> UInt64 {
        var marker = UInt64.random(in: UInt64.min ... UInt64.max)
        if marker == 0 { marker = 1 }
        return marker
    }
}

enum UserActivityTapSignal: Equatable {
    case event(UserActivityEvent)
    case disabledByTimeout
    case disabledByUserInput
}

protocol UserActivityWorkSubmitting: AnyObject {
    func submit(_ work: @escaping () -> Void)
}

final class UserActivitySerialWorker: UserActivityWorkSubmitting {
    private let queue = DispatchQueue(label: "astra.user-activity-worker", qos: .userInitiated)
    func submit(_ work: @escaping () -> Void) { queue.async(execute: work) }
}

protocol UserActivityTapRunning: AnyObject {
    func start(
        generation: UUID,
        signal: @escaping (UserActivityTapSignal) -> Void,
        publish: @escaping (Bool) -> Bool,
        exited: @escaping () -> Void
    )
    func stop(generation: UUID)
}

final class SystemUserActivityTapRuntime: UserActivityTapRunning {
    private final class Control {
        var stopRequested = false
        var tap: CFMachPort?
        var runLoop: CFRunLoop?
    }

    private final class TapContext {
        let signal: (UserActivityTapSignal) -> Void
        init(signal: @escaping (UserActivityTapSignal) -> Void) { self.signal = signal }
    }

    private let lock = NSLock()
    private var controls: [UUID: Control] = [:]

    func start(
        generation: UUID,
        signal: @escaping (UserActivityTapSignal) -> Void,
        publish: @escaping (Bool) -> Bool,
        exited: @escaping () -> Void
    ) {
        let control = Control()
        lock.lock()
        controls[generation] = control
        lock.unlock()
        let context = TapContext(signal: signal)
        let thread = Thread { [weak self] in
            guard let self else {
                _ = publish(false)
                exited()
                return
            }
            guard let tap = CGEvent.tapCreate(
                tap: .cgSessionEventTap,
                place: .headInsertEventTap,
                options: .listenOnly,
                eventsOfInterest: Self.eventMask,
                callback: Self.tapCallback,
                userInfo: Unmanaged.passUnretained(context).toOpaque()
            ) else {
                _ = publish(false)
                self.remove(generation)
                exited()
                return
            }
            let source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
            self.lock.lock()
            let mayPublish = self.controls[generation] === control && !control.stopRequested
            if mayPublish {
                control.tap = tap
                control.runLoop = CFRunLoopGetCurrent()
            }
            self.lock.unlock()
            guard mayPublish else {
                self.remove(generation)
                exited()
                return
            }
            CFRunLoopAddSource(CFRunLoopGetCurrent(), source, .commonModes)
            CGEvent.tapEnable(tap: tap, enable: true)
            guard publish(true) else {
                CGEvent.tapEnable(tap: tap, enable: false)
                CFRunLoopRemoveSource(CFRunLoopGetCurrent(), source, .commonModes)
                self.remove(generation)
                exited()
                return
            }
            self.lock.lock()
            let shouldRun = self.controls[generation] === control && !control.stopRequested
            self.lock.unlock()
            if shouldRun { CFRunLoopRun() }
            CGEvent.tapEnable(tap: tap, enable: false)
            CFRunLoopRemoveSource(CFRunLoopGetCurrent(), source, .commonModes)
            self.remove(generation)
            exited()
            withExtendedLifetime(context) {}
        }
        thread.name = "astra.user-activity-tap"
        thread.start()
    }

    func stop(generation: UUID) {
        lock.lock()
        let control = controls[generation]
        control?.stopRequested = true
        let tap = control?.tap
        let runLoop = control?.runLoop
        lock.unlock()
        if let tap { CGEvent.tapEnable(tap: tap, enable: false) }
        if let runLoop { CFRunLoopStop(runLoop) }
    }

    private func remove(_ generation: UUID) {
        lock.lock()
        controls.removeValue(forKey: generation)
        lock.unlock()
    }

    private static let eventMask: CGEventMask = [
        CGEventType.mouseMoved, .leftMouseDragged, .rightMouseDragged, .otherMouseDragged,
        .leftMouseDown, .rightMouseDown, .otherMouseDown, .scrollWheel, .keyDown, .flagsChanged,
    ].reduce(0) { $0 | (CGEventMask(1) << $1.rawValue) }

    private static let tapCallback: CGEventTapCallBack = { _, type, event, userInfo in
        guard let userInfo else { return Unmanaged.passUnretained(event) }
        let context = Unmanaged<TapContext>.fromOpaque(userInfo).takeUnretainedValue()
        if type == .tapDisabledByTimeout {
            context.signal(.disabledByTimeout)
            return Unmanaged.passUnretained(event)
        }
        if type == .tapDisabledByUserInput {
            context.signal(.disabledByUserInput)
            return Unmanaged.passUnretained(event)
        }
        let marker = UInt64(bitPattern: event.getIntegerValueField(.eventSourceUserData))
        let bounded: UserActivityEvent?
        switch type {
        case .mouseMoved, .leftMouseDragged, .rightMouseDragged, .otherMouseDragged:
            bounded = .pointerMotion(
                deltaX: Double(event.getIntegerValueField(.mouseEventDeltaX)),
                deltaY: Double(event.getIntegerValueField(.mouseEventDeltaY)),
                marker: marker
            )
        case .leftMouseDown, .rightMouseDown, .otherMouseDown:
            bounded = .pointerButton(marker: marker)
        case .scrollWheel:
            bounded = .scroll(
                deltaX: scrollActivityDelta(event, fields: [
                    .scrollWheelEventPointDeltaAxis2,
                    .scrollWheelEventFixedPtDeltaAxis2,
                    .scrollWheelEventDeltaAxis2,
                ]),
                deltaY: scrollActivityDelta(event, fields: [
                    .scrollWheelEventPointDeltaAxis1,
                    .scrollWheelEventFixedPtDeltaAxis1,
                    .scrollWheelEventDeltaAxis1,
                ]),
                marker: marker
            )
        case .keyDown:
            bounded = .keyDown(keyCode: event.getIntegerValueField(.keyboardEventKeycode), marker: marker)
        case .flagsChanged:
            bounded = .modifierFlagsChanged(marker: marker)
        default:
            bounded = nil
        }
        if let bounded { context.signal(.event(bounded)) }
        return Unmanaged.passUnretained(event)
    }

    private static func scrollActivityDelta(_ event: CGEvent, fields: [CGEventField]) -> Double {
        for field in fields {
            let value = event.getIntegerValueField(field)
            if value != 0 { return Double(value) }
        }
        return 0
    }
}

final class UserActivityArbiter: HelperSessionActivityMonitoring {
    private final class StartupWaiter {
        let semaphore = DispatchSemaphore(value: 0)
        private let lock = NSLock()
        private var result: Bool?

        func complete(_ value: Bool) {
            lock.lock()
            guard result == nil else {
                lock.unlock()
                return
            }
            result = value
            lock.unlock()
            semaphore.signal()
        }

        var value: Bool? {
            lock.lock()
            defer { lock.unlock() }
            return result
        }
    }

    private final class Session {
        enum FragmentState {
            case unused
            case active
            case finished
        }

        let generation: UUID
        let marker: UInt64
        let lease: UserActivitySessionLease
        let heldInputScope: HeldInputScope
        let classifier: UserActivityClassifier
        let latch = UserActivityPauseLatch()
        let notification: UserActivityPauseSignal
        let waiter = StartupWaiter()
        let teardownSemaphore = DispatchSemaphore(value: 0)
        var tapExited = false
        var disarmRequested = false
        var pauseWorkScheduled = false
        var pauseWorkCompleted = false
        var teardownCompleted = false
        var fragmentState = FragmentState.unused
        var terminalInterrupted: Bool?
        var acceptingSignals = true
        var inFlightSignals = 0

        init(generation: UUID, marker: UInt64, notification: UserActivityPauseSignal) {
            self.generation = generation
            self.marker = marker
            lease = UserActivitySessionLease(marker: marker)
            heldInputScope = .cooperative(marker: marker, generation: generation)
            classifier = UserActivityClassifier(marker: marker)
            self.notification = notification
        }
    }

    private enum Phase {
        case idle
        case starting(Session)
        case armed(Session)
        case disabledRunning(Session)
        case disabledExited(Session)
        case stopping(Session)

        var session: Session? {
            switch self {
            case .idle: nil
            case let .starting(session), let .armed(session), let .disabledRunning(session),
                 let .disabledExited(session), let .stopping(session): session
            }
        }
    }

    private let stateLock = NSLock()
    private let signalCondition = NSCondition()
    private let eventGate = NSLock()
    private let heldInputs: HeldInputRegistry
    private let runtime: any UserActivityTapRunning
    private let worker: any UserActivityWorkSubmitting
    private let startupTimeout: TimeInterval
    private let afterSignalRegistration: () -> Void
    private var phase: Phase = .idle
    private var terminalStopRequested = false

    init(
        heldInputs: HeldInputRegistry = .shared,
        runtime: any UserActivityTapRunning = SystemUserActivityTapRuntime(),
        worker: any UserActivityWorkSubmitting = UserActivitySerialWorker(),
        startupTimeout: TimeInterval = 2,
        afterSignalRegistration: @escaping () -> Void = {}
    ) {
        self.heldInputs = heldInputs
        self.runtime = runtime
        self.worker = worker
        self.startupTimeout = startupTimeout
        self.afterSignalRegistration = afterSignalRegistration
    }

    var paused: Bool {
        stateLock.lock()
        defer { stateLock.unlock() }
        return switch phase {
        case let .armed(session): session.latch.paused
        case .disabledRunning, .disabledExited: true
        case let .stopping(session): session.terminalInterrupted ?? session.latch.paused
        case .idle, .starting: false
        }
    }

    // Stop admitting work for this helper, but leave the tap and teardown lease
    // intact. Only actual interference (or a failed drain) should veto restoration.
    func requestTerminalStop() {
        stateLock.lock()
        terminalStopRequested = true
        stateLock.unlock()
    }

    func arm(marker: UInt64, notification: UserActivityPauseSignal) throws -> UserActivitySessionLease {
        guard marker != 0 else { throw UserActivityMonitoringError.eventTapUnavailable }
        let session = Session(generation: UUID(), marker: marker, notification: notification)
        stateLock.lock()
        guard !terminalStopRequested else {
            stateLock.unlock()
            throw ActionExecutionError.helperFailed
        }
        guard case .idle = phase else {
            stateLock.unlock()
            throw UserActivityMonitoringError.alreadyArmed
        }
        phase = .starting(session)
        stateLock.unlock()
        runtime.start(
            generation: session.generation,
            signal: { [weak self] signal in self?.receiveFenced(signal, session: session) },
            publish: { [weak self] enabled in self?.publish(enabled: enabled, session: session) ?? false },
            exited: { [weak self] in self?.exitedFenced(session: session) }
        )
        guard session.waiter.semaphore.wait(timeout: .now() + startupTimeout) == .success else {
            stateLock.lock()
            if let current = phase.session, current.generation == session.generation {
                current.disarmRequested = true
                phase = .stopping(current)
            }
            stateLock.unlock()
            runtime.stop(generation: session.generation)
            session.teardownSemaphore.wait()
            throw UserActivityMonitoringError.startupTimedOut
        }
        guard session.waiter.value == true else {
            runtime.stop(generation: session.generation)
            session.teardownSemaphore.wait()
            throw UserActivityMonitoringError.eventTapUnavailable
        }
        return session.lease
    }

    func beginFragment(lease: UserActivitySessionLease) throws {
        stateLock.lock()
        defer { stateLock.unlock() }
        guard !terminalStopRequested else { throw ActionExecutionError.helperFailed }
        guard case let .armed(session) = phase,
              session.lease === lease,
              !session.latch.paused,
              session.fragmentState == .unused
        else { throw UserActivityMonitoringError.notArmed }
        session.fragmentState = .active
    }

    func endFragment(lease: UserActivitySessionLease) {
        stateLock.lock()
        defer { stateLock.unlock() }
        guard let session = phase.session, session.lease === lease, session.fragmentState == .active else { return }
        session.fragmentState = .finished
        finishStoppingIfPossibleLocked(session)
    }

    func assertNotPaused(lease: UserActivitySessionLease) throws {
        stateLock.lock()
        defer { stateLock.unlock() }
        guard !terminalStopRequested else { throw ActionExecutionError.helperFailed }
        switch phase {
        case let .armed(session) where session.lease === lease:
            if session.latch.paused { throw UserActivityMonitoringError.paused }
        case let .disabledRunning(session) where session.lease === lease,
             let .disabledExited(session) where session.lease === lease:
            throw UserActivityMonitoringError.eventTapDisabled
        case .idle, .starting, .armed, .disabledRunning, .disabledExited, .stopping:
            throw UserActivityMonitoringError.notArmed
        }
    }

    func heldInputScope(lease: UserActivitySessionLease) throws -> HeldInputScope {
        try assertNotPaused(lease: lease)
        stateLock.lock()
        defer { stateLock.unlock() }
        guard case let .armed(session) = phase, session.lease === lease else {
            throw UserActivityMonitoringError.notArmed
        }
        return session.heldInputScope
    }

    func performPIDEvent(
        lease: UserActivitySessionLease,
        validate: () throws -> Void,
        mutation: () throws -> Void
    ) throws {
        eventGate.lock()
        defer { eventGate.unlock() }
        try assertNotPaused(lease: lease)
        try validate()
        try assertNotPaused(lease: lease)
        try mutation()
    }

    func performPIDCleanup(lease: UserActivitySessionLease, _ cleanup: () throws -> Void) throws {
        eventGate.lock()
        defer { eventGate.unlock() }
        stateLock.lock()
        let ownsCurrentGeneration = phase.session?.lease === lease
        stateLock.unlock()
        guard ownsCurrentGeneration else { throw UserActivityMonitoringError.notArmed }
        try cleanup()
    }

    func cleanupHeldInputs(lease: UserActivitySessionLease) throws -> HeldInputCleanupResult {
        eventGate.lock()
        defer { eventGate.unlock() }
        stateLock.lock()
        guard let session = phase.session, session.lease === lease else {
            stateLock.unlock()
            throw UserActivityMonitoringError.notArmed
        }
        let scope = session.heldInputScope
        stateLock.unlock()
        return heldInputs.cleanupAll(scope: scope)
    }

    @discardableResult
    func disarm(lease: UserActivitySessionLease) throws -> Bool {
        stateLock.lock()
        switch phase {
        case .idle:
            stateLock.unlock()
            throw UserActivityMonitoringError.notArmed
        case let .starting(session), let .armed(session), let .disabledRunning(session),
             let .disabledExited(session):
            guard session.lease === lease else {
                stateLock.unlock()
                throw UserActivityMonitoringError.notArmed
            }
            session.disarmRequested = true
            phase = .stopping(session)
            let shouldStop = !session.tapExited
            finishStoppingIfPossibleLocked(session)
            stateLock.unlock()
            if shouldStop { runtime.stop(generation: session.generation) }
            let drained = waitForSignalDrain(session)
            stateLock.lock()
            let terminalInterrupted = session.latch.paused || !drained
            session.terminalInterrupted = terminalInterrupted
            stateLock.unlock()
            return terminalInterrupted
        case let .stopping(session):
            guard session.lease === lease else {
                stateLock.unlock()
                throw UserActivityMonitoringError.notArmed
            }
            stateLock.unlock()
            let drained = waitForSignalDrain(session)
            stateLock.lock()
            let terminalInterrupted = session.terminalInterrupted ?? (session.latch.paused || !drained)
            session.terminalInterrupted = terminalInterrupted
            stateLock.unlock()
            return terminalInterrupted
        }
    }

    private func receiveFenced(_ signal: UserActivityTapSignal, session: Session) {
        signalCondition.lock()
        guard session.acceptingSignals else {
            signalCondition.unlock()
            return
        }
        session.inFlightSignals += 1
        signalCondition.unlock()
        afterSignalRegistration()
        receive(signal, generation: session.generation)
        signalCondition.lock()
        session.inFlightSignals -= 1
        signalCondition.broadcast()
        signalCondition.unlock()
        stateLock.lock()
        finishStoppingIfPossibleLocked(session)
        stateLock.unlock()
    }

    private func exitedFenced(session: Session) {
        signalCondition.lock()
        session.acceptingSignals = false
        signalCondition.broadcast()
        signalCondition.unlock()
        exited(generation: session.generation)
    }

    private func waitForSignalDrain(_ session: Session) -> Bool {
        let deadline = Date(timeIntervalSinceNow: max(0.05, startupTimeout))
        signalCondition.lock()
        defer { signalCondition.unlock() }
        while session.acceptingSignals || session.inFlightSignals > 0 {
            guard signalCondition.wait(until: deadline) else { return false }
        }
        return true
    }

    private func publish(enabled: Bool, session: Session) -> Bool {
        stateLock.lock()
        guard case let .starting(current) = phase, current.generation == session.generation else {
            stateLock.unlock()
            return false
        }
        if enabled {
            phase = .armed(current)
        } else {
            current.disarmRequested = true
            phase = .stopping(current)
        }
        stateLock.unlock()
        current.waiter.complete(enabled)
        return enabled
    }

    private func receive(_ signal: UserActivityTapSignal, generation: UUID) {
        stateLock.lock()
        let session: Session
        let wasStopping: Bool
        switch phase {
        case let .armed(current) where current.generation == generation:
            session = current
            wasStopping = false
        case let .stopping(current) where current.generation == generation:
            session = current
            wasStopping = true
        default:
            stateLock.unlock()
            return
        }
        let reason: UserActivityReason?
        let disableTap: Bool
        switch signal {
        case let .event(event):
            if case let .pause(value) = session.classifier.consume(event) { reason = value } else { reason = nil }
            disableTap = false
        case .disabledByTimeout, .disabledByUserInput:
            reason = .monitorDisabled
            disableTap = true
        }
        guard let reason, session.latch.pause(reason: reason) else {
            stateLock.unlock()
            return
        }
        session.pauseWorkScheduled = true
        if disableTap, !wasStopping { phase = .disabledRunning(session) }
        stateLock.unlock()
        enqueuePauseWork(session: session, reason: reason)
    }

    private func enqueuePauseWork(session: Session, reason: UserActivityReason) {
        worker.submit { [weak self, heldInputs, runtime] in
            if reason == .monitorDisabled { runtime.stop(generation: session.generation) }
            guard let self else { return }
            self.eventGate.lock()
            _ = heldInputs.cleanupAll(scope: session.heldInputScope)
            self.eventGate.unlock()
            session.notification.offer(reason)
            self.stateLock.lock()
            session.pauseWorkCompleted = true
            self.finishStoppingIfPossibleLocked(session)
            self.stateLock.unlock()
        }
    }

    private func exited(generation: UUID) {
        var unexpected: Session?
        stateLock.lock()
        switch phase {
        case let .starting(session) where session.generation == generation:
            session.tapExited = true
            session.disarmRequested = true
            phase = .stopping(session)
            session.waiter.complete(false)
            finishStoppingIfPossibleLocked(session)
        case let .armed(session) where session.generation == generation:
            session.tapExited = true
            if session.latch.pause(reason: .monitorDisabled) {
                session.pauseWorkScheduled = true
                unexpected = session
            }
            phase = .disabledExited(session)
        case let .disabledRunning(session) where session.generation == generation:
            session.tapExited = true
            phase = .disabledExited(session)
        case let .stopping(session) where session.generation == generation:
            session.tapExited = true
            session.waiter.complete(false)
            finishStoppingIfPossibleLocked(session)
        case .idle, .starting, .armed, .disabledRunning, .disabledExited, .stopping:
            break
        }
        stateLock.unlock()
        if let unexpected { enqueuePauseWork(session: unexpected, reason: .monitorDisabled) }
    }

    private func finishStoppingIfPossibleLocked(_ session: Session) {
        guard phase.session === session, session.disarmRequested, session.tapExited else { return }
        signalCondition.lock()
        let signalsDrained = !session.acceptingSignals && session.inFlightSignals == 0
        signalCondition.unlock()
        guard signalsDrained else { return }
        guard !session.pauseWorkScheduled || session.pauseWorkCompleted else { return }
        guard session.fragmentState != .active else { return }
        phase = .idle
        guard !session.teardownCompleted else { return }
        session.teardownCompleted = true
        session.teardownSemaphore.signal()
    }
}
