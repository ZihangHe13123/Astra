@preconcurrency import ApplicationServices
import CryptoKit
import Foundation

private let helperProcessInputDispatchNonce = UUID()

struct DispatchContext: Equatable {
    let guardValue: ActionGuard
}

struct PlannedDispatchEntry {
    let sourceIndex: Int
    let source: NativeAction
    let backend: DispatchBackend
    let actionClass: DispatchActionClass?
    let resolved: ResolvedAction?
    let pointerSafeRegion: PointerSafeRegionAuthority?
    let targetKeyboardFocus: KeyboardFocusAuthority?

    init(
        sourceIndex: Int,
        source: NativeAction,
        backend: DispatchBackend,
        actionClass: DispatchActionClass?,
        resolved: ResolvedAction?,
        pointerSafeRegion: PointerSafeRegionAuthority? = nil,
        targetKeyboardFocus: KeyboardFocusAuthority? = nil
    ) {
        self.sourceIndex = sourceIndex
        self.source = source
        self.backend = backend
        self.actionClass = actionClass
        self.resolved = resolved
        self.pointerSafeRegion = pointerSafeRegion
        self.targetKeyboardFocus = targetKeyboardFocus
    }
}

struct ForegroundPlanConsumptionAuthority {
    let planRef: String
    let snapshotID: String
    let interactionMode: InteractionMode
    let actions: [NativeAction]
    let backends: [DispatchBackend]
    let guardValue: ActionGuard
}

struct DispatchPlan {
    let planRef: String
    let interactionMode: InteractionMode
    let requiresTakeover: Bool
    let reason: String
    let actionClasses: [DispatchActionClass]
    let pidActionClasses: [DispatchActionClass]
    let lastAcknowledgedAction: Int
    let cooperativeError: CooperativeErrorCode?
    let backends: [DispatchBackend]
    let syntheticRequirements: [PlannedSyntheticRequirement]
    let stageDigest: String
    fileprivate let actionDigest: String
    fileprivate let processNonce: UUID

    init(
        planRef: String,
        interactionMode: InteractionMode,
        requiresTakeover: Bool,
        reason: String,
        actionClasses: [DispatchActionClass],
        pidActionClasses: [DispatchActionClass],
        lastAcknowledgedAction: Int,
        cooperativeError: CooperativeErrorCode?,
        backends: [DispatchBackend],
        syntheticRequirements: [PlannedSyntheticRequirement],
        stageDigest: String = "",
        actionDigest: String,
        processNonce: UUID
    ) {
        self.planRef = planRef
        self.interactionMode = interactionMode
        self.requiresTakeover = requiresTakeover
        self.reason = reason
        self.actionClasses = actionClasses
        self.pidActionClasses = pidActionClasses
        self.lastAcknowledgedAction = lastAcknowledgedAction
        self.cooperativeError = cooperativeError
        self.backends = backends
        self.syntheticRequirements = syntheticRequirements
        self.stageDigest = stageDigest
        self.actionDigest = actionDigest
        self.processNonce = processNonce
    }

    func matches(actions: [NativeAction]) -> Bool {
        actionDigest == InputDispatcher.digest(actions)
    }

    var summary: DispatchPlanSummary {
        DispatchPlanSummary(
            planRef: planRef,
            interactionMode: interactionMode,
            requiresTakeover: requiresTakeover,
            reason: reason,
            actionClasses: actionClasses,
            lastAcknowledgedAction: lastAcknowledgedAction,
            plannedPIDActionClasses: pidActionClasses
        )
    }
}

protocol InputDispatching: AnyObject {
    func plan(actions: [NativeAction], context: DispatchContext) throws -> DispatchPlan
    func execute(_ plan: DispatchPlan, context: DispatchContext) throws -> ActionBatchResult
    func invalidatePlans()
}

enum InputDispatchError: Error, Equatable {
    case backgroundActionUnsupported
}

// Routes foreground-plan guard reasons into the shared diagnostics channel.
private func consumeDiag(_ line: String) {
    logActionRejected("consume_plan \(line)")
}

final class InputDispatcher: InputDispatching {
    static let fragmentDraftLifetime: TimeInterval = 5
    static let maximumFragmentDrafts = 128
    private let performer: any ActionProviding
    private let application: PIDTargetApplication?
    private let syntheticPolicy: SyntheticInputPlanningPolicy
    private let backgroundTextInputSafety: any BackgroundTextInputSafetyDetecting
    private let processNonce: UUID
    private let fragmentDraftClock: () -> TimeInterval
    private let effectReader: any AXEffectReading
    private let effectClock: () -> TimeInterval
    private let effectSleeper: (TimeInterval) -> Void
    private let effectSettleDuration: TimeInterval
    private let effectPollInterval: TimeInterval
    private let lock = NSLock()
    private var records: [String: PlanRecord] = [:]
    private var fragmentDrafts: [String: FragmentDraftRecord] = [:]
    private var nextFragmentDraftSequence: UInt64 = 0

    var pendingFragmentDraftCount: Int {
        lock.lock()
        pruneFragmentDraftsLocked(now: fragmentDraftClock())
        let count = fragmentDrafts.count
        lock.unlock()
        return count
    }

    init(
        performer: any ActionProviding,
        application: PIDTargetApplication?,
        syntheticPolicy: SyntheticInputPlanningPolicy,
        backgroundTextInputSafety: any BackgroundTextInputSafetyDetecting = SystemBackgroundTextInputSafetyDetector(),
        processNonce: UUID = helperProcessInputDispatchNonce,
        fragmentDraftClock: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
        effectReader: any AXEffectReading = SystemAXEffectReader(),
        effectClock: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
        effectSleeper: @escaping (TimeInterval) -> Void = { Thread.sleep(forTimeInterval: $0) },
        effectSettleDuration: TimeInterval = 0.2,
        effectPollInterval: TimeInterval = 0.02
    ) {
        self.performer = performer
        self.application = application
        self.syntheticPolicy = syntheticPolicy
        self.backgroundTextInputSafety = backgroundTextInputSafety
        self.processNonce = processNonce
        self.fragmentDraftClock = fragmentDraftClock
        self.effectReader = effectReader
        self.effectClock = effectClock
        self.effectSleeper = effectSleeper
        self.effectSettleDuration = min(max(effectSettleDuration, 0), 0.25)
        self.effectPollInterval = min(max(effectPollInterval, 0.001), 0.05)
    }

    convenience init(
        performer: any ActionProviding,
        backgroundTextInputSafety: any BackgroundTextInputSafetyDetecting = SystemBackgroundTextInputSafetyDetector(),
        processNonce: UUID = helperProcessInputDispatchNonce,
        effectReader: any AXEffectReading = SystemAXEffectReader(),
        effectClock: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
        effectSleeper: @escaping (TimeInterval) -> Void = { Thread.sleep(forTimeInterval: $0) },
        effectSettleDuration: TimeInterval = 0.2,
        effectPollInterval: TimeInterval = 0.02
    ) {
        self.init(
            performer: performer,
            application: nil,
            syntheticPolicy: SyntheticInputPlanningPolicy(
                pointerCapability: .unavailable,
                keyboardCapability: .unavailable,
                registry: PIDInputCompatibilityRegistry()
            ),
            backgroundTextInputSafety: backgroundTextInputSafety,
            processNonce: processNonce,
            effectReader: effectReader,
            effectClock: effectClock,
            effectSleeper: effectSleeper,
            effectSettleDuration: effectSettleDuration,
            effectPollInterval: effectPollInterval
        )
    }

    func plan(actions: [NativeAction], context: DispatchContext) throws -> DispatchPlan {
        try plan(actions: actions, context: context, asFragmentDraft: false)
    }

    func planFragmentDraft(actions: [NativeAction], context: DispatchContext) throws -> DispatchPlan {
        try plan(actions: actions, context: context, asFragmentDraft: true)
    }

