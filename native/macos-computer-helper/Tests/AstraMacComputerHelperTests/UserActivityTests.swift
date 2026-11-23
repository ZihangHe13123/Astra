@testable import AstraMacComputerHelperCore
import CoreGraphics
import Foundation
import Testing

@Test func astraMarkerAndSmallPointerJitterDoNotPause() {
    let clock = UserActivityTestClock()
    let classifier = UserActivityClassifier(marker: 42, clock: { clock.now })

    #expect(classifier.consume(.pointerMotion(deltaX: 99, deltaY: 99, marker: 42)) == .ignoredSynthetic)
    #expect(classifier.consume(.pointerMotion(deltaX: 5, deltaY: 5, marker: 0)) == .jitter)
}

@Test func pointerMotionUsesStrictTwelvePointAndInclusiveOneHundredFiftyMillisecondBoundaries() {
    let clock = UserActivityTestClock()
    let classifier = UserActivityClassifier(marker: 42, clock: { clock.now })

    #expect(classifier.consume(.pointerMotion(deltaX: 7, deltaY: 0, marker: 0)) == .jitter)
    clock.now = 0.150
    #expect(classifier.consume(.pointerMotion(deltaX: 5, deltaY: 0, marker: 0)) == .jitter)
    #expect(classifier.consume(.pointerMotion(deltaX: 0.001, deltaY: 0, marker: 0)) == .pause(.pointerMotion))

    let expiredClock = UserActivityTestClock()
    let expired = UserActivityClassifier(marker: 42, clock: { expiredClock.now })
    #expect(expired.consume(.pointerMotion(deltaX: 12, deltaY: 0, marker: 0)) == .jitter)
    expiredClock.now = 0.150_001
    #expect(expired.consume(.pointerMotion(deltaX: 0.001, deltaY: 0, marker: 0)) == .jitter)
}

@Test func highFrequencyMicroMotionUsesBoundedRollingStorageWithoutChangingThreshold() {
    let classifier = UserActivityClassifier(marker: 42, clock: { 0 })
    let micro = 1.0 / 1_024
    for _ in 0 ..< 12_288 {
        #expect(classifier.consume(.pointerMotion(deltaX: micro, deltaY: 0, marker: 0)) == .jitter)
    }
    #expect(classifier.consume(.pointerMotion(deltaX: micro, deltaY: 0, marker: 0)) == .pause(.pointerMotion))
    #expect(classifier.bufferedMotionSampleCount <= classifier.motionSampleCapacity)

    let overflow = UserActivityClassifier(marker: 42, clock: { 0 })
    for _ in 0 ..< overflow.motionSampleCapacity {
        #expect(overflow.consume(.pointerMotion(deltaX: 0.0007, deltaY: 0, marker: 0)) == .jitter)
    }
    #expect(overflow.consume(.pointerMotion(deltaX: 0.0007, deltaY: 0, marker: 0)) == .pause(.pointerMotion))
    #expect(overflow.bufferedMotionSampleCount == overflow.motionSampleCapacity)
}

@Test func expiredMotionCleanupHasAFixedBudgetAndFailsClosedWhenExceeded() {
    let clock = UserActivityTestClock()
    let classifier = UserActivityClassifier(marker: 42, clock: { clock.now })
    for _ in 0 ... classifier.motionExpirationBudget {
        #expect(classifier.consume(.pointerMotion(deltaX: 0.0001, deltaY: 0, marker: 0)) == .jitter)
    }

    clock.now = 1
    #expect(classifier.consume(.pointerMotion(deltaX: 0.0001, deltaY: 0, marker: 0)) == .pause(.pointerMotion))
    #expect(classifier.lastMotionExpirationCount == classifier.motionExpirationBudget)
}

@Test func physicalClickScrollAndOrdinaryKeyPauseWhileModifiersDoNot() {
    for (event, reason) in [
        (UserActivityEvent.pointerButton(marker: 0), UserActivityReason.pointerButton),
        (.scroll(deltaX: 0, deltaY: 1, marker: 0), .scroll),
        (.keyDown(keyCode: 0, marker: 0), .keyboard),
    ] {
        let classifier = UserActivityClassifier(marker: 42, clock: { 0 })
        #expect(classifier.consume(event) == .pause(reason))
    }

    let classifier = UserActivityClassifier(marker: 42, clock: { 0 })
    #expect(classifier.consume(.modifierFlagsChanged(marker: 0)) == .ignoredModifier)
    #expect(classifier.consume(.scroll(deltaX: 0, deltaY: 0, marker: 0)) == .jitter)
}

@Test func markerFilteringAppliesToEveryImmediatePauseFamily() {
    let classifier = UserActivityClassifier(marker: 42, clock: { 0 })
    let marked: [UserActivityEvent] = [
        .pointerButton(marker: 42),
        .scroll(deltaX: 0, deltaY: 9, marker: 42),
        .keyDown(keyCode: 12, marker: 42),
        .modifierFlagsChanged(marker: 42),
    ]
    for event in marked {
        #expect(classifier.consume(event) == .ignoredSynthetic)
    }
}

