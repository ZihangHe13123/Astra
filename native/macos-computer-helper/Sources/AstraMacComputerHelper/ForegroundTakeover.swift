@preconcurrency import ApplicationServices
import CoreGraphics
import Foundation

struct TakeoverToken: Equatable {
    let ref: String
}

enum TakeoverError: Error, Equatable {
    case stalePlan
    case alreadyConsumed
    case authorityMismatch
    case targetChanged
    case targetNotFrontmost
    case activityUnavailable
    case sidecarFailed
    case cleanupFailed
}

struct ForegroundTakeoverPlanAuthority {
    let target: WindowTarget
    let guardValue: ActionGuard
    // Immutable observation authority; planning may enrich the execution focus.
    let snapshotGuard: ActionGuard
    let plan: DispatchPlan
    let actions: [NativeAction]
    let application: PIDTargetApplication
    let dispatcher: InputDispatcher?
    let fragmentRequest: ForegroundFragmentPlanRequest?

    init(
        target: WindowTarget,
        guardValue: ActionGuard,
        plan: DispatchPlan,
        actions: [NativeAction],
        application: PIDTargetApplication,
        dispatcher: InputDispatcher? = nil,
        fragmentRequest: ForegroundFragmentPlanRequest? = nil,
        snapshotGuard: ActionGuard? = nil
    ) {
        self.target = target
        self.guardValue = guardValue
        self.snapshotGuard = snapshotGuard ?? guardValue
        self.plan = plan
        self.actions = actions
        self.application = application
        self.dispatcher = dispatcher
        self.fragmentRequest = fragmentRequest
    }
}

struct ForegroundTakeoverExecutionAuthority {
    let planAuthority: ForegroundTakeoverPlanAuthority
    let lease: UserActivitySessionLease
}

protocol ForegroundTakeoverCoordinating: AnyObject {
    func begin(
        target: WindowTarget,
        snapshotID: String,
        planRef: String,
        stageDigest: String
    ) throws -> TakeoverToken
    func consume(_ takeoverRef: String, actions: [NativeAction]) throws -> DispatchPlan
    func end(_ takeoverRef: String, allowRestore: Bool) throws -> TakeoverOutcome
}

final class ForegroundTakeoverCoordinator: ForegroundTakeoverCoordinating {
    typealias PlanConsumer = (String) -> ForegroundTakeoverPlanAuthority?

    private final class Record {
        var authority: ForegroundTakeoverPlanAuthority
        let lease: UserActivitySessionLease
        let oldFrontmostPID: pid_t?
        let fragmentAuthority: ForegroundFragmentAuthority?
        let restorePreviousFocus: Bool
        var expiryTimer: DispatchSourceTimer?
        var actConsumed = false
        var cleanupOutcome: TakeoverOutcome?
        var cleanupInProgress = false
        var terminalClaimed = false
        var commitInProgress = false
        var terminationPending = false

        init(
            authority: ForegroundTakeoverPlanAuthority,
            lease: UserActivitySessionLease,
            oldFrontmostPID: pid_t?,
            fragmentAuthority: ForegroundFragmentAuthority? = nil,
            restorePreviousFocus: Bool = true
        ) {
            self.authority = authority
            self.lease = lease
            self.oldFrontmostPID = oldFrontmostPID
            self.fragmentAuthority = fragmentAuthority
            self.restorePreviousFocus = restorePreviousFocus
        }
    }

    private let activity: any UserActivityMonitoring
    private let activation: any ApplicationActivationControlling
    private let cursor: any VirtualCursorPresenting
    private let frontmostPID: () -> pid_t?
    private let applicationExists: (pid_t) -> Bool
    private let revalidate: (WindowTarget) throws -> WindowTarget
    private let takePlan: PlanConsumer
    private let beforeFragmentCommit: () -> Void
    private let lock = NSLock()
    private var records: [String: Record] = [:]
    private var beginReserved = false

    init(
        activity: any UserActivityMonitoring,
        activation: any ApplicationActivationControlling,
        cursor: any VirtualCursorPresenting,
        frontmostPID: @escaping () -> pid_t?,
        applicationExists: @escaping (pid_t) -> Bool,
        revalidate: @escaping (WindowTarget) throws -> WindowTarget,
        takePlan: @escaping PlanConsumer,
        beforeFragmentCommit: @escaping () -> Void = {}
    ) {
        self.activity = activity
        self.activation = activation
        self.cursor = cursor
        self.frontmostPID = frontmostPID
        self.applicationExists = applicationExists
        self.revalidate = revalidate
        self.takePlan = takePlan
        self.beforeFragmentCommit = beforeFragmentCommit
    }

    func begin(
        target: WindowTarget,
        snapshotID: String,
        planRef: String,
        stageDigest: String
    ) throws -> TakeoverToken {
        try begin(
            target: target,
            snapshotID: snapshotID,
            planRef: planRef,
            stageDigest: stageDigest,
            declaration: nil
        )
    }

    func beginFragment(
        target: WindowTarget,
        snapshotID: String,
        planRef: String,
        declaration: ForegroundFragmentDeclaration
    ) throws -> TakeoverToken {
        try begin(
            target: target,
            snapshotID: snapshotID,
            planRef: planRef,
            stageDigest: nil,
            declaration: declaration
        )
    }