    private func plan(
        actions: [NativeAction],
        context: DispatchContext,
        asFragmentDraft: Bool
    ) throws -> DispatchPlan {
        guard actions.count <= maximumNativeActions else { throw ActionExecutionError.invalidAction }
        try verify(expected: context.guardValue, current: performer.currentTargetState())

        var entries: [PlannedDispatchEntry] = []
        var backends: [DispatchBackend] = []
        var actionClasses: [DispatchActionClass] = []
        var pidActionClasses: [DispatchActionClass] = []
        var syntheticRequirements: [PlannedSyntheticRequirement] = []
        var requiresTakeover = context.guardValue.interactionMode == .foregroundTakeover
        var cooperativeError: CooperativeErrorCode?
        let requiresSafeTextInput = actions.contains { $0.kind == .type }
        let textInputSafety = requiresSafeTextInput ? backgroundTextInputSafety.detect() : nil
        for (sourceIndex, action) in actions.enumerated() {
            var planned = try resolveBackground(
                action,
                expected: context.guardValue,
                textInputSafety: textInputSafety
            )
            if context.guardValue.interactionMode == .foregroundTakeover,
               planned.backend == .pidPointer,
               let actionClass = planned.actionClass,
               syntheticPolicy.allowsForeground(application: application, intents: [.pointer(actionClass)]) {
                planned = PlannedAction(
                    resolved: planned.resolved, backend: .foregroundPointer,
                    actionClass: actionClass,
                    pointerSafeRegion: planned.pointerSafeRegion,
                    targetKeyboardFocus: planned.targetKeyboardFocus,
                    requiresTakeover: true, cooperativeError: planned.cooperativeError
                )
            }
            if context.guardValue.interactionMode == .foregroundTakeover,
               planned.backend == .pidKeyboard,
               syntheticPolicy.allowsForeground(application: application, intents: [.textEntry]) {
                planned = PlannedAction(resolved: planned.resolved, backend: .foregroundKeyboard,
                    actionClass: planned.actionClass, targetKeyboardFocus: planned.targetKeyboardFocus,
                    requiresTakeover: true, cooperativeError: planned.cooperativeError)
            }
            backends.append(planned.backend)
            if let actionClass = planned.actionClass {
                if !actionClasses.contains(actionClass) { actionClasses.append(actionClass) }
                if planned.backend == .pidPointer, !pidActionClasses.contains(actionClass) {
                    pidActionClasses.append(actionClass)
                }
            }
            entries.append(PlannedDispatchEntry(
                sourceIndex: sourceIndex,
                source: action,
                backend: planned.backend,
                actionClass: planned.actionClass,
                resolved: planned.resolved,
                pointerSafeRegion: planned.pointerSafeRegion,
                targetKeyboardFocus: planned.targetKeyboardFocus
            ))
            if let requirement = syntheticRequirement(for: action, planned: planned) {
                syntheticRequirements.append(requirement)
            }
            requiresTakeover = requiresTakeover || planned.requiresTakeover
            if let plannedError = planned.cooperativeError {
                logPlanDenied("decision \(plannedError.rawValue) action=\(action.kind.rawValue)")
                cooperativeError = cooperativeError ?? plannedError
            }
        }
        if entries.contains(where: { [.pidPointer, .foregroundPointer].contains($0.backend) && $0.pointerSafeRegion == nil }) {
            logPlanDenied("pidPointer missing safe region")
            cooperativeError = cooperativeError ?? .backgroundActionUnsupported
        }
        if !requiresTakeover,
           !syntheticPolicy.allows(
            application: application,
            intents: syntheticRequirements.map(\.intent)
        ) {
            logPlanDenied("syntheticPolicy denied requirements=\(syntheticRequirements.count)")
            cooperativeError = cooperativeError ?? .backgroundActionUnsupported
        }
        // 这条闸保护的是"不许盲投键盘"。快照期权威在接管态下不可能存在（见上面 .type 分支的
        // 降级说明），所以放行证据不能是"焦点已验证"，只能是**动作点明确有一个目标元素**：
        // 元素身份会在投递前与激活后活读的 currentFocus 一起被 validateEnvironment 强制对齐，
        // secure / secureInput 也在那一刻重查。没点名元素的 type 等于"投给当时谁在焦点上"：
        // P1 降级授权后，兼容表已批 textEntry 的 app（registry cell 即用户授权）在接管
        // 激活后允许把焦点委托给前台系统焦点；未批 app 依旧失败关闭（盲投边界平移）。
        let keyboardTargetIsUnnamed = entries.contains {
            $0.backend == .foregroundKeyboard && $0.source.elementRef == nil
        }
        if syntheticRequirements.contains(where: { $0.backend == .foregroundKeyboard }),
           context.guardValue.keyboardFocus == nil,
           context.guardValue.interactionMode != .foregroundTakeover
            || (keyboardTargetIsUnnamed
                && !allowsTextEntry(expected: context.guardValue)) {
            logPlanDenied("foregroundKeyboard requires keyboardFocus")
            cooperativeError = cooperativeError ?? .backgroundActionUnsupported
        }
        let digest = Self.digest(actions)
        let stageDigest = Self.stageDigest(actions: actions, entries: entries)
        let reference = "plan_\(UUID().uuidString.lowercased())"
        // requires_active 的 app: pointer 动作计划阶段即强制走 takeover 激活
        // 路径, reason 用独立标识与普通 foreground takeover 区分
        // (see docs/macos-computer-use.md#input-delivery-contracts)
        let activationRequired = syntheticPolicy.pointerRequiresActive(application: application)
            && entries.contains { $0.backend == .pidPointer }
        let plan = DispatchPlan(
            planRef: reference,
            interactionMode: context.guardValue.interactionMode,
            requiresTakeover: requiresTakeover,
            reason: activationRequired
                ? CooperativeErrorCode.requiresActiveForegroundTakeover.rawValue
                : cooperativeError?.rawValue ?? (requiresTakeover ? CooperativeErrorCode.foregroundTakeoverRequired.rawValue : "background_ax_only"),
            actionClasses: actionClasses,
            pidActionClasses: pidActionClasses,
            lastAcknowledgedAction: -1,
            cooperativeError: cooperativeError,
            backends: backends,
            syntheticRequirements: syntheticRequirements,
            stageDigest: stageDigest,
            actionDigest: digest,
            processNonce: processNonce
        )
        logActionRejected(
            "PLAN-RESULT snapshotID=\(context.guardValue.snapshotID) mode=\(context.guardValue.interactionMode)"
                + " requiresTakeover=\(requiresTakeover) activationRequired=\(activationRequired)"
                + " reason=\(plan.reason) entries=\(entries.count)"
        )
        if cooperativeError == nil {
            let record = PlanRecord(
                processNonce: processNonce,
                guardValue: context.guardValue,
                actionDigest: digest,
                stageDigest: stageDigest,
                interactionMode: context.guardValue.interactionMode,
                entries: entries,
                requiresTakeover: requiresTakeover,
                cooperativeError: cooperativeError,
                syntheticRequirements: syntheticRequirements,
                requiresSafeTextInput: requiresSafeTextInput
            )
            lock.lock()
            if asFragmentDraft {
                let now = fragmentDraftClock()
                pruneFragmentDraftsLocked(now: now)
                while fragmentDrafts.count >= Self.maximumFragmentDrafts,
                      let oldest = fragmentDrafts.min(by: { $0.value.sequence < $1.value.sequence })?.key {
                    fragmentDrafts.removeValue(forKey: oldest)
                }
                let sequence = nextFragmentDraftSequence
                nextFragmentDraftSequence &+= 1
                fragmentDrafts[reference] = FragmentDraftRecord(
                    plan: plan,
                    record: record,
                    expiresAt: now + Self.fragmentDraftLifetime,
                    sequence: sequence
                )
            } else {
                records[reference] = record
            }
            lock.unlock()
        }
        return plan
    }

    @discardableResult
    func sealFragmentDraft(
        planRef: String,
        actions: [NativeAction],
        stage: ForegroundFragmentStageDeclaration,
        authority: FragmentStageAuthority
    ) throws -> DispatchPlan {
        lock.lock()
        pruneFragmentDraftsLocked(now: fragmentDraftClock())
        let draft = fragmentDrafts.removeValue(forKey: planRef)
        lock.unlock()
        guard let draft,
              draft.expiresAt >= fragmentDraftClock(),
              draft.plan.planRef == planRef,
              draft.record.guardValue.snapshotID == authority.inputSnapshotID,
              draft.plan.matches(actions: actions),
              draft.record.actionDigest == InputDispatcher.digest(actions),
              authority.stageHash == stage.stageHash,
              stage.expectedActionCount == actions.count,
              draft.plan.foregroundFragmentRequirements(actions: actions) == stage.actions
        else { throw ActionExecutionError.staleSnapshot }
        lock.lock()
        guard records[planRef] == nil else {
            lock.unlock()
            logActionRejected("STORE-DUP planRef=\(planRef)")
            throw ActionExecutionError.staleSnapshot
        }
        records[planRef] = draft.record
        lock.unlock()
        return draft.plan
    }