@Test func pauseLatchTransitionsAtomicallyOnlyOnce() {
    let latch = UserActivityPauseLatch()

    #expect(latch.pause(reason: .keyboard))
    #expect(!latch.pause(reason: .scroll))
    #expect(latch.paused)
}

@Test func tapDisableFailsClosedWithoutRunningCleanupOrNotificationInline() throws {
    let runtime = ControlledUserActivityTapRuntime(autoPublish: true)
    let worker = ControlledUserActivityWorker()
    let registry = HeldInputRegistry()
    var releaseCount = 0
    let notification = UserActivityPauseSignal()
    let arbiter = UserActivityArbiter(
        heldInputs: registry,
        runtime: runtime,
        worker: worker,
        startupTimeout: 0.05
    )
    let lease = try arbiter.arm(marker: 42, notification: notification)
    try registry.begin(
        token: UUID(),
        scope: arbiter.heldInputScope(lease: lease),
        postDown: {},
        release: {},
        cleanupRelease: { releaseCount += 1 }
    )

    runtime.emit(.disabledByTimeout)

    #expect(arbiter.paused)
    #expect(throws: UserActivityMonitoringError.eventTapDisabled) { try arbiter.assertNotPaused(lease: lease) }
    #expect(releaseCount == 0)
    #expect(notification.poll() == nil)
    #expect(worker.pendingCount == 1)

    worker.runNext()
    #expect(releaseCount == 1)
    #expect(notification.poll() == .monitorDisabled)
}

@Test func terminalHeldInputCleanupRequiresTheExactLeaseAndRetriesOnlyItsScope() throws {
    let runtime = ControlledUserActivityTapRuntime(autoPublish: true)
    let registry = HeldInputRegistry()
    let arbiter = UserActivityArbiter(
        heldInputs: registry,
        runtime: runtime,
        startupTimeout: 0.05
    )
    let lease = try arbiter.arm(marker: 44, notification: UserActivityPauseSignal())
    let scope = try arbiter.heldInputScope(lease: lease)
    var scopedReleaseAttempts = 0
    try registry.begin(
        token: UUID(),
        scope: scope,
        postDown: {},
        release: {},
        cleanupRelease: {
            scopedReleaseAttempts += 1
            if scopedReleaseAttempts == 1 { throw SyntheticInputFailure(error: .helperFailed, inputStarted: true) }
        }
    )
    try registry.begin(
        token: UUID(),
        scope: .legacy,
        postDown: {},
        release: {},
        cleanupRelease: { Issue.record("terminal cleanup crossed held-input scope") }
    )

    #expect(throws: UserActivityMonitoringError.notArmed) {
        _ = try arbiter.cleanupHeldInputs(lease: UserActivitySessionLease(marker: lease.marker))
    }
    #expect(try arbiter.cleanupHeldInputs(lease: lease) == HeldInputCleanupResult(
        attempted: 1,
        released: 0,
        failed: 1
    ))
    #expect(registry.heldCount(scope: scope) == 1)
    #expect(try arbiter.cleanupHeldInputs(lease: lease) == HeldInputCleanupResult(
        attempted: 1,
        released: 1,
        failed: 0
    ))
    #expect(registry.heldCount(scope: scope) == 0)
    #expect(registry.heldCount(scope: .legacy) == 1)
    try arbiter.disarm(lease: lease)
}

@Test func userInputTapDisableAlsoFailsClosedAndDefersPauseWork() throws {
    let runtime = ControlledUserActivityTapRuntime(autoPublish: true)
    let worker = ControlledUserActivityWorker()
    let notification = UserActivityPauseSignal()
    let arbiter = UserActivityArbiter(runtime: runtime, worker: worker, startupTimeout: 0.05)
    let lease = try arbiter.arm(marker: 43, notification: notification)

    runtime.emit(.disabledByUserInput)

    #expect(throws: UserActivityMonitoringError.eventTapDisabled) { try arbiter.assertNotPaused(lease: lease) }
    #expect(notification.poll() == nil)
    #expect(worker.pendingCount == 1)
    worker.runNext()
    #expect(notification.poll() == .monitorDisabled)
}

@Test func startupTimeoutCompletesTeardownAndRejectsLatePublish() throws {
    let runtime = ControlledUserActivityTapRuntime(autoPublish: false)
    let arbiter = UserActivityArbiter(runtime: runtime, worker: ControlledUserActivityWorker(), startupTimeout: 0.001)

    #expect(throws: UserActivityMonitoringError.startupTimedOut) {
        try arbiter.arm(marker: 41, notification: UserActivityPauseSignal())
    }
    #expect(!runtime.publish(enabled: true))

    runtime.autoPublish = true
    let lease = try arbiter.arm(marker: 42, notification: UserActivityPauseSignal())
    try arbiter.assertNotPaused(lease: lease)
}

