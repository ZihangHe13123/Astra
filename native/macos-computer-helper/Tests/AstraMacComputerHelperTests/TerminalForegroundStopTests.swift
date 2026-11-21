@testable import AstraMacComputerHelperCore
import Foundation
import Testing

private final class TerminalTestTap: UserActivityTapRunning {
    var onSignal: ((UserActivityTapSignal) -> Void)?
    private var onExit: (() -> Void)?

    func start(generation: UUID, signal: @escaping (UserActivityTapSignal) -> Void,
               publish: @escaping (Bool) -> Bool, exited: @escaping () -> Void) {
        onSignal = signal
        onExit = exited
        _ = publish(true)
    }

    func stop(generation: UUID) { onExit?() }
}

private final class TerminalTestDesktop: ActionProviding, ApplicationActivationControlling, PIDTargetedInputPosting {
    let target = WindowTarget(appRef: "app", windowRef: "window", pid: 11, windowID: 22,
        bounds: CGRect(x: 100, y: 200, width: 300, height: 200), title: "Editor", axIdentity: 33)
    var frontmost: pid_t = 77
    var events: [String] = []

    func activate(_ target: WindowTarget) throws { frontmost = target.pid }
    func restore(pid: pid_t) throws { frontmost = pid; events.append("restore") }
    func currentTargetState() -> ActionTargetState {
        ActionTargetState(pid: target.pid, windowID: target.windowID, bounds: target.bounds, axIdentity: 33)
    }
    func element(reference: String, snapshotID: String) -> ActionElement? { nil }
    func perform(_ action: ResolvedAction) -> ActionPerformance {
        events.append("completed wait")
        return .init(inputStarted: false)
    }
    func preflight(targetPID: pid_t, marker: UInt64) -> Bool { true }
    func post(_ event: SyntheticInputEvent, to targetPID: pid_t, marker: UInt64) throws { events.append("input") }
}

@Test func terminalForegroundStopCutsLongWaitBeforeKillAndRestoresWithoutSuffix() throws {
    let desktop = TerminalTestDesktop(), target = desktop.target
    let held = HeldInputRegistry(), activity = UserActivityArbiter(heldInputs: held, runtime: TerminalTestTap())
    let cursor = InMemoryVirtualCursorPresenter()
    let expected = ActionGuard(pid: target.pid, windowID: target.windowID, bounds: target.bounds,
        axIdentity: 33, snapshotID: "snapshot", interactionMode: .foregroundTakeover)
    let application = PIDTargetApplication(bundleIdentifier: "test.editor", version: "1")
    let actions: [NativeAction] = [.wait(durationMS: 3_000), .click(x: 10, y: 10)]
    let planner = InputDispatcher(performer: desktop, application: application, syntheticPolicy: .init(
        pointerCapability: .available, keyboardCapability: .unavailable, registry: .init(), genericForegroundEnabled: true))
    let plan = try planner.plan(actions: actions, context: .init(guardValue: expected))
    let loan = ForegroundTakeoverCoordinator(activity: activity, activation: desktop, cursor: cursor,
        frontmostPID: { desktop.frontmost }, applicationExists: { $0 == 77 }, inputCounts: { [0] }, physicalIdleSeconds: { 20 }, revalidate: { $0 },
        takePlan: { _ in .init(target: target, guardValue: expected, plan: plan, actions: actions, application: application) },
        classifyPopupPointer: { _ in nil })
    let token = try loan.begin(target: target, snapshotID: expected.snapshotID, planRef: plan.planRef, stageDigest: plan.stageDigest)
    _ = try loan.consume(token.ref, actions: actions)
    let lease = try loan.executionAuthority(for: token.ref, snapshotID: expected.snapshotID,
        planRef: plan.planRef, consumedGuard: expected).lease
    let entries = try planner.consumeForegroundPlan(plan, authority: .init(planRef: plan.planRef,
        snapshotID: expected.snapshotID, interactionMode: .foregroundTakeover, actions: actions, backends: plan.backends, guardValue: expected),
        validateFocusMutation: { try activity.assertNotPaused(lease: lease) })
    try held.begin(token: UUID(), scope: activity.heldInputScope(lease: lease), postDown: {}, release: {},
                   cleanupRelease: { desktop.events.append("release") })
    let pointer = PIDTargetedActionExecutor(poster: desktop, genericForegroundEnabled: true, activity: activity,
        validator: ExactPIDActionGuardValidator(state: {
            .init(target: desktop.currentTargetState(), snapshotID: expected.snapshotID, isFrontmost: true, isKeyWindow: true)
        }), heldInputs: held, element: { _, _ in nil })
    let waiting = DispatchSemaphore(value: 0)
    let executor = ForegroundPlanExecutor(activity: activity, performer: desktop, pidExecutor: pointer,
        waitSleeper: { interval in waiting.signal(); Thread.sleep(forTimeInterval: interval) })

    // Run the sealed mock-OS batch at cooperativeAct's first in-lock callback:
    // both act and close use the production actionLock, with no live window lookup.
    var receipt: PIDTargetedActionResult?
    let windows = SystemWindowObserver(userActivity: activity, takeoverCoordinator: loan, cooperativePlanClock: {
        receipt = executor.run(expected: expected, application: application, lease: lease, entries: entries)
        return 0
    })
    let dispatcher = Dispatcher(windows: windows)
    let cleanup = HelperTerminalCleanupCoordinator(steps: [.init(name: "dispatcher", action: dispatcher.close)])
    DispatchQueue.global().async {
        _ = windows.cooperativeAct(snapshotID: "", interactionMode: .foregroundTakeover,
                                   planRef: "", takeoverRef: nil, actions: [])
    }
    try #require(waiting.wait(timeout: .now() + 2) == .success)
    let done = DispatchGroup()
    DispatchQueue.global().async(group: done) { _ = runHelperSIGTERMCleanup(cleanup) }
    #expect(done.wait(timeout: .now() + 1) == .success, "close is still blocked behind the 3 s wait")
    // Join even on RED so the test can report the baseline's actual suffix and restoration.
    try #require(done.wait(timeout: .now() + 4) == .success)
    let result = try #require(receipt)
    #expect(result.lastAcknowledgedAction == -1)
    #expect(result.error == .helperFailed)
    #expect(result.cooperativeError == nil)
    #expect(desktop.events == ["release", "restore"])
    #expect(desktop.frontmost == 77)
    #expect(held.heldCount == 0)
    #expect(throws: TakeoverError.alreadyConsumed) { _ = try loan.consume(token.ref, actions: actions) }
}