    func execute(_ plan: DispatchPlan, context: DispatchContext) throws -> ActionBatchResult {
        lock.lock()
        let record = records.removeValue(forKey: plan.planRef)
        fragmentDrafts.removeValue(forKey: plan.planRef)
        lock.unlock()
        // Split from one conjunctive guard into ordered single-purpose guards: same
        // evaluation order, same error code, but each rejection now names itself.
        guard let record else {
            logActionRejected("EXECUTE-NO-RECORD planRef=\(plan.planRef)")
            throw ActionExecutionError.staleSnapshot
        }
        guard plan.processNonce == processNonce, record.processNonce == processNonce else {
            logActionRejected("EXECUTE-NONCE planRef=\(plan.planRef)")
            throw ActionExecutionError.staleSnapshot
        }
        guard record.guardValue == context.guardValue else {
            logActionRejected(
                "EXECUTE-GUARD-VALUE planRef=\(plan.planRef) snapshotID=\(record.guardValue.snapshotID)"
            )
            throw ActionExecutionError.staleSnapshot
        }
        guard record.actionDigest == plan.actionDigest,
              record.stageDigest == plan.stageDigest
        else {
            logActionRejected("EXECUTE-DIGEST planRef=\(plan.planRef)")
            throw ActionExecutionError.staleSnapshot
        }
        guard record.interactionMode == plan.interactionMode,
              record.entries.map(\.backend) == plan.backends,
              record.syntheticRequirements == plan.syntheticRequirements
        else {
            logActionRejected("EXECUTE-MODE-OR-BACKENDS planRef=\(plan.planRef)")
            throw ActionExecutionError.staleSnapshot
        }
        if record.requiresSafeTextInput,
           backgroundTextInputSafety.detect() != .safeASCIIKeyboardLayout {
            throw InputDispatchError.backgroundActionUnsupported
        }
        guard record.cooperativeError == nil else { throw ActionExecutionError.invalidAction }
        guard !record.requiresTakeover, record.interactionMode == .background else {
            throw ActionExecutionError.targetNotFrontmost
        }

        var outcomes: [ActionOutcome] = []
        var acknowledged = -1
        for entry in record.entries {
            do {
                try verify(expected: record.guardValue, current: performer.currentTargetState())
                guard let action = entry.resolved else { throw ActionExecutionError.invalidAction }
                let currentElement = try preflightBackgroundAXEntry(entry, expected: record.guardValue)
                let effectDeadline = effectClock() + effectSettleDuration
                let effectProbe = makeEffectProbe(
                    entry,
                    currentElement: currentElement,
                    deadline: effectDeadline
                )
                _ = try preflightBackgroundAXEntry(entry, expected: record.guardValue)
                let performance: ActionPerformance
                do {
                    performance = try performer.perform(action)
                } catch let failure as ActionPerformFailure {
                    let reported = failure.inputStarted ? ActionExecutionError.unknownOutcome : failure.error
                    logActionRejected("EXECUTE-PERFORM snapshotID=\(record.guardValue.snapshotID) error=\(reported) inputStarted=\(failure.inputStarted)")
                    outcomes.append(ActionOutcome(index: entry.sourceIndex, ok: false, error: reported))
                    return ActionBatchResult(outcomes: outcomes, lastAcknowledgedAction: acknowledged, error: reported)
                } catch {
                    logActionRejected("EXECUTE-PERFORM-OTHER snapshotID=\(record.guardValue.snapshotID) raw=\(error)")
                    outcomes.append(ActionOutcome(index: entry.sourceIndex, ok: false, error: .unknownOutcome))
                    return ActionBatchResult(outcomes: outcomes, lastAcknowledgedAction: acknowledged, error: .unknownOutcome)
                }
                acknowledged = entry.sourceIndex
                outcomes.append(ActionOutcome(
                    index: entry.sourceIndex,
                    ok: true,
                    error: nil,
                    effectVerification: performance.effectVerification ?? verifyEffect(effectProbe)
                ))
            } catch {
                let typed = inputDispatchActionError(error)
                logActionRejected("EXECUTE-OTHER snapshotID=\(record.guardValue.snapshotID) error=\(typed) raw=\(error)")
                outcomes.append(ActionOutcome(index: entry.sourceIndex, ok: false, error: typed))
                return ActionBatchResult(outcomes: outcomes, lastAcknowledgedAction: acknowledged, error: typed)
            }
        }
        return ActionBatchResult(outcomes: outcomes, lastAcknowledgedAction: acknowledged, error: nil)
    }

    func consumeForegroundPlan(
        _ plan: DispatchPlan,
        authority: ForegroundPlanConsumptionAuthority,
        validateFocusMutation: () throws -> Void
    ) throws -> [PlannedDispatchEntry] {
        lock.lock()
        let record = records.removeValue(forKey: plan.planRef)
        fragmentDrafts.removeValue(forKey: plan.planRef)
        lock.unlock()
        guard let record else {
            consumeDiag("consumeForegroundPlan: no record for planRef=\(plan.planRef)")
            throw ActionExecutionError.staleSnapshot
        }
        guard plan.processNonce == processNonce,
              record.processNonce == processNonce,
              authority.planRef == plan.planRef,
              record.actionDigest == plan.actionDigest,
              record.actionDigest == Self.digest(authority.actions),
              record.stageDigest == plan.stageDigest
        else {
            consumeDiag("consumeForegroundPlan: digest/nonce mismatch planRef=\(plan.planRef)")
            throw ActionExecutionError.staleSnapshot
        }
        guard record.guardValue == authority.guardValue,
              record.guardValue.snapshotID == authority.snapshotID
        else {
            consumeDiag("consumeForegroundPlan: guard mismatch snapshotID=\(authority.snapshotID)")
            throw ActionExecutionError.staleSnapshot
        }
        guard record.interactionMode == .foregroundTakeover,
              authority.interactionMode == .foregroundTakeover,
              plan.interactionMode == .foregroundTakeover,
              plan.requiresTakeover,
              record.requiresTakeover,
              record.cooperativeError == nil,
              plan.cooperativeError == nil,
              record.entries.count == authority.actions.count,
              authority.backends.count == authority.actions.count,
              plan.backends == authority.backends,
              record.entries.map(\.backend) == authority.backends,
              record.syntheticRequirements == plan.syntheticRequirements
        else {
            consumeDiag("consumeForegroundPlan: mode/backends mismatch planRef=\(plan.planRef)")
            throw ActionExecutionError.staleSnapshot
        }
        // 复核必须用 consume 时刻**真实可见**的判据：`resolved`/`verifiedElement` 只挂在
        // .accessibilityText 路由上（键盘路由不带元素），所以按 role 判"会不会投真实按键码"在这里
        // 不可求值 —— 实机前一次尝试因此仍然被拒（用例红在同一个 backgroundActionUnsupported）。
        // 而这条不变量真正保护的是 **AX 文本 mutation**（`productionTextPreflightRejectsIME…`）：
        // 输入法激活时不许去写 AXSelectedText。unicode 投递免疫输入法（探针实测），故只拦会走
        // AX 写的那条批。残留缺口（如实记）：若计划时布局安全、投递前才切到拼音，而目标是非白名单
        // role（perform 会投真实按键码），本处无法识别 ⇒ 后果是文字被候选窗截走（正确性问题，
        // 非安全边界），要收口得把逐条目的形态决策随 plan 记录一起存下来。
        if record.requiresSafeTextInput,
           backgroundTextInputSafety.detect() != .safeASCIIKeyboardLayout,
           record.entries.contains(where: { $0.backend == .axSelectedText }) {
            consumeDiag("consumeForegroundPlan: unsafe layout must not perform an AX text write")
            throw InputDispatchError.backgroundActionUnsupported
        }

        for (index, entry) in record.entries.enumerated() {
            guard entry.sourceIndex == index,
                  entry.source == authority.actions[index],
                  entry.backend == authority.backends[index]
            else {
                consumeDiag("consumeForegroundPlan: entry mismatch index=\(index) sourceIndex=\(entry.sourceIndex) entries=\(record.entries.count) actions=\(authority.actions.count)")
                throw ActionExecutionError.staleSnapshot
            }
            try preflightForegroundEntry(entry, expected: record.guardValue,
                validateFocusMutation: validateFocusMutation)
        }
        try verify(expected: record.guardValue, current: performer.currentTargetState())
        return record.entries
    }