    private func begin(
        target: WindowTarget,
        snapshotID: String,
        planRef: String,
        stageDigest: String?,
        declaration: ForegroundFragmentDeclaration?
    ) throws -> TakeoverToken {
        lock.lock()
        guard records.isEmpty, !beginReserved else {
            lock.unlock()
            throw TakeoverError.alreadyConsumed
        }
        beginReserved = true
        lock.unlock()

        var committed = false
        defer {
            if !committed {
                lock.lock()
                beginReserved = false
                lock.unlock()
            }
        }

        guard let authority = takePlan(planRef) else { throw TakeoverError.stalePlan }
        let fragmentAuthority: ForegroundFragmentAuthority?
        if let declaration {
            do {
                let created = try ForegroundFragmentAuthority(declaration: declaration)
                guard case let .initial(stage)? = authority.fragmentRequest,
                      let dispatcher = authority.dispatcher,
                      let requirements = authority.plan.foregroundFragmentRequirements(actions: authority.actions)
                else { throw ForegroundFragmentAuthorityError.authorityMismatch }
                try created.bindPlannedStage(stage, requirements: requirements, planRef: planRef)
                _ = try dispatcher.sealFragmentDraft(
                    planRef: planRef,
                    actions: authority.actions,
                    stage: declaration.stages[0],
                    authority: stage
                )
                fragmentAuthority = created
            } catch {
                authority.dispatcher?.invalidatePlans()
                throw TakeoverError.authorityMismatch
            }
        } else {
            guard authority.fragmentRequest == nil else {
                authority.dispatcher?.invalidatePlans()
                throw TakeoverError.authorityMismatch
            }
            fragmentAuthority = nil
        }
        let planMatches = authority.plan.planRef == planRef &&
            authority.plan.interactionMode == .foregroundTakeover &&
            authority.plan.requiresTakeover &&
            authority.guardValue.interactionMode == .foregroundTakeover &&
            authority.guardValue.snapshotID == snapshotID &&
            sameSnapshotIdentity(authority.snapshotGuard, authority.guardValue) &&
            authority.plan.matches(actions: authority.actions) &&
            (stageDigest == nil || authority.plan.stageDigest == stageDigest) &&
            exactTarget(authority.target, target)
        guard planMatches else {
            fragmentAuthority?.consume()
            authority.dispatcher?.invalidatePlans()
            throw TakeoverError.authorityMismatch
        }

        let reference = "takeover_\(UUID().uuidString.lowercased())"
        let oldPID = frontmostPID()
        let lease: UserActivitySessionLease
        var activationAttempted = false
        var activeTarget: WindowTarget?
        let notification: UserActivityPauseSignal
        do {
            notification = UserActivityPauseSignal(onOffer: declaration == nil ? nil : { [weak self] in
                self?.pause(reference)
            })
            lease = try activity.arm(marker: AstraEventMarker.random(), notification: notification)
        } catch {
            throw TakeoverError.activityUnavailable
        }

        do {
            let current = try revalidate(target)
            guard exactTarget(current, target) else { throw TakeoverError.targetChanged }
            let foregroundTarget = WindowTarget(
                appRef: current.appRef,
                windowRef: current.windowRef,
                pid: current.pid,
                windowID: current.windowID,
                bounds: current.bounds,
                title: current.title,
                axIdentity: current.axIdentity,
                axElement: current.axElement,
                interactionMode: .foregroundTakeover
            )
            let cursorPoint = try initialCursorPoint(actions: authority.actions, target: current)
            activationAttempted = true
            try activation.activate(foregroundTarget)
            try cursor.show(at: cursorPoint)
            activeTarget = foregroundTarget
        } catch {
            let cleanupRecord = Record(
                authority: authority,
                lease: lease,
                oldFrontmostPID: oldPID,
                fragmentAuthority: fragmentAuthority,
                restorePreviousFocus: declaration?.restorePreviousFocus ?? true
            )
            let cleanupOutcome = cleanup(cleanupRecord, allowRestore: activationAttempted)
            if cleanupOutcome.cleanupFailed {
                let cleanupReference = "takeover_cleanup_\(UUID().uuidString.lowercased())"
                lock.lock()
                records[cleanupReference] = cleanupRecord
                lock.unlock()
                throw TakeoverError.cleanupFailed
            }
            if error is VirtualCursorClientError { throw TakeoverError.sidecarFailed }
            if case WindowObservationError.targetNotFrontmost = error {
                throw TakeoverError.targetNotFrontmost
            }
            if let typed = error as? TakeoverError { throw typed }
            throw TakeoverError.targetChanged
        }

        guard let activeTarget else {
            fragmentAuthority?.consume()
            throw TakeoverError.targetChanged
        }
        let activeAuthority = ForegroundTakeoverPlanAuthority(
            target: activeTarget,
            guardValue: authority.guardValue,
            plan: authority.plan,
            actions: authority.actions,
            application: authority.application,
            dispatcher: authority.dispatcher,
            fragmentRequest: authority.fragmentRequest,
            snapshotGuard: authority.snapshotGuard
        )
        if fragmentAuthority != nil {
            do { try activity.beginFragment(lease: lease) }
            catch {
                let cleanupRecord = Record(
                    authority: activeAuthority,
                    lease: lease,
                    oldFrontmostPID: oldPID,
                    fragmentAuthority: fragmentAuthority,
                    restorePreviousFocus: declaration?.restorePreviousFocus ?? true
                )
                fragmentAuthority?.consume()
                let outcome = cleanup(cleanupRecord, allowRestore: false)
                if outcome.cleanupFailed {
                    lock.lock()
                    records[reference] = cleanupRecord
                    lock.unlock()
                    throw TakeoverError.cleanupFailed
                }
                throw TakeoverError.activityUnavailable
            }
        }
        let record = Record(
            authority: activeAuthority,
            lease: lease,
            oldFrontmostPID: oldPID,
            fragmentAuthority: fragmentAuthority,
            restorePreviousFocus: declaration?.restorePreviousFocus ?? true
        )
        lock.lock()
        records[reference] = record
        beginReserved = false
        committed = true
        lock.unlock()
        if fragmentAuthority != nil, notification.poll() != nil || activity.paused {
            let outcome = terminateFragment(reference)
            if outcome?.cleanupFailed == true { throw TakeoverError.cleanupFailed }
            throw TakeoverError.activityUnavailable
        }
        if let declaration {
            let timer = DispatchSource.makeTimerSource(queue: .global(qos: .userInitiated))
            timer.schedule(deadline: .now() + .milliseconds(declaration.wallClockLimitMS))
            timer.setEventHandler { [weak self] in self?.expire(reference) }
            lock.lock()
            if records[reference] === record { record.expiryTimer = timer }
            lock.unlock()
            timer.resume()
        }
        return TakeoverToken(ref: reference)
    }

