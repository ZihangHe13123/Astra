@testable import AstraMacComputerHelperCore
import ApplicationServices
import CoreGraphics
import Foundation
import Testing

@Test func popupPointerPlanGateAcceptsOnlyOneRawClickOnExactDialog() throws {
    let element = AXUIElementCreateApplication(11)
    let identity = CFHash(element)
    let bounds = CGRect(x: 100, y: 200, width: 300, height: 200)
    let target = WindowTarget(
        appRef: "app", windowRef: "popup", pid: 11, windowID: 22,
        bounds: bounds, title: "", axIdentity: identity, axElement: element,
        interactionMode: .background
    )
    let guardValue = ActionGuard(
        pid: 11, windowID: 22, bounds: bounds, axIdentity: identity,
        snapshotID: "fresh-popup", interactionMode: .foregroundTakeover
    )
    let application = PIDTargetApplication(bundleIdentifier: "com.example.Popup", version: "1")
    func authority(_ actions: [NativeAction]) throws -> ForegroundTakeoverPlanAuthority {
        let plan = try InputDispatcher(
            performer: TakeoverPerformer(state: ActionTargetState(
                pid: 11, windowID: 22, bounds: bounds, axIdentity: identity
            ), snapshotID: guardValue.snapshotID),
            application: application,
            syntheticPolicy: SyntheticInputPlanningPolicy(
                pointerCapability: .available, keyboardCapability: .available,
                registry: PIDInputCompatibilityRegistry(), genericForegroundEnabled: true
            )
        ).plan(actions: actions, context: DispatchContext(guardValue: guardValue))
        return ForegroundTakeoverPlanAuthority(
            target: target, guardValue: guardValue, plan: plan,
            actions: actions, application: application
        )
    }
    let click = try authority([.click(x: 80, y: 42)])
    #expect(click.plan.backends == [.foregroundPointer])
    let popup = VisibleWindowRecord(pid: 11, windowID: 22, bounds: bounds,
                                    layer: 8, alpha: 1, zOrder: 2)
    let parent = VisibleWindowRecord(pid: 11, windowID: 23,
        bounds: CGRect(x: 0, y: 0, width: 800, height: 600),
        layer: 0, alpha: 1, zOrder: 5)
    let popupRows = [popup, parent]
    // The production authority still carries the background snapshot target
    // at begin; after takeover it carries this exact target in foreground mode.
    #expect(popupPointerClickPoint(click, role: "AXWindow", subrole: "AXDialog",
        visibleWindows: popupRows) == CGPoint(x: 180, y: 242))
    let activeTarget = WindowTarget(
        appRef: target.appRef, windowRef: target.windowRef, pid: target.pid,
        windowID: target.windowID, bounds: target.bounds, title: target.title,
        axIdentity: target.axIdentity, axElement: target.axElement,
        interactionMode: .foregroundTakeover
    )
    let activeClick = ForegroundTakeoverPlanAuthority(
        target: activeTarget, guardValue: guardValue, plan: click.plan,
        actions: click.actions, application: application
    )
    #expect(popupPointerClickPoint(activeClick, role: "AXWindow", subrole: "AXDialog",
        visibleWindows: popupRows) == CGPoint(x: 180, y: 242))
    #expect(popupPointerClickPoint(click, role: "AXWindow", subrole: "AXStandardWindow",
        visibleWindows: popupRows) == nil)
    #expect(popupPointerClickPoint(click, role: "AXSheet", subrole: "AXDialog",
        visibleWindows: popupRows) == nil)
    #expect(popupPointerClickPoint(try authority([.click(x: 80, y: 42, within: "pointer")]),
        role: "AXWindow", subrole: "AXDialog", visibleWindows: popupRows) == nil)
    #expect(popupPointerClickPoint(try authority([.click(x: 80, y: 42), .click(x: 80, y: 42)]),
        role: "AXWindow", subrole: "AXDialog", visibleWindows: popupRows) == nil)
    #expect(popupPointerClickPoint(try authority([.keypress(key: "return")]),
        role: "AXWindow", subrole: "AXDialog", visibleWindows: popupRows) == nil)
    #expect(popupPointerClickPoint(try authority([.type(text: "abc")]),
        role: "AXWindow", subrole: "AXDialog", visibleWindows: popupRows) == nil)
    var gateDiagnostics: [String] = []
    let ordinaryDialog = VisibleWindowRecord(pid: 11, windowID: 22, bounds: bounds,
        layer: 0, alpha: 1, zOrder: 2)
    #expect(popupPointerClickPoint(click, role: "AXWindow", subrole: "AXDialog",
        visibleWindows: [ordinaryDialog, parent],
        diagnostic: { gateDiagnostics.append($0) }) == nil)
    #expect(gateDiagnostics.isEmpty)
    #expect(popupPointerClickPoint(click, role: "AXWindow", subrole: "AXDialog",
        visibleWindows: [popup],
        diagnostic: { gateDiagnostics.append($0) }) == nil)
    #expect(gateDiagnostics.count == 1)
    #expect(gateDiagnostics[0].contains("POPUP-POINTER-GATE-FAIL stage=popup_shape"))
    let otherAppParent = VisibleWindowRecord(pid: 12, windowID: 23,
        bounds: parent.bounds, layer: 0, alpha: 1, zOrder: 5)
    #expect(popupPointerClickPoint(click, role: "AXWindow", subrole: "AXDialog",
        visibleWindows: [popup, otherAppParent], diagnostic: { _ in }) == nil)
    let frontParent = VisibleWindowRecord(pid: 11, windowID: 23,
        bounds: parent.bounds, layer: 9, alpha: 1, zOrder: 1)
    #expect(popupPointerClickPoint(click, role: "AXWindow", subrole: "AXDialog",
        visibleWindows: [popup, frontParent], diagnostic: { _ in }) == nil)
}

@Test func layerZeroAXDialogUsesOrdinaryActivationAndPopupPointIsClassifiedOnce() throws {
    let element = AXUIElementCreateApplication(11)
    let identity = CFHash(element)
    let bounds = CGRect(x: 100, y: 200, width: 300, height: 200)
    let target = WindowTarget(
        appRef: "app", windowRef: "dialog", pid: 11, windowID: 22,
        bounds: bounds, title: "", axIdentity: identity, axElement: element,
        interactionMode: .background
    )
    let guardValue = ActionGuard(
        pid: 11, windowID: 22, bounds: bounds, axIdentity: identity,
        snapshotID: "dialog-snapshot", interactionMode: .foregroundTakeover
    )
    let application = PIDTargetApplication(bundleIdentifier: "com.example.Dialog", version: "1")
    let actions: [NativeAction] = [.click(x: 80, y: 42)]
    let plan = try InputDispatcher(
        performer: TakeoverPerformer(state: ActionTargetState(
            pid: 11, windowID: 22, bounds: bounds, axIdentity: identity
        ), snapshotID: guardValue.snapshotID),
        application: application,
        syntheticPolicy: SyntheticInputPlanningPolicy(
            pointerCapability: .available, keyboardCapability: .available,
            registry: PIDInputCompatibilityRegistry(), genericForegroundEnabled: true
        )
    ).plan(actions: actions, context: DispatchContext(guardValue: guardValue))
    let authority = ForegroundTakeoverPlanAuthority(
        target: target, guardValue: guardValue, plan: plan,
        actions: actions, application: application
    )
    let parent = VisibleWindowRecord(pid: 11, windowID: 23,
        bounds: CGRect(x: 0, y: 0, width: 800, height: 600),
        layer: 0, alpha: 1, zOrder: 5)

    func beginWithSelectedLayer(_ layer: Int) throws -> (ordinary: Int, popup: Int,
        point: CGPoint?, classifies: Int, diagnostics: [String]) {
        let selected = VisibleWindowRecord(pid: 11, windowID: 22,
            bounds: bounds, layer: layer, alpha: 1, zOrder: 2)
        let activation = TakeoverActivation(currentFrontmostPID: 77)
        let activity = TakeoverActivity(failArm: false)
        let cursor = TakeoverCursor(failShow: false)
        let vault = TakeoverPlanVault(authority: authority)
        var classifies = 0
        var diagnostics: [String] = []
        let coordinator = ForegroundTakeoverCoordinator(
            activity: activity, activation: activation, cursor: cursor,
            frontmostPID: { activation.currentFrontmostPID },
            applicationExists: { $0 == 77 || $0 == 11 },
            revalidate: { $0 },
            takePlan: vault.take,
            classifyPopupPointer: { candidate in
                classifies += 1
                return popupPointerClickPoint(
                    candidate, role: "AXWindow", subrole: "AXDialog",
                    visibleWindows: [selected, parent],
                    diagnostic: { diagnostics.append($0) }
                )
            }
        )
        let token = try coordinator.begin(
            target: target, snapshotID: guardValue.snapshotID,
            planRef: plan.planRef, stageDigest: plan.stageDigest
        )
        _ = try coordinator.consume(token.ref, actions: actions)
        let execution = try coordinator.executionAuthority(
            for: token.ref, snapshotID: guardValue.snapshotID,
            planRef: plan.planRef, consumedGuard: guardValue
        )
        _ = try coordinator.end(token.ref, allowRestore: false)
        return (activation.activated.count, activation.popupActivated.count,
                execution.popupPointerPoint, classifies, diagnostics)
    }

    let ordinary = try beginWithSelectedLayer(0)
    #expect(ordinary.ordinary == 1)
    #expect(ordinary.popup == 0)
    #expect(ordinary.point == nil)
    #expect(ordinary.classifies == 1)
    #expect(ordinary.diagnostics.isEmpty)

    let popup = try beginWithSelectedLayer(8)
    #expect(popup.ordinary == 0)
    #expect(popup.popup == 1)
    #expect(popup.point == CGPoint(x: 180, y: 242))
    #expect(popup.classifies == 1)
    #expect(popup.diagnostics.isEmpty)
}

@Test func popupPointerPointDoesNotSurviveFragmentReplan() throws {
    var classifiedSnapshots: [String] = []
    let point = CGPoint(x: 110, y: 210)
    let fixture = FragmentCoordinatorFixture(
        wallClockLimitMS: 5_000,
        classifyPopupPointer: { authority in
            classifiedSnapshots.append(authority.guardValue.snapshotID)
            return authority.guardValue.snapshotID == "snapshot" ? point : nil
        }
    )
    let token = try fixture.begin()
    #expect(fixture.activation.popupActivated.count == 1)
    try fixture.runStage(index: 0, takeoverRef: token.ref, freshSnapshotID: "snapshot-1")
    try fixture.authorizeStage(
        index: 1, takeoverRef: token.ref, snapshotID: "snapshot-1",
        target: fixture.foregroundTarget()
    )
    try fixture.finishStage(index: 1, takeoverRef: token.ref)
    let execution = try fixture.coordinator.executionAuthority(
        for: token.ref, snapshotID: "snapshot-1",
        planRef: fixture.plans[1].planRef,
        consumedGuard: fixture.observedGuard(
            snapshotID: "snapshot-1", interactionMode: .foregroundTakeover
        )
    )
    #expect(execution.popupPointerPoint == nil)
    #expect(classifiedSnapshots == ["snapshot", "snapshot-1"])
    _ = try fixture.coordinator.end(token.ref, allowRestore: false)
}

@Test func approvedForegroundPlanProducesOneUseTakeoverAuthority() throws {
    let fixture = TakeoverFixture()
    let token = try fixture.coordinator.begin(
        target: fixture.target,
        snapshotID: fixture.guardValue.snapshotID,
        planRef: fixture.plan.planRef,
        stageDigest: fixture.plan.stageDigest
    )

    let consumed = try fixture.coordinator.consume(token.ref, actions: fixture.actions)

    #expect(consumed.planRef == fixture.plan.planRef)
    #expect(throws: TakeoverError.alreadyConsumed) {
        try fixture.coordinator.consume(token.ref, actions: fixture.actions)
    }
    #expect(fixture.activation.activated.count == 1)
}

@Test func stagedFragmentKeepsOneLeaseCursorAndPriorApplicationAcrossFreshPlans() throws {
    let fixture = FragmentCoordinatorFixture(wallClockLimitMS: 1_000)
    let token = try fixture.begin()

    try fixture.runStage(
        index: 0,
        takeoverRef: token.ref,
        freshSnapshotID: "snapshot-1"
    )
    #expect(fixture.activity.fragmentsStarted == 1)
    #expect(fixture.activation.activated.count == 1)
    #expect(fixture.cursor.closeCount == 0)

    try fixture.authorizeStage(
        index: 1,
        takeoverRef: token.ref,
        snapshotID: "snapshot-1",
        target: fixture.foregroundTarget()
    )
    let terminal = try fixture.runStage(
        index: 1,
        takeoverRef: token.ref,
        freshSnapshotID: "snapshot-terminal"
    )

    #expect(terminal.terminal)
    #expect(terminal.takeover?.restoration == .restored)
    #expect(fixture.activity.fragmentsStarted == 1)
    #expect(fixture.activity.fragmentsEnded == 1)
    #expect(fixture.activity.disarmCount == 1)
    #expect(fixture.cursor.closeCount == 1)
    #expect(fixture.activation.restored == [77])
}

@Test func activeFragmentFreshObservationUsesExactForegroundTargetWithoutRearming() throws {
    let fixture = FragmentCoordinatorFixture(wallClockLimitMS: 1_000)
    let token = try fixture.begin()

    #expect(fixture.coordinator.fragmentObservationTarget(for: fixture.target) == nil)
    try fixture.finishStage(index: 0, takeoverRef: token.ref)

    let observed = fixture.coordinator.fragmentObservationTarget(for: fixture.target)
    let changed = WindowTarget(
        appRef: fixture.target.appRef,
        windowRef: fixture.target.windowRef,
        pid: fixture.target.pid,
        windowID: fixture.target.windowID + 1,
        bounds: fixture.target.bounds,
        title: fixture.target.title,
        axIdentity: fixture.target.axIdentity,
        interactionMode: .background
    )

    #expect(observed?.interactionMode == .foregroundTakeover)
    #expect(observed?.pid == fixture.target.pid)
    #expect(observed?.windowID == fixture.target.windowID)
    #expect(fixture.coordinator.fragmentObservationTarget(for: changed) == nil)
    #expect(fixture.activation.activated.count == 1)
    #expect(fixture.activity.fragmentsStarted == 1)
}

@Test func continuingFragmentBindRejectsBackgroundTargetOrPlanGuard() throws {
    do {
        let fixture = FragmentCoordinatorFixture(wallClockLimitMS: 1_000)
        let token = try fixture.begin()
        try fixture.runStage(index: 0, takeoverRef: token.ref, freshSnapshotID: "snapshot-1")

        #expect(throws: TakeoverError.authorityMismatch) {
            try fixture.authorizeStage(
                index: 1,
                takeoverRef: token.ref,
                snapshotID: "snapshot-1",
                target: fixture.target
            )
        }
    }

    do {
        let fixture = FragmentCoordinatorFixture(wallClockLimitMS: 1_000)
        let token = try fixture.begin()
        try fixture.runStage(index: 0, takeoverRef: token.ref, freshSnapshotID: "snapshot-1")
        let foregroundTarget = fixture.foregroundTarget()

        #expect(throws: TakeoverError.authorityMismatch) {
            try fixture.authorizeStage(
                index: 1,
                takeoverRef: token.ref,
                snapshotID: "snapshot-1",
                target: foregroundTarget,
                authorityGuardMode: .background
            )
        }
    }
}

@Test func fragmentCommitRejectsBackgroundTargetOrObservedGuard() throws {
    do {
        let fixture = FragmentCoordinatorFixture(wallClockLimitMS: 1_000)
        let token = try fixture.begin()
        #expect(throws: TakeoverError.alreadyConsumed) {
            try fixture.runStage(
                index: 0,
                takeoverRef: token.ref,
                freshSnapshotID: "snapshot-background-target",
                observedTarget: fixture.target
            )
        }
    }

    do {
        let fixture = FragmentCoordinatorFixture(wallClockLimitMS: 1_000)
        let token = try fixture.begin()
        #expect(throws: TakeoverError.alreadyConsumed) {
            try fixture.runStage(
                index: 0,
                takeoverRef: token.ref,
                freshSnapshotID: "snapshot-background-guard",
                observedGuardMode: .background
            )
        }
    }
}

@Test func terminalFragmentCleanupFailureLeavesOnlyExactCleanupRetryAuthority() throws {
    let fixture = FragmentCoordinatorFixture(wallClockLimitMS: 1_000)
    let token = try fixture.begin()
    try fixture.runStage(index: 0, takeoverRef: token.ref, freshSnapshotID: "snapshot-1")
    try fixture.authorizeStage(
        index: 1,
        takeoverRef: token.ref,
        snapshotID: "snapshot-1",
        target: fixture.foregroundTarget()
    )
    fixture.activity.retainHeldInput(cleanupFailures: .max)

    #expect(throws: TakeoverError.cleanupFailed) {
        try fixture.runStage(index: 1, takeoverRef: token.ref, freshSnapshotID: "snapshot-terminal")
    }
    #expect(fixture.activity.cleanupAttempts == 1)
    #expect(fixture.coordinator.fragmentObservationTarget(for: fixture.target) == nil)

    #expect(throws: TakeoverError.authorityMismatch) {
        try fixture.authorizeStage(
            index: 1,
            takeoverRef: token.ref,
            snapshotID: "snapshot-1",
            target: fixture.foregroundTarget()
        )
    }
    #expect(fixture.activity.cleanupAttempts == 1)

    let terminalStage = fixture.stages[1]
    let terminalPlan = fixture.plans[1]
    #expect(throws: TakeoverError.alreadyConsumed) {
        try fixture.coordinator.commitFragmentStage(
            token.ref,
            commit: FragmentStageCommit(
                fragmentHash: terminalStage.fragmentHash,
                stageIndex: terminalStage.stageIndex,
                stageHash: terminalStage.stageHash,
                planRef: terminalPlan.planRef,
                freshSnapshotID: "snapshot-terminal",
                postconditionVerified: true
            ),
            observedTarget: fixture.foregroundTarget(),
            observedGuard: fixture.observedGuard(
                snapshotID: "snapshot-terminal",
                interactionMode: .foregroundTakeover
            )
        )
    }
    #expect(fixture.activity.cleanupAttempts == 1)

    fixture.activity.allowHeldInputCleanup()
    let cleanup = try fixture.coordinator.end(token.ref, allowRestore: false)
    #expect(!cleanup.cleanupFailed)
    #expect(fixture.activity.cleanupAttempts == 2)
}