    /// Consume a background-delivery-family plan (no-takeover PID delivery).
    /// Mirrors `consumeForegroundPlan` minus the foreground authority
    /// requirements: a non-takeover pidPointer batch planned against the same
    /// guard snapshot. Keyboard entries are not yet supported by the
    /// background executor and are rejected explicitly.
    func consumeBackgroundPlan(
        _ plan: DispatchPlan,
        resolvedActions: [NativeAction]
    ) throws -> [PlannedDispatchEntry] {
        lock.lock()
        let record = records.removeValue(forKey: plan.planRef)
        fragmentDrafts.removeValue(forKey: plan.planRef)
        lock.unlock()
        guard let record,
              plan.processNonce == processNonce,
              record.processNonce == processNonce,
              record.actionDigest == plan.actionDigest,
              record.actionDigest == Self.digest(resolvedActions),
              record.stageDigest == plan.stageDigest,
              plan.interactionMode == .background,
              record.interactionMode == .background,
              !plan.requiresTakeover,
              !record.requiresTakeover,
              record.cooperativeError == nil,
              plan.cooperativeError == nil,
              record.entries.count == resolvedActions.count,
              record.entries.map(\.backend) == plan.backends,
              record.syntheticRequirements == plan.syntheticRequirements
        else { throw ActionExecutionError.staleSnapshot }
        guard record.entries.allSatisfy({ $0.backend == .pidPointer }) else {
            throw ActionExecutionError.helperFailed
        }
        return record.entries
    }

    func invalidatePlans() {
        lock.lock()
        records.removeAll(keepingCapacity: false)
        fragmentDrafts.removeAll(keepingCapacity: false)
        lock.unlock()
    }

    private func pruneFragmentDraftsLocked(now: TimeInterval) {
        fragmentDrafts = fragmentDrafts.filter { $0.value.expiresAt >= now }
    }