@Test func physicalPauseBeforeArmHandoffStillReturnsTeardownAuthority() throws {
    let runtime = ControlledUserActivityTapRuntime(autoPublish: true)
    let worker = ControlledUserActivityWorker()
    let arbiter = UserActivityArbiter(runtime: runtime, worker: worker, startupTimeout: 0.05)
    runtime.afterAutoPublish = { runtime.emit(.event(.pointerButton(marker: 0))) }

    let lease = try arbiter.arm(marker: 501, notification: UserActivityPauseSignal())

    #expect(arbiter.paused)
    try arbiter.disarm(lease: lease)
    worker.runNext()
    runtime.afterAutoPublish = nil
    let nextLease = try arbiter.arm(marker: 502, notification: UserActivityPauseSignal())
    try arbiter.assertNotPaused(lease: nextLease)
}

@Test func disarmReturnsPhysicalPauseForTheExactGeneration() throws {
    let runtime = ControlledUserActivityTapRuntime(autoPublish: true)
    let arbiter = UserActivityArbiter(runtime: runtime, worker: ControlledUserActivityWorker(), startupTimeout: 0.05)
    let lease = try arbiter.arm(marker: 503, notification: UserActivityPauseSignal())

    runtime.emit(.event(.pointerButton(marker: 0)))

    #expect(try arbiter.disarm(lease: lease))
}

@Test func disarmReturnsMonitorFailureForTheExactGeneration() throws {
    let runtime = ControlledUserActivityTapRuntime(autoPublish: true)
    let arbiter = UserActivityArbiter(runtime: runtime, worker: ControlledUserActivityWorker(), startupTimeout: 0.05)
    let lease = try arbiter.arm(marker: 504, notification: UserActivityPauseSignal())

    runtime.emit(.disabledByTimeout)

    #expect(try arbiter.disarm(lease: lease))
}

@Test func disarmReturnsUninterruptedForTheExactGeneration() throws {
    let runtime = ControlledUserActivityTapRuntime(autoPublish: true)
    let arbiter = UserActivityArbiter(runtime: runtime, worker: ControlledUserActivityWorker(), startupTimeout: 0.05)
    let lease = try arbiter.arm(marker: 505, notification: UserActivityPauseSignal())

    #expect(try !arbiter.disarm(lease: lease))
}

// With pre-existing Input Monitoring access, exercise the OS lifecycle without
// subscribing to any physical input. No permission prompt or desktop input is needed.
@Test(.enabled(if: CGPreflightListenEventAccess()))
func quietSystemTapDisarmsAndRearmsWithoutReportingInterference() throws {
    let runtime = SystemUserActivityTapRuntime(eventsOfInterest: 1 << CGEventType.null.rawValue)
    let arbiter = UserActivityArbiter(runtime: runtime)
    for marker: UInt64 in 1...3 {
        let lease = try arbiter.arm(marker: marker, notification: UserActivityPauseSignal())
        #expect(try !arbiter.disarm(lease: lease))
    }
}

@Test(.enabled(if: CGPreflightListenEventAccess()))
func systemTapCanStopTwiceBeforeEnteringTheRunLoop() {
    let runtime = SystemUserActivityTapRuntime(eventsOfInterest: 1 << CGEventType.null.rawValue)
    let generation = UUID()
    let finished = DispatchSemaphore(value: 0)
    runtime.start(generation: generation, signal: { _ in
        Issue.record("A quiet tap reported interference")
    }, publish: { enabled in
        #expect(enabled)
        runtime.stop(generation: generation)
        runtime.stop(generation: generation)
        return enabled
    }, exited: { finished.signal() })
    #expect(finished.wait(timeout: .now() + 2) == .success)
}

@Test func pendingTapDrainRequiresAnIdleRunLoopWithinItsBudget() {
    var results: [CFRunLoopRunResult] = [.handledSource, .handledSource, .timedOut]
    #expect(SystemUserActivityTapRuntime.drainPendingEvents(next: { results.removeFirst() }, now: { 0 }))
    #expect(results.isEmpty)
    for unexpected in [CFRunLoopRunResult.stopped, .finished] {
        #expect(!SystemUserActivityTapRuntime.drainPendingEvents(next: { unexpected }, now: { 0 }))
    }
    var time: TimeInterval = 0
    #expect(!SystemUserActivityTapRuntime.drainPendingEvents(next: {
        time += 0.1
        return .handledSource
    }, now: { time }))
    time = 0
    #expect(!SystemUserActivityTapRuntime.drainPendingEvents(next: {
        time += 0.1
        return .timedOut
    }, now: { time }))
}

@Test(arguments: [
    UserActivityTapSignal.event(.pointerButton(marker: 0)), .disabledByTimeout,
    .disabledByUserInput, .monitoringIncomplete,
])
func lateInputOrMonitoringFailureDuringStopStillPreventsRestoration(_ signal: UserActivityTapSignal) throws {
    let runtime = ControlledUserActivityTapRuntime(autoPublish: true)
    let arbiter = UserActivityArbiter(runtime: runtime, worker: ControlledUserActivityWorker())
    let lease = try arbiter.arm(marker: 508, notification: UserActivityPauseSignal())
    runtime.onStop = { runtime.emit(signal) }
    #expect(try arbiter.disarm(lease: lease))
}