@Test(arguments: [false, true])
func terminalForegroundStopRevokesNewInputButNotCleanupOrHumanInterruption(humanInput: Bool) throws {
    let tap = TerminalTestTap(), signal = UserActivityPauseSignal()
    let activity = UserActivityArbiter(runtime: tap)
    let lease = try activity.arm(marker: 7, notification: signal)
    let dispatcher = Dispatcher(windows: SystemWindowObserver(userActivity: activity))
    var posted = false, released = false
    #expect(throws: ActionExecutionError.helperFailed) {
        try activity.performPIDEvent(lease: lease, validate: { dispatcher.close() }, mutation: { posted = true })
    }
    #expect(!posted)
    #expect(!activity.paused)
    #expect(signal.poll() == nil)
    #expect(throws: ActionExecutionError.helperFailed) { try activity.beginFragment(lease: lease) }
    if humanInput { tap.onSignal?(.event(.keyDown(keyCode: 0, marker: 0))) }
    try activity.performPIDCleanup(lease: lease) { released = true }
    #expect(released)
    #expect(try activity.disarm(lease: lease) == humanInput)
    #expect(throws: ActionExecutionError.helperFailed) {
        _ = try activity.arm(marker: 8, notification: UserActivityPauseSignal())
    }
}

@Test func terminalForegroundStopDoesNotDispatchABufferedRequestAfterCancelledAct() {
    let cleanup = HelperTerminalCleanupCoordinator(steps: [])
    var requests = ["act", "queued input"], handled: [String] = []
    let status = runHelperLoop(next: {
        requests.isEmpty ? nil : .line(Data(requests.removeFirst().utf8))
    }, handle: { request in
        handled.append(request)
        if request == "act" { _ = runHelperSIGTERMCleanup(cleanup) }
        return Dispatcher.invalidRequest(message: "mock response")
    }, write: { _ in }, cleanup: { _ = cleanup.run() }, terminalCleanupStarted: { cleanup.hasStarted }, report: { _ in })
    #expect(handled == ["act"])
    #expect(status == 128 + SIGTERM)
}