    private func resolveBackground(
        _ action: NativeAction,
        expected: ActionGuard,
        textInputSafety: BackgroundTextInputSafety?
    ) throws -> PlannedAction {
        guard action.elementRef == nil || action.targetElementRef == nil else {
            throw ActionExecutionError.invalidAction
        }
        let element = action.elementRef.flatMap { performer.element(reference: $0, snapshotID: expected.snapshotID) }
        if action.elementRef != nil, element == nil { throw ActionExecutionError.staleSnapshot }
        // IME 一刀切闸窄化（实测驱动，see docs/macos-computer-use.md#input-delivery-contracts）：
        // 原写法只要布局不安全就把整发 type 判死。实测证明这既不必要也不正确 —— 拼音激活下
        // virtualKey 0 + unicode 串的 postToPid 事件能把「测试」「abc」都送进 Safari 网页输入框
        // （AX value 可见、发送按钮由灰变黑）。会被输入法截走候选的是**真实按键码**。
        // 所以本闸只管一件事：这一发最终会不会投真实按键码 —— role 不在 AX 写白名单 ⇒ perform
        // 会选物理键 ⇒ 照旧拒；元素没点名 ⇒ 形态不可知 ⇒ 保守拒。AX 写路径由下面的 preflight
        // 跳过与 perform 侧守卫两处拦住，那条不变量不松。
        if action.kind == .type,
           textInputSafety != .safeASCIIKeyboardLayout,
           expected.interactionMode == .background
            || (!(element?.supportsAXSelectedTextWrite ?? false)
                && !(expected.interactionMode == .foregroundTakeover
                    && allowsTextEntry(expected: expected))) {
            logPlanDenied(
                "type blocked pre-preflight textInputSafety=\(textInputSafety.map { String(describing: $0) } ?? "nil") helperSees=\(currentKeyboardInputSourceIDForDiagnostics())"
            )
            return .unsupported(backend: .foregroundKeyboard, actionClass: .text)
        }

        switch action.kind {
        case .wait:
            return PlannedAction(
                resolved: ResolvedAction(source: action, method: .wait, screenPoint: nil, endScreenPoint: nil, element: nil),
                backend: .wait,
                actionClass: nil
            )
        case .rightClick:
            // Right-click has no AXPress semantics; always resolve to PID
            // pointer delivery (takeover or background when enabled).
            if action.x != nil || action.y != nil {
                let pointerElement = action.targetElementRef.flatMap {
                    performer.pointerElement(reference: $0, snapshotID: expected.snapshotID)
                }
                let safeRegion = try pointerSafeRegion(for: action, element: pointerElement, expected: expected)
                let backgroundDelivery = syntheticPolicy.allowsBackgroundDelivery(
                    application: application,
                    backend: .pidPointer,
                    action: .click
                )
                return .takeover(
                    backend: .pidPointer,
                    actionClass: .click,
                    pointerSafeRegion: safeRegion,
                    backgroundDelivery: backgroundDelivery
                )
            }
            let element = action.elementRef.flatMap { performer.element(reference: $0, snapshotID: expected.snapshotID) }
            if action.elementRef != nil, element == nil { throw ActionExecutionError.staleSnapshot }
            guard let element else { return .takeover(backend: .pidPointer, actionClass: .click) }
            guard !element.isSecure else { return .unsupported(backend: .axPress, actionClass: .press) }
            guard element.enabled != false else { throw ActionExecutionError.helperFailed }
            let pointerElement = action.elementRef.flatMap {
                performer.pointerElement(reference: $0, snapshotID: expected.snapshotID)
            }
            let safeRegion = try pointerSafeRegion(for: action, element: pointerElement, expected: expected)
            let backgroundDelivery = syntheticPolicy.allowsBackgroundDelivery(
                application: application,
                backend: .pidPointer,
                action: .click
            )
            return .takeover(
                backend: .pidPointer,
                actionClass: .click,
                pointerSafeRegion: safeRegion,
                backgroundDelivery: backgroundDelivery
            )
        case .click:
            if action.x != nil || action.y != nil {
                let pointerElement = action.targetElementRef.flatMap {
                    performer.pointerElement(reference: $0, snapshotID: expected.snapshotID)
                }
                let safeRegion = try pointerSafeRegion(for: action, element: pointerElement, expected: expected)
                return .takeover(backend: .pidPointer, actionClass: .click, pointerSafeRegion: safeRegion)
            }
            guard let element else { return .takeover(backend: .pidPointer, actionClass: .click) }
            guard !element.isSecure else { return .unsupported(backend: .axPress, actionClass: .press) }
            guard element.enabled != false else { throw ActionExecutionError.helperFailed }
            guard let actionNames = element.actionNames.completeNames else {
                if let error = element.actionNames.error { throw mapAXActionError(error) }
                throw ActionExecutionError.helperFailed
            }
            guard actionNames.contains(kAXPressAction as String) else {
                let pointerElement = action.elementRef.flatMap {
                    performer.pointerElement(reference: $0, snapshotID: expected.snapshotID)
                }
                let safeRegion = try pointerSafeRegion(for: action, element: pointerElement, expected: expected)
                let backgroundDelivery = syntheticPolicy.allowsBackgroundDelivery(
                    application: application,
                    backend: .pidPointer,
                    action: .click
                )
                return .takeover(
                    backend: .pidPointer,
                    actionClass: .click,
                    pointerSafeRegion: safeRegion,
                    backgroundDelivery: backgroundDelivery
                )
            }
            guard let axElement = element.element else { throw ActionExecutionError.staleSnapshot }
            return PlannedAction(
                resolved: ResolvedAction(
                    source: action,
                    method: .accessibilityPress,
                    screenPoint: nil,
                    endScreenPoint: nil,
                    element: axElement,
                    verifiedElement: element
                ),
                backend: .axPress,
                actionClass: .press,
                // A file-picker AXPress can succeed while its system panel is
                // unfocused and unbindable. Activate before that single press.
                requiresTakeover: element.opensFilePanel
            )
        case .type:
            guard let element else { return .takeover(backend: .foregroundKeyboard, actionClass: .text) }
            // 一次性把三道判据的输入全部告出：preflight 与焦点 gate 共用
            // supportsAXSelectedTextWrite(role 白名单且 roleResult 完整) + !isSecure + enabled == true，
            // 实机两次（后台/前台）都落到 .unsupported(foregroundKeyboard) 且焦点 gate 未尝试获取，
            // 却看不出是哪个谓词为假 —— 别再猜。
            logPlanDenied(
                "TYPE-PREFLIGHT role=\(element.roleResult) enabled=\(String(describing: element.enabled)) secure=\(element.isSecure)"
            )
            if action.replace != nil {
                guard action.replace == true, action.elementRef != nil,
                      element.supportsAXSelectedTextWrite, !element.isSecure, element.enabled == true,
                      textInputSafety == .safeASCIIKeyboardLayout
                else { return .unsupported(backend: .axSelectedText, actionClass: .text) }
                switch performer.preflightAXTextReplacement(element) {
                case .unsupported: return .unsupported(backend: .axSelectedText, actionClass: .text)
                case let .failed(error): throw error
                case .settable: break
                }
                guard let axElement = element.element,
                      let authority = inputDispatchKeyboardFocusAuthority(element, localToWindowBounds: expected.bounds)
                else { throw ActionExecutionError.inputFocusRequired }
                // No focus acquisition while planning; the existing foreground
                // authorization and activity lease must guard the mutation.
                return PlannedAction(resolved: ResolvedAction(source: action, method: .accessibilityText,
                    screenPoint: nil, endScreenPoint: nil, element: axElement, verifiedElement: element),
                    backend: .axSelectedText, actionClass: .text,
                    targetKeyboardFocus: authority, requiresTakeover: true)
            }
            guard !element.isSecure else { return .unsupported(backend: .axSelectedText, actionClass: .text) }
            // 焦点权威在计划期可能**根本读不到**：macOS 只在 app 处于前台时才报
            // kAXFocusedUIElement（只读探针实测：后台 -25212 kAXErrorNoValue，前台才有值），
            // 而前台恰恰是这份计划要去批准的接管结果 ⇒ 要求计划期读到焦点是顺序上不可能满足的
            // 条件（实机 FOCUS-ACQUIRE-EARLY unverified → AX-MAP raw=-25212 → PLAN-THROW）。
            // 已请求接管时保留快照里指定元素的身份和屏幕几何；不能降为 nil 而把目标
            // 偷换成激活后的任意焦点。投递前仍须与该指定元素匹配并重查 secure。
            // 后台请求没有"激活必然先于投递"这个前提 ⇒ 照旧抛出，绝不盲投。
            let targetKeyboardFocus: KeyboardFocusAuthority
            do {
                let focused = try performer.focusedKeyboardElement(matching: element)
                guard let authority = inputDispatchKeyboardFocusAuthority(focused) else {
                    throw ActionExecutionError.secureTarget
                }
                targetKeyboardFocus = authority
            } catch {
                guard expected.interactionMode == .foregroundTakeover else { throw error }
                guard let authority = inputDispatchKeyboardFocusAuthority(element, localToWindowBounds: expected.bounds) else {
                    throw ActionExecutionError.inputFocusRequired
                }
                logPlanDenied("type focus unavailable pre-activation; preserving named target for delivery")
                targetKeyboardFocus = authority
            }
            // 输入法不安全 ⇒ 连 AX 文本写的 preflight 都不做，本发强制走键盘 unicode 通道
            // （不变量：不安全布局下绝不做文本 mutation；perform 侧另有同向守卫）。
            let mutationPreflight: AXTextMutationPreflight =
                textInputSafety == .safeASCIIKeyboardLayout
                    ? performer.preflightAXTextMutation(element)
                    : .unsupported
            logActionRejected("TYPE-ROUTE safety=\(String(describing: textInputSafety)) preflight=\(mutationPreflight)")
            switch mutationPreflight {
            case .settable:
                guard let axElement = element.element else { throw ActionExecutionError.staleSnapshot }
                return PlannedAction(
                    resolved: ResolvedAction(
                        source: action,
                        method: .accessibilityText,
                        screenPoint: nil,
                        endScreenPoint: nil,
                        element: axElement,
                        verifiedElement: element
                    ),
                    backend: .axSelectedText,
                    actionClass: .text,
                    targetKeyboardFocus: targetKeyboardFocus
                )
            case .unsupported:
                return .takeover(
                    backend: .foregroundKeyboard,
                    actionClass: .text,
                    targetKeyboardFocus: targetKeyboardFocus
                )
            case let .failed(error):
                throw error
            }
        case .keypress:
            if let element, element.isSecure {
                logPlanDenied("keypress secureFail ref=\(action.elementRef ?? "?") role=\(element.roleResult.status == .complete ? (element.roleResult.value ?? "nil") : "incomplete-\(element.roleResult.status)")")
                return .unsupported(backend: .foregroundKeyboard, actionClass: .text)
            }
            if ApprovedKeyChord(action: action) == nil {
                logPlanDenied("keypress chordNil key=\(action.key ?? "?")")
                return .unsupported(backend: .foregroundKeyboard, actionClass: .text)
            }
            if let element {
                let targetFocus: KeyboardFocusAuthority
                do {
                    let focused = try performer.focusedKeyboardElement(matching: element)
                    guard let authority = inputDispatchKeyboardFocusAuthority(focused) else {
                        throw ActionExecutionError.inputFocusRequired
                    }
                    targetFocus = authority
                } catch {
                    guard expected.interactionMode == .foregroundTakeover else { throw error }
                    guard let authority = inputDispatchKeyboardFocusAuthority(element, localToWindowBounds: expected.bounds) else {
                        throw ActionExecutionError.inputFocusRequired
                    }
                    targetFocus = authority
                }
                // PID/window-command delivery must not erase an explicit field
                // recipient. The foreground executor checks it before posting.
                return .takeover(backend: .foregroundKeyboard, actionClass: .text, targetKeyboardFocus: targetFocus)
            }
            let backgroundDelivery = syntheticPolicy.allowsBackgroundDelivery(
                application: application,
                backend: .pidKeyboard,
                action: .text
            )
            return .takeover(
                backend: .pidKeyboard,
                actionClass: .text,
                backgroundDelivery: backgroundDelivery
            )
        case .doubleClick:
            if let element, element.isSecure { return .unsupported(backend: .pidPointer, actionClass: .doubleClick) }
            let pointerElement = pointerReference(for: action).flatMap {
                performer.pointerElement(reference: $0, snapshotID: expected.snapshotID)
            }
            let doubleClickRegion = try pointerSafeRegion(for: action, element: pointerElement, expected: expected)
            let backgroundDelivery = syntheticPolicy.allowsBackgroundDelivery(
                application: application,
                backend: .pidPointer,
                action: .doubleClick
            )
            return .takeover(
                backend: .pidPointer,
                actionClass: .doubleClick,
                pointerSafeRegion: doubleClickRegion,
                backgroundDelivery: backgroundDelivery
            )
        case .scroll:
            if let element,
               action.x == nil,
               action.y == nil,
               action.deltaX == nil || action.deltaX == 0,
               let deltaY = action.deltaY,
               deltaY != 0,
               !element.isSecure,
               element.enabled == true,
               let actionNames = element.actionNames.completeNames {
                let axAction = deltaY > 0 ? kAXIncrementAction as String : kAXDecrementAction as String
                if actionNames.contains(axAction) {
                    guard let axElement = element.element else { throw ActionExecutionError.staleSnapshot }
                    return PlannedAction(
                        resolved: ResolvedAction(
                            source: action,
                            method: deltaY > 0 ? .accessibilityIncrement : .accessibilityDecrement,
                            screenPoint: nil,
                            endScreenPoint: nil,
                            element: axElement,
                            verifiedElement: element
                        ),
                        backend: deltaY > 0 ? .axIncrement : .axDecrement,
                        actionClass: .scroll
                    )
                }
            }
            if let reference = action.elementRef,
               action.x == nil,
               action.y == nil,
               action.deltaX == nil || action.deltaX == 0,
               let deltaY = action.deltaY,
               deltaY != 0,
               let target = performer.scrollPressTarget(
                   reference: reference,
                   snapshotID: expected.snapshotID,
                   direction: deltaY > 0 ? .increment : .decrement
               ),
               inputDispatchValidScrollPressTarget(
                   target,
                   direction: deltaY > 0 ? .increment : .decrement
               ),
               let buttonElement = target.button.element,
               let ownerElement = target.owner.element {
                return PlannedAction(
                    resolved: ResolvedAction(
                        source: action,
                        method: .accessibilityPress,
                        screenPoint: nil,
                        endScreenPoint: nil,
                        element: buttonElement,
                        verifiedElement: target.button,
                        effectElement: ownerElement,
                        effectVerifiedElement: target.owner
                    ),
                    backend: .axPress,
                    actionClass: .scroll
                )
            }
            if pointerReference(for: action) == nil, pointerLocalPoints(action).isEmpty {
                return .unsupported(backend: .pidPointer, actionClass: .scroll)
            }
            let pointerElement = pointerReference(for: action).flatMap {
                performer.pointerElement(reference: $0, snapshotID: expected.snapshotID)
            }
            let scrollRegion = try pointerSafeRegion(for: action, element: pointerElement, expected: expected)
            let backgroundDelivery = syntheticPolicy.allowsBackgroundDelivery(
                application: application,
                backend: .pidPointer,
                action: .scroll
            )
            return .takeover(
                backend: .pidPointer,
                actionClass: .scroll,
                pointerSafeRegion: scrollRegion,
                backgroundDelivery: backgroundDelivery
            )
        case .drag:
            let pointerElement = action.targetElementRef.flatMap {
                performer.pointerElement(reference: $0, snapshotID: expected.snapshotID)
            }
            let dragRegion = try pointerSafeRegion(for: action, element: pointerElement, expected: expected)
            let backgroundDelivery = syntheticPolicy.allowsBackgroundDelivery(
                application: application,
                backend: .pidPointer,
                action: .drag
            )
            return .takeover(
                backend: .pidPointer,
                actionClass: .drag,
                pointerSafeRegion: dragRegion,
                backgroundDelivery: backgroundDelivery
            )
        }
    }