    func bindFragmentStage(
        _ takeoverRef: String,
        snapshotID: String,
        planRef: String,
        stage: FragmentStageAuthority
    ) throws {
        guard let next = takePlan(planRef) else { throw TakeoverError.stalePlan }
        var record: Record?
        do {
            lock.lock()
            guard let active = records[takeoverRef],
                  let fragment = active.fragmentAuthority
            else {
                lock.unlock()
                throw TakeoverError.authorityMismatch
            }
            guard !active.terminalClaimed,
                  active.cleanupOutcome == nil,
                  !active.cleanupInProgress,
                  !fragment.isTerminal,
                  !fragment.isConsumed
            else {
                lock.unlock()
                throw TakeoverError.alreadyConsumed
            }
            record = active
            guard foregroundAuthorityMatchesExactTarget(active.authority),
                  foregroundAuthorityMatchesExactTarget(next),
                  !active.actConsumed,
                  case let .continuing(reference, declared)? = next.fragmentRequest,
                  reference == takeoverRef,
                  declared == stage,
                  stage.inputSnapshotID == snapshotID,
                  next.guardValue.snapshotID == snapshotID,
                  sameTargetIdentity(active.authority.target, next.target),
                  let dispatcher = next.dispatcher,
                  let requirements = next.plan.foregroundFragmentRequirements(actions: next.actions),
                  next.plan.interactionMode == .foregroundTakeover,
                  next.plan.requiresTakeover
            else {
                lock.unlock()
                throw TakeoverError.authorityMismatch
            }
            lock.unlock()
            try fragment.bindPlannedStage(stage, requirements: requirements, planRef: planRef)
            _ = try dispatcher.sealFragmentDraft(
                planRef: planRef,
                actions: next.actions,
                stage: fragment.declaration.stages[stage.stageIndex],
                authority: stage
            )
            lock.lock()
            guard records[takeoverRef] === active, !active.terminalClaimed else {
                lock.unlock()
                throw TakeoverError.authorityMismatch
            }
            active.authority = next
            lock.unlock()
        } catch {
            next.dispatcher?.invalidatePlans()
            record?.fragmentAuthority?.consume()
            if record != nil { terminateFragment(takeoverRef) }
            throw TakeoverError.authorityMismatch
        }
    }

    func consumeFragment(
        _ takeoverRef: String,
        actions: [NativeAction],
        stage: FragmentStageAuthority,
        planRef: String
    ) throws -> DispatchPlan {
        lock.lock()
        guard let record = records[takeoverRef],
              let fragment = record.fragmentAuthority,
              record.authority.fragmentRequest?.authority == stage
        else {
            lock.unlock()
            throw TakeoverError.authorityMismatch
        }
        lock.unlock()
        do { try fragment.beginStage(stage, planRef: planRef) }
        catch {
            fragment.consume()
            _ = cleanup(record, allowRestore: false)
            throw TakeoverError.authorityMismatch
        }
        do { return try consume(takeoverRef, actions: actions) }
        catch {
            fragment.consume()
            terminateFragment(takeoverRef)
            throw error
        }
    }

    func finishFragmentStage(
        _ takeoverRef: String,
        stage: FragmentStageAuthority,
        planRef: String,
        succeeded: Bool
    ) {
        lock.lock()
        let record = records[takeoverRef]
        lock.unlock()
        guard let record, let fragment = record.fragmentAuthority else { return }
        fragment.finishStage(stage, planRef: planRef, succeeded: succeeded)
        if !succeeded { terminateFragment(takeoverRef) }
    }

    @discardableResult
    func commitFragmentStage(
        _ takeoverRef: String,
        commit: FragmentStageCommit,
        observedTarget: WindowTarget?,
        observedGuard: ActionGuard
    ) throws -> FragmentStageCommitOutcome {
        lock.lock()
        guard let record = records[takeoverRef], let fragment = record.fragmentAuthority else {
            lock.unlock()
            throw TakeoverError.alreadyConsumed
        }
        guard !record.terminalClaimed,
              record.cleanupOutcome == nil,
              !record.cleanupInProgress,
              !record.commitInProgress,
              !fragment.isTerminal,
              !fragment.isConsumed
        else {
            lock.unlock()
            throw TakeoverError.alreadyConsumed
        }
        guard foregroundAuthorityMatchesExactTarget(record.authority),
              let observedTarget,
              observedTarget.interactionMode == .foregroundTakeover,
              observedGuard.interactionMode == .foregroundTakeover,
              observedGuard.snapshotID == commit.freshSnapshotID,
              sameTargetIdentity(record.authority.target, observedTarget),
              guardMatchesExactTarget(observedGuard, record.authority.target)
        else {
            lock.unlock()
            terminateFragment(takeoverRef)
            throw TakeoverError.alreadyConsumed
        }
        record.commitInProgress = true
        lock.unlock()
        beforeFragmentCommit()
        let terminal: Bool
        do { terminal = try fragment.commit(commit) }
        catch {
            fragment.consume()
            lock.lock()
            record.commitInProgress = false
            record.terminationPending = true
            lock.unlock()
            let outcome = terminateFragment(takeoverRef)
            if outcome?.cleanupFailed == true { throw TakeoverError.cleanupFailed }
            throw TakeoverError.authorityMismatch
        }
        lock.lock()
        record.commitInProgress = false
        let mustTerminate = terminal || record.terminationPending
        if mustTerminate {
            record.terminationPending = false
            record.terminalClaimed = true
            fragment.consume()
        } else if records[takeoverRef] === record {
            record.actConsumed = false
        }
        lock.unlock()
        guard mustTerminate else {
            return FragmentStageCommitOutcome(terminal: false, takeover: nil)
        }
        let outcome = cleanup(record, allowRestore: terminal && record.restorePreviousFocus)
        lock.lock()
        if outcome.cleanupFailed {
            record.terminalClaimed = false
        } else if records[takeoverRef] === record {
            records.removeValue(forKey: takeoverRef)
        }
        lock.unlock()
        if outcome.cleanupFailed { throw TakeoverError.cleanupFailed }
        guard terminal else { throw TakeoverError.alreadyConsumed }
        return FragmentStageCommitOutcome(terminal: true, takeover: outcome)
    }