@Test func fragmentCommitRejectsFreshObservationFromAnotherTargetAndTerminates() throws {
    let fixture = FragmentCoordinatorFixture(wallClockLimitMS: 1_000)
    let token = try fixture.begin()
    var other = fixture.target
    other = WindowTarget(
        appRef: other.appRef,
        windowRef: "other-window",
        pid: other.pid,
        windowID: other.windowID + 1,
        bounds: other.bounds,
        title: other.title,
        axIdentity: other.axIdentity,
        interactionMode: other.interactionMode
    )

    #expect(throws: TakeoverError.alreadyConsumed) {
        try fixture.runStage(
            index: 0,
            takeoverRef: token.ref,
            freshSnapshotID: "snapshot-other",
            observedTarget: other
        )
    }
    #expect(fixture.activity.fragmentsEnded == 1)
    #expect(fixture.cursor.closeCount == 1)
}

@Test func fragmentPauseConsumesAuthorityHidesCursorAndPreservesUserFocus() throws {
    let fixture = FragmentCoordinatorFixture(wallClockLimitMS: 1_000)
    let token = try fixture.begin()

    fixture.activity.offerPause()
    Thread.sleep(forTimeInterval: 0.05)

    #expect(fixture.cursor.hideCount == 1)
    #expect(fixture.cursor.closeCount == 1)
    #expect(fixture.activation.restored.isEmpty)
    #expect(throws: TakeoverError.self) {
        _ = try fixture.coordinator.consumeFragment(
            token.ref,
            actions: fixture.actions,
            stage: fixture.stages[0],
            planRef: fixture.plans[0].planRef
        )
    }
}

@Test func activeFragmentMonotonicExpiryTriggersTerminalCleanupWithoutAnotherRequest() throws {
    let fixture = FragmentCoordinatorFixture(wallClockLimitMS: 10)
    _ = try fixture.begin()

    Thread.sleep(forTimeInterval: 0.05)

    #expect(fixture.cursor.hideCount == 1)
    #expect(fixture.cursor.closeCount == 1)
    #expect(fixture.activity.fragmentsEnded == 1)
    #expect(fixture.activation.restored.isEmpty)
}

@Test func fragmentBeginRechecksPausePublishedBeforeRecordBecameActive() throws {
    let fixture = FragmentCoordinatorFixture(wallClockLimitMS: 1_000)
    fixture.activity.offerPauseDuringArm = true

    #expect(throws: TakeoverError.activityUnavailable) {
        _ = try fixture.begin()
    }
    #expect(fixture.activity.fragmentsStarted == 1)
    #expect(fixture.activity.fragmentsEnded == 1)
    #expect(fixture.activity.disarmCount == 1)
    #expect(fixture.cursor.closeCount == 1)
}

@Test func fragmentBeginAndCleanupFailureLeavesTerminalTombstoneForInvalidateRetry() throws {
    let fixture = FragmentCoordinatorFixture(wallClockLimitMS: 1_000)
    fixture.activity.failBeginFragment = true
    fixture.activity.retainHeldInput(cleanupFailures: .max)

    #expect(throws: TakeoverError.cleanupFailed) {
        _ = try fixture.begin()
    }
    #expect(fixture.activity.cleanupAttempts == 1)
    fixture.activity.allowHeldInputCleanup()
    fixture.coordinator.invalidate(allowRestore: false)
    #expect(fixture.activity.cleanupAttempts == 2)
    #expect(fixture.activity.disarmCount == 1)
}

@Test(arguments: [false, true])
func fragmentCommitAndPauseHaveOneTerminalCleanupLinearizationPoint(terminal: Bool) throws {
    let entered = DispatchSemaphore(value: 0)
    let release = DispatchSemaphore(value: 0)
    let finished = DispatchSemaphore(value: 0)
    var barrierEnabled = false
    let fixture = FragmentCoordinatorFixture(
        wallClockLimitMS: 1_000,
        beforeFragmentCommit: {
            if barrierEnabled {
                entered.signal()
                release.wait()
            }
        }
    )
    let token = try fixture.begin()
    var stageIndex = 0
    if terminal {
        try fixture.runStage(index: 0, takeoverRef: token.ref, freshSnapshotID: "snapshot-1")
        try fixture.authorizeStage(
            index: 1,
            takeoverRef: token.ref,
            snapshotID: "snapshot-1",
            target: fixture.foregroundTarget()
        )
        stageIndex = 1
    }
    try fixture.finishStage(index: stageIndex, takeoverRef: token.ref)
    let observed = try #require(fixture.coordinator.fragmentObservationTarget(for: fixture.target))
    let stage = fixture.stages[stageIndex]
    let plan = fixture.plans[stageIndex]
    barrierEnabled = true

    Thread.detachNewThread {
        _ = try? fixture.coordinator.commitFragmentStage(
            token.ref,
            commit: FragmentStageCommit(
                fragmentHash: stage.fragmentHash,
                stageIndex: stage.stageIndex,
                stageHash: stage.stageHash,
                planRef: plan.planRef,
                freshSnapshotID: "commit-snapshot",
                postconditionVerified: true
            ),
            observedTarget: observed,
            observedGuard: fixture.observedGuard(
                snapshotID: "commit-snapshot",
                interactionMode: .foregroundTakeover
            )
        )
        finished.signal()
    }
    entered.wait()
    fixture.coordinator.cancelFragment(token.ref)
    #expect(fixture.activity.fragmentsEnded == 0)
    release.signal()
    finished.wait()

    #expect(fixture.activity.fragmentsEnded == 1)
    #expect(fixture.activity.disarmCount == 1)
    #expect(fixture.cursor.closeCount == 1)
    #expect(throws: TakeoverError.alreadyConsumed) {
        try fixture.coordinator.end(token.ref, allowRestore: false)
    }
}

@Test func actionOrTargetMismatchConsumesAuthorityBeforeInput() throws {
    let fixture = TakeoverFixture()
    let token = try fixture.coordinator.begin(
        target: fixture.target,
        snapshotID: fixture.guardValue.snapshotID,
        planRef: fixture.plan.planRef,
        stageDigest: fixture.plan.stageDigest
    )

    #expect(throws: TakeoverError.authorityMismatch) {
        try fixture.coordinator.consume(token.ref, actions: [.click(x: 12, y: 12)])
    }
    #expect(throws: TakeoverError.alreadyConsumed) {
        try fixture.coordinator.consume(token.ref, actions: fixture.actions)
    }
    #expect(fixture.activity.fragmentsStarted == 0)
}

@Test func wrongSnapshotAtBeginConsumesThePlanBeforeRejecting() throws {
    let fixture = TakeoverFixture()
    #expect(throws: TakeoverError.authorityMismatch) {
        try fixture.coordinator.begin(
            target: fixture.target,
            snapshotID: "wrong-snapshot",
            planRef: fixture.plan.planRef,
            stageDigest: fixture.plan.stageDigest
        )
    }
    #expect(throws: TakeoverError.stalePlan) { try fixture.begin() }
}

@Test func rawActionDigestCannotSubstituteForBoundStageDigest() throws {
    let fixture = TakeoverFixture()

    #expect(throws: TakeoverError.authorityMismatch) {
        try fixture.coordinator.begin(
            target: fixture.target,
            snapshotID: fixture.guardValue.snapshotID,
            planRef: fixture.plan.planRef,
            stageDigest: InputDispatcher.digest(fixture.actions)
        )
    }
    #expect(throws: TakeoverError.stalePlan) { try fixture.begin() }
}

@Test(arguments: ["plan", "snapshot", "guard"])
func postConsumeAuthorityMismatchCancelsInputAndLeavesOneCleanupTombstone(kind: String) throws {
    let fixture = TakeoverFixture()
    let token = try fixture.begin()
    _ = try fixture.coordinator.consume(token.ref, actions: fixture.actions)
    let mismatchedGuard = ActionGuard(
        pid: kind == "guard" ? 99 : fixture.guardValue.pid,
        windowID: fixture.guardValue.windowID,
        bounds: fixture.guardValue.bounds,
        axIdentity: fixture.guardValue.axIdentity,
        snapshotID: fixture.guardValue.snapshotID,
        interactionMode: .background
    )

    #expect(throws: TakeoverError.authorityMismatch) {
        try fixture.coordinator.executionAuthority(
            for: token.ref,
            snapshotID: kind == "snapshot" ? "wrong-snapshot" : fixture.guardValue.snapshotID,
            planRef: kind == "plan" ? "wrong-plan" : fixture.plan.planRef,
            consumedGuard: mismatchedGuard
        )
    }
    #expect(fixture.activity.fragmentsStarted == 0)
    #expect(fixture.cursor.closeCount == 1)
    #expect(try fixture.coordinator.end(token.ref, allowRestore: true).restoration == .preservedUserFocus)
    #expect(throws: TakeoverError.alreadyConsumed) {
        try fixture.coordinator.end(token.ref, allowRestore: true)
    }
}

@Test func unresolvedTombstoneRejectsAnotherBeginWithoutConsumingItsPlan() throws {
    let fixture = TakeoverFixture()
    let first = try fixture.begin()
    #expect(throws: TakeoverError.authorityMismatch) {
        try fixture.coordinator.consume(first.ref, actions: [.click(x: 12, y: 12)])
    }
    let secondPlan = fixture.authorizeAnotherPlan()

    #expect(throws: TakeoverError.alreadyConsumed) {
        try fixture.coordinator.begin(
            target: fixture.target,
            snapshotID: fixture.guardValue.snapshotID,
            planRef: secondPlan.planRef,
            stageDigest: secondPlan.stageDigest
        )
    }
    _ = try fixture.coordinator.end(first.ref, allowRestore: false)
    let second = try fixture.coordinator.begin(
        target: fixture.target,
        snapshotID: fixture.guardValue.snapshotID,
        planRef: secondPlan.planRef,
        stageDigest: secondPlan.stageDigest
    )
    _ = try fixture.coordinator.end(second.ref, allowRestore: false)
}

@Test(arguments: ["end", "invalidate"])
func cleanupKeepsTakeoverCapacityReservedUntilExternalTeardownFinishes(operation: String) throws {
    let fixture = TakeoverFixture()
    let first = try fixture.begin()
    let secondPlan = fixture.authorizeAnotherPlan()
    let cleanupEntered = DispatchSemaphore(value: 0)
    let allowCleanup = DispatchSemaphore(value: 0)
    let cleanupFinished = DispatchSemaphore(value: 0)
    fixture.cursor.onClose = {
        cleanupEntered.signal()
        allowCleanup.wait()
    }

    Thread.detachNewThread {
        if operation == "end" {
            _ = try? fixture.coordinator.end(first.ref, allowRestore: false)
        } else {
            fixture.coordinator.invalidate(allowRestore: false)
        }
        cleanupFinished.signal()
    }
    cleanupEntered.wait()
    #expect(throws: TakeoverError.alreadyConsumed) {
        try fixture.coordinator.begin(
            target: fixture.target,
            snapshotID: fixture.guardValue.snapshotID,
            planRef: secondPlan.planRef,
            stageDigest: secondPlan.stageDigest
        )
    }
    allowCleanup.signal()
    cleanupFinished.wait()

    let second = try fixture.coordinator.begin(
        target: fixture.target,
        snapshotID: fixture.guardValue.snapshotID,
        planRef: secondPlan.planRef,
        stageDigest: secondPlan.stageDigest
    )
    fixture.cursor.onClose = nil
    _ = try fixture.coordinator.end(second.ref, allowRestore: false)
}

@Test(arguments: ["end", "invalidate"])
func terminalTakeoverCleanupRetriesTheExactHeldReleaseBeforeDisarm(operation: String) throws {
    let fixture = TakeoverFixture()
    let token = try fixture.begin()
    fixture.activity.retainHeldInput(cleanupFailures: 1)
    #expect(fixture.activity.attemptHeldInputCleanup() == HeldInputCleanupResult(
        attempted: 1,
        released: 0,
        failed: 1
    ))

    if operation == "end" {
        let outcome = try fixture.coordinator.end(token.ref, allowRestore: false)
        #expect(!outcome.cleanupFailed)
    } else {
        fixture.coordinator.invalidate(allowRestore: false)
    }

    #expect(fixture.activity.cleanupAttempts == 2)
    #expect(fixture.activity.heldInputCount == 0)
    #expect(fixture.activity.cleanupObservedBeforeDisarm)
}

@Test func terminalTakeoverCleanupRetainsAndReportsPersistentReleaseFailure() throws {
    let fixture = TakeoverFixture()
    let token = try fixture.begin()
    fixture.activity.retainHeldInput(cleanupFailures: .max)
    #expect(fixture.activity.attemptHeldInputCleanup().failed == 1)

    let outcome = try fixture.coordinator.end(token.ref, allowRestore: false)

    #expect(outcome.cleanupFailed)
    #expect(fixture.activity.heldInputCount == 1)
    #expect(fixture.activity.cleanupAttempts == 2)
    #expect(fixture.activity.cleanupObservedBeforeDisarm)
    #expect(fixture.activity.disarmCount == 0)

    fixture.activity.allowHeldInputCleanup()
    let retried = try fixture.coordinator.end(token.ref, allowRestore: false)
    #expect(!retried.cleanupFailed)
    #expect(fixture.activity.heldInputCount == 0)
    #expect(fixture.activity.cleanupAttempts == 3)
    #expect(fixture.activity.disarmCount == 1)
}

@Test func failedTakeoverBeginCleansHeldInputBeforeDisarmAndRestoration() {
    let fixture = TakeoverFixture(failure: .sidecar)
    fixture.activity.retainHeldInput(cleanupFailures: 0)

    #expect(throws: TakeoverError.sidecarFailed) { try fixture.begin() }

    #expect(fixture.activity.heldInputCount == 0)
    #expect(fixture.activity.cleanupAttempts == 1)
    #expect(fixture.activity.cleanupObservedBeforeDisarm)
}

@Test func failedTakeoverBeginRetainsInternalCleanupAuthorityUntilInvalidateCanRetry() {
    let fixture = TakeoverFixture(failure: .sidecar)
    fixture.activity.retainHeldInput(cleanupFailures: .max)

    #expect(throws: TakeoverError.cleanupFailed) { try fixture.begin() }
    #expect(fixture.activity.heldInputCount == 1)
    #expect(fixture.activity.disarmCount == 0)
    #expect(throws: TakeoverError.alreadyConsumed) { try fixture.begin() }

    fixture.activity.allowHeldInputCleanup()
    fixture.coordinator.invalidate(allowRestore: true)

    #expect(fixture.activity.heldInputCount == 0)
    #expect(fixture.activity.disarmCount == 1)
    let nextPlan = fixture.authorizeAnotherPlan()
    #expect(throws: TakeoverError.sidecarFailed) {
        try fixture.coordinator.begin(
            target: fixture.target,
            snapshotID: fixture.guardValue.snapshotID,
            planRef: nextPlan.planRef,
            stageDigest: nextPlan.stageDigest
        )
    }
}

@Test func unknownTakeoverReferenceFailsClosedAndConsumesActiveAuthority() throws {
    let fixture = TakeoverFixture()
    let token = try fixture.begin()

    #expect(throws: TakeoverError.authorityMismatch) {
        try fixture.coordinator.consume("takeover_wrong", actions: fixture.actions)
    }
    #expect(throws: TakeoverError.alreadyConsumed) {
        try fixture.coordinator.consume(token.ref, actions: fixture.actions)
    }
    #expect(fixture.activity.fragmentsStarted == 0)
}

@Test func takeoverRejectsOutOfWindowPresentationPointBeforeShowingSidecar() throws {
    let application = PIDTargetApplication(bundleIdentifier: "com.example.Editor", version: "1")
    let performer = TakeoverPerformer(state: ActionTargetState(
        pid: 11,
        windowID: 22,
        bounds: CGRect(x: 100, y: 200, width: 300, height: 200),
        axIdentity: 33
    ))
    let dispatcher = InputDispatcher(
        performer: performer,
        application: application,
        syntheticPolicy: foregroundTakeoverPlanningPolicy(
            application: application,
            pointerActions: [.click]
        )
    )

    #expect(throws: ActionExecutionError.outOfBounds) {
        _ = try dispatcher.plan(
            actions: [.click(x: 500, y: 10, within: "pointer")],
            context: DispatchContext(guardValue: ActionGuard(
                pid: 11,
                windowID: 22,
                bounds: CGRect(x: 100, y: 200, width: 300, height: 200),
                axIdentity: 33,
                snapshotID: "snapshot",
                interactionMode: .foregroundTakeover
            ))
        )
    }
}

@Test func physicalActivityPreservesUserFocusAndClosesSidecar() throws {
    let fixture = TakeoverFixture(oldFrontmostPID: 77)
    let token = try fixture.begin()
    _ = try fixture.coordinator.consume(token.ref, actions: fixture.actions)
    fixture.activity.isPaused = true

    let outcome = try fixture.coordinator.end(token.ref, allowRestore: true)

    #expect(outcome == TakeoverOutcome(started: true, restoration: .preservedUserFocus))
    #expect(fixture.activation.restored.isEmpty)
    #expect(fixture.cursor.hideCount == 1)
    #expect(fixture.cursor.closeCount == 1)
    #expect(fixture.activity.disarmCount == 1)
}

@Test func physicalActivityDuringSidecarClosePreventsFocusRestoration() throws {
    let fixture = TakeoverFixture(oldFrontmostPID: 77)
    let token = try fixture.begin()
    fixture.cursor.onClose = { fixture.activity.terminalInterrupted = true }

    let outcome = try fixture.coordinator.end(token.ref, allowRestore: true)

    #expect(outcome.restoration == .preservedUserFocus)
    #expect(fixture.activation.restored.isEmpty)
}

@Test func monitorFailureDuringSidecarClosePreventsFocusRestoration() throws {
    let fixture = TakeoverFixture(oldFrontmostPID: 77)
    let token = try fixture.begin()
    fixture.cursor.onClose = { fixture.activity.terminalInterrupted = true }

    let outcome = try fixture.coordinator.end(token.ref, allowRestore: true)

    #expect(outcome.restoration == .preservedUserFocus)
    #expect(fixture.activation.restored.isEmpty)
}