    private func syntheticRequirement(
        for action: NativeAction,
        planned: PlannedAction
    ) -> PlannedSyntheticRequirement? {
        guard let actionClass = planned.actionClass else { return nil }
        switch planned.backend {
        case .pidPointer, .foregroundPointer:
            return PlannedSyntheticRequirement(
                backend: planned.backend,
                actionClass: actionClass,
                intent: .pointer(actionClass)
            )
        case .foregroundKeyboard:
            if action.kind == .type {
                return PlannedSyntheticRequirement(
                    backend: .foregroundKeyboard,
                    actionClass: actionClass,
                    intent: .textEntry
                )
            }
            guard let chord = ApprovedKeyChord(action: action) else { return nil }
            return PlannedSyntheticRequirement(
                backend: .foregroundKeyboard,
                actionClass: actionClass,
                intent: .keyChord(chord)
            )
        case .pidKeyboard:
            if action.kind == .type {
                return PlannedSyntheticRequirement(
                    backend: .pidKeyboard,
                    actionClass: actionClass,
                    intent: .textEntry
                )
            }
            guard let chord = ApprovedKeyChord(action: action) else { return nil }
            return PlannedSyntheticRequirement(
                backend: .pidKeyboard,
                actionClass: actionClass,
                intent: .keyChord(chord)
            )
        case .axPress, .axIncrement, .axDecrement, .axSelectedText, .wait:
            return nil
        }
    }

    private func preflightBackgroundAXEntry(
        _ entry: PlannedDispatchEntry,
        expected: ActionGuard
    ) throws -> ActionElement? {
        switch entry.backend {
        case .axIncrement:
            guard entry.actionClass == .scroll,
                  entry.resolved?.method == .accessibilityIncrement
            else { throw ActionExecutionError.invalidAction }
            return try preflightFreshAXElement(
                entry,
                expected: expected,
                requiredAction: kAXIncrementAction as String
            )
        case .axDecrement:
            guard entry.actionClass == .scroll,
                  entry.resolved?.method == .accessibilityDecrement
            else { throw ActionExecutionError.invalidAction }
            return try preflightFreshAXElement(
                entry,
                expected: expected,
                requiredAction: kAXDecrementAction as String
            )
        case .axPress:
            guard entry.resolved?.method == .accessibilityPress else {
                throw ActionExecutionError.invalidAction
            }
            if entry.actionClass == .scroll {
                return try preflightFreshAXScrollPress(entry, expected: expected)
            }
            guard entry.actionClass == .press else { throw ActionExecutionError.invalidAction }
            return try preflightFreshAXElement(entry, expected: expected, requiredAction: kAXPressAction as String)
        case .axSelectedText:
            guard entry.actionClass == .text,
                  entry.resolved?.method == .accessibilityText
            else { throw ActionExecutionError.invalidAction }
            let current = try preflightFreshAXElement(
                entry,
                expected: expected,
                requiredAction: nil
            )
            guard entry.source.replace == nil else { throw InputDispatchError.backgroundActionUnsupported }
            guard !current.isSecure else { throw ActionExecutionError.secureTarget }
            _ = try performer.focusedKeyboardElement(matching: current)
            switch performer.preflightAXTextMutation(current) {
            case .settable: return current
            case .unsupported: throw InputDispatchError.backgroundActionUnsupported
            case let .failed(error): throw error
            }
        case .pidKeyboard, .pidPointer, .foregroundPointer, .foregroundKeyboard, .wait:
            return nil
        }
    }

    private var effectVerifier: AXActionEffectVerifier {
        AXActionEffectVerifier(reader: effectReader, clock: effectClock,
            sleeper: effectSleeper, pollInterval: effectPollInterval)
    }

    private func makeEffectProbe(_ entry: PlannedDispatchEntry, currentElement: ActionElement?,
                                 deadline: TimeInterval) -> AXActionEffectVerifier.EffectProbe? {
        effectVerifier.makeProbe(entry, currentElement: currentElement, deadline: deadline)
    }

    private func verifyEffect(_ probe: AXActionEffectVerifier.EffectProbe?) -> ActionEffectVerification {
        effectVerifier.verify(probe)
    }

    private func preflightForegroundEntry(_ entry: PlannedDispatchEntry, expected: ActionGuard,
        validateFocusMutation: () throws -> Void
    ) throws {
        switch entry.backend {
        case .axPress:
            guard entry.resolved?.method == .accessibilityPress else {
                throw ActionExecutionError.invalidAction
            }
            if entry.actionClass == .scroll {
                _ = try preflightFreshAXScrollPress(entry, expected: expected)
            } else {
                guard entry.actionClass == .press else { throw ActionExecutionError.invalidAction }
                try preflightFreshAXElement(entry, expected: expected, requiredAction: kAXPressAction as String)
            }
        case .axIncrement:
            guard entry.actionClass == .scroll,
                  entry.resolved?.method == .accessibilityIncrement
            else { throw ActionExecutionError.invalidAction }
            try preflightFreshAXElement(
                entry,
                expected: expected,
                requiredAction: kAXIncrementAction as String
            )
        case .axDecrement:
            guard entry.actionClass == .scroll,
                  entry.resolved?.method == .accessibilityDecrement
            else { throw ActionExecutionError.invalidAction }
            try preflightFreshAXElement(
                entry,
                expected: expected,
                requiredAction: kAXDecrementAction as String
            )
        case .axSelectedText:
            guard entry.actionClass == .text,
                  entry.resolved?.method == .accessibilityText
            else { throw ActionExecutionError.invalidAction }
            let current = try preflightFreshAXElement(entry, expected: expected, requiredAction: nil)
            guard !current.isSecure else { throw ActionExecutionError.secureTarget }
            // Replacement consume is read-only. Focus acquisition belongs to
            // performReplacement, where this invocation's activity lease guards it.
            if entry.source.replace != true {
                _ = try performer.focusedKeyboardElement(matching: current,
                    validateFocusMutation: validateFocusMutation)
            }
            switch entry.source.replace == true
                ? performer.preflightAXTextReplacement(current) : performer.preflightAXTextMutation(current) {
            case .settable: break
            case .unsupported: throw InputDispatchError.backgroundActionUnsupported
            case let .failed(error): throw error
            }
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
                  [.click, .doubleClick, .scroll, .drag].contains(actionClass),
                  entry.pointerSafeRegion != nil
            else { throw ActionExecutionError.invalidAction }
        case .foregroundKeyboard:
            guard entry.resolved == nil,
                  entry.actionClass == .text,
                  entry.source.kind == .type ||
                    (entry.source.kind == .keypress && ApprovedKeyChord(action: entry.source) != nil)
            else { throw ActionExecutionError.invalidAction }
            if entry.targetKeyboardFocus != nil {
                try preflightFreshTargetedKeyboardElement(entry, expected: expected,
                    validateFocusMutation: validateFocusMutation)
            }
        case .pidKeyboard:
            guard entry.resolved == nil,
                  entry.actionClass == .text,
                  entry.source.kind == .keypress && ApprovedKeyChord(action: entry.source) != nil
            else { throw ActionExecutionError.invalidAction }
        }
    }

    private func preflightFreshTargetedKeyboardElement(
        _ entry: PlannedDispatchEntry,
        expected: ActionGuard,
        validateFocusMutation: () throws -> Void
    ) throws {
        guard let reference = entry.source.elementRef,
              let retainedFocus = entry.targetKeyboardFocus,
              let current = performer.element(reference: reference, snapshotID: expected.snapshotID),
              let currentFocus = inputDispatchKeyboardFocusAuthority(
                  current,
                  localToWindowBounds: expected.bounds
              ),
              currentFocus == retainedFocus
        else { throw ActionExecutionError.staleSnapshot }
        _ = try performer.focusedKeyboardElement(matching: current,
            validateFocusMutation: validateFocusMutation)
    }