    func fragmentObservationTarget(for selected: WindowTarget) -> WindowTarget? {
        lock.lock()
        defer { lock.unlock() }
        let matching = records.values.filter { record in
            !record.terminalClaimed &&
                record.cleanupOutcome == nil &&
                !record.cleanupInProgress &&
                record.actConsumed &&
                record.fragmentAuthority?.permitsFreshObservation == true &&
                foregroundAuthorityMatchesExactTarget(record.authority) &&
                sameTargetIdentity(record.authority.target, selected)
        }
        guard matching.count == 1, let target = matching.first?.authority.target else { return nil }
        return WindowTarget(
            appRef: target.appRef,
            windowRef: target.windowRef,
            pid: target.pid,
            windowID: target.windowID,
            bounds: target.bounds,
            title: target.title,
            axIdentity: target.axIdentity,
            axElement: target.axElement,
            interactionMode: .foregroundTakeover
        )
    }

    private func pause(_ takeoverRef: String) {
        terminateFragment(takeoverRef)
    }

    private func expire(_ takeoverRef: String) {
        terminateFragment(takeoverRef)
    }

    func cancelFragment(_ takeoverRef: String) {
        terminateFragment(takeoverRef)
    }

    @discardableResult
    private func terminateFragment(_ takeoverRef: String) -> TakeoverOutcome? {
        lock.lock()
        guard let record = records[takeoverRef], !record.terminalClaimed else {
            lock.unlock()
            return nil
        }
        if record.commitInProgress {
            record.terminationPending = true
            lock.unlock()
            return nil
        }
        record.terminalClaimed = true
        record.fragmentAuthority?.consume()
        lock.unlock()
        let outcome = cleanup(record, allowRestore: false)
        lock.lock()
        if outcome.cleanupFailed {
            record.terminalClaimed = false
        } else if records[takeoverRef] === record {
            records.removeValue(forKey: takeoverRef)
        }
        lock.unlock()
        return outcome
    }

    func consume(_ takeoverRef: String, actions: [NativeAction]) throws -> DispatchPlan {
        lock.lock()
        guard let record = records[takeoverRef] else {
            let active = records.values.filter { !$0.actConsumed && $0.cleanupOutcome == nil }
            active.forEach { $0.actConsumed = true }
            lock.unlock()
            active.forEach { _ = cleanup($0, allowRestore: false) }
            throw active.isEmpty ? TakeoverError.alreadyConsumed : TakeoverError.authorityMismatch
        }
        guard !record.actConsumed, record.cleanupOutcome == nil else {
            lock.unlock()
            throw TakeoverError.alreadyConsumed
        }
        record.actConsumed = true
        let matches = record.authority.actions == actions &&
            record.authority.plan.matches(actions: actions) &&
            InputDispatcher.digest(actions) == InputDispatcher.digest(record.authority.actions)
        lock.unlock()
        guard matches else {
            _ = cleanup(record, allowRestore: false)
            throw TakeoverError.authorityMismatch
        }
        return record.authority.plan
    }

    func executionAuthority(
        for takeoverRef: String,
        snapshotID: String,
        planRef: String,
        consumedGuard: ActionGuard?
    ) throws -> ForegroundTakeoverExecutionAuthority {
        lock.lock()
        guard let record = records[takeoverRef],
              record.actConsumed,
              record.cleanupOutcome == nil
        else {
            lock.unlock()
            throw TakeoverError.alreadyConsumed
        }
        let matches = record.authority.plan.planRef == planRef &&
            record.authority.guardValue.snapshotID == snapshotID &&
            consumedGuard.map { sameSnapshotAuthority($0, record.authority.snapshotGuard) } == true
        lock.unlock()
        guard matches else {
            _ = cleanup(record, allowRestore: false)
            throw TakeoverError.authorityMismatch
        }
        return ForegroundTakeoverExecutionAuthority(
            planAuthority: record.authority,
            lease: record.lease
        )
    }

    func end(_ takeoverRef: String, allowRestore: Bool) throws -> TakeoverOutcome {
        lock.lock()
        guard let record = records[takeoverRef], !record.terminalClaimed else {
            lock.unlock()
            throw TakeoverError.alreadyConsumed
        }
        record.terminalClaimed = true
        lock.unlock()
        let outcome = cleanup(record, allowRestore: allowRestore)
        lock.lock()
        if outcome.cleanupFailed {
            record.terminalClaimed = false
        } else if records[takeoverRef] === record {
            records.removeValue(forKey: takeoverRef)
        }
        lock.unlock()
        return outcome
    }

    func invalidate(allowRestore: Bool) {
        lock.lock()
        let active = records.compactMap { reference, record -> (String, Record)? in
            guard !record.terminalClaimed else { return nil }
            record.terminalClaimed = true
            return (reference, record)
        }
        lock.unlock()
        active.forEach { reference, record in
            let outcome = cleanup(record, allowRestore: allowRestore)
            lock.lock()
            if outcome.cleanupFailed {
                record.terminalClaimed = false
            } else if records[reference] === record {
                records.removeValue(forKey: reference)
            }
            lock.unlock()
        }
    }