@Test func priorApplicationRestoresOnlyWhenItStillExists() throws {
    let restorable = TakeoverFixture(oldFrontmostPID: 77, existingPIDs: [77])
    let restorableToken = try restorable.begin()
    let restored = try restorable.coordinator.end(restorableToken.ref, allowRestore: true)
    #expect(restored.restoration == .restored)
    #expect(restorable.activation.restored == [77])

    let gone = TakeoverFixture(oldFrontmostPID: 88, existingPIDs: [])
    let goneToken = try gone.begin()
    let preserved = try gone.coordinator.end(goneToken.ref, allowRestore: true)
    #expect(preserved.restoration == .preservedUserFocus)
    #expect(gone.activation.restored.isEmpty)
}

@Test func focusChangedWithoutPhysicalInputIsNotOverriddenDuringRestoration() throws {
    let fixture = TakeoverFixture(oldFrontmostPID: 77, existingPIDs: [77, 99])
    let token = try fixture.begin()
    fixture.activation.currentFrontmostPID = 99

    let outcome = try fixture.coordinator.end(token.ref, allowRestore: true)

    #expect(outcome.restoration == .preservedUserFocus)
    #expect(fixture.activation.restored.isEmpty)
}

@Test(arguments: [TakeoverFixture.Failure.target, .sidecar, .activity])
private func beginFailureConsumesPlanAndCleansUp(_ failure: TakeoverFixture.Failure) throws {
    let fixture = TakeoverFixture(failure: failure)
    #expect(throws: TakeoverError.self) { try fixture.begin() }
    #expect(throws: TakeoverError.stalePlan) { try fixture.begin() }
    #expect(fixture.activity.disarmCount <= 1)
    #expect(fixture.cursor.hideCount <= 1)
    if failure == .sidecar {
        #expect(fixture.activation.restored == [77])
    }
}

@Test func activationFailureRemainsTargetNotFrontmostAtProtocolBoundary() {
    let fixture = TakeoverFixture(failure: .activation)
    let dispatcher = Dispatcher(
        permissions: TakeoverPermissions(),
        windows: TakeoverBeginFailureWindows(fixture: fixture)
    )

    let response = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"begin","operation":"takeover_begin","payload":{"snapshot_id":"snapshot","plan_ref":"plan","fragment":{"fragment_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","stages":[{"stage_hash":"0000000000000000000000000000000000000000000000000000000000000000","expected_action_count":1,"requirements":[{"backend":"pid_pointer","action_class":"click","intent":"pointer:click"}]}],"max_actions":1,"wall_clock_limit_ms":1000,"restore_previous_focus":true}}}"#
    )

    #expect(!response.ok)
    #expect(response.error?.code == "target_not_frontmost")
    #expect(response.error?.code != "target_gone")
}

@Test func strictProtocolRoutesTakeoverAndRejectsLegacyForegroundShapes() {
    let windows = TakeoverProtocolWindows()
    let dispatcher = Dispatcher(
        permissions: TakeoverPermissions(),
        windows: windows
    )
    let begin = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"b","operation":"takeover_begin","payload":{"snapshot_id":"s","plan_ref":"p","fragment":{"fragment_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","stages":[{"stage_hash":"0000000000000000000000000000000000000000000000000000000000000000","expected_action_count":1,"requirements":[{"backend":"pid_pointer","action_class":"click","intent":"pointer:click"}]}],"max_actions":1,"wall_clock_limit_ms":1000,"restore_previous_focus":true}}}"#
    )
    #expect(begin.ok)
    #expect(windows.beginCalls == 1)

    let foreground = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"a","operation":"act","payload":{"interaction_mode":"foreground_takeover","snapshot_id":"s","plan_ref":"p","takeover_ref":"t","actions":[],"fragment_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","stage_index":0,"stage_hash":"0000000000000000000000000000000000000000000000000000000000000000"}}"#
    )
    #expect(foreground.ok)
    #expect(windows.foregroundActCalls == 1)

    let end = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"e","operation":"takeover_end","payload":{"takeover_ref":"t"}}"#
    )
    #expect(end.ok)
    guard case let .object(endResult)? = end.result
    else {
        Issue.record("takeover_end omitted its result")
        return
    }
    #expect(Set(endResult.keys) == ["started", "restoration"])
    #expect(windows.endCalls == 1)
    let staleEnd = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"e2","operation":"takeover_end","payload":{"takeover_ref":"t"}}"#
    )
    #expect(!staleEnd.ok)

    let cleanupFailureWindows = TakeoverProtocolWindows()
    cleanupFailureWindows.cleanupFailed = true
    let cleanupFailure = Dispatcher(
        permissions: TakeoverPermissions(),
        windows: cleanupFailureWindows
    ).handle(
        #"{"protocol_version":4,"request_id":"cleanup","operation":"takeover_end","payload":{"takeover_ref":"t"}}"#
    )
    #expect(!cleanupFailure.ok)
    #expect(cleanupFailure.error?.code == "helper_failed")
    if case .some = cleanupFailure.result {
        Issue.record("failed takeover_end returned a success result")
    }

    let legacyFocus = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"f","operation":"focus","payload":{"app_ref":"a","window_ref":"w"}}"#
    )
    let legacyAct = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"l","operation":"act","payload":{"app_ref":"a","window_ref":"w","snapshot_id":"s","actions":[]}}"#
    )
    #expect(!legacyFocus.ok)
    #expect(!legacyAct.ok)
}

@Test func foregroundExecutorRunsEveryApprovedMixedBackendEntryInOriginalOrderWithinOneFragment() throws {
    let scenarios: [(
        actions: [NativeAction],
        enabled: Set<DispatchActionClass>,
        expectedBackends: [DispatchBackend],
        expectedLog: [String]
    )] = [
        (
            [.click(elementRef: "press"), foregroundSafeScroll(deltaY: 10)],
            [.scroll],
            [.axPress, .pidPointer],
            ["ax_press:0", "pid_scroll", "evidence:1"]
        ),
        (
            [.wait(durationMS: 0), .click(x: 10, y: 10, within: "pointer")],
            [.click],
            [.wait, .pidPointer],
            ["wait:0", "pid_down", "pid_up", "evidence:1"]
        ),
        (
            [.type(text: "hello", elementRef: "text"), foregroundSafeDrag()],
            [.drag],
            [.axSelectedText, .pidPointer],
            ["ax_text:0", "pid_down", "pid_drag", "pid_up", "evidence:1"]
        ),
    ]

    for scenario in scenarios {
        let fixture = MixedForegroundExecutionFixture(enabled: scenario.enabled)
        let context = DispatchContext(guardValue: fixture.guardValue)
        let plan = try fixture.dispatcher.plan(actions: scenario.actions, context: context)
        let entries = try fixture.dispatcher.consumeForegroundPlan(
            plan,
            authority: ForegroundPlanConsumptionAuthority(
                planRef: plan.planRef,
                snapshotID: fixture.guardValue.snapshotID,
                interactionMode: .foregroundTakeover,
                actions: scenario.actions,
                backends: plan.backends,
                guardValue: fixture.guardValue
            )
        )

        let result = fixture.executor.run(
            expected: fixture.guardValue,
            application: fixture.application,
            lease: fixture.activity.lease,
            entries: entries
        )

        #expect(entries.map(\.sourceIndex) == Array(scenario.actions.indices))
        #expect(entries.map(\.backend) == scenario.expectedBackends)
        #expect(result.error == nil)
        #expect(result.cooperativeError == nil)
        #expect(result.lastAcknowledgedAction == scenario.actions.count - 1)
        #expect(result.outcomes == scenario.actions.indices.map {
            ActionOutcome(index: $0, ok: true, error: nil)
        })
        #expect(fixture.log.values == scenario.expectedLog)
        #expect(fixture.activity.fragmentsStarted == 1)
        #expect(fixture.activity.fragmentsEnded == 1)
    }
}

@Test func foregroundPointerObservationCheckpointStopsMixedBatch() throws {
    for hasSuffix in [false, true] {
        let fixture = MixedForegroundExecutionFixture(enabled: [.click], pointerEvidence: false)
        let actions: [NativeAction] = [.click(x: 10, y: 10, within: "pointer")] + (hasSuffix ? [.wait(durationMS: 0)] : [])
        let plan = try fixture.dispatcher.plan(actions: actions, context: DispatchContext(guardValue: fixture.guardValue))
        let entries = try fixture.dispatcher.consumeForegroundPlan(plan,
            authority: ForegroundPlanConsumptionAuthority(planRef: plan.planRef,
                snapshotID: fixture.guardValue.snapshotID, interactionMode: .foregroundTakeover,
                actions: actions, backends: plan.backends, guardValue: fixture.guardValue))
        let result = fixture.executor.run(expected: fixture.guardValue,
            application: fixture.application, lease: fixture.activity.lease, entries: entries)

        #expect(result.error == nil)
        #expect(result.cooperativeError == (hasSuffix ? .observationRequired : nil))
        #expect(result.lastAcknowledgedAction == 0)
        #expect(result.outcomes == [ActionOutcome(index: 0, ok: true, error: nil, observationRequired: true)])
        #expect(fixture.log.values == ["pid_down", "pid_up", "evidence:0"])
        #expect(fixture.activity.fragmentsEnded == 1)
    }
}

@Test func foregroundAXScrollRevalidatesImmediatelyAfterEarlierEntryMutatesAuthority() throws {
    enum Mutation: CaseIterable {
        case element
        case identity
        case bounds
        case enabled
        case actionName
    }

    for mutation in Mutation.allCases {
        let fixture = MixedForegroundExecutionFixture(enabled: [])
        let actions: [NativeAction] = [
            .wait(durationMS: 0),
            .scroll(deltaY: 1, elementRef: "scroll"),
        ]
        let context = DispatchContext(guardValue: fixture.guardValue)
        let plan = try fixture.dispatcher.plan(actions: actions, context: context)
        let entries = try fixture.dispatcher.consumeForegroundPlan(
            plan,
            authority: ForegroundPlanConsumptionAuthority(
                planRef: plan.planRef,
                snapshotID: fixture.guardValue.snapshotID,
                interactionMode: .foregroundTakeover,
                actions: actions,
                backends: plan.backends,
                guardValue: fixture.guardValue
            )
        )
        fixture.performer.onWait = {
            switch mutation {
            case .element:
                fixture.performer.scrollElement = AXUIElementCreateApplication(13)
            case .identity:
                fixture.performer.scrollIdentity = "changed-scroll"
            case .bounds:
                fixture.performer.scrollBounds.origin.x += 2
            case .enabled:
                fixture.performer.scrollEnabled = false
            case .actionName:
                fixture.performer.scrollActionNames = .complete([kAXDecrementAction as String])
            }
        }

        let result = fixture.executor.run(
            expected: fixture.guardValue,
            application: fixture.application,
            lease: fixture.activity.lease,
            entries: entries
        )

        #expect(plan.backends == [.wait, .axIncrement], "mutation: \(mutation)")
        #expect(result.error == .staleSnapshot, "mutation: \(mutation)")
        #expect(result.lastAcknowledgedAction == 0, "mutation: \(mutation)")
        #expect(result.outcomes == [
            ActionOutcome(index: 0, ok: true, error: nil),
            ActionOutcome(index: 1, ok: false, error: .staleSnapshot),
        ], "mutation: \(mutation)")
        #expect(fixture.performer.axScrollPerformCalls == 0, "mutation: \(mutation)")
        #expect(fixture.log.values == ["wait:0"], "mutation: \(mutation)")
    }
}

@Test func foregroundAXScrollPostDispatchFailureRemainsOneShotUnknownOutcome() throws {
    let fixture = MixedForegroundExecutionFixture(enabled: [])
    let actions: [NativeAction] = [
        .wait(durationMS: 0),
        .scroll(deltaY: -1, elementRef: "scroll"),
    ]
    let context = DispatchContext(guardValue: fixture.guardValue)
    let plan = try fixture.dispatcher.plan(actions: actions, context: context)
    let entries = try fixture.dispatcher.consumeForegroundPlan(
        plan,
        authority: ForegroundPlanConsumptionAuthority(
            planRef: plan.planRef,
            snapshotID: fixture.guardValue.snapshotID,
            interactionMode: .foregroundTakeover,
            actions: actions,
            backends: plan.backends,
            guardValue: fixture.guardValue
        )
    )
    fixture.performer.axScrollFailure = ActionPerformFailure(
        error: .helperFailed,
        inputStarted: true
    )

    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.activity.lease,
        entries: entries
    )

    #expect(plan.backends == [.wait, .axDecrement])
    #expect(result.error == .unknownOutcome)
    #expect(result.lastAcknowledgedAction == 0)
    #expect(result.outcomes == [
        ActionOutcome(index: 0, ok: true, error: nil),
        ActionOutcome(index: 1, ok: false, error: .unknownOutcome),
    ])
    #expect(fixture.performer.axScrollPerformCalls == 1)
    #expect(fixture.log.values == ["wait:0", "ax_decrement"])
}

@Test func foregroundAXScrollActivityPauseDuringLiveValidationBlocksMutation() throws {
    let fixture = MixedForegroundExecutionFixture(enabled: [])
    let actions: [NativeAction] = [
        .scroll(deltaY: 1, elementRef: "scroll"),
    ]
    let context = DispatchContext(guardValue: fixture.guardValue)
    let plan = try fixture.dispatcher.plan(actions: actions, context: context)
    let entries = try fixture.dispatcher.consumeForegroundPlan(
        plan,
        authority: ForegroundPlanConsumptionAuthority(
            planRef: plan.planRef,
            snapshotID: fixture.guardValue.snapshotID,
            interactionMode: .foregroundTakeover,
            actions: actions,
            backends: plan.backends,
            guardValue: fixture.guardValue
        )
    )
    fixture.performer.onScrollLookup = { fixture.activity.paused = true }

    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.activity.lease,
        entries: entries
    )

    #expect(result.cooperativeError == .userActivityPaused)
    #expect(result.lastAcknowledgedAction == -1)
    #expect(fixture.performer.axScrollPerformCalls == 0)
    #expect(fixture.log.values.isEmpty)
}

@Test func completeForegroundPreflightRejectsDisabledPIDCellBeforeAXWaitOrPIDAndConsumesFragmentAuthority() throws {
    let fixture = MixedForegroundExecutionFixture(enabled: [])
    let actions: [NativeAction] = [
        .click(elementRef: "press"),
        .wait(durationMS: 0),
        foregroundSafeScroll(deltaY: 10),
    ]
    let context = DispatchContext(guardValue: fixture.guardValue)
    let plan = try fixture.dispatcher.plan(actions: actions, context: context)
    let entries = try fixture.dispatcher.consumeForegroundPlan(
        plan,
        authority: ForegroundPlanConsumptionAuthority(
            planRef: plan.planRef,
            snapshotID: fixture.guardValue.snapshotID,
            interactionMode: .foregroundTakeover,
            actions: actions,
            backends: plan.backends,
            guardValue: fixture.guardValue
        )
    )

    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.activity.lease,
        entries: entries
    )

    #expect(result.cooperativeError == .backgroundActionUnsupported)
    #expect(result.outcomes.isEmpty)
    #expect(fixture.log.values.isEmpty)
    #expect(fixture.poster.events.isEmpty)
    #expect(fixture.activity.fragmentsStarted == 0)
    #expect(fixture.activity.fragmentsEnded == 0)
    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try fixture.dispatcher.consumeForegroundPlan(
            plan,
            authority: ForegroundPlanConsumptionAuthority(
                planRef: plan.planRef,
                snapshotID: fixture.guardValue.snapshotID,
                interactionMode: .foregroundTakeover,
                actions: actions,
                backends: plan.backends,
                guardValue: fixture.guardValue
            )
        )
    }
}

@Test func foregroundKeyboardMixedPlanPreflightsBeforeActionZeroAndPreservesOriginalOrder() {
    let enabled = MixedForegroundKeyboardFixture(keyboardEnabled: true)
    let enabledResult = enabled.executor.run(
        expected: enabled.guardValue,
        application: enabled.application,
        lease: enabled.activity.lease,
        entries: enabled.entries
    )

    #expect(enabledResult.error == nil)
    #expect(enabledResult.cooperativeError == nil)
    #expect(enabledResult.lastAcknowledgedAction == 2)
    #expect(enabled.log.values == ["ax_press", "keyboard_down", "keyboard_up", "wait"])
    #expect(enabled.activity.fragmentsStarted == 1)
    #expect(enabled.activity.fragmentsEnded == 1)

    let disabled = MixedForegroundKeyboardFixture(keyboardEnabled: false)
    let disabledResult = disabled.executor.run(
        expected: disabled.guardValue,
        application: disabled.application,
        lease: disabled.activity.lease,
        entries: disabled.entries
    )

    #expect(disabledResult.cooperativeError == .backgroundActionUnsupported)
    #expect(disabledResult.lastAcknowledgedAction == -1)
    #expect(disabledResult.outcomes.isEmpty)
    #expect(disabled.log.values.isEmpty)
    #expect(disabled.poster.events.isEmpty)
    #expect(disabled.activity.fragmentsStarted == 0)
}

@Test func axPrefixMakesLaterFocusObservationFailureUnknownPreservingTheUnderlyingCause() {
    let fixture = MixedForegroundKeyboardFixture(keyboardEnabled: true, changeFocusAfterAX: true)

    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.activity.lease,
        entries: fixture.entries
    )

    #expect(result.error == .unknownOutcome)
    #expect(result.lastAcknowledgedAction == 0)
    #expect(result.outcomes == [
        ActionOutcome(index: 0, ok: true, error: nil),
        ActionOutcome(index: 1, ok: false, error: .unknownOutcome, inputDiagnostics: KeyboardFailureDiagnostics(
            stage: .beforeKeyDown, cause: .staleSnapshot, userActivityPaused: false,
            inputMayHaveStarted: false, cleanupFailed: false
        )),
    ])
    #expect(fixture.log.values == ["ax_press"])
    #expect(fixture.poster.events.isEmpty)
}