@Test func disarmDrainsSameGenerationCallbackThatEnteredBeforeStopping() throws {
    let runtime = ControlledUserActivityTapRuntime(autoPublish: true)
    let callbackRegistered = DispatchSemaphore(value: 0)
    let allowCallbackCommit = DispatchSemaphore(value: 0)
    let stopObserved = DispatchSemaphore(value: 0)
    let disarmFinished = DispatchSemaphore(value: 0)
    runtime.onStop = { stopObserved.signal() }
    let arbiter = UserActivityArbiter(
        runtime: runtime,
        worker: ControlledUserActivityWorker(),
        startupTimeout: 0.2,
        afterSignalRegistration: {
            callbackRegistered.signal()
            _ = allowCallbackCommit.wait(timeout: .now() + 1)
        }
    )
    let lease = try arbiter.arm(marker: 506, notification: UserActivityPauseSignal())
    DispatchQueue.global().async {
        runtime.emit(.event(.pointerButton(marker: 0)))
    }
    #expect(callbackRegistered.wait(timeout: .now() + 1) == .success)
    var interrupted = false
    DispatchQueue.global().async {
        interrupted = (try? arbiter.disarm(lease: lease)) == true
        disarmFinished.signal()
    }

    #expect(stopObserved.wait(timeout: .now() + 1) == .success)
    #expect(disarmFinished.wait(timeout: .now() + 0.02) == .timedOut)
    allowCallbackCommit.signal()
    #expect(disarmFinished.wait(timeout: .now() + 1) == .success)
    #expect(interrupted)
}

@Test func tapDisableBeforeArmHandoffStillReturnsTeardownAuthority() throws {
    let runtime = ControlledUserActivityTapRuntime(autoPublish: true)
    let worker = ControlledUserActivityWorker()
    let arbiter = UserActivityArbiter(runtime: runtime, worker: worker, startupTimeout: 0.05)
    runtime.afterAutoPublish = { runtime.emit(.disabledByTimeout) }

    let lease = try arbiter.arm(marker: 511, notification: UserActivityPauseSignal())

    #expect(arbiter.paused)
    try arbiter.disarm(lease: lease)
    worker.runNext()
    runtime.afterAutoPublish = nil
    let nextLease = try arbiter.arm(marker: 512, notification: UserActivityPauseSignal())
    try arbiter.assertNotPaused(lease: nextLease)
}

@Test func unexpectedTapExitBeforeArmHandoffStillReturnsTeardownAuthority() throws {
    let runtime = ControlledUserActivityTapRuntime(autoPublish: true)
    let worker = ControlledUserActivityWorker()
    let arbiter = UserActivityArbiter(runtime: runtime, worker: worker, startupTimeout: 0.05)
    runtime.afterAutoPublish = { runtime.exit() }

    let lease = try arbiter.arm(marker: 521, notification: UserActivityPauseSignal())

    #expect(arbiter.paused)
    try arbiter.disarm(lease: lease)
    worker.runNext()
    runtime.afterAutoPublish = nil
    let nextLease = try arbiter.arm(marker: 522, notification: UserActivityPauseSignal())
    try arbiter.assertNotPaused(lease: nextLease)
}

@Test func startupTimeoutWaitsForTapExitBeforeThrowingAndAllowsImmediateRearm() throws {
    let runtime = ControlledUserActivityTapRuntime(autoPublish: false, autoExitOnStop: false)
    let stopObserved = DispatchSemaphore(value: 0)
    let armFinished = DispatchSemaphore(value: 0)
    var timedOut = false
    runtime.onStop = { stopObserved.signal() }
    let arbiter = UserActivityArbiter(runtime: runtime, worker: ControlledUserActivityWorker(), startupTimeout: 0.001)

    DispatchQueue.global().async {
        do {
            _ = try arbiter.arm(marker: 531, notification: UserActivityPauseSignal())
        } catch UserActivityMonitoringError.startupTimedOut {
            timedOut = true
        } catch {}
        armFinished.signal()
    }
    #expect(stopObserved.wait(timeout: .now() + 1) == .success)
    #expect(armFinished.wait(timeout: .now() + 0.02) == .timedOut)
    runtime.exit()
    #expect(armFinished.wait(timeout: .now() + 1) == .success)
    #expect(timedOut)

    runtime.autoPublish = true
    runtime.onStop = nil
    let lease = try arbiter.arm(marker: 532, notification: UserActivityPauseSignal())
    try arbiter.assertNotPaused(lease: lease)
}