    private func cleanup(_ record: Record, allowRestore: Bool) -> TakeoverOutcome {
        lock.lock()
        if record.cleanupInProgress {
            let outcome = record.cleanupOutcome ?? TakeoverOutcome(
                started: true,
                restoration: .preservedUserFocus,
                cleanupFailed: true
            )
            lock.unlock()
            return outcome
        }
        if let outcome = record.cleanupOutcome, !outcome.cleanupFailed {
            lock.unlock()
            return outcome
        }
        // Reserve cleanup before calling external components so re-entry cannot
        // repeat restoration or teardown authority.
        record.cleanupInProgress = true
        record.expiryTimer?.cancel()
        record.expiryTimer = nil
        record.cleanupOutcome = TakeoverOutcome(
            started: true,
            restoration: .preservedUserFocus,
            cleanupFailed: true
        )
        lock.unlock()

        cursor.hide()
        cursor.close()
        activity.endFragment(lease: record.lease)
        let cleanupFailed: Bool
        do {
            cleanupFailed = try !activity.cleanupHeldInputs(lease: record.lease).succeeded
        } catch {
            cleanupFailed = true
        }
        if cleanupFailed {
            let outcome = TakeoverOutcome(
                started: true,
                restoration: .preservedUserFocus,
                cleanupFailed: true
            )
            lock.lock()
            record.cleanupInProgress = false
            record.cleanupOutcome = outcome
            lock.unlock()
            return outcome
        }
        let interrupted = (try? activity.disarm(lease: record.lease)) ?? true

        let restoration: TakeoverRestoration
        if !allowRestore || !record.restorePreviousFocus || interrupted {
            restoration = .preservedUserFocus
        } else if frontmostPID() != record.authority.target.pid {
            restoration = .preservedUserFocus
        } else if let oldPID = record.oldFrontmostPID {
            if oldPID == record.authority.target.pid {
                restoration = .restored
            } else if applicationExists(oldPID) {
                do {
                    try activation.restore(pid: oldPID)
                    restoration = .restored
                } catch {
                    restoration = .restoreFailed
                }
            } else {
                restoration = .preservedUserFocus
            }
        } else {
            restoration = .preservedUserFocus
        }
        let outcome = TakeoverOutcome(
            started: true,
            restoration: restoration,
            cleanupFailed: cleanupFailed
        )
        lock.lock()
        record.cleanupInProgress = false
        record.cleanupOutcome = outcome
        lock.unlock()
        return outcome
    }

}

final class ForegroundPlanExecutor {
    private let activity: any UserActivityMonitoring
    private let performer: any ActionProviding
    private let pidExecutor: PIDTargetedActionExecutor
    private let keyboardExecutor: ForegroundKeyboardExecutor?
    private let now: () -> TimeInterval
    private let waitSleeper: (TimeInterval) -> Void
    private let effects: AXActionEffectVerifier

    init(
        activity: any UserActivityMonitoring,
        performer: any ActionProviding,
        pidExecutor: PIDTargetedActionExecutor,
        keyboardExecutor: ForegroundKeyboardExecutor? = nil,
        now: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
        waitSleeper: @escaping (TimeInterval) -> Void = { Thread.sleep(forTimeInterval: $0) },
        effectReader: any AXEffectReading = SystemAXEffectReader()
    ) {
        self.activity = activity
        self.performer = performer
        self.pidExecutor = pidExecutor
        self.keyboardExecutor = keyboardExecutor
        self.now = now
        self.waitSleeper = waitSleeper
        self.effects = AXActionEffectVerifier(reader: effectReader, clock: now, sleeper: waitSleeper)
    }