@Test func pidPointerPrefixMakesLaterFocusObservationFailureUnknownWithoutKeyboardInput() {
    let fixture = MixedForegroundKeyboardFixture(keyboardEnabled: true, pointerFocusChange: true)

    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.activity.lease,
        entries: fixture.entries
    )

    #expect(result.error == .unknownOutcome)
    #expect(result.lastAcknowledgedAction == 0)
    #expect(result.outcomes == [
        ActionOutcome(index: 0, ok: true, error: nil),
        ActionOutcome(index: 1, ok: false, error: .unknownOutcome, inputDiagnostics: KeyboardFailureDiagnostics(
            stage: .beforeKeyDown, cause: .staleSnapshot, userActivityPaused: false,
            inputMayHaveStarted: false, cleanupFailed: false
        )),
    ])
    #expect(fixture.log.values == ["pointer_down", "pointer_up"])
    #expect(fixture.poster.events.count == 2)
}

@Test func keyboardFocusCheckpointStopsBatchWithOnlyAcknowledgedPrefix() {
    let fixture = MixedForegroundKeyboardFixture(
        keyboardEnabled: true,
        keyboardPrefixFocusChange: true
    )

    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.activity.lease,
        entries: fixture.entries
    )

    #expect(result.error == nil)
    #expect(result.cooperativeError == .observationRequired)
    #expect(result.lastAcknowledgedAction == 0)
    #expect(result.outcomes == [
        ActionOutcome(index: 0, ok: true, error: nil, observationRequired: true),
    ])
    #expect(fixture.log.values == ["keyboard_down", "keyboard_up"])
    #expect(fixture.poster.events.count == 2)
}

@Test func keyboardFocusCheckpointAtEndSucceedsWithoutInventingRemainingActions() {
    let fixture = MixedForegroundKeyboardFixture(keyboardEnabled: true, keyboardPrefixFocusChange: true)
    let result = fixture.executor.run(expected: fixture.guardValue, application: fixture.application,
        lease: fixture.activity.lease, entries: [fixture.entries[0]])
    #expect(result.error == nil)
    #expect(result.cooperativeError == nil)
    #expect(result.lastAcknowledgedAction == 0)
    #expect(result.outcomes == [ActionOutcome(index: 0, ok: true, error: nil, observationRequired: true)])
    #expect(fixture.log.values == ["keyboard_down", "keyboard_up"])
}

@Test func keyboardFocusCheckpointStopsBeforeWaitAndAXActions() {
    for method: ResolvedActionMethod in [.wait, .accessibilityPress] {
        let fixture = MixedForegroundKeyboardFixture(keyboardEnabled: true, keyboardPrefixFocusChange: true)
        let source: NativeAction = method == .wait ? .wait(durationMS: 0) : .click(elementRef: "press")
        let suffix = PlannedDispatchEntry(sourceIndex: 1, source: source,
            backend: method == .wait ? .wait : .axPress, actionClass: method == .wait ? nil : .press,
            resolved: ResolvedAction(source: source, method: method, screenPoint: nil, endScreenPoint: nil,
                element: method == .wait ? nil : AXUIElementCreateApplication(11)))
        let result = fixture.executor.run(expected: fixture.guardValue, application: fixture.application,
            lease: fixture.activity.lease, entries: [fixture.entries[0], suffix])
        #expect(result.cooperativeError == .observationRequired)
        #expect(result.outcomes.count == 1)
        #expect(result.lastAcknowledgedAction == 0)
        #expect(fixture.log.values == ["keyboard_down", "keyboard_up"])
    }
}

@Test func priorInputDoesNotGeneralizeALaterPreInputPosterCapabilityFailure() {
    let fixture = MixedForegroundKeyboardFixture(
        keyboardEnabled: true,
        failKeyboardDownBeforeInput: true
    )

    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.activity.lease,
        entries: fixture.entries
    )

    #expect(result.error == .permissionDenied)
    #expect(result.lastAcknowledgedAction == 0)
    #expect(result.outcomes == [
        ActionOutcome(index: 0, ok: true, error: nil),
        ActionOutcome(index: 1, ok: false, error: .permissionDenied, inputDiagnostics: KeyboardFailureDiagnostics(
            stage: .keyDown, cause: .permissionDenied, userActivityPaused: false,
            inputMayHaveStarted: false, cleanupFailed: false
        )),
    ])
    #expect(fixture.log.values == ["ax_press"])
}

@Test func priorInputMakesLaterLeaseObservationFailureUnknownPreservingTheUnderlyingCause() {
    let fixture = MixedForegroundKeyboardFixture(
        keyboardEnabled: true,
        pauseBeforeKeyboardEvent: true
    )

    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.activity.lease,
        entries: fixture.entries
    )

    #expect(result.error == .unknownOutcome)
    #expect(result.cooperativeError == .userActivityPaused)
    #expect(result.lastAcknowledgedAction == 0)
    #expect(result.outcomes == [
        ActionOutcome(index: 0, ok: true, error: nil),
        ActionOutcome(index: 1, ok: false, error: nil, inputDiagnostics: KeyboardFailureDiagnostics(
            stage: .beforeKeyDown, cause: nil, userActivityPaused: true,
            inputMayHaveStarted: false, cleanupFailed: false
        )),
    ])
    #expect(fixture.log.values == ["ax_press"])
}

@Test func pointerAndAXOnlyForegroundPlanIgnoresUnrelatedKeyboardFocusChangeEndToEnd() throws {
    let guardFocus = KeyboardFocusAuthority(
        identityToken: "ax:guard",
        bounds: CGRect(x: 120, y: 230, width: 200, height: 100),
        role: "AXTextArea",
        subrole: nil
    )
    let currentFocus = KeyboardFocusAuthority(
        identityToken: "ax:current",
        bounds: guardFocus.bounds,
        role: guardFocus.role,
        subrole: guardFocus.subrole
    )
    let fixture = MixedForegroundExecutionFixture(
        enabled: [.scroll],
        guardKeyboardFocus: guardFocus,
        currentKeyboardFocus: currentFocus
    )
    let actions: [NativeAction] = [
        .click(elementRef: "press"),
        foregroundSafeScroll(deltaY: 10),
    ]
    let context = DispatchContext(guardValue: fixture.guardValue)
    let plan = try fixture.dispatcher.plan(actions: actions, context: context)
    let entries = try fixture.dispatcher.consumeForegroundPlan(
        plan,
        authority: ForegroundPlanConsumptionAuthority(
            planRef: plan.planRef,
            snapshotID: fixture.guardValue.snapshotID,
            interactionMode: .foregroundTakeover,
            actions: actions,
            backends: plan.backends,
            guardValue: fixture.guardValue
        )
    )

    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.activity.lease,
        entries: entries
    )

    #expect(result.error == nil)
    #expect(result.lastAcknowledgedAction == 1)
    #expect(fixture.log.values == ["ax_press:0", "pid_scroll", "evidence:1"])
}

@Test func productionPIDStateFactoryKeepsOuterKeyWindowSeparateFromContainedFocusedRoot() {
    let target = containedOverlayTargetState()
    let factory = ForegroundPIDActionStateFactory(
        focusedAXWindow: {
            PIDAXFocusedWindowObservation(
                pid: target.pid,
                bounds: CGRect(x: 100.5, y: 200, width: 300, height: 200),
                axIdentity: target.axIdentity,
                title: "Editor"
            )
        },
        screenCaptureWindows: {
            [PIDScreenCaptureWindowObservation(
                pid: target.pid,
                windowID: target.windowID,
                bounds: target.bounds,
                title: "Editor",
                isOnScreen: true
            )]
        }
    )

    let state = factory.make(
        target: target,
        snapshotID: "snapshot",
        frontmostPID: target.pid
    )

    #expect(state.isKeyWindow)
    #expect(state.target.focusedRootPreference == .containedOverlay)
    #expect(state.target.focusedAXIdentity == 44)
    #expect(state.target.focusedAXBounds == CGRect(x: 140, y: 230, width: 220, height: 120))
}

@Test func productionPIDStateFactoryRejectsSameProcessDifferentOuterWindow() {
    let target = containedOverlayTargetState()
    let factory = ForegroundPIDActionStateFactory(
        focusedAXWindow: {
            PIDAXFocusedWindowObservation(
                pid: target.pid,
                bounds: CGRect(x: 450, y: 200, width: 300, height: 200),
                axIdentity: 55,
                title: "Other"
            )
        },
        screenCaptureWindows: {
            [
                PIDScreenCaptureWindowObservation(
                    pid: target.pid,
                    windowID: target.windowID,
                    bounds: target.bounds,
                    title: "Editor",
                    isOnScreen: true
                ),
                PIDScreenCaptureWindowObservation(
                    pid: target.pid,
                    windowID: 23,
                    bounds: CGRect(x: 450, y: 200, width: 300, height: 200),
                    title: "Other",
                    isOnScreen: true
                ),
            ]
        }
    )

    #expect(!factory.make(target: target, snapshotID: "snapshot", frontmostPID: target.pid).isKeyWindow)
}

@Test func productionPIDStateFactoryFailsClosedForMissingEmptyOrWhitespaceTitles() {
    let target = containedOverlayTargetState()
    let incompleteTitlePairs: [(ax: String?, screenCapture: String?)] = [
        (nil, "Editor"),
        ("", "Editor"),
        ("   \t", "Editor"),
        ("Editor", nil),
        ("Editor", ""),
        ("Editor", "  \n"),
        ("", ""),
        (" \t", " \t"),
    ]
    for titles in incompleteTitlePairs {
        let factory = ForegroundPIDActionStateFactory(
            focusedAXWindow: {
                PIDAXFocusedWindowObservation(
                    pid: target.pid,
                    bounds: target.bounds,
                    axIdentity: target.axIdentity,
                    title: titles.ax
                )
            },
            screenCaptureWindows: {
                [PIDScreenCaptureWindowObservation(
                    pid: target.pid,
                    windowID: target.windowID,
                    bounds: target.bounds,
                    title: titles.screenCapture,
                    isOnScreen: true
                )]
            }
        )

        #expect(!factory.make(target: target, snapshotID: "snapshot", frontmostPID: target.pid).isKeyWindow)
    }

    let valid = PIDScreenCaptureWindowObservation(
        pid: target.pid,
        windowID: target.windowID,
        bounds: target.bounds,
        title: "Editor",
        isOnScreen: true
    )
    let mixedTitleEvidence = ForegroundPIDActionStateFactory(
        focusedAXWindow: {
            PIDAXFocusedWindowObservation(
                pid: target.pid,
                bounds: target.bounds,
                axIdentity: target.axIdentity,
                title: "Editor"
            )
        },
        screenCaptureWindows: {
            [valid, PIDScreenCaptureWindowObservation(
                pid: target.pid,
                windowID: 23,
                bounds: target.bounds,
                title: nil,
                isOnScreen: true
            )]
        }
    )
    #expect(!mixedTitleEvidence.make(
        target: target,
        snapshotID: "snapshot",
        frontmostPID: target.pid
    ).isKeyWindow)
}

@Test(arguments: [false, true], [false, true])
func productionPIDStateFactoryMapsEqualBoundsWithOneTitleMatch(targetFirst: Bool, axHasSuffix: Bool) {
    let target = containedOverlayTargetState()
    let candidate = PIDScreenCaptureWindowObservation(
        pid: target.pid,
        windowID: target.windowID,
        bounds: target.bounds,
        title: axHasSuffix ? "Editor" : "Editor - Microsoft Edge",
        isOnScreen: true
    )
    let sibling = PIDScreenCaptureWindowObservation(
        pid: candidate.pid,
        windowID: 23,
        bounds: candidate.bounds,
        title: "Other",
        isOnScreen: true
    )
    let factory = ForegroundPIDActionStateFactory(
        focusedAXWindow: {
            PIDAXFocusedWindowObservation(
                pid: target.pid,
                bounds: target.bounds,
                axIdentity: target.axIdentity,
                title: axHasSuffix ? "Editor - Microsoft Edge" : "Editor"
            )
        },
        screenCaptureWindows: {
            targetFirst ? [candidate, sibling] : [sibling, candidate]
        }
    )

    #expect(factory.make(target: target, snapshotID: "snapshot", frontmostPID: target.pid).isKeyWindow)
}

@Test(arguments: [nil, "", " \t", "Editor", "Editor - Other App"] as [String?])
func productionPIDStateFactoryRejectsAmbiguousOrIncompleteEqualBoundsTitles(siblingTitle: String?) {
    let target = containedOverlayTargetState()
    let factory = ForegroundPIDActionStateFactory(
        focusedAXWindow: {
            PIDAXFocusedWindowObservation(pid: target.pid, bounds: target.bounds,
                axIdentity: target.axIdentity, title: "Editor")
        },
        screenCaptureWindows: {
            [PIDScreenCaptureWindowObservation(pid: target.pid, windowID: target.windowID,
                bounds: target.bounds, title: "Editor", isOnScreen: true),
             PIDScreenCaptureWindowObservation(pid: target.pid, windowID: 23,
                bounds: target.bounds, title: siblingTitle, isOnScreen: true)]
        }
    )
    #expect(!factory.make(target: target, snapshotID: "snapshot", frontmostPID: target.pid).isKeyWindow)
}

@Test(arguments: ["ax_identity", "native_id", "geometry", "pid"])
func productionPIDStateFactoryKeepsExactIdentityAfterTitleDisambiguation(mismatch: String) {
    let target = containedOverlayTargetState()
    let observedBounds = mismatch == "geometry" ? target.bounds.offsetBy(dx: 10, dy: 0) : target.bounds
    let observedPID = mismatch == "pid" ? target.pid + 1 : target.pid
    let factory = ForegroundPIDActionStateFactory(
        focusedAXWindow: {
            PIDAXFocusedWindowObservation(pid: observedPID, bounds: observedBounds,
                axIdentity: mismatch == "ax_identity" ? target.axIdentity + 1 : target.axIdentity,
                title: "Editor")
        },
        screenCaptureWindows: {
            [PIDScreenCaptureWindowObservation(pid: observedPID,
                windowID: mismatch == "native_id" ? target.windowID + 1 : target.windowID,
                bounds: observedBounds, title: "Editor", isOnScreen: true),
             PIDScreenCaptureWindowObservation(pid: observedPID, windowID: 23,
                bounds: observedBounds, title: "Other", isOnScreen: true)]
        }
    )
    #expect(!factory.make(target: target, snapshotID: "snapshot", frontmostPID: target.pid).isKeyWindow)
}

@Test(arguments: [false, true])
func productionPIDStateFactoryStopsAtDifferentKeyWindowAfterCompleteDelivery(sameBounds: Bool) throws {
    let targetState = containedOverlayTargetState()
    let guardValue = ActionGuard(
        pid: targetState.pid,
        windowID: targetState.windowID,
        bounds: targetState.bounds,
        axIdentity: targetState.axIdentity,
        focusedAXIdentity: targetState.focusedAXIdentity,
        focusedAXBounds: targetState.focusedAXBounds,
        focusedRootPreference: targetState.focusedRootPreference,
        snapshotID: "snapshot",
        interactionMode: .foregroundTakeover
    )
    let actions: [NativeAction] = [
        foregroundSafeScroll(deltaY: 10),
        .wait(durationMS: 0),
    ]
    let log = MixedForegroundLog()
    let performer = MixedForegroundPerformer(log: log, state: targetState)
    let poster = MixedForegroundPoster(log: log)
    let application = PIDTargetApplication(bundleIdentifier: "com.example.Editor", version: "1")
    let dispatcher = InputDispatcher(
        performer: performer,
        application: application,
        syntheticPolicy: foregroundTakeoverPlanningPolicy(
            application: application,
            pointerActions: [.scroll]
        ),
        backgroundTextInputSafety: InputDispatcherTextSafetyForTakeover(.safeASCIIKeyboardLayout)
    )
    let plan = try dispatcher.plan(actions: actions, context: DispatchContext(guardValue: guardValue))
    let target = WindowTarget(
        appRef: "app",
        windowRef: "window",
        pid: guardValue.pid,
        windowID: guardValue.windowID,
        bounds: guardValue.bounds,
        title: "Editor",
        axIdentity: guardValue.axIdentity,
        interactionMode: .background
    )
    let activity = TakeoverActivity(failArm: false)
    let activation = TakeoverActivation(currentFrontmostPID: 77)
    let cursor = TakeoverCursor(failShow: false)
    let vault = TakeoverPlanVault(authority: ForegroundTakeoverPlanAuthority(
        target: target,
        guardValue: guardValue,
        plan: plan,
        actions: actions,
        application: application,
        dispatcher: dispatcher
    ))
    let coordinator = ForegroundTakeoverCoordinator(
        activity: activity,
        activation: activation,
        cursor: cursor,
        frontmostPID: { activation.currentFrontmostPID },
        applicationExists: { $0 == 77 },
        revalidate: { $0 },
        takePlan: vault.take
    )
    let token = try coordinator.begin(
        target: target,
        snapshotID: guardValue.snapshotID,
        planRef: plan.planRef,
        stageDigest: plan.stageDigest
    )
    _ = try coordinator.consume(token.ref, actions: actions)
    let execution = try coordinator.executionAuthority(
        for: token.ref,
        snapshotID: guardValue.snapshotID,
        planRef: plan.planRef,
        consumedGuard: guardValue
    )
    let consumption = ForegroundPlanConsumptionAuthority(
        planRef: plan.planRef,
        snapshotID: guardValue.snapshotID,
        interactionMode: .foregroundTakeover,
        actions: actions,
        backends: plan.backends,
        guardValue: guardValue
    )
    let entries = try dispatcher.consumeForegroundPlan(plan, authority: consumption)
    let approvedAXWindow = PIDAXFocusedWindowObservation(
        pid: guardValue.pid,
        bounds: guardValue.bounds,
        axIdentity: guardValue.axIdentity,
        title: "Editor"
    )
    let changedAXWindow = PIDAXFocusedWindowObservation(
        pid: guardValue.pid,
        bounds: sameBounds ? guardValue.bounds : CGRect(x: 450, y: 200, width: 300, height: 200),
        axIdentity: 44,
        title: "Other"
    )
    let approvedScreenWindow = PIDScreenCaptureWindowObservation(
        pid: guardValue.pid,
        windowID: guardValue.windowID,
        bounds: guardValue.bounds,
        title: "Editor",
        isOnScreen: true
    )
    let changedScreenWindow = PIDScreenCaptureWindowObservation(
        pid: guardValue.pid,
        windowID: 23,
        bounds: changedAXWindow.bounds,
        title: "Other",
        isOnScreen: true
    )
    var inputDelivered = false
    poster.onPost = { inputDelivered = true }
    let stateFactory = ForegroundPIDActionStateFactory(
        focusedAXWindow: { inputDelivered ? changedAXWindow : approvedAXWindow },
        screenCaptureWindows: { [approvedScreenWindow, changedScreenWindow] }
    )
    let validator = ExactPIDActionGuardValidator {
        stateFactory.make(
            target: try performer.currentTargetState(),
            snapshotID: guardValue.snapshotID,
            frontmostPID: guardValue.pid
        )
    }
    var evidenceCalls = 0
    let pidExecutor = PIDTargetedActionExecutor(
        poster: poster,
        compatibility: PIDInputCompatibilityRegistry(cells: [PIDInputCompatibilityCell(
            bundleIdentifier: application.bundleIdentifier,
            version: application.version,
            action: .scroll
        )]),
        activity: activity,
        validator: validator,
        element: { reference, snapshotID in
            performer.element(reference: reference, snapshotID: snapshotID)
        },
        evidence: { _, expected in
            evidenceCalls += 1
            log.values.append("evidence:0")
            return (try? validator.revalidate(expected: expected, point: nil)) != nil
        },
        delay: { _ in }
    )

    let result = ForegroundPlanExecutor(
        activity: activity,
        performer: performer,
        pidExecutor: pidExecutor
    ).run(
        expected: guardValue,
        application: application,
        lease: execution.lease,
        entries: entries
    )

    // The one scroll was delivered; the changed key window is still rejected
    // as authority for any further action, including the queued wait.
    #expect(!stateFactory.make(target: targetState, snapshotID: guardValue.snapshotID,
        frontmostPID: guardValue.pid).isKeyWindow)
    #expect(result.error == nil)
    #expect(result.cooperativeError == .observationRequired)
    #expect(result.lastAcknowledgedAction == 0)
    #expect(result.outcomes == [ActionOutcome(index: 0, ok: true, error: nil, observationRequired: true)])
    #expect(poster.events.count == 1)
    #expect(evidenceCalls == 1)
    #expect(log.values == ["pid_scroll", "evidence:0"])
    #expect(activity.fragmentsStarted == 1)
    #expect(activity.fragmentsEnded == 1)

    _ = try coordinator.end(token.ref, allowRestore: false)
    #expect(activity.disarmCount == 1)
    #expect(cursor.hideCount == 1)
    #expect(cursor.closeCount == 1)
    #expect(throws: TakeoverError.alreadyConsumed) {
        try coordinator.consume(token.ref, actions: actions)
    }
    #expect(throws: TakeoverError.alreadyConsumed) {
        try coordinator.end(token.ref, allowRestore: false)
    }
    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try dispatcher.consumeForegroundPlan(plan, authority: consumption)
    }
    #expect(poster.events.count == 1)
}