@Test(arguments: [UInt64(41), UInt64(42)])
func oldLeaseIsRejectedAfterRearmWithSameOrDifferentMarker(nextMarker: UInt64) throws {
    let runtime = ControlledUserActivityTapRuntime(autoPublish: true)
    let arbiter = UserActivityArbiter(runtime: runtime, worker: ControlledUserActivityWorker(), startupTimeout: 0.05)
    let oldLease = try arbiter.arm(marker: 41, notification: UserActivityPauseSignal())
    try arbiter.disarm(lease: oldLease)
    _ = try arbiter.arm(marker: nextMarker, notification: UserActivityPauseSignal())

    #expect(throws: UserActivityMonitoringError.notArmed) {
        try arbiter.assertNotPaused(lease: oldLease)
    }
    #expect(throws: UserActivityMonitoringError.notArmed) {
        try arbiter.heldInputScope(lease: oldLease)
    }
    var posted = false
    #expect(throws: UserActivityMonitoringError.notArmed) {
        try arbiter.performPIDEvent(lease: oldLease, validate: {}, mutation: { posted = true })
    }
    #expect(!posted)
}

@Test func disarmWaitsForExitBeforeRearmAndOldWorkerIsGenerationScoped() throws {
    let runtime = ControlledUserActivityTapRuntime(autoPublish: true, autoExitOnStop: false)
    let worker = ControlledUserActivityWorker()
    let registry = HeldInputRegistry()
    var oldReleases = 0
    var newReleases = 0
    let oldNotification = UserActivityPauseSignal()
    let arbiter = UserActivityArbiter(
        heldInputs: registry,
        runtime: runtime,
        worker: worker,
        startupTimeout: 0.05
    )
    let oldLease = try arbiter.arm(marker: 1, notification: oldNotification)
    let oldScope = try arbiter.heldInputScope(lease: oldLease)
    try registry.begin(token: UUID(), scope: oldScope, postDown: {}, release: {}, cleanupRelease: { oldReleases += 1 })
    runtime.emit(.event(.pointerButton(marker: 0)))
    try arbiter.disarm(lease: oldLease)

    #expect(throws: UserActivityMonitoringError.alreadyArmed) {
        try arbiter.arm(marker: 2, notification: UserActivityPauseSignal())
    }
    runtime.exit()
    #expect(throws: UserActivityMonitoringError.alreadyArmed) {
        try arbiter.arm(marker: 1, notification: UserActivityPauseSignal())
    }

    worker.runNext()
    #expect(oldReleases == 1)
    #expect(oldNotification.poll() == .pointerButton)

    let newLease = try arbiter.arm(marker: 1, notification: UserActivityPauseSignal())
    let newScope = try arbiter.heldInputScope(lease: newLease)
    try registry.begin(token: UUID(), scope: newScope, postDown: {}, release: {}, cleanupRelease: { newReleases += 1 })
    #expect(newReleases == 0)
    #expect(oldScope != newScope)
    #expect(registry.heldCount(scope: newScope) == 1)
}

@Test func differentMarkerRearmWaitsForOldCleanupAndIsolatesOldNotification() throws {
    let runtime = ControlledUserActivityTapRuntime(autoPublish: true, autoExitOnStop: false)
    let worker = ControlledUserActivityWorker()
    let registry = HeldInputRegistry()
    var cleanupMarkers: [UInt64] = []
    let oldNotification = UserActivityPauseSignal()
    let arbiter = UserActivityArbiter(
        heldInputs: registry,
        runtime: runtime,
        worker: worker,
        startupTimeout: 0.05
    )
    let oldLease = try arbiter.arm(marker: 11, notification: oldNotification)
    let oldScope = try arbiter.heldInputScope(lease: oldLease)
    try registry.begin(
        token: UUID(),
        scope: oldScope,
        postDown: {},
        release: {},
        cleanupRelease: {
            cleanupMarkers.append(11)
            runtime.emit(.event(.pointerButton(marker: 11)))
        }
    )
    runtime.emit(.event(.pointerButton(marker: 0)))
    try arbiter.disarm(lease: oldLease)
    runtime.exit()

    #expect(throws: UserActivityMonitoringError.alreadyArmed) {
        try arbiter.arm(marker: 22, notification: UserActivityPauseSignal())
    }
    #expect(cleanupMarkers.isEmpty)
    worker.runNext()
    #expect(cleanupMarkers == [11])
    #expect(oldNotification.poll() == .pointerButton)

    _ = try arbiter.arm(marker: 22, notification: UserActivityPauseSignal())
    #expect(!arbiter.paused)
}

@Test func notificationConsumerCanDisarmAfterBoundedHandoff() throws {
    let runtime = ControlledUserActivityTapRuntime(autoPublish: true)
    let worker = ControlledUserActivityWorker()
    let notification = UserActivityPauseSignal()
    let arbiter = UserActivityArbiter(runtime: runtime, worker: worker, startupTimeout: 0.05)
    let lease = try arbiter.arm(marker: 51, notification: notification)
    runtime.emit(.event(.pointerButton(marker: 0)))

    worker.runNext()

    #expect(notification.poll() == .pointerButton)
    try arbiter.disarm(lease: lease)
    _ = try arbiter.arm(marker: 52, notification: UserActivityPauseSignal())
}