    @discardableResult
    private func preflightFreshAXElement(
        _ entry: PlannedDispatchEntry,
        expected: ActionGuard,
        requiredAction: String?
    ) throws -> ActionElement {
        func rejected(_ why: String) -> ActionExecutionError {
            logActionRejected("PREFLIGHT-AX \(why)")
            return ActionExecutionError.staleSnapshot
        }
        guard let reference = entry.source.elementRef else {
            throw rejected("a no-elementRef")
        }
        guard let resolvedElement = entry.resolved?.element else {
            throw rejected("b no-resolved-element")
        }
        guard let plannedElement = entry.resolved?.verifiedElement else {
            throw rejected("c no-verified-element")
        }
        guard let current = performer.element(reference: reference, snapshotID: expected.snapshotID) else {
            throw rejected("d element-lookup-nil ref=\(reference) wantSnapshot=\(expected.snapshotID)")
        }
        guard let currentElement = current.element else {
            throw rejected("e current-ax-handle-nil")
        }
        guard CFEqual(resolvedElement, currentElement) else {
            throw rejected("f ax-handle-changed")
        }
        guard current.identityToken == plannedElement.identityToken else {
            throw rejected("g identity-token-changed")
        }
        guard current.bounds == plannedElement.bounds else {
            throw rejected("h bounds-moved planned=\(plannedElement.bounds) current=\(current.bounds)")
        }
        // 只拒绝「确认禁用」。AXEnabled 读不出值时是 nil（WebKit 远程供给的元素在
        // accessibility token 就绪前会这样），nil 不是禁用证据；若元素真的失效，随后
        // 的 AX 调用会返回 invalidUIElement，由既有映射正确报为 stale。
        // 与 PIDTargetedInput.prepareSafeRegion 的 `enabled != false` 保持同一约定。
        guard current.enabled != false else {
            throw rejected("i disabled enabled=\(String(describing: current.enabled))")
        }
        if let requiredAction {
            guard current.actionNames.completeNames?.contains(requiredAction) == true else {
                throw rejected("j required-action-missing want=\(requiredAction) have=\(String(describing: current.actionNames.completeNames))")
            }
        }
        return current
    }

    private func preflightFreshAXScrollPress(
        _ entry: PlannedDispatchEntry,
        expected: ActionGuard
    ) throws -> ActionElement {
        guard let reference = entry.source.elementRef,
              let deltaY = entry.source.deltaY,
              deltaY != 0,
              entry.source.deltaX == nil || entry.source.deltaX == 0,
              let plannedButton = entry.resolved?.verifiedElement,
              let plannedOwner = entry.resolved?.effectVerifiedElement,
              let plannedButtonElement = entry.resolved?.element,
              let plannedOwnerElement = entry.resolved?.effectElement,
              let current = performer.scrollPressTarget(
                  reference: reference,
                  snapshotID: expected.snapshotID,
                  direction: deltaY > 0 ? .increment : .decrement
              ),
              let currentButtonElement = current.button.element,
              let currentOwnerElement = current.owner.element,
              CFEqual(plannedButtonElement, currentButtonElement),
              CFEqual(plannedOwnerElement, currentOwnerElement),
              current.button.identityToken == plannedButton.identityToken,
              current.owner.identityToken == plannedOwner.identityToken,
              current.button.bounds == plannedButton.bounds,
              current.owner.bounds == plannedOwner.bounds,
              inputDispatchValidScrollPressTarget(
                  current,
                  direction: deltaY > 0 ? .increment : .decrement
              )
        else { throw ActionExecutionError.staleSnapshot }
        return current.owner
    }

    private func verify(expected: ActionGuard, current: ActionTargetState) throws {
        guard current.pid == expected.pid else { throw ActionExecutionError.targetNotFrontmost }
        guard current.windowID == expected.windowID, current.axIdentity == expected.axIdentity else {
            logActionRejected("DISPATCH-VERIFY window-identity")
            throw ActionExecutionError.staleSnapshot
        }
        guard inputDispatchFiniteRect(current.bounds) else {
            logActionRejected("DISPATCH-VERIFY bounds-nonfinite")
            throw ActionExecutionError.staleSnapshot
        }
        guard inputDispatchApproximatelyEqual(current.bounds, expected.bounds) else {
            logActionRejected("DISPATCH-VERIFY bounds-drift expected=\(expected.bounds) current=\(current.bounds)")
            throw ActionExecutionError.staleSnapshot
        }
        guard current.focusedAXIdentity == expected.focusedAXIdentity else {
            logActionRejected("DISPATCH-VERIFY focused-identity")
            throw ActionExecutionError.staleSnapshot
        }
        guard inputDispatchFiniteRect(current.focusedAXBounds) else {
            logActionRejected("DISPATCH-VERIFY focused-bounds-nonfinite")
            throw ActionExecutionError.staleSnapshot
        }
        guard inputDispatchApproximatelyEqual(current.focusedAXBounds, expected.focusedAXBounds) else {
            logActionRejected("DISPATCH-VERIFY focused-bounds-drift expected=\(expected.focusedAXBounds) current=\(current.focusedAXBounds)")
            throw ActionExecutionError.staleSnapshot
        }
        guard current.focusedRootPreference == expected.focusedRootPreference else {
            logActionRejected("DISPATCH-VERIFY focused-root-preference")
            throw ActionExecutionError.staleSnapshot
        }
    }

    static func digest(_ actions: [NativeAction]) -> String {
        var bytes: [UInt8] = []
        for action in actions {
            append(action.kind.rawValue, to: &bytes)
            append(action.x.map { String(Double($0).bitPattern) }, to: &bytes)
            append(action.y.map { String(Double($0).bitPattern) }, to: &bytes)
            append(action.endX.map { String(Double($0).bitPattern) }, to: &bytes)
            append(action.endY.map { String(Double($0).bitPattern) }, to: &bytes)
            append(action.text, to: &bytes)
            append(action.key, to: &bytes)
            append(action.deltaX.map { String(Double($0).bitPattern) }, to: &bytes)
            append(action.deltaY.map { String(Double($0).bitPattern) }, to: &bytes)
            append(action.durationMS.map(String.init), to: &bytes)
            append(action.elementRef, to: &bytes)
            append(action.targetElementRef, to: &bytes)
            append(String(action.modifiers.count), to: &bytes)
            action.modifiers.forEach { append($0, to: &bytes) }
            // Preserve existing action digests when no checked goal was supplied.
            if let checked = action.checked { append("checked:\(checked)", to: &bytes) }
            if let replace = action.replace { append("replace:\(replace)", to: &bytes) }
        }
        return SHA256.hash(data: Data(bytes)).map { String(format: "%02x", $0) }.joined()
    }

    static func stageDigest(actions: [NativeAction], entries: [PlannedDispatchEntry]) -> String {
        var bytes: [UInt8] = []
        append(digest(actions), to: &bytes)
        append(String(entries.count), to: &bytes)
        for entry in entries {
            append(String(entry.sourceIndex), to: &bytes)
            append(entry.backend.rawValue, to: &bytes)
            append(entry.actionClass?.rawValue, to: &bytes)
            append(entry.pointerSafeRegion?.reference, to: &bytes)
            append(entry.pointerSafeRegion?.identityToken, to: &bytes)
            append(entry.targetKeyboardFocus?.identityToken, to: &bytes)
            append(entry.targetKeyboardFocus?.role, to: &bytes)
            append(entry.targetKeyboardFocus?.subrole, to: &bytes)
            if let bounds = entry.targetKeyboardFocus?.bounds {
                append(String(Double(bounds.origin.x).bitPattern), to: &bytes)
                append(String(Double(bounds.origin.y).bitPattern), to: &bytes)
                append(String(Double(bounds.width).bitPattern), to: &bytes)
                append(String(Double(bounds.height).bitPattern), to: &bytes)
            } else {
                append(nil, to: &bytes)
                append(nil, to: &bytes)
                append(nil, to: &bytes)
                append(nil, to: &bytes)
            }
            if let bounds = entry.pointerSafeRegion?.bounds {
                append(String(Double(bounds.origin.x).bitPattern), to: &bytes)
                append(String(Double(bounds.origin.y).bitPattern), to: &bytes)
                append(String(Double(bounds.width).bitPattern), to: &bytes)
                append(String(Double(bounds.height).bitPattern), to: &bytes)
            } else {
                append(nil, to: &bytes)
                append(nil, to: &bytes)
                append(nil, to: &bytes)
                append(nil, to: &bytes)
            }
        }
        return SHA256.hash(data: Data(bytes)).map { String(format: "%02x", $0) }.joined()
    }

    private func allowsTextEntry(expected: ActionGuard) -> Bool {
        (expected.interactionMode == .foregroundTakeover &&
            syntheticPolicy.allowsForeground(application: application, intents: [.textEntry])) ||
            syntheticPolicy.allows(application: application, intents: [.textEntry])
    }