    func run(
        expected: ActionGuard,
        application: PIDTargetApplication,
        lease: UserActivitySessionLease,
        entries: [PlannedDispatchEntry],
        managesActivityFragment: Bool = true
    ) -> PIDTargetedActionResult {
        let deadline = now() + maximumForegroundExecutionSeconds
        let plannedMilliseconds = entries.reduce(0) { sum, entry in
            sum + (entry.source.kind == .wait ? (entry.source.durationMS ?? 0)
                : entry.source.kind == .drag ? (entry.source.durationMS ?? 300) : 0)
        }
        guard plannedMilliseconds <= maximumNativeWaitMilliseconds else { return stopped(error: .actionTimeout) }
        let preparedPID: PIDTargetedPreparedPlan
        let preparedKeyboard: ForegroundKeyboardPreparedPlan?
        do {
            try preflight(expected: expected, entries: entries)
            preparedPID = try pidExecutor.preflight(
                expected: expected,
                application: application,
                marker: lease.marker,
                entries: entries
            )
            // 键盘执行器服务**两个**后端（见下面 case .foregroundKeyboard / case .pidKeyboard 都用
            // preparedKeyboard）。原来只筛 .foregroundKeyboard ⇒ 一条 .pidKeyboard 的文本条目会让
            // preparedKeyboard 停在 nil，执行阶段落到 else 分支静默返回 backgroundActionUnsupported：
            // 实机（2026-09-03 20:56，Safari oMLX）计划合法（cooperativeError=nil、entries=1）、
            // 一个事件都没投、且不落任何日志 —— Safari 的 text 授权恰好只在 pid_keyboard cell 里。
            let keyboardEntries = entries.filter {
                $0.backend == .foregroundKeyboard || $0.backend == .pidKeyboard
            }
            if keyboardEntries.isEmpty {
                preparedKeyboard = nil
            } else {
                guard let keyboardExecutor else {
                    // 本文件原来**零诊断**：整条接管执行路径的拒绝都不落日志，实机排查
                    // "计划合法却打不进字"只能靠猜（2026-09-03 一整晚）。补齐点名。
                    logActionRejected(
                        "FG-KB-EXECUTOR-MISSING entries=\(keyboardEntries.count) "
                            + "pidActions=\(entries.filter { $0.backend == .pidKeyboard }.count)"
                    )
                    throw InputDispatchError.backgroundActionUnsupported
                }
                preparedKeyboard = try keyboardExecutor.preflight(
                    expected: expected,
                    application: application,
                    marker: lease.marker,
                    entries: entries
                )
            }
        } catch PIDTargetedActionFailure.compatibilityDisabled {
            logActionRejected("FG-COMPAT-DENIED backend=pid bundle=\(application.bundleIdentifier) v=\(application.version)")
            return stopped(cooperativeError: .backgroundActionUnsupported)
        } catch ForegroundKeyboardFailure.compatibilityDisabled {
            logActionRejected("FG-COMPAT-DENIED backend=foregroundKeyboard bundle=\(application.bundleIdentifier) v=\(application.version)")
            return stopped(cooperativeError: .backgroundActionUnsupported)
        } catch InputDispatchError.backgroundActionUnsupported {
            logActionRejected("FG-COMPAT-DENIED backend=other bundle=\(application.bundleIdentifier) v=\(application.version)")
            return stopped(cooperativeError: .backgroundActionUnsupported)
        } catch let error as ActionExecutionError {
            return stopped(error: error)
        } catch {
            return stopped(error: .helperFailed)
        }

        if managesActivityFragment {
            do {
                try activity.beginFragment(lease: lease)
            } catch is UserActivityMonitoringError {
                return stopped(cooperativeError: .userActivityPaused)
            } catch {
                return stopped(error: .helperFailed)
            }
        }
        defer {
            if managesActivityFragment { activity.endFragment(lease: lease) }
        }

        var outcomes: [ActionOutcome] = []
        var acknowledged = -1
        var priorNonWaitInputMayHaveStarted = false
        for entry in entries {
            guard now() <= deadline else {
                return PIDTargetedActionResult(outcomes: outcomes, lastAcknowledgedAction: acknowledged,
                    error: .actionTimeout, cooperativeError: nil)
            }
            let result: PIDTargetedActionResult
            switch entry.backend {
            case .pidPointer, .foregroundPointer:
                result = pidExecutor.executePrepared(
                    sourceIndex: entry.sourceIndex,
                    from: preparedPID,
                    expected: expected,
                    lease: lease,
                    batchDeadline: deadline
                )
            case .axPress, .axIncrement, .axDecrement, .axSelectedText, .wait:
                result = executeResolved(entry, expected: expected, lease: lease, deadline: deadline)
            case .foregroundKeyboard:
                if let keyboardExecutor, let preparedKeyboard {
                    result = keyboardExecutor.executePrepared(
                        sourceIndex: entry.sourceIndex,
                        from: preparedKeyboard,
                        expected: expected,
                        lease: lease,
                        batchDeadline: deadline
                    )
                } else {
                    result = stopped(cooperativeError: .backgroundActionUnsupported)
                }
            case .pidKeyboard:
                if let keyboardExecutor, let preparedKeyboard {
                    result = keyboardExecutor.executePrepared(
                        sourceIndex: entry.sourceIndex,
                        from: preparedKeyboard,
                        expected: expected,
                        lease: lease,
                        batchDeadline: deadline
                    )
                } else {
                    result = stopped(cooperativeError: .backgroundActionUnsupported)
                }
            }
            outcomes.append(contentsOf: result.outcomes)
            guard result.error == nil, result.cooperativeError == nil else {
                let reportedError = priorNonWaitInputMayHaveStarted && observationFailed(result)
                    ? ActionExecutionError.unknownOutcome
                    : result.error
                // A mixed batch may already have changed the application before
                // the next key's preflight fails. Keep its conservative batch
                // error consistent with the current outcome on the wire, while
                // preserving the original cause in bounded input diagnostics.
                if reportedError == .unknownOutcome, result.cooperativeError == nil {
                    if outcomes.last?.index == entry.sourceIndex, let current = outcomes.last {
                        outcomes[outcomes.count - 1] = ActionOutcome(
                            index: current.index, ok: false, error: .unknownOutcome,
                            inputDiagnostics: current.inputDiagnostics
                        )
                    } else {
                        outcomes.append(ActionOutcome(index: entry.sourceIndex, ok: false, error: .unknownOutcome))
                    }
                }
                return PIDTargetedActionResult(
                    outcomes: outcomes,
                    lastAcknowledgedAction: acknowledged,
                    error: reportedError,
                    cooperativeError: result.cooperativeError,
                    cleanupFailed: result.cleanupFailed
                )
            }
            acknowledged = entry.sourceIndex
            if result.outcomes.last?.observationRequired == true {
                return PIDTargetedActionResult(
                    outcomes: outcomes, lastAcknowledgedAction: acknowledged, error: nil,
                    cooperativeError: entry.sourceIndex == entries.last?.sourceIndex ? nil : .observationRequired
                )
            }
            if entry.backend != .wait { priorNonWaitInputMayHaveStarted = true }
        }
        return PIDTargetedActionResult(
            outcomes: outcomes,
            lastAcknowledgedAction: acknowledged,
            error: nil,
            cooperativeError: nil
        )
    }

    private func observationFailed(_ result: PIDTargetedActionResult) -> Bool {
        if result.cooperativeError == .userActivityPaused { return true }
        return switch result.error {
        case .targetGone?, .targetNotFrontmost?, .staleSnapshot?, .secureTarget?,
             .inputFocusRequired?: true
        case nil, .invalidAction?, .permissionDenied?, .outOfBounds?, .actionTimeout?,
             .helperFailed?, .unknownOutcome?: false
        }
    }