@Test func rearmWaitsWhileOldCleanupIsActivelyRunning() throws {
    let runtime = ControlledUserActivityTapRuntime(autoPublish: true, autoExitOnStop: false)
    let worker = ControlledUserActivityWorker()
    let registry = HeldInputRegistry()
    let cleanupStarted = DispatchSemaphore(value: 0)
    let allowCleanup = DispatchSemaphore(value: 0)
    let cleanupFinished = DispatchSemaphore(value: 0)
    let arbiter = UserActivityArbiter(
        heldInputs: registry,
        runtime: runtime,
        worker: worker,
        startupTimeout: 0.05
    )
    let lease = try arbiter.arm(marker: 61, notification: UserActivityPauseSignal())
    try registry.begin(
        token: UUID(),
        scope: arbiter.heldInputScope(lease: lease),
        postDown: {},
        release: {},
        cleanupRelease: {
            cleanupStarted.signal()
            _ = allowCleanup.wait(timeout: .now() + 1)
        }
    )
    runtime.emit(.event(.pointerButton(marker: 0)))
    try arbiter.disarm(lease: lease)
    runtime.exit()
    DispatchQueue.global().async {
        worker.runNext()
        cleanupFinished.signal()
    }
    #expect(cleanupStarted.wait(timeout: .now() + 1) == .success)

    #expect(throws: UserActivityMonitoringError.alreadyArmed) {
        try arbiter.arm(marker: 62, notification: UserActivityPauseSignal())
    }
    allowCleanup.signal()
    #expect(cleanupFinished.wait(timeout: .now() + 1) == .success)
    _ = try arbiter.arm(marker: 62, notification: UserActivityPauseSignal())
}

@Test func disarmWaitsForInFlightFragmentToFinishBeforeRearm() throws {
    let runtime = ControlledUserActivityTapRuntime(autoPublish: true)
    let arbiter = UserActivityArbiter(runtime: runtime, worker: ControlledUserActivityWorker(), startupTimeout: 0.05)
    let lease = try arbiter.arm(marker: 71, notification: UserActivityPauseSignal())
    try arbiter.beginFragment(lease: lease)

    try arbiter.disarm(lease: lease)

    #expect(throws: UserActivityMonitoringError.alreadyArmed) {
        try arbiter.arm(marker: 72, notification: UserActivityPauseSignal())
    }
    arbiter.endFragment(lease: lease)
    _ = try arbiter.arm(marker: 72, notification: UserActivityPauseSignal())
}

@Test func fragmentLeaseIsSingleUseWithinAnArmedSession() throws {
    let runtime = ControlledUserActivityTapRuntime(autoPublish: true)
    let arbiter = UserActivityArbiter(runtime: runtime, worker: ControlledUserActivityWorker(), startupTimeout: 0.05)
    let lease = try arbiter.arm(marker: 81, notification: UserActivityPauseSignal())
    try arbiter.beginFragment(lease: lease)
    arbiter.endFragment(lease: lease)

    #expect(throws: UserActivityMonitoringError.notArmed) {
        try arbiter.beginFragment(lease: lease)
    }
}

@Test func pauseCleanupWaitsForSuccessfulEventCommitAndUsesCommittedPoint() throws {
    let runtime = ControlledUserActivityTapRuntime(autoPublish: true)
    let worker = ControlledUserActivityWorker()
    let registry = HeldInputRegistry()
    let eventPosted = DispatchSemaphore(value: 0)
    let allowCommit = DispatchSemaphore(value: 0)
    let eventFinished = DispatchSemaphore(value: 0)
    let cleanupFinished = DispatchSemaphore(value: 0)
    var lastPoint = CGPoint(x: 10, y: 10)
    var cleanupPoint: CGPoint?
    var postedPoints: [CGPoint] = []
    let arbiter = UserActivityArbiter(
        heldInputs: registry,
        runtime: runtime,
        worker: worker,
        startupTimeout: 0.05
    )
    let lease = try arbiter.arm(marker: 91, notification: UserActivityPauseSignal())
    try arbiter.beginFragment(lease: lease)
    try registry.begin(
        token: UUID(),
        scope: arbiter.heldInputScope(lease: lease),
        postDown: {},
        release: {},
        cleanupRelease: { cleanupPoint = lastPoint }
    )
    DispatchQueue.global().async {
        try? arbiter.performPIDEvent(
            lease: lease,
            validate: {},
            mutation: {
                postedPoints.append(CGPoint(x: 20, y: 20))
                eventPosted.signal()
                _ = allowCommit.wait(timeout: .now() + 1)
                lastPoint = CGPoint(x: 20, y: 20)
            }
        )
        eventFinished.signal()
    }
    #expect(eventPosted.wait(timeout: .now() + 1) == .success)
    runtime.emit(.event(.pointerButton(marker: 0)))
    DispatchQueue.global().async {
        worker.runNext()
        cleanupFinished.signal()
    }
    #expect(cleanupFinished.wait(timeout: .now() + 0.02) == .timedOut)

    allowCommit.signal()
    #expect(eventFinished.wait(timeout: .now() + 1) == .success)
    #expect(cleanupFinished.wait(timeout: .now() + 1) == .success)
    #expect(cleanupPoint == CGPoint(x: 20, y: 20))
    #expect(throws: UserActivityMonitoringError.paused) {
        try arbiter.performPIDEvent(
            lease: lease,
            validate: {},
            mutation: { postedPoints.append(CGPoint(x: 30, y: 30)) }
        )
    }
    #expect(postedPoints == [CGPoint(x: 20, y: 20)])
}