private func containedOverlayTargetState() -> ActionTargetState {
    ActionTargetState(
        pid: 11,
        windowID: 22,
        bounds: CGRect(x: 100, y: 200, width: 300, height: 200),
        axIdentity: 33,
        focusedAXIdentity: 44,
        focusedAXBounds: CGRect(x: 140, y: 230, width: 220, height: 120),
        focusedRootPreference: .containedOverlay
    )
}

@Test func replacementAXObservationCheckpointStopsBatchSuffix() throws {
    let f = MixedForegroundExecutionFixture(enabled: [])
    f.performer.requiresObservation = true
    let source = NativeAction.click(elementRef: "press")
    let target = try #require(f.performer.element(reference: "press", snapshotID: "snapshot"))
    let entries = [
        PlannedDispatchEntry(sourceIndex: 0, source: source, backend: .axPress, actionClass: .press,
            resolved: ResolvedAction(source: source, method: .accessibilityPress,
                screenPoint: nil, endScreenPoint: nil, element: target.element, verifiedElement: target),
            pointerSafeRegion: nil, targetKeyboardFocus: nil),
        PlannedDispatchEntry(sourceIndex: 1, source: .wait(durationMS: 0), backend: .wait, actionClass: nil,
            resolved: ResolvedAction(source: .wait(durationMS: 0), method: .wait,
                screenPoint: nil, endScreenPoint: nil, element: nil), pointerSafeRegion: nil, targetKeyboardFocus: nil),
    ]
    let result = f.executor.run(expected: f.guardValue, application: f.application,
        lease: f.activity.lease, entries: entries)
    #expect(result.error == nil && result.cooperativeError == .observationRequired)
    #expect(result.lastAcknowledgedAction == 0 && result.outcomes.count == 1)
    #expect(result.outcomes.first?.observationRequired == true)
    #expect(result.outcomes.first?.effectVerification == .unverified)
    #expect(!f.log.values.contains(where: { $0.hasPrefix("wait:") }))
}

@Test func replacementConsumeIsReadOnlyEvenWhenUserPausesDuringStateRead() throws {
    let f = try runReplacementForegroundCase("consume_pause")
    #expect(f.writes == 0 && f.value == "old")
    #expect(f.focusWritesWhilePaused == 0 && f.fallbacks == 0)
    #expect(f.result.error == nil && f.result.cooperativeError == .userActivityPaused)
    #expect(f.result.lastAcknowledgedAction == -1 && f.result.outcomes.count <= 1)
    #expect(f.gates == 1) // The event gate is entered once and rejects before mutation.
}

@Test(arguments: ["pre_read", "last_target_validation", "before_focus"])
func replacementPausedDuringPreparationNeverStartsWrite(scenario: String) throws {
    let f = try runReplacementForegroundCase(scenario)
    #expect(f.writes == 0 && f.value == "old")
    #expect(f.result.error == nil && f.result.cooperativeError == .userActivityPaused)
    #expect(f.result.lastAcknowledgedAction == -1 && f.result.outcomes.count <= 1)
    #expect(f.gates == 1 && f.fallbacks == 0)
    #expect(f.focusWritesWhilePaused == 0)
}

@Test(arguments: ["setter", "post_read"])
func replacementPausedAfterWriteIsUnknownWithoutSecondDelivery(scenario: String) throws {
    let f = try runReplacementForegroundCase(scenario)
    #expect(f.writes == 1 && f.value == "new 中文")
    #expect(f.result.error == .unknownOutcome && f.result.cooperativeError == .userActivityPaused)
    #expect(f.result.lastAcknowledgedAction == -1 && f.result.outcomes.count == 1)
    #expect(f.gates == 1 && f.fallbacks == 0)
}

@Test(arguments: ["secure", "disabled", "ime", "missing_target_validator"])
func replacementExecutionRejectsUnsafeOrUnvalidatedTarget(scenario: String) throws {
    let f = try runReplacementForegroundCase(scenario)
    #expect(f.writes == 0 && f.value == "old")
    #expect(f.result.error != nil && f.result.error != .unknownOutcome)
    #expect(f.result.lastAcknowledgedAction == -1 && f.result.outcomes.count <= 1)
    #expect(f.fallbacks == 0)
}