    private func preflight(expected: ActionGuard, entries: [PlannedDispatchEntry]) throws {
        guard expected.interactionMode == .foregroundTakeover,
              entries.count <= maximumNativeActions,
              entries.map(\.sourceIndex) == Array(entries.indices)
        else { throw ActionExecutionError.invalidAction }
        for entry in entries {
            switch entry.backend {
            case .axPress:
                guard entry.actionClass == .press,
                      entry.resolved?.method == .accessibilityPress,
                      entry.resolved?.element != nil
                else { throw ActionExecutionError.staleSnapshot }
            case .axIncrement:
                guard entry.actionClass == .scroll,
                      entry.resolved?.method == .accessibilityIncrement,
                      entry.resolved?.element != nil
                else { throw ActionExecutionError.staleSnapshot }
            case .axDecrement:
                guard entry.actionClass == .scroll,
                      entry.resolved?.method == .accessibilityDecrement,
                      entry.resolved?.element != nil
                else { throw ActionExecutionError.staleSnapshot }
            case .axSelectedText:
                guard entry.actionClass == .text,
                      entry.resolved?.method == .accessibilityText,
                      entry.resolved?.element != nil
                else { throw ActionExecutionError.staleSnapshot }
            case .wait:
                guard entry.actionClass == nil,
                      entry.resolved?.method == .wait,
                      let duration = entry.source.durationMS,
                      duration >= 0,
                      duration <= maximumNativeWaitMilliseconds
                else { throw ActionExecutionError.invalidAction }
            case .pidPointer, .foregroundPointer:
                guard entry.resolved == nil,
                      let actionClass = entry.actionClass,
                      [.click, .doubleClick, .scroll, .drag].contains(actionClass)
                else { throw ActionExecutionError.invalidAction }
            case .foregroundKeyboard:
                guard entry.resolved == nil,
                      entry.actionClass == .text,
                      entry.source.kind == .type ||
                        (entry.source.kind == .keypress && ApprovedKeyChord(action: entry.source) != nil)
                else { throw ActionExecutionError.invalidAction }
            case .pidKeyboard:
                guard entry.resolved == nil,
                      entry.actionClass == .text,
                      entry.source.kind == .keypress && ApprovedKeyChord(action: entry.source) != nil
                else { throw ActionExecutionError.invalidAction }
            }
        }
        let current = try performer.currentTargetState()
        guard current.pid == expected.pid else { throw ActionExecutionError.targetNotFrontmost }
        if current.windowID != expected.windowID
            || current.axIdentity != expected.axIdentity
            || current.bounds != expected.bounds
            || current.focusedAXIdentity != expected.focusedAXIdentity
            || current.focusedAXBounds != expected.focusedAXBounds
            || current.focusedRootPreference != expected.focusedRootPreference {
            logActionRejected(
                "PREFLIGHT-STALE winID=\(current.windowID == expected.windowID)"
                + " axId=\(current.axIdentity == expected.axIdentity)"
                + " bounds=\(current.bounds == expected.bounds)"
                + " fAXId=\(current.focusedAXIdentity == expected.focusedAXIdentity)"
                + " fAXB=\(current.focusedAXBounds == expected.focusedAXBounds)"
                + " fRoot=\(current.focusedRootPreference == expected.focusedRootPreference)"
            )
            throw ActionExecutionError.staleSnapshot
        }
    }

    private func executeResolved(
        _ entry: PlannedDispatchEntry,
        expected: ActionGuard,
        lease: UserActivitySessionLease,
        deadline: TimeInterval
    ) -> PIDTargetedActionResult {
        guard var action = entry.resolved else { return stopped(error: .invalidAction) }
        do {
            var performance: ActionPerformance?
            let effectProbe = effects.makeProbe(entry,
                currentElement: entry.resolved?.verifiedElement,
                deadline: min(deadline, now() + 0.2))
            do {
                if entry.backend == .wait {
                    let until = now() + Double(entry.source.durationMS ?? 0) / 1000
                    guard until <= deadline else { throw ActionExecutionError.actionTimeout }
                    while now() < until {
                        try activity.assertNotPaused(lease: lease)
                        waitSleeper(min(0.02, max(0, until - now())))
                    }
                    try activity.assertNotPaused(lease: lease)
                    guard now() <= deadline else { throw ActionExecutionError.actionTimeout }
                    // A wait never posts input: preserve the performer observation hook,
                    // but perform only a zero-duration wait outside the event gate.
                    action = ResolvedAction(source: .wait(durationMS: 0), method: .wait,
                        screenPoint: nil, endScreenPoint: nil, element: nil)
                    performance = try performer.perform(action)
                } else {
                    try activity.performPIDEvent(
                        lease: lease,
                        validate: {
                            try self.preflightFreshAXScroll(entry, expected: expected, includePress: effectProbe != nil)
                            guard self.now() <= deadline else { throw ActionExecutionError.actionTimeout }
                        },
                        mutation: {
                            if action.source.replace == true {
                                performance = try self.performer.performReplacement(action, validateMutation: {
                                    try self.activity.assertNotPaused(lease: lease)
                                })
                            } else {
                                performance = try self.performer.perform(action)
                            }
                        }
                    )
                }
            } catch let error as ActionExecutionError {
                return PIDTargetedActionResult(
                    outcomes: [ActionOutcome(index: entry.sourceIndex, ok: false, error: error)],
                    lastAcknowledgedAction: -1,
                    error: error,
                    cooperativeError: nil
                )
            }
            guard let performance else { return stopped(error: .helperFailed) }
            do {
                try activity.assertNotPaused(lease: lease)
            } catch is UserActivityMonitoringError {
                let error = performance.inputStarted ? ActionExecutionError.unknownOutcome : nil
                return PIDTargetedActionResult(
                    outcomes: [ActionOutcome(index: entry.sourceIndex, ok: false, error: error)],
                    lastAcknowledgedAction: -1,
                    error: error,
                    cooperativeError: .userActivityPaused
                )
            }
            return PIDTargetedActionResult(
                outcomes: [ActionOutcome(index: entry.sourceIndex, ok: true, error: nil,
                    effectVerification: performance.effectVerification ?? effectProbe.map { effects.verify($0) },
                    observationRequired: performance.observationRequired)],
                lastAcknowledgedAction: entry.sourceIndex,
                error: nil,
                cooperativeError: nil
            )
        } catch is UserActivityMonitoringError {
            return stopped(cooperativeError: .userActivityPaused)
        } catch let failure as ActionPerformFailure {
            if entry.source.replace == true {
                do { try activity.assertNotPaused(lease: lease) }
                catch {
                    let error: ActionExecutionError? = failure.inputStarted ? .unknownOutcome : nil
                    return PIDTargetedActionResult(
                        outcomes: [ActionOutcome(index: entry.sourceIndex, ok: false, error: error)],
                        lastAcknowledgedAction: -1, error: error, cooperativeError: .userActivityPaused
                    )
                }
            }
            let reported = failure.inputStarted ? ActionExecutionError.unknownOutcome : failure.error
            return PIDTargetedActionResult(
                outcomes: [ActionOutcome(index: entry.sourceIndex, ok: false, error: reported)],
                lastAcknowledgedAction: -1,
                error: reported,
                cooperativeError: nil
            )
        } catch {
            return PIDTargetedActionResult(
                outcomes: [ActionOutcome(index: entry.sourceIndex, ok: false, error: .unknownOutcome)],
                lastAcknowledgedAction: -1,
                error: .unknownOutcome,
                cooperativeError: nil
            )
        }
    }