@Test func pauseCleanupWinningEventGatePreventsOldPost() throws {
    let runtime = ControlledUserActivityTapRuntime(autoPublish: true)
    let worker = ControlledUserActivityWorker()
    let registry = HeldInputRegistry()
    let cleanupStarted = DispatchSemaphore(value: 0)
    let allowCleanup = DispatchSemaphore(value: 0)
    let eventFinished = DispatchSemaphore(value: 0)
    var posted = 0
    let arbiter = UserActivityArbiter(
        heldInputs: registry,
        runtime: runtime,
        worker: worker,
        startupTimeout: 0.05
    )
    let lease = try arbiter.arm(marker: 92, notification: UserActivityPauseSignal())
    try arbiter.beginFragment(lease: lease)
    try registry.begin(
        token: UUID(),
        scope: arbiter.heldInputScope(lease: lease),
        postDown: {},
        release: {},
        cleanupRelease: {
            cleanupStarted.signal()
            _ = allowCleanup.wait(timeout: .now() + 1)
        }
    )
    runtime.emit(.event(.pointerButton(marker: 0)))
    DispatchQueue.global().async { worker.runNext() }
    #expect(cleanupStarted.wait(timeout: .now() + 1) == .success)
    DispatchQueue.global().async {
        try? arbiter.performPIDEvent(lease: lease, validate: {}, mutation: { posted += 1 })
        eventFinished.signal()
    }
    #expect(eventFinished.wait(timeout: .now() + 0.02) == .timedOut)

    allowCleanup.signal()
    #expect(eventFinished.wait(timeout: .now() + 1) == .success)
    #expect(posted == 0)
}

@Test func eventGateUnlocksAfterEventAndCleanupFailures() throws {
    enum GateFailure: Error { case expected }
    let runtime = ControlledUserActivityTapRuntime(autoPublish: true)
    let arbiter = UserActivityArbiter(runtime: runtime, worker: ControlledUserActivityWorker(), startupTimeout: 0.05)
    let lease = try arbiter.arm(marker: 93, notification: UserActivityPauseSignal())
    try arbiter.beginFragment(lease: lease)

    #expect(throws: GateFailure.expected) {
        try arbiter.performPIDEvent(lease: lease, validate: {}, mutation: { throw GateFailure.expected })
    }
    var eventRan = false
    try arbiter.performPIDEvent(lease: lease, validate: {}, mutation: { eventRan = true })
    #expect(eventRan)
    #expect(throws: GateFailure.expected) {
        try arbiter.performPIDCleanup(lease: lease) { throw GateFailure.expected }
    }
    var cleanupRan = false
    try arbiter.performPIDCleanup(lease: lease) { cleanupRan = true }
    #expect(cleanupRan)
}

@Test func targetValidationWaitsForTheEventGateAndStaleTargetPreventsPosting() throws {
    enum TargetFailure: Error { case stale }
    let runtime = ControlledUserActivityTapRuntime(autoPublish: true)
    let arbiter = UserActivityArbiter(runtime: runtime, worker: ControlledUserActivityWorker(), startupTimeout: 0.05)
    let lease = try arbiter.arm(marker: 94, notification: UserActivityPauseSignal())
    try arbiter.beginFragment(lease: lease)
    let firstMutationStarted = DispatchSemaphore(value: 0)
    let allowFirstMutation = DispatchSemaphore(value: 0)
    let firstFinished = DispatchSemaphore(value: 0)
    let validatorStarted = DispatchSemaphore(value: 0)
    let secondFinished = DispatchSemaphore(value: 0)
    var targetIsStale = false
    var posted = false

    DispatchQueue.global().async {
        try? arbiter.performPIDEvent(
            lease: lease,
            validate: {},
            mutation: {
                firstMutationStarted.signal()
                _ = allowFirstMutation.wait(timeout: .now() + 1)
            }
        )
        firstFinished.signal()
    }
    #expect(firstMutationStarted.wait(timeout: .now() + 1) == .success)
    DispatchQueue.global().async {
        try? arbiter.performPIDEvent(
            lease: lease,
            validate: {
                validatorStarted.signal()
                if targetIsStale { throw TargetFailure.stale }
            },
            mutation: { posted = true }
        )
        secondFinished.signal()
    }
    #expect(validatorStarted.wait(timeout: .now() + 0.02) == .timedOut)

    targetIsStale = true
    allowFirstMutation.signal()
    #expect(firstFinished.wait(timeout: .now() + 1) == .success)
    #expect(secondFinished.wait(timeout: .now() + 1) == .success)
    #expect(!posted)
}