@Test(arguments: ["verified", "clear", "noop", "mismatch", "missing_readback"])
func replacementRealConsumerPreservesVerificationAndStopsUnverifiedSuffix(scenario: String) throws {
    let f = try runReplacementForegroundCase(scenario)
    let pending = scenario == "mismatch" || scenario == "missing_readback"
    #expect(f.writes == (scenario == "noop" ? 0 : 1))
    #expect(f.result.error == nil)
    #expect(f.result.cooperativeError == (pending ? .observationRequired : nil))
    #expect(f.result.outcomes.first?.effectVerification == (pending ? .unverified : scenario == "noop" ? .noop : .verified))
    #expect(f.result.lastAcknowledgedAction == (pending ? 0 : 1))
    #expect(f.result.outcomes.count == (pending ? 1 : 2))
    #expect(f.fallbacks == 0)
    if scenario == "clear" { #expect(f.value.isEmpty) }
}

@Test(arguments: ["refresh_ax", "refresh_keyboard", "read_ax", "read_keyboard", "safe_ax", "safe_keyboard"])
func InputSourceRefreshPauseCannotAcquireFocusDuringConsume(scenario: String) throws {
    let base = MixedForegroundExecutionFixture(enabled: [])
    let activity = base.activity
    let ax = AXUIElementCreateApplication(11)
    let target = ActionElement(element: ax, identityToken: "field",
        bounds: CGRect(x: 10, y: 10, width: 30, height: 20),
        roleResult: .init(value: "AXTextField", status: .complete),
        subroleResult: .init(value: nil, status: .complete), enabled: true,
        actionNames: .complete([]))
    let focusedTarget = ActionElement(element: ax, identityToken: "field",
        bounds: target.bounds.offsetBy(dx: base.guardValue.bounds.minX, dy: base.guardValue.bounds.minY),
        roleResult: target.roleResult, subroleResult: target.subroleResult,
        enabled: true, actionNames: .complete([]))
    var consuming = false
    var consumeReads = 0
    var focusWritesWhilePaused = 0
    var textWrites = 0
    let gate = KeyboardInputSourceReadGate(isMainThread: { true }, refresh: {
        if consuming && scenario.hasPrefix("refresh_") { activity.paused = true }
    })
    let safety = SystemBackgroundTextInputSafetyDetector(snapshot: {
        BackgroundTextInputSourceSnapshot(category: "TISCategoryKeyboardInputSource",
            sourceType: "TISTypeKeyboardLayout", isASCIICapable: true)
    }, readGate: gate)
    let performer = SystemActionPerformer(state: {
        if consuming {
            consumeReads += 1
            if scenario.hasPrefix("read_"), consumeReads >= 1 { activity.paused = true }
        }
        return base.performer.state
    }, lookup: { _, _ in target },
        selectedTextWriter: SystemAXSelectedTextWriter(isSettable: { _ in
            (.success, scenario.hasSuffix("_ax"))
        }, setValue: { _, _ in textWrites += 1; return .success }),
        focusedKeyboard: { _ in focusedTarget }, focusAcquisition: { _, _ in
            if activity.paused { focusWritesWhilePaused += 1 }
            return focusedTarget
        }, textInputSafety: { safety.detect() })
    let dispatcher = InputDispatcher(performer: performer, application: base.application,
        syntheticPolicy: foregroundTakeoverPlanningPolicy(application: base.application, pointerActions: []),
        backgroundTextInputSafety: safety)
    let source = try NativeAction.parse(.object([
        "type": .string("type"), "element_ref": .string("field"), "text": .string("test"),
    ]))
    let plan = try dispatcher.plan(actions: [source], context: DispatchContext(guardValue: base.guardValue))
    #expect(plan.backends == [scenario.hasSuffix("_ax") ? .axSelectedText : .foregroundKeyboard])
    consuming = true
    var leaseChecks = 0
    do {
        let entries = try dispatcher.consumeForegroundPlan(plan, authority: ForegroundPlanConsumptionAuthority(
            planRef: plan.planRef, snapshotID: "snapshot", interactionMode: .foregroundTakeover,
            actions: [source], backends: plan.backends, guardValue: base.guardValue), validateFocusMutation: {
                leaseChecks += 1
                try activity.assertNotPaused(lease: activity.lease)
            })
        #expect(scenario.hasPrefix("safe_"))
        #expect(entries.count == 1)
    } catch is UserActivityMonitoringError {
        #expect(!scenario.hasPrefix("safe_"))
    }
    #expect(leaseChecks == 1)
    #expect(activity.paused == !scenario.hasPrefix("safe_"))
    #expect(focusWritesWhilePaused == 0)
    #expect(textWrites == 0)
    #expect(base.poster.events.isEmpty)
}

/// Uses the production performer AND AXValue replacer; only native I/O and live state are injected.
private func runReplacementForegroundCase(_ scenario: String) throws -> (
    result: PIDTargetedActionResult, writes: Int, value: String, gates: Int, fallbacks: Int, focusWritesWhilePaused: Int
) {
    let base = MixedForegroundExecutionFixture(enabled: [])
    let activity = base.activity
    let ax = AXUIElementCreateApplication(11)
    let bounds = CGRect(x: 10, y: 10, width: 30, height: 20)
    func field(_ role: String = "AXTextField", enabled: Bool = true) -> ActionElement {
        ActionElement(element: ax, identityToken: "field", bounds: bounds,
            roleResult: .init(value: role, status: .complete),
            subroleResult: .init(value: nil, status: .complete), enabled: enabled,
            actionNames: .complete([]))
    }
    let target = field()
    var current = target
    var safety = BackgroundTextInputSafety.safeASCIIKeyboardLayout
    var reads = 0, writes = 0, validations = 0, fallbacks = 0
    var stateReads = 0, focusWritesWhilePaused = 0
    var consuming = false
    var value = "old"
    let writer = SystemAXTextValueReplacer(clock: { 0 },
        setMessagingTimeout: { _, _ in .success }, isSettable: { _ in (.success, true) },
        copyValue: { _ in
            reads += 1
            if reads == 1 {
                if scenario == "pre_read" { activity.paused = true }
                if scenario == "secure" { current = field("AXSecureTextField") }
                if scenario == "disabled" { current = field(enabled: false) }
                if scenario == "ime" { safety = .imeOrCandidate }
            } else {
                if scenario == "post_read" { activity.paused = true }
                if scenario == "missing_readback" { return (.cannotComplete, nil) }
                if scenario == "mismatch" { return (.success, "not the requested value" as CFString) }
            }
            return (.success, value as CFString)
        }, setValue: { _, text in
            writes += 1
            value = text as String
            if scenario == "setter" { activity.paused = true }
            return .success
        })
    let validate: ((ActionElement) throws -> Void)? = scenario == "missing_target_validator" ? nil : { expected in
        try validateReplacementField(expected: expected, current: current, belongs: { CFEqual($0, ax) })
        validations += 1
        if scenario == "last_target_validation", validations == 3 { activity.paused = true }
    }
    let performer = SystemActionPerformer(state: {
        stateReads += 1
        if scenario == "before_focus", stateReads == 4 { activity.paused = true }
        if consuming { activity.paused = true }
        return base.performer.state
    }, lookup: { _, _ in current },
        selectedTextWriter: SystemAXSelectedTextWriter(isSettable: { _ in (.success, true) },
            setValue: { _, _ in fallbacks += 1; return .success }),
        textValueReplacer: writer, replacementTargetValidation: validate,
        focusedKeyboard: { _ in current }, focusAcquisition: { _, _ in
            if activity.paused { focusWritesWhilePaused += 1 }
            return current
        }, textInputSafety: { safety })
    let pid = PIDTargetedActionExecutor(poster: base.poster,
        compatibility: PIDInputCompatibilityRegistry(cells: []), activity: activity,
        validator: ExactPIDActionGuardValidator(state: {
            PIDActionTargetState(target: base.performer.state, snapshotID: "snapshot", isFrontmost: true, isKeyWindow: true)
        }), element: { _, _ in current }, evidence: { _, _ in false }, delay: { _ in })
    let executor = ForegroundPlanExecutor(activity: activity, performer: performer, pidExecutor: pid)
    let source = try NativeAction.parse(.object([
        "type": .string("type"), "element_ref": .string("field"), "replace": .bool(true),
        "text": .string(scenario == "clear" ? "" : scenario == "noop" ? "old" : "new 中文"),
    ]))
    var entries = [
        PlannedDispatchEntry(sourceIndex: 0, source: source, backend: .axSelectedText, actionClass: .text,
            resolved: ResolvedAction(source: source, method: .accessibilityText,
                screenPoint: nil, endScreenPoint: nil, element: ax, verifiedElement: target),
            pointerSafeRegion: nil, targetKeyboardFocus: nil),
        PlannedDispatchEntry(sourceIndex: 1, source: .wait(durationMS: 0), backend: .wait, actionClass: nil,
            resolved: ResolvedAction(source: .wait(durationMS: 0), method: .wait,
                screenPoint: nil, endScreenPoint: nil, element: nil), pointerSafeRegion: nil, targetKeyboardFocus: nil),
    ]
    if scenario == "consume_pause" {
        let dispatcher = InputDispatcher(performer: performer, application: base.application,
            syntheticPolicy: foregroundTakeoverPlanningPolicy(application: base.application, pointerActions: []),
            backgroundTextInputSafety: InputDispatcherTextSafetyForTakeover(.safeASCIIKeyboardLayout))
        let actions = [source, NativeAction.wait(durationMS: 0)]
        let plan = try dispatcher.plan(actions: actions, context: DispatchContext(guardValue: base.guardValue))
        consuming = true
        entries = try dispatcher.consumeForegroundPlan(plan, authority: ForegroundPlanConsumptionAuthority(
            planRef: plan.planRef, snapshotID: "snapshot", interactionMode: .foregroundTakeover,
            actions: actions, backends: plan.backends, guardValue: base.guardValue))
        consuming = false
        #expect(activity.paused)
        #expect(entries.count == 2 && entries.first?.source.replace == true)
    }
    let result = executor.run(expected: base.guardValue, application: base.application,
        lease: activity.lease, entries: entries)
    #expect(base.poster.events.isEmpty)
    return (result, writes, value, activity.eventGateCalls, fallbacks, focusWritesWhilePaused)
}

private final class MixedForegroundExecutionFixture {
    let guardValue: ActionGuard
    let application = PIDTargetApplication(bundleIdentifier: "com.example.Editor", version: "1")
    let log = MixedForegroundLog()
    let performer: MixedForegroundPerformer
    let activity = MixedForegroundActivity()
    let poster: MixedForegroundPoster
    let dispatcher: InputDispatcher
    let executor: ForegroundPlanExecutor

    init(
        enabled: Set<DispatchActionClass>,
        guardKeyboardFocus: KeyboardFocusAuthority? = nil,
        currentKeyboardFocus: KeyboardFocusAuthority? = nil,
        pointerEvidence: Bool = true,
        now: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
        waitSleeper: @escaping (TimeInterval) -> Void = { Thread.sleep(forTimeInterval: $0) },
        effectReader: any AXEffectReading = TakeoverEffectReader([])
    ) {
        guardValue = ActionGuard(
            pid: 11,
            windowID: 22,
            bounds: CGRect(x: 100, y: 200, width: 300, height: 200),
            axIdentity: 33,
            keyboardFocus: guardKeyboardFocus,
            snapshotID: "snapshot",
            interactionMode: .foregroundTakeover
        )
        performer = MixedForegroundPerformer(
            log: log,
            state: ActionTargetState(
                pid: 11,
                windowID: 22,
                bounds: CGRect(x: 100, y: 200, width: 300, height: 200),
                axIdentity: 33,
                keyboardFocus: currentKeyboardFocus
            )
        )
        poster = MixedForegroundPoster(log: log)
        dispatcher = InputDispatcher(
            performer: performer,
            application: application,
            syntheticPolicy: foregroundTakeoverPlanningPolicy(
                application: application,
                pointerActions: [.click, .scroll, .drag]
            ),
            backgroundTextInputSafety: InputDispatcherTextSafetyForTakeover(.safeASCIIKeyboardLayout)
        )
        let application = self.application
        let compatibility = PIDInputCompatibilityRegistry(cells: enabled.map {
            PIDInputCompatibilityCell(
                bundleIdentifier: application.bundleIdentifier,
                version: application.version,
                action: $0
            )
        })
        let state = guardValue
        let executionLog = log
        let actionPerformer = performer
        let pid = PIDTargetedActionExecutor(
            poster: poster,
            compatibility: compatibility,
            activity: activity,
            validator: ExactPIDActionGuardValidator(state: {
                PIDActionTargetState(
                    target: ActionTargetState(
                        pid: state.pid,
                        windowID: state.windowID,
                        bounds: state.bounds,
                        axIdentity: state.axIdentity,
                        focusedAXIdentity: state.focusedAXIdentity,
                        focusedAXBounds: state.focusedAXBounds,
                        focusedRootPreference: state.focusedRootPreference
                    ),
                    snapshotID: state.snapshotID,
                    isFrontmost: true,
                    isKeyWindow: true
                )
            }),
            element: { reference, snapshotID in
                actionPerformer.element(reference: reference, snapshotID: snapshotID)
            },
            evidence: { entry, expected in
                executionLog.values.append("evidence:\(entry.sourceIndex)")
                guard pointerEvidence else { return false }
                guard let targetState = try? actionPerformer.currentTargetState() else { return false }
                let current = PIDActionTargetState(
                    target: targetState,
                    snapshotID: expected.snapshotID,
                    isFrontmost: true,
                    isKeyWindow: true
                )
                return (try? ExactPIDActionGuardValidator(state: { current }).revalidate(expected: expected, point: nil)) != nil
            },
            delay: { _ in }
        )
        executor = ForegroundPlanExecutor(activity: activity, performer: performer, pidExecutor: pid,
            now: now, waitSleeper: waitSleeper, effectReader: effectReader)
    }
}

@Test func foregroundTextPredownTransitionRequiresObservationBeforeSuffix() {
    for transition in [2, 4] {
        let fixture = MixedForegroundKeyboardFixture(keyboardEnabled: true, textTransitionAt: transition)
        let result = fixture.executor.run(expected: fixture.guardValue,
            application: fixture.application, lease: fixture.activity.lease, entries: fixture.entries)
        #expect(result.error == nil)
        #expect(result.cooperativeError == .observationRequired)
        #expect(result.lastAcknowledgedAction == 0)
        #expect(result.outcomes == [ActionOutcome(index: 0, ok: true, error: nil, observationRequired: true)])
        let units: [[UInt16]] = [[65], [0x4e2d], [66]]
        #expect(fixture.poster.events == units.flatMap { [SyntheticInputEvent.unicodeKeyDown($0), .unicodeKeyUp($0)] })
        #expect(!fixture.log.values.contains("wait"))
        #expect(!fixture.log.values.contains("keyboard_down"))
        #expect(fixture.activity.fragmentsEnded == 1)
    }
}

private final class MixedForegroundKeyboardFixture {
    let application = PIDTargetApplication(bundleIdentifier: "com.example.Editor", version: "1")
    let focusState = MixedForegroundKeyboardFocusState()
    let guardValue: ActionGuard
    let log = MixedForegroundLog()
    let performer: MixedForegroundKeyboardPerformer
    let activity = MixedForegroundActivity()
    let poster: MixedForegroundKeyboardPoster
    let executor: ForegroundPlanExecutor
    let entries: [PlannedDispatchEntry]

    init(
        keyboardEnabled: Bool,
        changeFocusAfterAX: Bool = false,
        pointerFocusChange: Bool = false,
        keyboardPrefixFocusChange: Bool = false,
        failKeyboardDownBeforeInput: Bool = false,
        pauseBeforeKeyboardEvent: Bool = false,
        textTransitionAt: Int? = nil
    ) {
        let focus = focusState.value
        guardValue = ActionGuard(
            pid: 11,
            windowID: 22,
            bounds: CGRect(x: 100, y: 200, width: 300, height: 200),
            axIdentity: 33,
            keyboardFocus: focus,
            snapshotID: "snapshot",
            interactionMode: .foregroundTakeover
        )
        performer = MixedForegroundKeyboardPerformer(
            guardValue: guardValue,
            focusState: focusState,
            log: log,
            changeFocusAfterAX: changeFocusAfterAX
        )
        poster = MixedForegroundKeyboardPoster(log: log)
        poster.failKeyboardDownBeforeInput = failKeyboardDownBeforeInput
        let expectedGuard = guardValue
        let actionPerformer = performer
        let sharedFocusState = focusState
        let sharedActivity = activity
        let validator = ExactPIDActionGuardValidator {
            PIDActionTargetState(
                target: try actionPerformer.currentTargetState(),
                snapshotID: expectedGuard.snapshotID,
                isFrontmost: true,
                isKeyWindow: true
            )
        }
        let registry = keyboardEnabled ? PIDInputCompatibilityRegistry(cells: [
            PIDInputCompatibilityCell(
                bundleIdentifier: application.bundleIdentifier,
                version: application.version,
                backend: .foregroundKeyboard,
                action: .text,
                allowedKeyChords: [ApprovedKeyChord(rawValue: "return")!],
                allowTextEntry: textTransitionAt != nil
            ),
        ]) : PIDInputCompatibilityRegistry()
        if pointerFocusChange {
            poster.onMouseUp = {
                sharedFocusState.value = KeyboardFocusAuthority(
                    identityToken: "ax:changed",
                    bounds: sharedFocusState.value.bounds,
                    role: sharedFocusState.value.role,
                    subrole: sharedFocusState.value.subrole
                )
            }
        }
        if keyboardPrefixFocusChange {
            poster.onKeyboardUp = {
                sharedFocusState.value = KeyboardFocusAuthority(
                    identityToken: "ax:changed",
                    bounds: sharedFocusState.value.bounds,
                    role: sharedFocusState.value.role,
                    subrole: sharedFocusState.value.subrole
                )
            }
        }
        let pidRegistry = pointerFocusChange ? PIDInputCompatibilityRegistry(cells: [
            PIDInputCompatibilityCell(
                bundleIdentifier: application.bundleIdentifier,
                version: application.version,
                backend: .pidPointer,
                action: .click
            ),
        ]) : PIDInputCompatibilityRegistry()
        let pidExecutor = PIDTargetedActionExecutor(
            poster: poster,
            compatibility: pidRegistry,
            activity: activity,
            validator: validator,
            element: { reference, snapshotID in
                actionPerformer.pointerElement(reference: reference, snapshotID: snapshotID)
            },
            evidence: { _, expected in
                (try? validator.revalidate(expected: expected, point: nil)) != nil
            },
            delay: { _ in }
        )
        var continuationCalls = 0
        let continuation: ForegroundKeyboardExecutor.ContinuingTextFocusLookup?
        if let textTransitionAt {
            continuation = { _, wanted in
                continuationCalls += 1
                return KeyboardTextContinuationObservation(focus: .authority(wanted),
                    observationRequired: continuationCalls == textTransitionAt)
            }
        } else { continuation = nil }
        let keyboardExecutor = ForegroundKeyboardExecutor(
            poster: poster,
            compatibility: registry,
            activity: activity,
            validator: validator,
            secureInput: MixedForegroundKeyboardSecureInput(),
            heldInputs: HeldInputRegistry(),
            focus: {
                sharedFocusState.lookups += 1
                if pauseBeforeKeyboardEvent, sharedFocusState.lookups == 2 {
                    sharedActivity.paused = true
                }
                return .authority(sharedFocusState.value)
            },
            continuingTextFocus: continuation,
            experimentalUnicodeChunkGraphemes: 1
        )
        executor = ForegroundPlanExecutor(
            activity: activity,
            performer: performer,
            pidExecutor: pidExecutor,
            keyboardExecutor: keyboardExecutor
        )
        let firstEntry = textTransitionAt != nil
            ? PlannedDispatchEntry(sourceIndex: 0, source: .type(text: "A中B"),
                backend: .foregroundKeyboard, actionClass: .text, resolved: nil)
            : keyboardPrefixFocusChange
            ? PlannedDispatchEntry(
                sourceIndex: 0,
                source: .keypress(key: "return"),
                backend: .foregroundKeyboard,
                actionClass: .text,
                resolved: nil
            )
            : pointerFocusChange
            ? PlannedDispatchEntry(
                sourceIndex: 0,
                source: .click(x: 10, y: 10, within: "pointer"),
                backend: .pidPointer,
                actionClass: .click,
                resolved: nil,
                pointerSafeRegion: {
                    let pointer = actionPerformer.pointerElement(
                        reference: "pointer",
                        snapshotID: expectedGuard.snapshotID
                    )!
                    return PointerSafeRegionAuthority(
                        reference: "pointer",
                        identityToken: pointer.identityToken,
                        bounds: pointer.bounds
                    )
                }()
            )
            : PlannedDispatchEntry(
                sourceIndex: 0,
                source: .click(elementRef: "press"),
                backend: .axPress,
                actionClass: .press,
                resolved: ResolvedAction(
                    source: .click(elementRef: "press"),
                    method: .accessibilityPress,
                    screenPoint: nil,
                    endScreenPoint: nil,
                    element: AXUIElementCreateApplication(11)
                )
            )
        entries = [
            firstEntry,
            PlannedDispatchEntry(
                sourceIndex: 1,
                source: .keypress(key: "return"),
                backend: .foregroundKeyboard,
                actionClass: .text,
                resolved: nil
            ),
            PlannedDispatchEntry(
                sourceIndex: 2,
                source: .wait(durationMS: 0),
                backend: .wait,
                actionClass: nil,
                resolved: ResolvedAction(
                    source: .wait(durationMS: 0),
                    method: .wait,
                    screenPoint: nil,
                    endScreenPoint: nil,
                    element: nil
                )
            ),
        ]
    }
}

private final class MixedForegroundKeyboardFocusState {
    var lookups = 0
    var value = KeyboardFocusAuthority(
        identityToken: "ax:focus",
        bounds: CGRect(x: 120, y: 230, width: 200, height: 100),
        role: "AXTextArea",
        subrole: nil
    )
}

private final class MixedForegroundKeyboardPerformer: ActionProviding {
    let guardValue: ActionGuard
    let focusState: MixedForegroundKeyboardFocusState
    let log: MixedForegroundLog
    let changeFocusAfterAX: Bool
    private let pointerAXElement = AXUIElementCreateApplication(11)

    init(
        guardValue: ActionGuard,
        focusState: MixedForegroundKeyboardFocusState,
        log: MixedForegroundLog,
        changeFocusAfterAX: Bool
    ) {
        self.guardValue = guardValue
        self.focusState = focusState
        self.log = log
        self.changeFocusAfterAX = changeFocusAfterAX
    }

    func currentTargetState() throws -> ActionTargetState {
        ActionTargetState(
            pid: guardValue.pid,
            windowID: guardValue.windowID,
            bounds: guardValue.bounds,
            axIdentity: guardValue.axIdentity,
            keyboardFocus: focusState.value
        )
    }

    func element(reference _: String, snapshotID _: String) -> ActionElement? { nil }

    func pointerElement(reference: String, snapshotID: String) -> ActionElement? {
        guard reference == "pointer", snapshotID == guardValue.snapshotID else { return nil }
        return ActionElement(
            element: pointerAXElement,
            bounds: CGRect(x: 1, y: 1, width: 98, height: 98),
            role: kAXGroupRole as String,
            subrole: nil,
            actions: []
        )
    }

    func perform(_ action: ResolvedAction) throws -> ActionPerformance {
        switch action.method {
        case .accessibilityPress:
            log.values.append("ax_press")
            if changeFocusAfterAX {
                focusState.value = KeyboardFocusAuthority(
                    identityToken: "ax:changed",
                    bounds: focusState.value.bounds,
                    role: focusState.value.role,
                    subrole: focusState.value.subrole
                )
            }
            return ActionPerformance(inputStarted: true)
        case .wait:
            log.values.append("wait")
            return ActionPerformance(inputStarted: false)
        default:
            throw ActionPerformFailure(error: .invalidAction, inputStarted: false)
        }
    }
}

private final class MixedForegroundKeyboardPoster: PIDTargetedInputPosting {
    let log: MixedForegroundLog
    var events: [SyntheticInputEvent] = []
    var onMouseUp: (() -> Void)?
    var onKeyboardUp: (() -> Void)?
    var failKeyboardDownBeforeInput = false

    init(log: MixedForegroundLog) { self.log = log }

    func preflight(targetPID: pid_t, marker: UInt64) -> Bool { targetPID == 11 && marker != 0 }

    func post(_ event: SyntheticInputEvent, to targetPID: pid_t, marker: UInt64) throws {
        guard targetPID == 11, marker != 0 else {
            throw SyntheticInputFailure(error: .permissionDenied, inputStarted: false)
        }
        if case .virtualKeyDown = event, failKeyboardDownBeforeInput {
            throw SyntheticInputFailure(error: .permissionDenied, inputStarted: false)
        }
        events.append(event)
        switch event {
        case .mouseDown: log.values.append("pointer_down")
        case .mouseUp:
            log.values.append("pointer_up")
            onMouseUp?()
        case .virtualKeyDown: log.values.append("keyboard_down")
        case .unicodeKeyDown: log.values.append("unicode_down")
        case .unicodeKeyUp: log.values.append("unicode_up")
        case .virtualKeyUp:
            log.values.append("keyboard_up")
            onKeyboardUp?()
            onKeyboardUp = nil
        default: Issue.record("mixed keyboard fixture received non-keyboard input")
        }
    }
}

private struct MixedForegroundKeyboardSecureInput: SecureInputDetecting {
    func isSecureInputEnabled() -> Bool { false }
}

private final class MixedForegroundLog {
    var values: [String] = []
}

private final class MixedForegroundPerformer: ActionProviding {
    var requiresObservation = false
    let log: MixedForegroundLog
    let state: ActionTargetState
    private let pointerAXElement = AXUIElementCreateApplication(11)
    var scrollElement = AXUIElementCreateApplication(12)
    var scrollIdentity = "scroll"
    var scrollBounds = CGRect(x: 10, y: 10, width: 20, height: 60)
    var scrollEnabled: Bool? = true
    var scrollActionNames = ActionNameResults.complete([
        kAXIncrementAction as String,
        kAXDecrementAction as String,
    ])
    var onWait: (() -> Void)?
    var onScrollLookup: (() -> Void)?
    var axScrollPerformCalls = 0
    var axScrollFailure: ActionPerformFailure?
    init(
        log: MixedForegroundLog,
        state: ActionTargetState = ActionTargetState(
            pid: 11,
            windowID: 22,
            bounds: CGRect(x: 100, y: 200, width: 300, height: 200),
            axIdentity: 33
        )
    ) {
        self.log = log
        self.state = state
    }
    func currentTargetState() throws -> ActionTargetState { state }
    func element(reference: String, snapshotID: String) -> ActionElement? {
        guard snapshotID == "snapshot",
              reference == "press" || reference == "text" || reference == "pointer" || reference == "scroll"
        else { return nil }
        if reference == "scroll" {
            onScrollLookup?()
            return ActionElement(
                element: scrollElement,
                identityToken: scrollIdentity,
                bounds: scrollBounds,
                roleResult: .init(value: kAXScrollBarRole as String, status: .complete),
                subroleResult: .init(value: nil, status: .complete),
                enabled: scrollEnabled,
                actionNames: scrollActionNames
            )
        }
        return ActionElement(
            element: reference == "pointer" ? pointerAXElement : AXUIElementCreateApplication(11),
            bounds: reference == "pointer"
                ? CGRect(x: 1, y: 1, width: 98, height: 98)
                : CGRect(x: 10, y: 10, width: 20, height: 20),
            role: reference == "press"
                ? kAXButtonRole as String
                : reference == "text" ? kAXTextFieldRole as String : kAXGroupRole as String,
            subrole: nil,
            actions: reference == "press" ? [kAXPressAction as String] : []
        )
    }
    func pointerElement(reference: String, snapshotID: String) -> ActionElement? {
        element(reference: reference, snapshotID: snapshotID)
    }
    func preflightAXTextMutation(_: ActionElement) -> AXTextMutationPreflight { .settable }
    func focusedKeyboardElement(matching expected: ActionElement?) throws -> ActionElement {
        guard let expected else { throw ActionExecutionError.targetNotFrontmost }
        return expected
    }
    func perform(_ action: ResolvedAction) throws -> ActionPerformance {
        switch action.method {
        case .accessibilityPress: log.values.append("ax_press:\(action.source.kind == .click ? 0 : -1)")
        case .accessibilityIncrement:
            axScrollPerformCalls += 1
            log.values.append("ax_increment")
            if let axScrollFailure { throw axScrollFailure }
        case .accessibilityDecrement:
            axScrollPerformCalls += 1
            log.values.append("ax_decrement")
            if let axScrollFailure { throw axScrollFailure }
        case .accessibilityText: log.values.append("ax_text:\(action.source.kind == .type ? 0 : -1)")
        case .wait:
            log.values.append("wait:\(action.source.kind == .wait ? 0 : -1)")
            onWait?()
        default: throw ActionPerformFailure(error: .invalidAction, inputStarted: false)
        }
        return ActionPerformance(inputStarted: action.method != .wait,
            effectVerification: requiresObservation ? .unverified : nil,
            observationRequired: requiresObservation)
    }
}

private final class MixedForegroundPoster: PIDTargetedInputPosting {
    let log: MixedForegroundLog
    var events: [SyntheticInputEvent] = []
    var onPost: (() -> Void)?
    init(log: MixedForegroundLog) { self.log = log }
    func preflight(targetPID: pid_t, marker: UInt64) -> Bool { targetPID == 11 && marker != 0 }
    func post(_ event: SyntheticInputEvent, to _: pid_t, marker _: UInt64) throws {
        events.append(event)
        onPost?()
        switch event {
        case .mouseDown: log.values.append("pid_down")
        case .mouseUp: log.values.append("pid_up")
        case .mouseDragged: log.values.append("pid_drag")
        case .scroll: log.values.append("pid_scroll")
        case .unicodeKeyDown, .unicodeKeyUp, .virtualKeyDown, .virtualKeyUp:
            Issue.record("foreground PID plan delivered keyboard input")
        }
    }
}

private final class MixedForegroundActivity: UserActivityMonitoring {
    let lease = UserActivitySessionLease(marker: 123)
    var paused = false
    var eventGateCalls = 0
    var fragmentsStarted = 0
    var fragmentsEnded = 0
    private var fragmentConsumed = false
    func arm(marker _: UInt64, notification _: UserActivityPauseSignal) throws -> UserActivitySessionLease { lease }
    func beginFragment(lease: UserActivitySessionLease) throws {
        guard lease === self.lease, !fragmentConsumed else { throw UserActivityMonitoringError.notArmed }
        fragmentConsumed = true
        fragmentsStarted += 1
    }
    func endFragment(lease: UserActivitySessionLease) {
        if lease === self.lease { fragmentsEnded += 1 }
    }
    func assertNotPaused(lease: UserActivitySessionLease) throws {
        guard lease === self.lease, !paused else { throw UserActivityMonitoringError.paused }
    }
    func heldInputScope(lease: UserActivitySessionLease) throws -> HeldInputScope {
        try assertNotPaused(lease: lease)
        return .cooperative(marker: lease.marker, generation: UUID())
    }
    func performPIDEvent(lease: UserActivitySessionLease, validate: () throws -> Void, mutation: () throws -> Void) throws {
        eventGateCalls += 1
        try assertNotPaused(lease: lease)
        try validate()
        try assertNotPaused(lease: lease)
        try mutation()
    }
    func performPIDCleanup(lease _: UserActivitySessionLease, _ cleanup: () throws -> Void) throws { try cleanup() }
    func cleanupHeldInputs(lease _: UserActivitySessionLease) throws -> HeldInputCleanupResult {
        HeldInputCleanupResult(attempted: 0, released: 0, failed: 0)
    }
    func disarm(lease _: UserActivitySessionLease) throws -> Bool { paused }
}

private struct InputDispatcherTextSafetyForTakeover: BackgroundTextInputSafetyDetecting {
    let value: BackgroundTextInputSafety
    init(_ value: BackgroundTextInputSafety) { self.value = value }
    func detect() -> BackgroundTextInputSafety { value }
}


private final class TakeoverFixture {
    enum Failure: CaseIterable { case target, activation, sidecar, activity }

    let actions: [NativeAction]
    let target = WindowTarget(
        appRef: "app", windowRef: "window", pid: 11, windowID: 22,
        bounds: CGRect(x: 100, y: 200, width: 300, height: 200), title: "Editor",
        axIdentity: 33, interactionMode: .background
    )
    let guardValue: ActionGuard
    let plan: DispatchPlan
    let activation: TakeoverActivation
    let activity: TakeoverActivity
    let cursor: TakeoverCursor
    let vault: TakeoverPlanVault
    let coordinator: ForegroundTakeoverCoordinator

    init(
        plannedFocus: KeyboardFocusAuthority? = nil,
        snapshotGuard: ActionGuard? = nil,
        oldFrontmostPID: pid_t? = 77,
        existingPIDs: Set<pid_t> = [77],
        failure: Failure? = nil,
        actions: [NativeAction] = [.click(x: 10, y: 10, within: "pointer")]
    ) {
        guardValue = ActionGuard(
            pid: 11, windowID: 22,
            bounds: CGRect(x: 100, y: 200, width: 300, height: 200), axIdentity: 33,
            keyboardFocus: plannedFocus,
            snapshotID: "snapshot", interactionMode: .foregroundTakeover
        )
        self.actions = actions
        activation = TakeoverActivation(
            currentFrontmostPID: oldFrontmostPID,
            failActivation: failure == .activation
        )
        let performer = TakeoverPerformer(state: ActionTargetState(
            pid: 11, windowID: 22,
            bounds: CGRect(x: 100, y: 200, width: 300, height: 200), axIdentity: 33
        ))
        let application = PIDTargetApplication(bundleIdentifier: "com.example.Editor", version: "1")
        plan = try! InputDispatcher(
            performer: performer,
            application: application,
            syntheticPolicy: foregroundTakeoverPlanningPolicy(
                application: application,
                pointerActions: [.click]
            )
        ).plan(
            actions: actions,
            context: DispatchContext(guardValue: guardValue)
        )
        activity = TakeoverActivity(failArm: failure == .activity)
        cursor = TakeoverCursor(failShow: failure == .sidecar)
        vault = TakeoverPlanVault(authority: ForegroundTakeoverPlanAuthority(
            target: target,
            guardValue: guardValue,
            plan: plan,
            actions: actions,
            application: application,
            snapshotGuard: snapshotGuard
        ))
        coordinator = ForegroundTakeoverCoordinator(
            activity: activity,
            activation: activation,
            cursor: cursor,
            frontmostPID: { [activation] in activation.currentFrontmostPID },
            applicationExists: { existingPIDs.contains($0) },
            revalidate: { target in
                if failure == .target { throw WindowObservationError.targetGone }
                return target
            },
            takePlan: vault.take
        )
    }

    func begin() throws -> TakeoverToken {
        try coordinator.begin(
            target: target,
            snapshotID: guardValue.snapshotID,
            planRef: plan.planRef,
            stageDigest: plan.stageDigest
        )
    }

    func authorizeAnotherPlan() -> DispatchPlan {
        let performer = TakeoverPerformer(state: ActionTargetState(
            pid: guardValue.pid,
            windowID: guardValue.windowID,
            bounds: guardValue.bounds,
            axIdentity: guardValue.axIdentity
        ))
        let application = PIDTargetApplication(bundleIdentifier: "com.example.Editor", version: "1")
        let next = try! InputDispatcher(
            performer: performer,
            application: application,
            syntheticPolicy: foregroundTakeoverPlanningPolicy(
                application: application,
                pointerActions: [.click]
            )
        ).plan(
            actions: actions,
            context: DispatchContext(guardValue: guardValue)
        )
        vault.insert(ForegroundTakeoverPlanAuthority(
            target: target,
            guardValue: guardValue,
            plan: next,
            actions: actions,
            application: application
        ))
        return next
    }
}

private final class FragmentCoordinatorFixture {
    let actions: [NativeAction] = [.click(x: 10, y: 10, within: "pointer")]
    let target = WindowTarget(
        appRef: "app", windowRef: "window", pid: 11, windowID: 22,
        bounds: CGRect(x: 100, y: 200, width: 300, height: 200), title: "Editor",
        axIdentity: 33, interactionMode: .background
    )
    let activation = TakeoverActivation(currentFrontmostPID: 77)
    let activity = TakeoverActivity(failArm: false)
    let cursor = TakeoverCursor(failShow: false)
    let vault: TakeoverPlanVault
    let coordinator: ForegroundTakeoverCoordinator
    let declaration: ForegroundFragmentDeclaration
    var stages: [FragmentStageAuthority]
    var plans: [DispatchPlan]
    private let application = PIDTargetApplication(bundleIdentifier: "com.example.Editor", version: "1")

    init(
        wallClockLimitMS: Int,
        beforeFragmentCommit: @escaping () -> Void = {},
        classifyPopupPointer: @escaping (ForegroundTakeoverPlanAuthority) -> CGPoint? = popupPointerClickPoint
    ) {
        let stage0 = FragmentStageAuthority(
            fragmentHash: String(repeating: "a", count: 64),
            stageIndex: 0,
            stageHash: String(repeating: "0", count: 64),
            inputSnapshotID: "snapshot"
        )
        let first = Self.makeDraft(
            actions: actions,
            application: application,
            snapshotID: stage0.inputSnapshotID
        )
        let requirements = first.plan.foregroundFragmentRequirements(actions: actions)!
        declaration = ForegroundFragmentDeclaration(
            fragmentHash: stage0.fragmentHash,
            stages: [
                .init(stageHash: stage0.stageHash, expectedActionCount: 1, actions: requirements),
                .init(stageHash: String(repeating: "1", count: 64), expectedActionCount: 1, actions: requirements),
            ],
            maxActions: 2,
            wallClockLimitMS: wallClockLimitMS,
            restorePreviousFocus: true
        )
        stages = [stage0]
        plans = [first.plan]
        vault = TakeoverPlanVault(authority: ForegroundTakeoverPlanAuthority(
            target: target,
            guardValue: first.guardValue,
            plan: first.plan,
            actions: actions,
            application: application,
            dispatcher: first.dispatcher,
            fragmentRequest: .initial(authority: stage0)
        ))
        coordinator = ForegroundTakeoverCoordinator(
            activity: activity,
            activation: activation,
            cursor: cursor,
            frontmostPID: { [activation] in activation.currentFrontmostPID },
            applicationExists: { $0 == 77 },
            revalidate: { $0 },
            takePlan: vault.take,
            classifyPopupPointer: classifyPopupPointer,
            beforeFragmentCommit: beforeFragmentCommit
        )
    }

    func begin() throws -> TakeoverToken {
        try coordinator.beginFragment(
            target: target,
            snapshotID: stages[0].inputSnapshotID,
            planRef: plans[0].planRef,
            declaration: declaration
        )
    }

    func authorizeStage(
        index: Int,
        takeoverRef: String,
        snapshotID: String,
        target stageTarget: WindowTarget? = nil,
        authorityGuardMode: InteractionMode = .foregroundTakeover
    ) throws {
        let stage = FragmentStageAuthority(
            fragmentHash: declaration.fragmentHash,
            stageIndex: index,
            stageHash: declaration.stages[index].stageHash,
            inputSnapshotID: snapshotID
        )
        let next = Self.makeDraft(actions: actions, application: application, snapshotID: snapshotID)
        let authorityGuard = ActionGuard(
            pid: next.guardValue.pid,
            windowID: next.guardValue.windowID,
            bounds: next.guardValue.bounds,
            axIdentity: next.guardValue.axIdentity,
            focusedAXIdentity: next.guardValue.focusedAXIdentity,
            focusedAXBounds: next.guardValue.focusedAXBounds,
            focusedRootPreference: next.guardValue.focusedRootPreference,
            keyboardFocus: next.guardValue.keyboardFocus,
            snapshotID: next.guardValue.snapshotID,
            interactionMode: authorityGuardMode
        )
        stages.append(stage)
        plans.append(next.plan)
        vault.insert(ForegroundTakeoverPlanAuthority(
            target: stageTarget ?? target,
            guardValue: authorityGuard,
            plan: next.plan,
            actions: actions,
            application: application,
            dispatcher: next.dispatcher,
            fragmentRequest: .continuing(takeoverRef: takeoverRef, authority: stage)
        ))
        try coordinator.bindFragmentStage(
            takeoverRef,
            snapshotID: snapshotID,
            planRef: next.plan.planRef,
            stage: stage
        )
    }

    @discardableResult
    func runStage(
        index: Int,
        takeoverRef: String,
        freshSnapshotID: String,
        observedTarget: WindowTarget? = nil,
        observedGuardMode: InteractionMode = .foregroundTakeover
    ) throws -> FragmentStageCommitOutcome {
        try finishStage(index: index, takeoverRef: takeoverRef)
        guard let observedTarget = observedTarget ?? coordinator.fragmentObservationTarget(for: target) else {
            throw TakeoverError.authorityMismatch
        }
        let stage = stages[index]
        let plan = plans[index]
        let observedGuard = observedGuard(
            snapshotID: freshSnapshotID,
            interactionMode: observedGuardMode
        )
        return try coordinator.commitFragmentStage(takeoverRef, commit: .init(
            fragmentHash: stage.fragmentHash,
            stageIndex: stage.stageIndex,
            stageHash: stage.stageHash,
            planRef: plan.planRef,
            freshSnapshotID: freshSnapshotID,
            postconditionVerified: true
        ), observedTarget: observedTarget, observedGuard: observedGuard)
    }

    func finishStage(index: Int, takeoverRef: String) throws {
        let stage = stages[index]
        let plan = plans[index]
        _ = try coordinator.consumeFragment(
            takeoverRef,
            actions: actions,
            stage: stage,
            planRef: plan.planRef
        )
        coordinator.finishFragmentStage(
            takeoverRef,
            stage: stage,
            planRef: plan.planRef,
            succeeded: true
        )
    }

    func foregroundTarget() -> WindowTarget {
        WindowTarget(
            appRef: target.appRef,
            windowRef: target.windowRef,
            pid: target.pid,
            windowID: target.windowID,
            bounds: target.bounds,
            title: target.title,
            axIdentity: target.axIdentity,
            interactionMode: .foregroundTakeover
        )
    }

    func observedGuard(snapshotID: String, interactionMode: InteractionMode) -> ActionGuard {
        ActionGuard(
            pid: target.pid,
            windowID: target.windowID,
            bounds: target.bounds,
            axIdentity: target.axIdentity!,
            snapshotID: snapshotID,
            interactionMode: interactionMode
        )
    }

    private static func makeDraft(
        actions: [NativeAction],
        application: PIDTargetApplication,
        snapshotID: String
    ) -> (dispatcher: InputDispatcher, plan: DispatchPlan, guardValue: ActionGuard) {
        let state = ActionTargetState(
            pid: 11, windowID: 22,
            bounds: CGRect(x: 100, y: 200, width: 300, height: 200), axIdentity: 33
        )
        let guardValue = ActionGuard(
            pid: 11, windowID: 22, bounds: state.bounds, axIdentity: 33,
            snapshotID: snapshotID, interactionMode: .foregroundTakeover
        )
        let dispatcher = InputDispatcher(
            performer: TakeoverPerformer(state: state, snapshotID: snapshotID),
            application: application,
            syntheticPolicy: foregroundTakeoverPlanningPolicy(
                application: application,
                pointerActions: [.click]
            )
        )
        let plan = try! dispatcher.planFragmentDraft(
            actions: actions,
            context: DispatchContext(guardValue: guardValue)
        )
        return (dispatcher, plan, guardValue)
    }
}

private func foregroundTakeoverPlanningPolicy(
    application: PIDTargetApplication,
    pointerActions: Set<DispatchActionClass>
) -> SyntheticInputPlanningPolicy {
    SyntheticInputPlanningPolicy(
        pointerCapability: .experimentalAvailable,
        keyboardCapability: .unavailable,
        registry: PIDInputCompatibilityRegistry(cells: pointerActions.map {
            PIDInputCompatibilityCell(
                bundleIdentifier: application.bundleIdentifier,
                version: application.version,
                backend: .pidPointer,
                action: $0
            )
        })
    )
}

private func foregroundSafeScroll(deltaY: CGFloat) -> NativeAction {
    .scroll(deltaY: deltaY, x: 10, y: 10, targetElementRef: "pointer")
}

private func foregroundSafeDrag() -> NativeAction {
    NativeAction(
        kind: .drag,
        x: 10,
        y: 10,
        endX: 20,
        endY: 20,
        text: nil,
        key: nil,
        deltaX: nil,
        deltaY: nil,
        durationMS: 0,
        elementRef: nil,
        targetElementRef: "pointer",
        modifiers: []
    )
}

private final class TakeoverPlanVault {
    private var authorities: [String: ForegroundTakeoverPlanAuthority]
    init(authority: ForegroundTakeoverPlanAuthority) { authorities = [authority.plan.planRef: authority] }
    func insert(_ authority: ForegroundTakeoverPlanAuthority) { authorities[authority.plan.planRef] = authority }
    func take(_ planRef: String) -> ForegroundTakeoverPlanAuthority? {
        authorities.removeValue(forKey: planRef)
    }
}

private final class TakeoverActivation: ApplicationActivationControlling {
    var activated: [WindowTarget] = []
    var popupActivated: [(WindowTarget, CGPoint)] = []
    var restored: [pid_t] = []
    var currentFrontmostPID: pid_t?
    let failActivation: Bool
    init(currentFrontmostPID: pid_t?, failActivation: Bool = false) {
        self.currentFrontmostPID = currentFrontmostPID
        self.failActivation = failActivation
    }
    func activate(_ target: WindowTarget) throws {
        activated.append(target)
        if failActivation { throw WindowObservationError.targetNotFrontmost }
        currentFrontmostPID = target.pid
    }
    func activatePopupPointerOnly(_ target: WindowTarget, at point: CGPoint) throws {
        popupActivated.append((target, point))
        if failActivation { throw WindowObservationError.targetNotFrontmost }
        currentFrontmostPID = target.pid
    }
    func restore(pid: pid_t) throws {
        restored.append(pid)
        currentFrontmostPID = pid
    }
}

private final class TakeoverBeginFailureWindows: WindowObserving {
    let fixture: TakeoverFixture
    init(fixture: TakeoverFixture) { self.fixture = fixture }
    func apps() throws -> JSONValue { .object(["apps": .array([])]) }
    func snapshot(appRef _: String, windowRef _: String, scope _: String, artifactName _: String?, textDetail _: SnapshotTextDetailRequest) throws -> JSONValue { .object([:]) }
    func takeoverBegin(snapshotID _: String, planRef _: String) throws -> String {
        _ = try fixture.begin()
        return "unexpected"
    }
    func takeoverBegin(
        snapshotID _: String,
        planRef _: String,
        declaration _: ForegroundFragmentDeclaration
    ) throws -> String {
        _ = try fixture.begin()
        return "unexpected"
    }
}

private final class TakeoverCursor: VirtualCursorPresenting {
    let sidecarWindowID: UInt32? = nil
    let windowAuthority: VirtualCursorWindowAuthority? = nil
    var presentation = VirtualCursorPresentation(virtualPointer: nil, cursorVisible: false)
    var hideCount = 0
    var closeCount = 0
    let failShow: Bool
    var onClose: (() -> Void)?
    init(failShow: Bool) { self.failShow = failShow }
    func displayExclusionWindowID(availableWindows _: [VirtualCursorWindowRecord]) -> UInt32? { nil }
    func show(at point: CGPoint) throws {
        if failShow { throw VirtualCursorClientError.unavailable }
        presentation = .init(virtualPointer: point, cursorVisible: true)
    }
    func move(to point: CGPoint) throws { presentation = .init(virtualPointer: point, cursorVisible: true) }
    func click(at point: CGPoint) throws { try move(to: point) }
    func hide() { hideCount += 1; presentation = .init(virtualPointer: presentation.virtualPointer, cursorVisible: false) }
    func close() {
        closeCount += 1
        presentation = .init(virtualPointer: presentation.virtualPointer, cursorVisible: false)
        onClose?()
    }
}

private final class TakeoverActivity: UserActivityMonitoring {
    var isPaused = false
    var terminalInterrupted = false
    var paused: Bool { isPaused }
    var disarmCount = 0
    var fragmentsStarted = 0
    var fragmentsEnded = 0
    var cleanupAttempts = 0
    var cleanupObservedBeforeDisarm = false
    let failArm: Bool
    var failBeginFragment = false
    var offerPauseDuringArm = false
    private let lease = UserActivitySessionLease(marker: 123)
    private let heldInputs = HeldInputRegistry()
    private var cleanupFailuresRemaining = 0
    private let scope = HeldInputScope.cooperative(marker: 123, generation: UUID())
    private var notification: UserActivityPauseSignal?
    var heldInputCount: Int { heldInputs.heldCount(scope: scope) }
    init(failArm: Bool) { self.failArm = failArm }
    func arm(marker _: UInt64, notification: UserActivityPauseSignal) throws -> UserActivitySessionLease {
        if failArm { throw UserActivityMonitoringError.eventTapUnavailable }
        self.notification = notification
        if offerPauseDuringArm {
            isPaused = true
            notification.offer(.keyboard)
        }
        return lease
    }
    func beginFragment(lease _: UserActivitySessionLease) throws {
        if failBeginFragment { throw UserActivityMonitoringError.paused }
        fragmentsStarted += 1
    }
    func endFragment(lease _: UserActivitySessionLease) { fragmentsEnded += 1 }
    func assertNotPaused(lease _: UserActivitySessionLease) throws {
        if isPaused { throw UserActivityMonitoringError.paused }
    }
    func heldInputScope(lease candidate: UserActivitySessionLease) throws -> HeldInputScope {
        guard candidate === lease else { throw UserActivityMonitoringError.notArmed }
        return scope
    }
    func performPIDEvent(lease _: UserActivitySessionLease, validate: () throws -> Void, mutation: () throws -> Void) throws {
        try validate(); try mutation()
    }
    func performPIDCleanup(lease _: UserActivitySessionLease, _ cleanup: () throws -> Void) throws { try cleanup() }
    func cleanupHeldInputs(lease candidate: UserActivitySessionLease) throws -> HeldInputCleanupResult {
        guard candidate === lease else { throw UserActivityMonitoringError.notArmed }
        cleanupObservedBeforeDisarm = cleanupObservedBeforeDisarm || disarmCount == 0
        return heldInputs.cleanupAll(scope: scope)
    }
    func disarm(lease _: UserActivitySessionLease) throws -> Bool {
        disarmCount += 1
        return terminalInterrupted || isPaused
    }

    func retainHeldInput(cleanupFailures: Int) {
        cleanupFailuresRemaining = cleanupFailures
        try! heldInputs.begin(
            token: UUID(),
            scope: scope,
            postDown: {},
            release: {},
            cleanupRelease: { [weak self] in
                guard let self else { return }
                cleanupAttempts += 1
                if cleanupFailuresRemaining > 0 {
                    cleanupFailuresRemaining -= 1
                    throw SyntheticInputFailure(error: .helperFailed, inputStarted: true)
                }
            }
        )
    }

    func attemptHeldInputCleanup() -> HeldInputCleanupResult {
        heldInputs.cleanupAll(scope: scope)
    }

    func allowHeldInputCleanup() {
        cleanupFailuresRemaining = 0
    }

    func offerPause() {
        isPaused = true
        notification?.offer(.keyboard)
    }
}

private final class TakeoverPerformer: ActionProviding {
    let state: ActionTargetState
    let snapshotID: String
    private let pointerAXElement = AXUIElementCreateApplication(11)
    init(state: ActionTargetState, snapshotID: String = "snapshot") {
        self.state = state
        self.snapshotID = snapshotID
    }
    func currentTargetState() throws -> ActionTargetState { state }
    func element(reference: String, snapshotID: String) -> ActionElement? {
        guard reference == "pointer", snapshotID == self.snapshotID else { return nil }
        return ActionElement(
            element: pointerAXElement,
            bounds: CGRect(x: 1, y: 1, width: 98, height: 98),
            role: kAXGroupRole as String,
            subrole: nil,
            actions: []
        )
    }
    func pointerElement(reference: String, snapshotID: String) -> ActionElement? {
        element(reference: reference, snapshotID: snapshotID)
    }
    func perform(_: ResolvedAction) throws -> ActionPerformance { .init(inputStarted: false) }
}

private struct TakeoverPermissions: PermissionStatusProviding {
    let accessibilityTrusted = true
    let screenRecordingAllowed = true
}

private final class TakeoverProtocolWindows: WindowObserving {
    var beginCalls = 0
    var foregroundActCalls = 0
    var endCalls = 0
    var cleanupFailed = false
    func apps() throws -> JSONValue { .object(["apps": .array([])]) }
    func snapshot(appRef _: String, windowRef _: String, scope _: String, artifactName _: String?, textDetail _: SnapshotTextDetailRequest) throws -> JSONValue { .object([:]) }
    func takeoverBegin(snapshotID _: String, planRef _: String) throws -> String { beginCalls += 1; return "t" }
    func takeoverBegin(
        snapshotID _: String,
        planRef _: String,
        declaration _: ForegroundFragmentDeclaration
    ) throws -> String { beginCalls += 1; return "t" }
    func cooperativeAct(snapshotID _: String, interactionMode _: InteractionMode, planRef _: String, takeoverRef _: String?, actions _: [NativeAction]) -> CooperativeActionResult {
        foregroundActCalls += 1
        return .init(batch: .init(outcomes: [], lastAcknowledgedAction: -1, error: nil), error: nil)
    }
    func cooperativeAct(
        snapshotID _: String,
        interactionMode _: InteractionMode,
        planRef _: String,
        takeoverRef _: String?,
        actions _: [NativeAction],
        fragmentStage _: FragmentStageAuthority?
    ) -> CooperativeActionResult {
        foregroundActCalls += 1
        return .init(batch: .init(outcomes: [], lastAcknowledgedAction: -1, error: nil), error: nil)
    }
    var restoreRequests: [Bool] = []
    func takeoverEnd(takeoverRef: String, restorePreviousFocus: Bool) throws -> TakeoverOutcome {
        restoreRequests.append(restorePreviousFocus)
        return try takeoverEnd(takeoverRef: takeoverRef)
    }
    func takeoverEnd(takeoverRef _: String) throws -> TakeoverOutcome {
        guard endCalls == 0 else { throw TakeoverError.alreadyConsumed }
        endCalls += 1
        return .init(started: true, restoration: .restored, cleanupFailed: cleanupFailed)
    }
}

@Test func foregroundRejectsExcessivePlannedWaitBeforeAnyInput() throws {
    let fixture = MixedForegroundExecutionFixture(enabled: [])
    let actions: [NativeAction] = [.wait(durationMS: 6000), .wait(durationMS: 6000)]
    let plan = try fixture.dispatcher.plan(actions: actions, context: DispatchContext(guardValue: fixture.guardValue))
    let entries = try fixture.dispatcher.consumeForegroundPlan(plan, authority: ForegroundPlanConsumptionAuthority(
        planRef: plan.planRef, snapshotID: fixture.guardValue.snapshotID, interactionMode: .foregroundTakeover,
        actions: actions, backends: plan.backends, guardValue: fixture.guardValue))
    let result = fixture.executor.run(expected: fixture.guardValue, application: fixture.application,
        lease: fixture.activity.lease, entries: entries)
    #expect(result.error == .actionTimeout)
    #expect(fixture.log.values.isEmpty)
}

@Test func foregroundWaitDoesNotHoldInputEventGate() throws {
    let fixture = MixedForegroundExecutionFixture(enabled: [])
    let actions: [NativeAction] = [.wait(durationMS: 0)]
    let plan = try fixture.dispatcher.plan(actions: actions, context: DispatchContext(guardValue: fixture.guardValue))
    let entries = try fixture.dispatcher.consumeForegroundPlan(plan, authority: ForegroundPlanConsumptionAuthority(
        planRef: plan.planRef, snapshotID: fixture.guardValue.snapshotID, interactionMode: .foregroundTakeover,
        actions: actions, backends: plan.backends, guardValue: fixture.guardValue))
    let result = fixture.executor.run(expected: fixture.guardValue, application: fixture.application,
        lease: fixture.activity.lease, entries: entries)
    #expect(result.error == nil)
    #expect(fixture.activity.eventGateCalls == 0)
}

private final class TakeoverEffectReader: AXEffectReading {
    var values: [AXEffectObservation]
    var beforeRead: (() -> Void)?
    init(_ values: [AXEffectObservation]) { self.values = values }
    func read(_ element: AXUIElement, remainingBudget: TimeInterval) -> AXEffectObservation? {
        beforeRead?()
        return values.isEmpty ? nil : values.removeFirst()
    }
}

@Test func foregroundAXIncrementUsesNarrowEffectVerification() throws {
    let reader = TakeoverEffectReader([
        AXEffectObservation(identityToken: "scroll", value: .number(1)),
        AXEffectObservation(identityToken: "scroll", value: .number(2)),
    ])
    let fixture = MixedForegroundExecutionFixture(enabled: [], effectReader: reader)
    let actions: [NativeAction] = [.scroll(deltaY: 1, elementRef: "scroll")]
    let plan = try fixture.dispatcher.plan(actions: actions, context: DispatchContext(guardValue: fixture.guardValue))
    let entries = try fixture.dispatcher.consumeForegroundPlan(plan, authority: ForegroundPlanConsumptionAuthority(
        planRef: plan.planRef, snapshotID: fixture.guardValue.snapshotID, interactionMode: .foregroundTakeover,
        actions: actions, backends: plan.backends, guardValue: fixture.guardValue))
    let result = fixture.executor.run(expected: fixture.guardValue, application: fixture.application,
        lease: fixture.activity.lease, entries: entries)
    #expect(result.error == nil)
    #expect(result.outcomes.first?.effectVerification == .verified)
}

@Test func foregroundPositiveWaitStopsOnPauseOutsideEventGate() throws {
    var time: TimeInterval = 0
    var interrupt: (() -> Void)?
    let fixture = MixedForegroundExecutionFixture(enabled: [], now: { time }, waitSleeper: { interval in
        #expect(interval <= 0.02)
        time += interval
        interrupt?()
    })
    interrupt = { fixture.activity.paused = true }
    let actions: [NativeAction] = [.wait(durationMS: 1000)]
    let plan = try fixture.dispatcher.plan(actions: actions, context: DispatchContext(guardValue: fixture.guardValue))
    let entries = try fixture.dispatcher.consumeForegroundPlan(plan, authority: ForegroundPlanConsumptionAuthority(
        planRef: plan.planRef, snapshotID: fixture.guardValue.snapshotID, interactionMode: .foregroundTakeover,
        actions: actions, backends: plan.backends, guardValue: fixture.guardValue))
    let result = fixture.executor.run(expected: fixture.guardValue, application: fixture.application,
        lease: fixture.activity.lease, entries: entries)
    #expect(result.cooperativeError == .userActivityPaused)
    #expect(result.lastAcknowledgedAction == -1)
    #expect(fixture.activity.eventGateCalls == 0)
    #expect(time <= 0.02)
    #expect(fixture.log.values.isEmpty)
}

@Test func foregroundEffectProbeCannotInvalidateTargetBeforeMutation() throws {
    let reader = TakeoverEffectReader([AXEffectObservation(identityToken: "scroll", value: .number(1))])
    let fixture = MixedForegroundExecutionFixture(enabled: [], effectReader: reader)
    let actions: [NativeAction] = [.scroll(deltaY: 1, elementRef: "scroll")]
    let plan = try fixture.dispatcher.plan(actions: actions, context: DispatchContext(guardValue: fixture.guardValue))
    let entries = try fixture.dispatcher.consumeForegroundPlan(plan, authority: ForegroundPlanConsumptionAuthority(
        planRef: plan.planRef, snapshotID: fixture.guardValue.snapshotID, interactionMode: .foregroundTakeover,
        actions: actions, backends: plan.backends, guardValue: fixture.guardValue))
    reader.beforeRead = { fixture.performer.scrollEnabled = false }
    let result = fixture.executor.run(expected: fixture.guardValue, application: fixture.application,
        lease: fixture.activity.lease, entries: entries)
    #expect(result.error == .staleSnapshot)
    #expect(fixture.performer.axScrollPerformCalls == 0)
    #expect(result.lastAcknowledgedAction == -1)
}

@Test func enrichedPlanningFocusDoesNotInvalidateOriginalSnapshotAuthority() throws {
    let focus = KeyboardFocusAuthority(
        identityToken: "save-name", bounds: CGRect(x: 120, y: 220, width: 100, height: 20),
        role: "AXTextField", subrole: nil
    )
    let original = ActionGuard(
        pid: 11, windowID: 22,
        bounds: CGRect(x: 100, y: 200, width: 300, height: 200), axIdentity: 33,
        snapshotID: "snapshot", interactionMode: .background
    )
    let fixture = TakeoverFixture(plannedFocus: focus, snapshotGuard: original)
    let token = try fixture.begin()
    _ = try fixture.coordinator.consume(token.ref, actions: fixture.actions)
    let execution = try fixture.coordinator.executionAuthority(
        for: token.ref, snapshotID: "snapshot", planRef: fixture.plan.planRef, consumedGuard: original
    )
    #expect(execution.planAuthority.guardValue.keyboardFocus == focus)
    _ = try fixture.coordinator.end(token.ref, allowRestore: true)
}

@Test(arguments: ["focus", "pid", "snapshot", "focused-root"])
func enrichedPlanningFocusStillRejectsChangedConsumedSnapshot(kind: String) throws {
    let bounds = CGRect(x: 100, y: 200, width: 300, height: 200)
    let focus = KeyboardFocusAuthority(
        identityToken: "save-name", bounds: CGRect(x: 120, y: 220, width: 100, height: 20),
        role: "AXTextField", subrole: nil
    )
    let original = ActionGuard(
        pid: 11, windowID: 22, bounds: bounds, axIdentity: 33,
        snapshotID: "snapshot", interactionMode: .background
    )
    let fixture = TakeoverFixture(plannedFocus: focus, snapshotGuard: original)
    let token = try fixture.begin()
    _ = try fixture.coordinator.consume(token.ref, actions: fixture.actions)
    let changed = ActionGuard(
        pid: kind == "pid" ? 99 : 11, windowID: 22, bounds: bounds, axIdentity: 33,
        focusedAXIdentity: kind == "focused-root" ? 44 : nil,
        keyboardFocus: kind == "focus" ? focus : nil,
        snapshotID: kind == "snapshot" ? "other" : "snapshot", interactionMode: .background
    )
    #expect(throws: TakeoverError.authorityMismatch) {
        try fixture.coordinator.executionAuthority(
            for: token.ref, snapshotID: "snapshot", planRef: fixture.plan.planRef, consumedGuard: changed
        )
    }
    #expect(fixture.activity.fragmentsStarted == 0)
    #expect(fixture.cursor.closeCount == 1)
}