    private func preflightFreshAXScroll(
        _ entry: PlannedDispatchEntry,
        expected: ActionGuard,
        includePress: Bool = false
    ) throws {
        let requiredAction: String
        switch entry.backend {
        case .axPress:
            guard includePress else { return }
            requiredAction = kAXPressAction as String
        case .axIncrement:
            requiredAction = kAXIncrementAction as String
        case .axDecrement:
            requiredAction = kAXDecrementAction as String
        case .axSelectedText, .pidPointer, .foregroundPointer, .pidKeyboard, .foregroundKeyboard, .wait:
            return
        }
        guard let reference = entry.source.elementRef,
              let resolvedElement = entry.resolved?.element,
              let plannedElement = entry.resolved?.verifiedElement,
              let current = performer.element(reference: reference, snapshotID: expected.snapshotID),
              let currentElement = current.element,
              CFEqual(resolvedElement, currentElement),
              current.identityToken == plannedElement.identityToken,
              current.bounds == plannedElement.bounds,
              current.enabled == true,
              current.actionNames.completeNames?.contains(requiredAction) == true
        else { throw ActionExecutionError.staleSnapshot }
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

private func exactTarget(_ lhs: WindowTarget, _ rhs: WindowTarget) -> Bool {
    sameTargetIdentity(lhs, rhs) && lhs.interactionMode == rhs.interactionMode
}

private func sameTargetIdentity(_ lhs: WindowTarget, _ rhs: WindowTarget) -> Bool {
    lhs.appRef == rhs.appRef && lhs.windowRef == rhs.windowRef &&
        lhs.pid == rhs.pid && lhs.windowID == rhs.windowID &&
        lhs.bounds == rhs.bounds && lhs.axIdentity == rhs.axIdentity
}

private func guardMatchesExactTarget(_ guardValue: ActionGuard, _ target: WindowTarget) -> Bool {
    guardValue.pid == target.pid &&
        guardValue.windowID == target.windowID &&
        guardValue.bounds == target.bounds &&
        guardValue.axIdentity == target.axIdentity
}

private func foregroundAuthorityMatchesExactTarget(_ authority: ForegroundTakeoverPlanAuthority) -> Bool {
    authority.plan.interactionMode == .foregroundTakeover &&
        authority.guardValue.interactionMode == .foregroundTakeover &&
        authority.target.interactionMode == .foregroundTakeover &&
        guardMatchesExactTarget(authority.guardValue, authority.target) &&
        sameSnapshotIdentity(authority.snapshotGuard, authority.guardValue)
}

private func sameSnapshotAuthority(_ lhs: ActionGuard, _ rhs: ActionGuard) -> Bool {
    sameSnapshotIdentity(lhs, rhs) && lhs.keyboardFocus == rhs.keyboardFocus
}

private func sameSnapshotIdentity(_ lhs: ActionGuard, _ rhs: ActionGuard) -> Bool {
    lhs.pid == rhs.pid && lhs.windowID == rhs.windowID &&
        lhs.bounds == rhs.bounds && lhs.axIdentity == rhs.axIdentity &&
        lhs.focusedAXIdentity == rhs.focusedAXIdentity &&
        lhs.focusedAXBounds == rhs.focusedAXBounds &&
        lhs.focusedRootPreference == rhs.focusedRootPreference &&
        lhs.snapshotID == rhs.snapshotID
}

private func initialCursorPoint(actions: [NativeAction], target: WindowTarget) throws -> CGPoint {
    if let action = actions.first(where: { $0.x != nil && $0.y != nil }),
       let x = action.x, let y = action.y
    {
        guard x.isFinite, y.isFinite,
              x >= 0, y >= 0,
              x < target.bounds.width, y < target.bounds.height
        else { throw TakeoverError.authorityMismatch }
        return CGPoint(x: target.bounds.minX + x, y: target.bounds.minY + y)
    }
    return CGPoint(x: target.bounds.midX, y: target.bounds.midY)
}

final class CursorPIDTargetedInputPoster: PIDTargetedInputPosting {
    private let base: any PIDTargetedInputPosting
    private let cursor: any VirtualCursorPresenting

    init(
        base: any PIDTargetedInputPosting = CGPIDTargetedInputPoster(),
        cursor: any VirtualCursorPresenting
    ) {
        self.base = base
        self.cursor = cursor
    }

    func preflight(targetPID: pid_t, marker: UInt64) -> Bool {
        base.preflight(targetPID: targetPID, marker: marker)
    }

    func post(_ event: SyntheticInputEvent, to targetPID: pid_t, marker: UInt64) throws {
        try post(event, to: targetPID, marker: marker, deadline: .infinity)
    }

    func post(_ event: SyntheticInputEvent, to targetPID: pid_t, marker: UInt64, deadline: TimeInterval) throws {
        if let point = event.takeoverPoint {
            switch event {
            case .mouseDown:
                try cursor.click(at: point)
            default:
                try cursor.move(to: point)
            }
        }
        try base.post(event, to: targetPID, marker: marker, deadline: deadline)
    }
}

private extension SyntheticInputEvent {
    var takeoverPoint: CGPoint? {
        switch self {
        case let .mouseDown(point, _, _), let .mouseUp(point, _, _), let .mouseDragged(point), let .scroll(point, _, _): point
        case .unicodeKeyDown, .unicodeKeyUp, .virtualKeyDown, .virtualKeyUp: nil
        }
    }
}