@Test func pauseDuringTargetValidationIsRecheckedBeforePosting() throws {
    let runtime = ControlledUserActivityTapRuntime(autoPublish: true)
    let arbiter = UserActivityArbiter(runtime: runtime, worker: ControlledUserActivityWorker(), startupTimeout: 0.05)
    let lease = try arbiter.arm(marker: 95, notification: UserActivityPauseSignal())
    try arbiter.beginFragment(lease: lease)
    var posted = false

    #expect(throws: UserActivityMonitoringError.paused) {
        try arbiter.performPIDEvent(
            lease: lease,
            validate: { runtime.emit(.event(.pointerButton(marker: 0))) },
            mutation: { posted = true }
        )
    }
    #expect(!posted)
}

@Test func blockingNotificationConsumerCannotDelayRearmOrCarryG1AuthorityIntoG2() throws {
    let runtime = ControlledUserActivityTapRuntime(autoPublish: true, autoExitOnStop: false)
    let worker = ControlledUserActivityWorker()
    let oldSignal = UserActivityPauseSignal()
    let newSignal = UserActivityPauseSignal()
    let consumerReceived = DispatchSemaphore(value: 0)
    let allowConsumer = DispatchSemaphore(value: 0)
    let arbiter = UserActivityArbiter(runtime: runtime, worker: worker, startupTimeout: 0.05)
    let oldLease = try arbiter.arm(marker: 101, notification: oldSignal)
    runtime.emit(.event(.pointerButton(marker: 0)))
    try arbiter.disarm(lease: oldLease)
    runtime.exit()
    worker.runNext()
    DispatchQueue.global().async {
        if oldSignal.wait(timeout: 1) == .pointerButton {
            consumerReceived.signal()
            _ = allowConsumer.wait(timeout: .now() + 1)
        }
    }
    #expect(consumerReceived.wait(timeout: .now() + 1) == .success)

    _ = try arbiter.arm(marker: 202, notification: newSignal)
    #expect(!arbiter.paused)
    #expect(newSignal.poll() == nil)
    allowConsumer.signal()
}

@Test func oldNotificationConsumerCannotDisarmTheRearmedSession() throws {
    let runtime = ControlledUserActivityTapRuntime(autoPublish: true)
    let worker = ControlledUserActivityWorker()
    let oldSignal = UserActivityPauseSignal()
    let arbiter = UserActivityArbiter(runtime: runtime, worker: worker, startupTimeout: 0.05)
    let oldLease = try arbiter.arm(marker: 301, notification: oldSignal)
    runtime.emit(.event(.pointerButton(marker: 0)))
    worker.runNext()
    #expect(oldSignal.poll() == .pointerButton)
    try arbiter.disarm(lease: oldLease)

    let newLease = try arbiter.arm(marker: 302, notification: UserActivityPauseSignal())
    #expect(throws: UserActivityMonitoringError.notArmed) {
        try arbiter.disarm(lease: oldLease)
    }
    try arbiter.assertNotPaused(lease: newLease)
}

private final class ControlledUserActivityWorker: UserActivityWorkSubmitting {
    private var pending: [() -> Void] = []
    var pendingCount: Int { pending.count }
    func submit(_ work: @escaping () -> Void) { pending.append(work) }
    func runNext() { pending.removeFirst()() }
}

private final class ControlledUserActivityTapRuntime: UserActivityTapRunning {
    var autoPublish: Bool
    let autoExitOnStop: Bool
    var afterAutoPublish: (() -> Void)?
    var onStop: (() -> Void)?
    private var generation: UUID?
    private var signal: ((UserActivityTapSignal) -> Void)?
    private var publishCallback: ((Bool) -> Bool)?
    private var exitCallback: (() -> Void)?

    init(autoPublish: Bool, autoExitOnStop: Bool = true) {
        self.autoPublish = autoPublish
        self.autoExitOnStop = autoExitOnStop
    }

    func start(
        generation: UUID,
        signal: @escaping (UserActivityTapSignal) -> Void,
        publish: @escaping (Bool) -> Bool,
        exited: @escaping () -> Void
    ) {
        self.generation = generation
        self.signal = signal
        publishCallback = publish
        exitCallback = exited
        if autoPublish {
            _ = publish(true)
            afterAutoPublish?()
        }
    }

    func stop(generation: UUID) {
        guard generation == self.generation else { return }
        onStop?()
        if autoExitOnStop { exit() }
    }

    func emit(_ value: UserActivityTapSignal) { signal?(value) }
    func publish(enabled: Bool) -> Bool { publishCallback?(enabled) ?? false }
    func exit() {
        let callback = exitCallback
        generation = nil
        signal = nil
        publishCallback = nil
        exitCallback = nil
        callback?()
    }
}

private final class UserActivityTestClock {
    var now: TimeInterval = 0
}