@Test func originalSnapshotCannotAuthorizeDifferentPlannedTarget() throws {
    let other = ActionGuard(
        pid: 99, windowID: 22, bounds: CGRect(x: 100, y: 200, width: 300, height: 200),
        axIdentity: 33, snapshotID: "snapshot", interactionMode: .background
    )
    let fixture = TakeoverFixture(snapshotGuard: other)
    #expect(throws: TakeoverError.authorityMismatch) { try fixture.begin() }
    #expect(fixture.activity.fragmentsStarted == 0)
}

@Test func continuousFocusReleasesAuthorityWithoutRestoringPreviousApp() throws {
    let fixture = TakeoverFixture(oldFrontmostPID: 77, existingPIDs: [77])
    let token = try fixture.begin()
    _ = try fixture.coordinator.consume(token.ref, actions: fixture.actions)
    let outcome = try fixture.coordinator.end(token.ref, allowRestore: false)
    #expect(outcome.restoration == .preservedUserFocus)
    #expect(fixture.activation.restored.isEmpty)
    #expect(fixture.activity.disarmCount == 1)
    #expect(fixture.cursor.closeCount == 1)
    #expect(throws: TakeoverError.self) { try fixture.coordinator.consume(token.ref, actions: fixture.actions) }
}

@Test func takeoverEndRoutesExplicitFocusPolicyAndRejectsMalformedValues() {
    for restore in [true, false] {
        let windows = TakeoverProtocolWindows()
        let dispatcher = Dispatcher(permissions: TakeoverPermissions(), windows: windows)
        let response = dispatcher.handle("{\"protocol_version\":4,\"request_id\":\"e\",\"operation\":\"takeover_end\",\"payload\":{\"takeover_ref\":\"t\",\"restore_previous_focus\":\(restore)}}")
        #expect(response.ok)
        #expect(windows.restoreRequests == [restore])
        #expect(windows.endCalls == 1)
    }
    for value in ["0", "1", "null", "\"false\"", "{}"] {
        let windows = TakeoverProtocolWindows()
        let dispatcher = Dispatcher(permissions: TakeoverPermissions(), windows: windows)
        let response = dispatcher.handle("{\"protocol_version\":4,\"request_id\":\"e\",\"operation\":\"takeover_end\",\"payload\":{\"takeover_ref\":\"t\",\"restore_previous_focus\":\(value)}}")
        #expect(!response.ok)
        #expect(windows.endCalls == 0)
    }
}