    private func pointerSafeRegion(
        for action: NativeAction,
        element: ActionElement?,
        expected: ActionGuard
    ) throws -> PointerSafeRegionAuthority? {
        guard let reference = pointerReference(for: action) else {
            let points = pointerLocalPoints(action)
            guard !points.isEmpty else { return nil }
            guard expected.interactionMode == .foregroundTakeover,
                  syntheticPolicy.allowsForeground(application: application, intents: [.pointer(.click)])
            else { throw ActionExecutionError.invalidAction }
            let localBounds = CGRect(origin: .zero, size: expected.bounds.size)
            guard inputDispatchValidLocalRect(localBounds, within: expected.bounds.size),
                  points.allSatisfy({ inputDispatchStrictlyContains(localBounds, point: $0) })
            else { throw ActionExecutionError.outOfBounds }
            return PointerSafeRegionAuthority(
                reference: foregroundWindowRegionReference,
                identityToken: expected.snapshotID, bounds: localBounds
            )
        }
        guard let element else { throw ActionExecutionError.staleSnapshot }
        guard element.enabled != false, !element.isSecure else { throw ActionExecutionError.staleSnapshot }
        guard inputDispatchValidLocalRect(element.bounds, within: expected.bounds.size) else {
            throw ActionExecutionError.outOfBounds
        }
        for point in pointerLocalPoints(action) {
            guard inputDispatchStrictlyContains(element.bounds, point: point) else {
                throw ActionExecutionError.outOfBounds
            }
        }
        return PointerSafeRegionAuthority(
            reference: reference,
            identityToken: element.identityToken,
            bounds: element.bounds
        )
    }

    private func pointerReference(for action: NativeAction) -> String? {
        pointerLocalPoints(action).isEmpty ? action.elementRef : action.targetElementRef
    }

    private static func append(_ value: String?, to bytes: inout [UInt8]) {
        guard let value else {
            bytes.append(contentsOf: [0, 0, 0, 0])
            return
        }
        let encoded = Array(value.utf8)
        var count = UInt32(encoded.count).bigEndian
        withUnsafeBytes(of: &count) { bytes.append(contentsOf: $0) }
        bytes.append(contentsOf: encoded)
    }
}

private struct PlanRecord {
    let processNonce: UUID
    let guardValue: ActionGuard
    let actionDigest: String
    let stageDigest: String
    let interactionMode: InteractionMode
    let entries: [PlannedDispatchEntry]
    let requiresTakeover: Bool
    let cooperativeError: CooperativeErrorCode?
    let syntheticRequirements: [PlannedSyntheticRequirement]
    let requiresSafeTextInput: Bool
}

private struct FragmentDraftRecord {
    let plan: DispatchPlan
    let record: PlanRecord
    let expiresAt: TimeInterval
    let sequence: UInt64
}

private struct PlannedAction {
    let resolved: ResolvedAction?
    let backend: DispatchBackend
    let actionClass: DispatchActionClass?
    let pointerSafeRegion: PointerSafeRegionAuthority?
    let targetKeyboardFocus: KeyboardFocusAuthority?
    let requiresTakeover: Bool
    let cooperativeError: CooperativeErrorCode?

    init(
        resolved: ResolvedAction?,
        backend: DispatchBackend,
        actionClass: DispatchActionClass?,
        pointerSafeRegion: PointerSafeRegionAuthority? = nil,
        targetKeyboardFocus: KeyboardFocusAuthority? = nil,
        requiresTakeover: Bool = false,
        cooperativeError: CooperativeErrorCode? = nil
    ) {
        self.resolved = resolved
        self.backend = backend
        self.actionClass = actionClass
        self.pointerSafeRegion = pointerSafeRegion
        self.targetKeyboardFocus = targetKeyboardFocus
        self.requiresTakeover = requiresTakeover
        self.cooperativeError = cooperativeError
    }

    static func takeover(
        backend: DispatchBackend,
        actionClass: DispatchActionClass,
        pointerSafeRegion: PointerSafeRegionAuthority? = nil,
        targetKeyboardFocus: KeyboardFocusAuthority? = nil,
        backgroundDelivery: Bool = false
    ) -> Self {
        Self(
            resolved: nil,
            backend: backend,
            actionClass: actionClass,
            pointerSafeRegion: pointerSafeRegion,
            targetKeyboardFocus: targetKeyboardFocus,
            requiresTakeover: !backgroundDelivery
        )
    }

    static func unsupported(backend: DispatchBackend, actionClass: DispatchActionClass) -> Self {
        Self(resolved: nil, backend: backend, actionClass: actionClass, cooperativeError: .backgroundActionUnsupported)
    }
}

private func pointerLocalPoints(_ action: NativeAction) -> [CGPoint] {
    var points: [CGPoint] = []
    if let x = action.x, let y = action.y { points.append(CGPoint(x: x, y: y)) }
    if let endX = action.endX, let endY = action.endY { points.append(CGPoint(x: endX, y: endY)) }
    return points
}

private func inputDispatchStrictlyContains(_ rect: CGRect, point: CGPoint) -> Bool {
    point.x.isFinite && point.y.isFinite &&
        point.x > rect.minX && point.x < rect.maxX && point.y > rect.minY && point.y < rect.maxY
}

private func inputDispatchValidLocalRect(_ rect: CGRect, within size: CGSize) -> Bool {
    inputDispatchFiniteRect(rect) && rect.minX >= 0 && rect.minY >= 0 &&
        rect.maxX <= size.width && rect.maxY <= size.height
}

private func inputDispatchActionError(_ error: Error) -> ActionExecutionError {
    error as? ActionExecutionError ?? .helperFailed
}

private func inputDispatchKeyboardFocusAuthority(
    _ element: ActionElement,
    localToWindowBounds windowBounds: CGRect? = nil
) -> KeyboardFocusAuthority? {
    guard element.enabled == true,
          !element.isSecure,
          inputDispatchFiniteRect(element.bounds),
          !element.identityToken.isEmpty,
          element.identityToken.unicodeScalars.count <= maximumAXStringCharacters,
          element.roleResult.status == .complete,
          let role = element.roleResult.value,
          !role.isEmpty,
          role.unicodeScalars.count <= maximumAXStringCharacters,
          element.subroleResult.status == .complete,
          (element.subroleResult.value?.unicodeScalars.count ?? 0) <= maximumAXStringCharacters
    else { return nil }
    let bounds = windowBounds.map { window in
        CGRect(
            x: window.minX + element.bounds.minX,
            y: window.minY + element.bounds.minY,
            width: element.bounds.width,
            height: element.bounds.height
        )
    } ?? element.bounds
    return KeyboardFocusAuthority(
        identityToken: element.identityToken,
        bounds: bounds,
        role: role,
        subrole: element.subroleResult.value
    )
}

private func inputDispatchFiniteRect(_ rect: CGRect) -> Bool {
    rect.origin.x.isFinite && rect.origin.y.isFinite && rect.width.isFinite && rect.height.isFinite && rect.width > 0 && rect.height > 0
}

private func inputDispatchApproximatelyEqual(_ lhs: CGRect, _ rhs: CGRect) -> Bool {
    abs(lhs.origin.x - rhs.origin.x) <= 1 && abs(lhs.origin.y - rhs.origin.y) <= 1 && abs(lhs.width - rhs.width) <= 1 && abs(lhs.height - rhs.height) <= 1
}

private func inputDispatchValidScrollPressTarget(
    _ target: AXScrollPressTarget,
    direction: AXScrollDirection
) -> Bool {
    let allowedSubroles = [direction.pageSubrole, direction.arrowSubrole]
    return target.owner.enabled != false
        && !target.owner.isSecure
        && target.owner.roleResult.status == .complete
        && target.owner.roleResult.value == kAXScrollBarRole as String
        && target.button.enabled != false
        && !target.button.isSecure
        && target.button.roleResult.status == .complete
        && target.button.roleResult.value == kAXButtonRole as String
        && target.button.subroleResult.status == .complete
        && allowedSubroles.contains(target.button.subroleResult.value ?? "")
        && target.button.actionNames.completeNames?.contains(kAXPressAction as String) == true
}

private func logPlanDenied(_ detail: String) {
    let line = "[plan_denied] \(detail)\n"
    if let handle = fopen("/tmp/astra-sipp-diagnostics.log", "a") {
        _ = fputs(line, handle)
        fclose(handle)
    }
}

/// Names the guard that refused an action before any input was produced. Shares the
/// `[plan_denied]` file and line convention so one operator log explains the whole
/// plan-to-dispatch decision. Internal so every file in the module can report into it.
/// 每次调用新建 formatter：这是稀有的拒绝路径，可读性与线程安全优先于微性能。
/// 带 UTC 时间戳是为了能用时间窗把真机复现和 swift test 写进同一文件的结果分开。
func logActionRejected(_ detail: String) {
    let stamp = ISO8601DateFormatter().string(from: Date())
    let line = "[action_rejected] \(stamp) \(detail)\n"
    if let handle = fopen("/tmp/astra-sipp-diagnostics.log", "a") {
        _ = fputs(line, handle)
        fclose(handle)
    }
}
