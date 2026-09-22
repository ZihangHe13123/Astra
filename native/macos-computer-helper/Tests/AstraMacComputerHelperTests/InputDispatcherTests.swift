@testable import AstraMacComputerHelperCore
import ApplicationServices
import Carbon
import CoreGraphics
import Testing

@Test func fragmentDraftIsNotConsumableBeforeExactSingleUseSeal() throws {
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy()
    )
    let context = DispatchContext(guardValue: inputDispatcherForegroundGuard())
    let actions = [NativeAction.click(x: 15, y: 15, within: "fallback")]
    let draft = try dispatcher.planFragmentDraft(actions: actions, context: context)

    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try dispatcher.consumeForegroundPlan(
            draft,
            authority: foregroundConsumptionAuthority(
                plan: draft,
                context: context,
                actions: actions
            )
        )
    }
    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try dispatcher.sealFragmentDraft(
            planRef: draft.planRef,
            actions: actions,
            stage: .init(
                stageHash: String(repeating: "0", count: 64),
                expectedActionCount: 1,
                actions: draft.foregroundFragmentRequirements(actions: actions) ?? []
            ),
            authority: .init(
                fragmentHash: String(repeating: "a", count: 64),
                stageIndex: 0,
                stageHash: String(repeating: "0", count: 64),
                inputSnapshotID: context.guardValue.snapshotID
            )
        )
    }
    #expect(performer.performed.isEmpty)
}

@Test func exactFragmentDraftSealMovesOnlyThatDraftIntoConsumableRecords() throws {
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy()
    )
    let context = DispatchContext(guardValue: inputDispatcherForegroundGuard())
    let actions = [NativeAction.click(x: 15, y: 15, within: "fallback")]
    let draft = try dispatcher.planFragmentDraft(actions: actions, context: context)
    let stage = ForegroundFragmentStageDeclaration(
        stageHash: String(repeating: "0", count: 64),
        expectedActionCount: 1,
        actions: draft.foregroundFragmentRequirements(actions: actions) ?? []
    )
    let authority = FragmentStageAuthority(
        fragmentHash: String(repeating: "a", count: 64),
        stageIndex: 0,
        stageHash: stage.stageHash,
        inputSnapshotID: context.guardValue.snapshotID
    )

    _ = try dispatcher.sealFragmentDraft(
        planRef: draft.planRef,
        actions: actions,
        stage: stage,
        authority: authority
    )
    let entries = try dispatcher.consumeForegroundPlan(
        draft,
        authority: foregroundConsumptionAuthority(
            plan: draft,
            context: context,
            actions: actions
        )
    )

    #expect(entries.count == 1)
    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try dispatcher.sealFragmentDraft(
            planRef: draft.planRef,
            actions: actions,
            stage: stage,
            authority: authority
        )
    }
}

@Test func expiredFragmentDraftIsConsumedAndCannotBeSealedOrExecuted() throws {
    var now: TimeInterval = 10
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy(),
        fragmentDraftClock: { now }
    )
    let context = DispatchContext(guardValue: inputDispatcherForegroundGuard())
    let actions = [NativeAction.click(x: 15, y: 15, within: "fallback")]
    let draft = try dispatcher.planFragmentDraft(actions: actions, context: context)
    let stage = ForegroundFragmentStageDeclaration(
        stageHash: String(repeating: "0", count: 64),
        expectedActionCount: 1,
        actions: draft.foregroundFragmentRequirements(actions: actions) ?? []
    )
    now = 16

    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try dispatcher.sealFragmentDraft(
            planRef: draft.planRef,
            actions: actions,
            stage: stage,
            authority: .init(
                fragmentHash: String(repeating: "a", count: 64),
                stageIndex: 0,
                stageHash: stage.stageHash,
                inputSnapshotID: context.guardValue.snapshotID
            )
        )
    }
    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try dispatcher.consumeForegroundPlan(
            draft,
            authority: foregroundConsumptionAuthority(plan: draft, context: context, actions: actions)
        )
    }
    #expect(performer.performed.isEmpty)
}

@Test func fragmentDraftPlanningPrunesExpiredAuthorityMonotonically() throws {
    var now: TimeInterval = 10
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy(),
        fragmentDraftClock: { now }
    )
    let context = DispatchContext(guardValue: inputDispatcherForegroundGuard())
    let actions = [NativeAction.click(x: 15, y: 15, within: "fallback")]
    _ = try dispatcher.planFragmentDraft(actions: actions, context: context)
    #expect(dispatcher.pendingFragmentDraftCount == 1)

    now = 16
    _ = try dispatcher.planFragmentDraft(actions: actions, context: context)
    #expect(dispatcher.pendingFragmentDraftCount == 1)
}

@Test func fragmentDraftPlanningHasDeterministicHardCapacityAndEvictsOldest() throws {
    var now: TimeInterval = 10
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy(),
        fragmentDraftClock: { now }
    )
    let context = DispatchContext(guardValue: inputDispatcherForegroundGuard())
    let actions = [NativeAction.click(x: 15, y: 15, within: "fallback")]
    var drafts: [DispatchPlan] = []
    for _ in 0 ... 128 {
        drafts.append(try dispatcher.planFragmentDraft(actions: actions, context: context))
        now += 0.001
    }
    #expect(dispatcher.pendingFragmentDraftCount == 128)
    let stage = ForegroundFragmentStageDeclaration(
        stageHash: String(repeating: "0", count: 64),
        expectedActionCount: 1,
        actions: drafts[0].foregroundFragmentRequirements(actions: actions) ?? []
    )
    let authority = FragmentStageAuthority(
        fragmentHash: String(repeating: "a", count: 64),
        stageIndex: 0,
        stageHash: stage.stageHash,
        inputSnapshotID: context.guardValue.snapshotID
    )

    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try dispatcher.sealFragmentDraft(
            planRef: drafts[0].planRef,
            actions: actions,
            stage: stage,
            authority: authority
        )
    }
    _ = try dispatcher.sealFragmentDraft(
        planRef: drafts[128].planRef,
        actions: actions,
        stage: stage,
        authority: authority
    )
}

@Test func dispatcherRejectsRawCoordinatePointerBeforePlanStorage() {
    let actions: [NativeAction] = [
        .click(x: 15, y: 15),
        .doubleClick(x: 15, y: 15),
        .scroll(deltaY: 10, x: 15, y: 15),
        .drag(x: 15, y: 15, endX: 20, endY: 20),
    ]

    for action in actions {
        let performer = InputDispatcherPerformerSpy()
        let dispatcher = InputDispatcher(
            performer: performer,
            application: inputDispatcherApplication,
            syntheticPolicy: inputDispatcherSyntheticPolicy()
        )

        #expect(throws: ActionExecutionError.invalidAction) {
            _ = try dispatcher.plan(
                actions: [action],
                context: DispatchContext(guardValue: inputDispatcherForegroundGuard())
            )
        }
        #expect(performer.performed.isEmpty)
    }
}

@Test func coordinateLessReferenceLessScrollReturnsUnstoredPlanningRejection() throws {
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy()
    )
    let context = DispatchContext(guardValue: inputDispatcherForegroundGuard())
    let action = NativeAction.scroll(deltaY: 10)
    let plan = try dispatcher.plan(actions: [action], context: context)

    #expect(plan.cooperativeError == .backgroundActionUnsupported)
    #expect(plan.lastAcknowledgedAction == -1)
    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try dispatcher.consumeForegroundPlan(
            plan,
            authority: foregroundConsumptionAuthority(
                plan: plan,
                context: context,
                actions: [action]
            )
        )
    }
    #expect(performer.performed.isEmpty)
}

@Test func systemPerformerWithoutExactPointerResolverRejectsExplicitReference() throws {
    let poster = InputDispatcherSyntheticPosterSpy()
    let fallback = ActionElement(
        element: AXUIElementCreateApplication(11),
        identityToken: "ordinary-lookup",
        bounds: CGRect(x: 10, y: 10, width: 20, height: 20),
        roleResult: .init(value: kAXGroupRole as String, status: .complete),
        subroleResult: .init(value: nil, status: .complete),
        enabled: true,
        actionNames: .complete([])
    )
    let performer = SystemActionPerformer(
        state: {
            ActionTargetState(
                pid: 11,
                windowID: 22,
                bounds: CGRect(x: 0, y: 0, width: 100, height: 100),
                axIdentity: 33
            )
        },
        lookup: { reference, snapshotID in
            reference == "fallback" && snapshotID == "snapshot" ? fallback : nil
        },
        inputPoster: poster
    )
    let dispatcher = InputDispatcher(
        performer: performer,
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy()
    )

    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try dispatcher.plan(
            actions: [.click(x: 15, y: 15, within: "fallback")],
            context: DispatchContext(guardValue: inputDispatcherForegroundGuard())
        )
    }
    #expect(poster.preflightCalls == 0)
    #expect(poster.events.isEmpty)
}

@Test func coordinatePointerStageDigestBindsExactSafeElementIdentityAndBounds() throws {
    let action = NativeAction.click(x: 15, y: 15, within: "fallback")
    let baselinePerformer = InputDispatcherPerformerSpy()
    baselinePerformer.fallbackIdentity = "canvas-a"
    baselinePerformer.fallbackBounds = CGRect(x: 10, y: 10, width: 20, height: 20)
    let changedIdentityPerformer = InputDispatcherPerformerSpy()
    changedIdentityPerformer.fallbackIdentity = "canvas-b"
    changedIdentityPerformer.fallbackBounds = baselinePerformer.fallbackBounds
    let changedBoundsPerformer = InputDispatcherPerformerSpy()
    changedBoundsPerformer.fallbackIdentity = baselinePerformer.fallbackIdentity
    changedBoundsPerformer.fallbackBounds = CGRect(x: 9, y: 9, width: 22, height: 22)
    let context = DispatchContext(guardValue: inputDispatcherForegroundGuard())
    func plan(performer: InputDispatcherPerformerSpy) throws -> DispatchPlan {
        try InputDispatcher(
            performer: performer,
            application: inputDispatcherApplication,
            syntheticPolicy: inputDispatcherSyntheticPolicy()
        ).plan(actions: [action], context: context)
    }
    let baseline = try plan(performer: baselinePerformer)
    let changedIdentity = try plan(performer: changedIdentityPerformer)
    let changedBounds = try plan(performer: changedBoundsPerformer)

    #expect(baseline.matches(actions: [action]))
    #expect(changedIdentity.matches(actions: [action]))
    #expect(changedBounds.matches(actions: [action]))
    #expect(baseline.stageDigest != changedIdentity.stageDigest)
    #expect(baseline.stageDigest != changedBounds.stageDigest)
}

@Test func coordinateMutationAfterPlanningConsumesAuthorityWithoutInput() throws {
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy()
    )
    let context = DispatchContext(guardValue: inputDispatcherForegroundGuard())
    let planned = NativeAction.click(x: 15, y: 15, within: "fallback")
    let mutated = NativeAction.click(x: 16, y: 15, within: "fallback")
    let plan = try dispatcher.plan(actions: [planned], context: context)

    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try dispatcher.consumeForegroundPlan(
            plan,
            authority: foregroundConsumptionAuthority(
                plan: plan,
                context: context,
                actions: [mutated]
            )
        )
    }
    #expect(performer.performed.isEmpty)
}

@Test func syntheticRequirementsPreserveResolvedActionOrderAndExcludeTrustedAXAndWait() throws {
    let performer = InputDispatcherPerformerSpy(textPreflight: .unsupported)
    let dispatcher = InputDispatcher(
        performer: performer,
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy(),
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let plan = try dispatcher.plan(
        actions: [
            .click(elementRef: "press"),
            .click(x: 15, y: 15, within: "fallback"),
            .keypress(key: "a", modifiers: ["command"]),
            .type(text: "marker", elementRef: "text"),
            .wait(durationMS: 0),
        ],
        context: DispatchContext(guardValue: inputDispatcherForegroundGuard(
            keyboardFocus: inputDispatcherKeyboardFocus("ax:focus")
        ))
    )

    #expect(plan.syntheticRequirements == [
        PlannedSyntheticRequirement(
            backend: .pidPointer,
            actionClass: .click,
            intent: .pointer(.click)
        ),
        PlannedSyntheticRequirement(
            backend: .pidKeyboard,
            actionClass: .text,
            intent: .keyChord(ApprovedKeyChord(rawValue: "command+a")!)
        ),
        PlannedSyntheticRequirement(
            backend: .foregroundKeyboard,
            actionClass: .text,
            intent: .textEntry
        ),
    ])
    #expect(plan.backends == [.axPress, .pidPointer, .pidKeyboard, .foregroundKeyboard, .wait])
    #expect(performer.performed.isEmpty)
}

@Test func unauthorizedMixedSyntheticBatchRejectsWhollyBeforePlanStorageOrInput() throws {
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy(
            pointerCapability: .experimentalAvailable,
            keyboardCapability: .unavailable
        ),
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let context = DispatchContext(guardValue: inputDispatcherForegroundGuard())

    let actions: [NativeAction] = [
        .click(elementRef: "press"),
        .click(x: 15, y: 15, within: "fallback"),
        .keypress(key: "a", modifiers: ["command"]),
    ]
    let plan = try dispatcher.plan(actions: actions, context: context)

    #expect(plan.cooperativeError == nil)
    #expect(plan.requiresTakeover)
    #expect(plan.lastAcknowledgedAction == -1)
    #expect(plan.syntheticRequirements.map(\.backend) == [.pidPointer, .pidKeyboard])
    #expect(throws: ActionExecutionError.self) {
        _ = try dispatcher.execute(plan, context: context)
    }
    #expect(performer.performed.isEmpty)
}

@Test func defaultDispatcherFailsClosedForSyntheticPlansWithoutStoringThem() throws {
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(performer: performer)
    let context = DispatchContext(guardValue: inputDispatcherForegroundGuard())
    let action = NativeAction.click(x: 15, y: 15, within: "fallback")
    let plan = try dispatcher.plan(actions: [action], context: context)

    #expect(plan.cooperativeError == nil)
    #expect(plan.requiresTakeover)
    #expect(plan.lastAcknowledgedAction == -1)
    // consume 不再因计划层拒绝而 throw(新契约: 计划放行给 takeover 授权);
    // fail-closed 由执行层/Python enabled_pid_actions 兜底, 此处验证不落地执行。
    _ = try dispatcher.consumeForegroundPlan(
        plan,
        authority: foregroundConsumptionAuthority(
            plan: plan,
            context: context,
            actions: [action]
        )
    )
    #expect(performer.performed.isEmpty)
}

@Test func everyActionResolvesBeforeSyntheticAuthorizationDenial() throws {
    let performer = InputDispatcherPerformerSpy()
    performer.unavailableReferences.insert("missing")
    let dispatcher = InputDispatcher(
        performer: performer,
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy(
            pointerCapability: .unavailable,
            keyboardCapability: .unavailable
        )
    )

    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try dispatcher.plan(
            actions: [
                .click(x: 15, y: 15, within: "fallback"),
                .click(elementRef: "missing"),
            ],
            context: DispatchContext(guardValue: inputDispatcherForegroundGuard())
        )
    }
    #expect(performer.performed.isEmpty)
}

@Test func mixedBackgroundBatchRejectsBeforeActionZero() throws {
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let result = try dispatcher.plan(
        actions: [.click(elementRef: "press"), .scroll(deltaY: 10)],
        context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    )

    #expect(!result.requiresTakeover)
    #expect(result.cooperativeError == .backgroundActionUnsupported)
    #expect(result.lastAcknowledgedAction == -1)
    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try dispatcher.execute(
            result,
            context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
        )
    }
    #expect(performer.performed.isEmpty)
}

@Test func trustedBackgroundAXBatchPlansThenExecutesWithoutPointerOrKeyboardMethods() throws {
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    let plan = try dispatcher.plan(
        actions: [.click(elementRef: "press"), .wait(durationMS: 1), .type(text: "hello", elementRef: "text")],
        context: context
    )

    #expect(!plan.requiresTakeover)
    #expect(plan.cooperativeError == nil)
    #expect(plan.backends == [.axPress, .wait, .axSelectedText])
    #expect(performer.performed.isEmpty)

    let result = try dispatcher.execute(plan, context: context)
    #expect(result.error == nil)
    #expect(result.lastAcknowledgedAction == 2)
    #expect(performer.performed.map(\.method) == [.accessibilityPress, .wait, .accessibilityText])
}

@Test func fileUploadAXPressRequiresTakeoverBeforeAnyBackgroundInput() throws {
    let performer = InputDispatcherPerformerSpy()
    performer.pressSubrole = "AXFileUploadButton"
    let dispatcher = InputDispatcher(performer: performer)
    let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    let plan = try dispatcher.plan(actions: [.click(elementRef: "press")], context: context)
    #expect(plan.backends == [.axPress])
    #expect(plan.requiresTakeover)
    #expect(plan.cooperativeError == nil)
    #expect(throws: ActionExecutionError.targetNotFrontmost) {
        _ = try dispatcher.execute(plan, context: context)
    }
    #expect(performer.performed.isEmpty)
}

@Test func fileUploadAXPressKeepsSinglePressWithinAuthorizedForegroundPlan() throws {
    let performer = InputDispatcherPerformerSpy()
    performer.pressSubrole = "AXFileUploadButton"
    let dispatcher = InputDispatcher(performer: performer)
    let context = DispatchContext(guardValue: inputDispatcherForegroundGuard())
    let actions = [NativeAction.click(elementRef: "press")]
    let plan = try dispatcher.plan(actions: actions, context: context)
    let entries = try dispatcher.consumeForegroundPlan(plan, authority: foregroundConsumptionAuthority(
        plan: plan, context: context, actions: actions))
    #expect(plan.requiresTakeover)
    #expect(entries.count == 1)
    #expect(entries.first?.backend == .axPress)
    #expect(performer.performed.isEmpty)
}

@Test(arguments: [CGFloat(1), CGFloat(999)])
func positiveVerticalElementScrollPlansOneBackgroundAXIncrement(deltaY: CGFloat) throws {
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    let action = deltaY == 1
        ? try NativeAction.parse(.object([
            "type": .string("scroll"),
            "delta_y": .number(Double(deltaY)),
            "element_ref": .string("scroll"),
        ]))
        : .scroll(deltaY: deltaY, elementRef: "scroll")
    let plan = try dispatcher.plan(
        actions: [action],
        context: context
    )

    #expect(plan.backends.map(\.rawValue) == ["ax_increment"])
    #expect(!plan.requiresTakeover)
    #expect(plan.cooperativeError == nil)
    #expect(plan.pidActionClasses.isEmpty)
    #expect(plan.syntheticRequirements.isEmpty)
    #expect(plan.actionClasses == [.scroll])

    let result = try dispatcher.execute(plan, context: context)
    #expect(result.error == nil)
    #expect(result.lastAcknowledgedAction == 0)
    #expect(performer.performed.count == 1)
    #expect(performer.performed.map { String(describing: $0.method) } == ["accessibilityIncrement"])
}

@Test(arguments: [CGFloat(-1), CGFloat(-999)])
func negativeVerticalElementScrollPlansOneBackgroundAXDecrement(deltaY: CGFloat) throws {
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    let plan = try dispatcher.plan(
        actions: [.scroll(deltaY: deltaY, elementRef: "scroll")],
        context: context
    )

    #expect(plan.backends.map(\.rawValue) == ["ax_decrement"])
    #expect(!plan.requiresTakeover)
    #expect(plan.cooperativeError == nil)
    #expect(plan.pidActionClasses.isEmpty)
    #expect(plan.syntheticRequirements.isEmpty)

    let result = try dispatcher.execute(plan, context: context)
    #expect(result.error == nil)
    #expect(result.lastAcknowledgedAction == 0)
    #expect(performer.performed.count == 1)
    #expect(performer.performed.map { String(describing: $0.method) } == ["accessibilityDecrement"])
}

@Test func scrollbarAXPressFallbackPlansOneBackgroundScrollWithoutPIDInput() throws {
    let performer = InputDispatcherPerformerSpy()
    performer.scrollActionNames = .complete([])
    performer.scrollPressDirection = .increment
    let dispatcher = InputDispatcher(
        performer: performer,
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    let plan = try dispatcher.plan(
        actions: [.scroll(deltaY: 400, elementRef: "scroll")],
        context: context
    )

    #expect(plan.backends == [.axPress])
    #expect(plan.actionClasses == [.scroll])
    #expect(plan.pidActionClasses.isEmpty)
    #expect(plan.syntheticRequirements.isEmpty)
    #expect(!plan.requiresTakeover)

    let result = try dispatcher.execute(plan, context: context)
    #expect(result.error == nil)
    #expect(result.lastAcknowledgedAction == 0)
    #expect(performer.performed.map(\.method) == [.accessibilityPress])
}

@Test func scrollbarAXPressFallbackAcceptsUnknownEnabledButRejectsExplicitlyDisabledButton() throws {
    for (enabled, expectedBackend) in [(nil, DispatchBackend.axPress), (false, .pidPointer)] {
        let performer = InputDispatcherPerformerSpy()
        performer.scrollActionNames = .complete([])
        performer.scrollPressDirection = .increment
        performer.scrollPressButtonEnabled = enabled
        let plan = try InputDispatcher(
            performer: performer,
            application: inputDispatcherApplication,
            syntheticPolicy: inputDispatcherSyntheticPolicy()
        ).plan(
            actions: [.scroll(deltaY: 1, elementRef: "scroll")],
            context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
        )

        #expect(plan.backends == [expectedBackend])
    }
}

@Test func scrollbarAXPressFallbackAcceptsUnknownEnabledDuringExecutionRevalidation() throws {
    let performer = InputDispatcherPerformerSpy()
    performer.scrollActionNames = .complete([])
    performer.scrollPressDirection = .increment
    let dispatcher = InputDispatcher(performer: performer)
    let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    let plan = try dispatcher.plan(
        actions: [.scroll(deltaY: 1, elementRef: "scroll")],
        context: context
    )

    performer.scrollPressButtonEnabled = nil
    let result = try dispatcher.execute(plan, context: context)

    #expect(result.error == nil)
    #expect(performer.performed.map(\.method) == [.accessibilityPress])
}

@Test func scrollbarAXPressFallbackRejectsWrongDirectionalSubrole() throws {
    let performer = InputDispatcherPerformerSpy()
    performer.scrollActionNames = .complete([])
    performer.scrollPressDirection = .increment
    performer.scrollPressButtonSubrole = "AXDecrementPage"
    let plan = try InputDispatcher(
        performer: performer,
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy()
    ).plan(
        actions: [.scroll(deltaY: 1, elementRef: "scroll")],
        context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    )

    #expect(plan.backends == [.pidPointer])
    #expect(plan.requiresTakeover)
}

@Test func scrollbarAXPressFallbackRevalidatesOwnerAndButtonBeforeMutation() throws {
    for mutation in ["owner", "button"] {
        let performer = InputDispatcherPerformerSpy()
        performer.scrollActionNames = .complete([])
        performer.scrollPressDirection = .decrement
        let dispatcher = InputDispatcher(performer: performer)
        let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())
        let plan = try dispatcher.plan(
            actions: [.scroll(deltaY: -1, elementRef: "scroll")],
            context: context
        )

        if mutation == "owner" { performer.scrollPressOwnerIdentity = "changed-owner" }
        if mutation == "button" { performer.scrollPressButtonIdentity = "changed-button" }

        let result = try dispatcher.execute(plan, context: context)
        #expect(result.error == .staleSnapshot, "mutation: \(mutation)")
        #expect(performer.performed.isEmpty, "mutation: \(mutation)")
    }
}

@Test func scrollbarAXPressFallbackVerifiesOwningScrollbarDirection() throws {
    let performer = InputDispatcherPerformerSpy()
    performer.scrollActionNames = .complete([])
    performer.scrollPressDirection = .increment
    let reader = InputDispatcherEffectReader([
        AXEffectObservation(identityToken: "scroll-owner", value: .number(0)),
        AXEffectObservation(identityToken: "scroll-owner", value: .number(1)),
    ])
    let dispatcher = InputDispatcher(performer: performer, effectReader: reader)
    let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    let plan = try dispatcher.plan(
        actions: [.scroll(deltaY: 400, elementRef: "scroll")],
        context: context
    )

    let result = try dispatcher.execute(plan, context: context)

    #expect(result.outcomes[0].effectVerification == .verified)
    #expect(reader.elements.count == 2)
    #expect(reader.elements.allSatisfy { CFEqual($0, performer.scrollPressOwner) })
}

@Test func scrollbarAXPressFailureIsUnknownAndNeverReplaysAsPIDScroll() throws {
    let performer = InputDispatcherPerformerSpy(
        failures: [0: ActionPerformFailure(error: .helperFailed, inputStarted: true)]
    )
    performer.scrollActionNames = .complete([])
    performer.scrollPressDirection = .increment
    let dispatcher = InputDispatcher(performer: performer)
    let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    let plan = try dispatcher.plan(
        actions: [.scroll(deltaY: 1, elementRef: "scroll")],
        context: context
    )

    let result = try dispatcher.execute(plan, context: context)

    #expect(result.error == .unknownOutcome)
    #expect(performer.performed.map(\.method) == [.accessibilityPress])
}

@Test func backgroundAXScrollRevalidatesExactElementBeforeDispatch() throws {
    enum Mutation: CaseIterable {
        case element
        case identity
        case bounds
        case enabled
        case actionName
        case truncatedActionNames
    }

    for mutation in Mutation.allCases {
        let performer = InputDispatcherPerformerSpy()
        let dispatcher = InputDispatcher(performer: performer)
        let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())
        let plan = try dispatcher.plan(
            actions: [.scroll(deltaY: 1, elementRef: "scroll")],
            context: context
        )

        switch mutation {
        case .element:
            performer.scrollElement = AXUIElementCreateApplication(13)
        case .identity:
            performer.scrollIdentity = "changed-scroll"
        case .bounds:
            performer.scrollBounds.origin.x += 2
        case .enabled:
            performer.scrollEnabled = false
        case .actionName:
            performer.scrollActionNames = .complete([kAXDecrementAction as String])
        case .truncatedActionNames:
            performer.scrollActionNames = ActionNameResults(
                values: [.init(value: kAXIncrementAction as String, status: .complete)],
                status: .truncated
            )
        }

        let result = try dispatcher.execute(plan, context: context)
        #expect(result.error == .staleSnapshot, "mutation: \(mutation)")
        #expect(result.lastAcknowledgedAction == -1, "mutation: \(mutation)")
        #expect(performer.performed.isEmpty, "mutation: \(mutation)")
    }
}

@Test func nonVerticalAndUnsupportedElementScrollsPreservePIDTakeover() throws {
    let cases: [NativeAction] = [
        .scroll(deltaY: 0, elementRef: "scroll"),
        .scroll(deltaX: 1, deltaY: 0, elementRef: "scroll"),
        .scroll(deltaX: 1, deltaY: 1, elementRef: "scroll"),
    ]

    for action in cases {
        let performer = InputDispatcherPerformerSpy()
        let plan = try InputDispatcher(
            performer: performer,
            application: inputDispatcherApplication,
            syntheticPolicy: inputDispatcherSyntheticPolicy()
        ).plan(
            actions: [action],
            context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
        )

        #expect(plan.backends == [.pidPointer])
        #expect(plan.requiresTakeover)
        #expect(plan.pidActionClasses == [.scroll])
        #expect(plan.syntheticRequirements.count == 1)
        #expect(performer.performed.isEmpty)
    }

    for actionNames in [
        ActionNameResults.complete([kAXDecrementAction as String]),
        ActionNameResults(
            values: [.init(value: kAXIncrementAction as String, status: .complete)],
            status: .truncated
        ),
        ActionNameResults(values: [], status: .failed, error: .cannotComplete),
    ] {
        let performer = InputDispatcherPerformerSpy()
        performer.scrollActionNames = actionNames
        let plan = try InputDispatcher(
            performer: performer,
            application: inputDispatcherApplication,
            syntheticPolicy: inputDispatcherSyntheticPolicy()
        ).plan(
            actions: [.scroll(deltaY: 1, elementRef: "scroll")],
            context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
        )

        #expect(plan.backends == [.pidPointer])
        #expect(plan.requiresTakeover)
        #expect(plan.pidActionClasses == [.scroll])
        #expect(performer.performed.isEmpty)
    }
}

@Test func missingDisabledAndSecureElementScrollsKeepExistingPlanningErrors() throws {
    let missingPerformer = InputDispatcherPerformerSpy()
    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try InputDispatcher(performer: missingPerformer).plan(
            actions: [.scroll(deltaY: 1, elementRef: "missing")],
            context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
        )
    }

    let disabledPerformer = InputDispatcherPerformerSpy()
    disabledPerformer.scrollEnabled = false
    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try InputDispatcher(performer: disabledPerformer).plan(
            actions: [.scroll(deltaY: 1, elementRef: "scroll")],
            context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
        )
    }

    let securePerformer = InputDispatcherPerformerSpy()
    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try InputDispatcher(performer: securePerformer).plan(
            actions: [.scroll(deltaY: 1, elementRef: "secure")],
            context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
        )
    }
    #expect(missingPerformer.performed.isEmpty)
    #expect(disabledPerformer.performed.isEmpty)
    #expect(securePerformer.performed.isEmpty)
}

@Test func backgroundAXScrollFailureIsUnknownOutcomeAndNeverFallsBackOrReplays() throws {
    let performer = InputDispatcherPerformerSpy(
        failures: [0: ActionPerformFailure(error: .helperFailed, inputStarted: true)]
    )
    let dispatcher = InputDispatcher(performer: performer)
    let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    let plan = try dispatcher.plan(
        actions: [.scroll(deltaY: 1, elementRef: "scroll")],
        context: context
    )

    let result = try dispatcher.execute(plan, context: context)

    #expect(result.error == .unknownOutcome)
    #expect(result.lastAcknowledgedAction == -1)
    #expect(result.outcomes == [ActionOutcome(index: 0, ok: false, error: .unknownOutcome)])
    #expect(performer.performed.count == 1)
    #expect(performer.performed.first?.method == .accessibilityIncrement)
    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try dispatcher.execute(plan, context: context)
    }
    #expect(performer.performed.count == 1)
}

@Test func backgroundAXEffectReadCannotInvalidateElementAuthorityBeforeMutation() throws {
    let performer = InputDispatcherPerformerSpy()
    let reader = InputDispatcherEffectReader(
        [AXEffectObservation(identityToken: "scroll", value: .number(10))],
        beforeRead: { readIndex, _ in
            if readIndex == 0 { performer.scrollEnabled = false }
        }
    )
    let dispatcher = InputDispatcher(performer: performer, effectReader: reader)
    let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    let plan = try dispatcher.plan(
        actions: [.scroll(deltaY: 1, elementRef: "scroll")],
        context: context
    )

    let result = try dispatcher.execute(plan, context: context)

    #expect(result.error == .staleSnapshot)
    #expect(result.lastAcknowledgedAction == -1)
    #expect(performer.performed.isEmpty)
    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try dispatcher.execute(plan, context: context)
    }
    #expect(performer.performed.isEmpty)
}

@Test(arguments: [
    (CGFloat(1), AXEffectValue.number(10), AXEffectValue.number(11)),
    (CGFloat(-1), AXEffectValue.number(10), AXEffectValue.number(9)),
])
func numericAXScrollEffectVerifiesOnlyTheRequestedDirection(
    deltaY: CGFloat,
    before: AXEffectValue,
    after: AXEffectValue
) throws {
    let performer = InputDispatcherPerformerSpy()
    let reader = InputDispatcherEffectReader([
        AXEffectObservation(identityToken: "scroll", value: before),
        AXEffectObservation(identityToken: "scroll", value: after),
    ])
    let dispatcher = InputDispatcher(performer: performer, effectReader: reader)
    let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    let plan = try dispatcher.plan(
        actions: [.scroll(deltaY: deltaY, elementRef: "scroll")],
        context: context
    )

    let result = try dispatcher.execute(plan, context: context)

    #expect(result.outcomes[0].effectVerification == .verified)
    #expect(performer.performed.count == 1)
    #expect(reader.elements.count == 2)
    #expect(reader.elements.allSatisfy { CFEqual($0, performer.scrollElement) })
}

@Test func numericAXScrollChangeInTheWrongDirectionStaysUnverified() throws {
    let performer = InputDispatcherPerformerSpy()
    let reader = InputDispatcherEffectReader([
        AXEffectObservation(identityToken: "scroll", value: .number(10)),
        AXEffectObservation(identityToken: "scroll", value: .number(9)),
    ])
    let dispatcher = InputDispatcher(performer: performer, effectReader: reader)
    let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    let plan = try dispatcher.plan(
        actions: [.scroll(deltaY: 1, elementRef: "scroll")],
        context: context
    )

    let result = try dispatcher.execute(plan, context: context)

    #expect(result.outcomes[0].effectVerification == .unverified)
    #expect(performer.performed.count == 1)
}

@Test(arguments: ["AXCheckBox", "AXRadioButton", "AXDisclosureTriangle"])
func statefulAXPressEffectVerifiesAnyBoundedValueChange(role: String) throws {
    let performer = InputDispatcherPerformerSpy()
    performer.pressRole = role
    let reader = InputDispatcherEffectReader([
        AXEffectObservation(identityToken: "press", value: .boolean(false)),
        AXEffectObservation(identityToken: "press", value: .boolean(true)),
    ])
    let dispatcher = InputDispatcher(performer: performer, effectReader: reader)
    let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    let plan = try dispatcher.plan(
        actions: [.click(elementRef: "press")],
        context: context
    )

    let result = try dispatcher.execute(plan, context: context)

    #expect(result.outcomes[0].effectVerification == .verified)
    #expect(performer.performed.count == 1)
}

@Test func comparableStateUnchangedThroughSettleDeadlineIsNoop() throws {
    var now: TimeInterval = 0
    let performer = InputDispatcherPerformerSpy()
    let unchanged = AXEffectObservation(identityToken: "scroll", value: .number(10))
    let reader = InputDispatcherEffectReader([unchanged, unchanged, unchanged, unchanged])
    let dispatcher = InputDispatcher(
        performer: performer,
        effectReader: reader,
        effectClock: { now },
        effectSleeper: { now += $0 },
        effectSettleDuration: 0.02,
        effectPollInterval: 0.01
    )
    let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    let plan = try dispatcher.plan(
        actions: [.scroll(deltaY: 1, elementRef: "scroll")],
        context: context
    )

    let result = try dispatcher.execute(plan, context: context)

    #expect(result.outcomes[0].effectVerification == .noop)
    #expect(performer.performed.count == 1)
    #expect(reader.elements.count == 3)
    #expect(reader.remainingBudgets == [0.02, 0.02, 0.01])
    #expect(reader.remainingBudgets.allSatisfy { $0 > 0 && $0 <= 0.02 })
}

@Test func effectReadOverrunFailsUnverifiedWithoutExtendingTheSettleLoop() throws {
    var now: TimeInterval = 0
    var sleeps: [TimeInterval] = []
    let performer = InputDispatcherPerformerSpy()
    let unchanged = AXEffectObservation(identityToken: "scroll", value: .number(10))
    let reader = InputDispatcherEffectReader(
        [unchanged, unchanged],
        beforeRead: { readIndex, remainingBudget in
            if readIndex == 1 {
                now += remainingBudget + 0.001
            }
        }
    )
    let dispatcher = InputDispatcher(
        performer: performer,
        effectReader: reader,
        effectClock: { now },
        effectSleeper: {
            sleeps.append($0)
            now += $0
        },
        effectSettleDuration: 0.02,
        effectPollInterval: 0.01
    )
    let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    let plan = try dispatcher.plan(
        actions: [.scroll(deltaY: 1, elementRef: "scroll")],
        context: context
    )

    let result = try dispatcher.execute(plan, context: context)

    #expect(result.outcomes[0].effectVerification == .unverified)
    #expect(performer.performed.count == 1)
    #expect(reader.elements.count == 2)
    #expect(reader.remainingBudgets == [0.02, 0.02])
    #expect(sleeps.isEmpty)
}

@Test func systemAXEffectReaderBoundsEachAccessorAndRestoresTheDefaultTimeout() {
    let budget: TimeInterval = 0.0123456789
    var configuredTimeouts: [Float] = []
    var accessCount = 0
    let element = AXUIElementCreateApplication(getpid())
    let reader = SystemAXEffectReader(
        clock: { 1 },
        copyValue: { _ in
            accessCount += 1
            return (.success, NSNumber(value: 7))
        },
        setMessagingTimeout: { _, timeout in
            configuredTimeouts.append(timeout)
            return .success
        }
    )

    let observation = reader.read(element, remainingBudget: budget)

    #expect(observation?.value == .number(7))
    #expect(accessCount == 1)
    #expect(configuredTimeouts.count == 2)
    #expect(configuredTimeouts[0] > 0)
    #expect(TimeInterval(configuredTimeouts[0]) <= budget)
    #expect(configuredTimeouts[1] == 0)
}

@Test func systemAXEffectReaderFailsClosedOnDeadlineOrDefaultTimeoutRestoreFailure() {
    let element = AXUIElementCreateApplication(getpid())
    var now: TimeInterval = 0
    var deadlineTimeouts: [Float] = []
    let expiredReader = SystemAXEffectReader(
        clock: { now },
        copyValue: { _ in
            now = 0.03
            return (.success, NSNumber(value: 7))
        },
        setMessagingTimeout: { _, timeout in
            deadlineTimeouts.append(timeout)
            return .success
        }
    )
    let resetFailureReader = SystemAXEffectReader(
        clock: { 0 },
        copyValue: { _ in (.success, NSNumber(value: 7)) },
        setMessagingTimeout: { _, timeout in timeout > 0 ? .success : .failure }
    )

    #expect(expiredReader.read(element, remainingBudget: 0.02) == nil)
    #expect(deadlineTimeouts.count == 2)
    #expect(deadlineTimeouts[1] == 0)
    #expect(resetFailureReader.read(element, remainingBudget: 0.02) == nil)
}

@Test func systemAXEffectReaderRejectsValueWhenBoundedLocalDecodeCrossesDeadline() {
    let element = AXUIElementCreateApplication(getpid())
    var clockValues: [TimeInterval] = [0, 0, 0.01, 0.03]
    let reader = SystemAXEffectReader(
        clock: { clockValues.removeFirst() },
        copyValue: { _ in (.success, "bounded" as CFString) },
        setMessagingTimeout: { _, _ in .success }
    )

    #expect(reader.read(element, remainingBudget: 0.02) == nil)
    #expect(clockValues.isEmpty)
}

@Test func missingUnreadableOrIdentityChangedEffectStateStaysUnverified() throws {
    let cases: [[AXEffectObservation?]] = [
        [nil],
        [
            AXEffectObservation(identityToken: "scroll", value: .number(10)),
            nil,
        ],
        [
            AXEffectObservation(identityToken: "scroll", value: .number(10)),
            AXEffectObservation(identityToken: "changed-scroll", value: .number(11)),
        ],
    ]

    for observations in cases {
        let performer = InputDispatcherPerformerSpy()
        let reader = InputDispatcherEffectReader(observations)
        let dispatcher = InputDispatcher(performer: performer, effectReader: reader)
        let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())
        let plan = try dispatcher.plan(
            actions: [.scroll(deltaY: 1, elementRef: "scroll")],
            context: context
        )

        let result = try dispatcher.execute(plan, context: context)

        #expect(result.outcomes[0].effectVerification == .unverified)
        #expect(performer.performed.count == 1)
    }
}

@Test func ordinaryAXButtonNeverClaimsTypedEffectVerification() throws {
    let performer = InputDispatcherPerformerSpy()
    let reader = InputDispatcherEffectReader([
        AXEffectObservation(identityToken: "press", value: .boolean(false)),
        AXEffectObservation(identityToken: "press", value: .boolean(true)),
    ])
    let dispatcher = InputDispatcher(performer: performer, effectReader: reader)
    let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    let plan = try dispatcher.plan(
        actions: [.click(elementRef: "press")],
        context: context
    )

    let result = try dispatcher.execute(plan, context: context)

    #expect(result.outcomes[0].effectVerification == .unverified)
    #expect(performer.performed.count == 1)
    #expect(reader.elements.isEmpty)
}

@Test func secureAndGlobalShortcutBackgroundActionsAreUnsupportedBeforeInput() throws {
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())

    let secure = try dispatcher.plan(actions: [.type(text: "secret", elementRef: "secure")], context: context)
    #expect(secure.cooperativeError == .backgroundActionUnsupported)
    #expect(secure.lastAcknowledgedAction == -1)

    let shortcut = try dispatcher.plan(actions: [.keypress(key: "space", modifiers: ["command"])], context: context)
    #expect(shortcut.cooperativeError == nil)
    #expect(shortcut.requiresTakeover)
    #expect(performer.performed.isEmpty)
}

@Test func productionTextPreflightRejectsIMEAndUnknownInputSourceBeforeAXMutation() throws {
    for safety in [BackgroundTextInputSafety.imeOrCandidate, .unknown] {
        let writer = InputDispatcherTextWriter()
        let element = AXUIElementCreateApplication(11)
        let performer = SystemActionPerformer(
            state: {
                ActionTargetState(
                    pid: 11,
                    windowID: 22,
                    bounds: CGRect(x: 0, y: 0, width: 100, height: 100),
                    axIdentity: 33
                )
            },
            lookup: { reference, snapshotID in
                guard reference == "text", snapshotID == "snapshot" else { return nil }
                return ActionElement(
                    element: element,
                    bounds: CGRect(x: 10, y: 10, width: 50, height: 20),
                    role: kAXTextFieldRole as String,
                    subrole: nil,
                    actions: []
                )
            },
            selectedTextWriter: writer
        )
        let dispatcher = InputDispatcher(
            performer: performer,
            backgroundTextInputSafety: InputDispatcherTextSafety(safety)
        )
        let plan = try dispatcher.plan(
            actions: [.type(text: "must-not-mutate", elementRef: "text")],
            context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
        )

        #expect(plan.cooperativeError == .backgroundActionUnsupported)
        #expect(plan.lastAcknowledgedAction == -1)
        #expect(writer.preflightCalls == 0)
        #expect(writer.writeCalls == 0)
    }
}

@Test func backgroundAXSelectedTextRequiresExactFocusAtPlanningAndBeforeMutation() throws {
    let target = AXUIElementCreateApplication(11)
    let other = AXUIElementCreateApplication(12)
    let targetElement = ActionElement(
        element: target,
        bounds: CGRect(x: 10, y: 10, width: 50, height: 20),
        role: kAXTextFieldRole as String,
        subrole: nil,
        actions: []
    )
    let otherElement = ActionElement(
        element: other,
        bounds: CGRect(x: 10, y: 10, width: 50, height: 20),
        role: kAXTextFieldRole as String,
        subrole: nil,
        actions: []
    )
    var focused = otherElement
    let planningWriter = InputDispatcherTextWriter()
    let planningPerformer = SystemActionPerformer(
        state: {
            ActionTargetState(
                pid: 11,
                windowID: 22,
                bounds: CGRect(x: 0, y: 0, width: 100, height: 100),
                axIdentity: 33
            )
        },
        lookup: { reference, snapshotID in
            reference == "text" && snapshotID == "snapshot" ? targetElement : nil
        },
        selectedTextWriter: planningWriter,
        focusedKeyboard: { _ in focused }
    )
    let planningDispatcher = InputDispatcher(
        performer: planningPerformer,
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())

    #expect(throws: ActionExecutionError.inputFocusRequired) {
        _ = try planningDispatcher.plan(
            actions: [.type(text: "must-not-write", elementRef: "text")],
            context: context
        )
    }
    #expect(planningWriter.preflightCalls == 0)
    #expect(planningWriter.writeCalls == 0)

    let executionWriter = InputDispatcherTextWriter()
    focused = targetElement
    let executionPerformer = SystemActionPerformer(
        state: {
            ActionTargetState(
                pid: 11,
                windowID: 22,
                bounds: CGRect(x: 0, y: 0, width: 100, height: 100),
                axIdentity: 33
            )
        },
        lookup: { reference, snapshotID in
            reference == "text" && snapshotID == "snapshot" ? targetElement : nil
        },
        selectedTextWriter: executionWriter,
        focusedKeyboard: { _ in focused }
    )
    let executionDispatcher = InputDispatcher(
        performer: executionPerformer,
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let plan = try executionDispatcher.plan(
        actions: [.type(text: "must-not-write", elementRef: "text")],
        context: context
    )
    focused = otherElement

    let result = try executionDispatcher.execute(plan, context: context)

    #expect(result.error == .inputFocusRequired)
    #expect(result.lastAcknowledgedAction == -1)
    #expect(result.outcomes == [
        ActionOutcome(index: 0, ok: false, error: .inputFocusRequired),
    ])
    #expect(executionWriter.writeCalls == 0)

    let identityWriter = InputDispatcherTextWriter()
    focused = targetElement
    let currentTarget = InputDispatcherElementState(targetElement)
    let identityPerformer = SystemActionPerformer(
        state: {
            ActionTargetState(
                pid: 11,
                windowID: 22,
                bounds: CGRect(x: 0, y: 0, width: 100, height: 100),
                axIdentity: 33
            )
        },
        lookup: { reference, snapshotID in
            reference == "text" && snapshotID == "snapshot" ? currentTarget.value : nil
        },
        selectedTextWriter: identityWriter,
        focusedKeyboard: { _ in focused }
    )
    let identityDispatcher = InputDispatcher(
        performer: identityPerformer,
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let identityPlan = try identityDispatcher.plan(
        actions: [.type(text: "must-not-write", elementRef: "text")],
        context: context
    )
    currentTarget.value = ActionElement(
        element: other,
        identityToken: "changed-identity",
        bounds: targetElement.bounds,
        roleResult: targetElement.roleResult,
        subroleResult: targetElement.subroleResult,
        enabled: targetElement.enabled,
        actionNames: targetElement.actionNames
    )
    #expect(identityPerformer.element(reference: "text", snapshotID: "snapshot")?.identityToken == "changed-identity")

    let identityResult = try identityDispatcher.execute(identityPlan, context: context)

    #expect(identityResult.error == .staleSnapshot)
    #expect(identityResult.lastAcknowledgedAction == -1)
    #expect(identityWriter.writeCalls == 0)
}

@Test func backgroundAXSelectedTextSecureFocusKeepsSecureTargetClassification() throws {
    let target = AXUIElementCreateApplication(11)
    let secure = AXUIElementCreateApplication(12)
    let targetElement = ActionElement(
        element: target,
        bounds: CGRect(x: 10, y: 10, width: 50, height: 20),
        role: kAXTextFieldRole as String,
        subrole: nil,
        actions: []
    )
    let secureElement = ActionElement(
        element: secure,
        bounds: CGRect(x: 10, y: 10, width: 50, height: 20),
        role: "AXSecureTextField",
        subrole: nil,
        actions: []
    )
    let writer = InputDispatcherTextWriter()
    let performer = SystemActionPerformer(
        state: {
            ActionTargetState(
                pid: 11,
                windowID: 22,
                bounds: CGRect(x: 0, y: 0, width: 100, height: 100),
                axIdentity: 33
            )
        },
        lookup: { reference, snapshotID in
            reference == "text" && snapshotID == "snapshot" ? targetElement : nil
        },
        selectedTextWriter: writer,
        focusedKeyboard: { _ in secureElement }
    )
    let dispatcher = InputDispatcher(
        performer: performer,
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )

    #expect(throws: ActionExecutionError.secureTarget) {
        _ = try dispatcher.plan(
            actions: [.type(text: "must-not-write", elementRef: "text")],
            context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
        )
    }
    #expect(writer.preflightCalls == 0)
    #expect(writer.writeCalls == 0)
}

@Test func systemTextInputDetectorAllowsOnlyCompleteASCIIKeyboardLayoutEvidence() {
    let safe = SystemBackgroundTextInputSafetyDetector(snapshot: {
        BackgroundTextInputSourceSnapshot(
            category: kTISCategoryKeyboardInputSource as String,
            sourceType: kTISTypeKeyboardLayout as String,
            isASCIICapable: true
        )
    })
    let nonASCII = SystemBackgroundTextInputSafetyDetector(snapshot: {
        BackgroundTextInputSourceSnapshot(
            category: kTISCategoryKeyboardInputSource as String,
            sourceType: kTISTypeKeyboardLayout as String,
            isASCIICapable: false
        )
    })
    let inputMode = SystemBackgroundTextInputSafetyDetector(snapshot: {
        BackgroundTextInputSourceSnapshot(
            category: kTISCategoryKeyboardInputSource as String,
            sourceType: kTISTypeKeyboardInputMode as String,
            isASCIICapable: true
        )
    })
    let unknown = SystemBackgroundTextInputSafetyDetector(snapshot: { nil })

    #expect(safe.detect() == .safeASCIIKeyboardLayout)
    #expect(nonASCII.detect() == .imeOrCandidate)
    #expect(inputMode.detect() == .imeOrCandidate)
    #expect(unknown.detect() == .unknown)
}

@Test func unsafeInputSourceRejectsUnreferencedTypeBeforeInput() throws {
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        backgroundTextInputSafety: InputDispatcherTextSafety(.imeOrCandidate)
    )
    let plan = try dispatcher.plan(
        actions: [.type(text: "must-not-type")],
        context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    )

    #expect(plan.cooperativeError == .backgroundActionUnsupported)
    #expect(plan.lastAcknowledgedAction == -1)
    #expect(performer.performed.isEmpty)
}

@Test func unsafeInputSourceRejectsFallbackTypeBeforeInput() throws {
    let performer = InputDispatcherPerformerSpy(textPreflight: .unsupported)
    let dispatcher = InputDispatcher(
        performer: performer,
        backgroundTextInputSafety: InputDispatcherTextSafety(.imeOrCandidate)
    )
    let plan = try dispatcher.plan(
        actions: [.type(text: "must-not-fallback", elementRef: "text")],
        context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    )

    #expect(plan.cooperativeError == .backgroundActionUnsupported)
    #expect(plan.lastAcknowledgedAction == -1)
    #expect(performer.performed.isEmpty)
}

@Test func unsafeInputSourceRejectsPlainKeypressBeforeInput() throws {
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        backgroundTextInputSafety: InputDispatcherTextSafety(.unknown)
    )
    let plan = try dispatcher.plan(
        actions: [.keypress(key: "a")],
        context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    )

    // 新契约(2026-09-02): 无 manifest 的合成键输入不再硬拒, 而是升级为
    // foreground takeover(显式授权路径); fail-closed 由 Python enabled_pid_actions 兜底。
    #expect(plan.cooperativeError == nil)
    #expect(plan.requiresTakeover)
    #expect(plan.lastAcknowledgedAction == -1)
    #expect(performer.performed.isEmpty)
}

@Test func safeTextPlanRejectsInputSourceChangeBeforeMutationAndCannotReplay() throws {
    let writer = InputDispatcherTextWriter()
    let element = AXUIElementCreateApplication(11)
    let performer = SystemActionPerformer(
        state: {
            ActionTargetState(
                pid: 11,
                windowID: 22,
                bounds: CGRect(x: 0, y: 0, width: 100, height: 100),
                axIdentity: 33
            )
        },
        lookup: { reference, snapshotID in
            guard reference == "text", snapshotID == "snapshot" else { return nil }
            return ActionElement(
                element: element,
                bounds: CGRect(x: 10, y: 10, width: 50, height: 20),
                role: kAXTextFieldRole as String,
                subrole: nil,
                actions: []
            )
        },
        selectedTextWriter: writer,
        focusedKeyboard: { _ in
            ActionElement(
                element: element,
                bounds: CGRect(x: 10, y: 10, width: 50, height: 20),
                role: kAXTextFieldRole as String,
                subrole: nil,
                actions: []
            )
        }
    )
    let safety = MutableInputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    let dispatcher = InputDispatcher(performer: performer, backgroundTextInputSafety: safety)
    let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    let plan = try dispatcher.plan(
        actions: [.type(text: "must-not-mutate", elementRef: "text")],
        context: context
    )

    safety.value = .imeOrCandidate
    #expect(throws: InputDispatchError.backgroundActionUnsupported) {
        _ = try dispatcher.execute(plan, context: context)
    }
    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try dispatcher.execute(plan, context: context)
    }
    #expect(writer.preflightCalls == 1)
    #expect(writer.writeCalls == 0)
}

@Test func secureReferencedKeypressAndSystemModifierFamiliesAreUnsupportedBeforeInput() throws {
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    let actions: [NativeAction] = [
        .keypress(key: "a", elementRef: "secure"),
        .keypress(key: "f1", modifiers: ["function"]),
        .keypress(key: "space", modifiers: ["option"]),
        .keypress(key: "a", modifiers: ["caps_lock"]),
    ]

    // 新契约(2026-09-02): 普通修饰键组合升级为 foreground takeover(显式授权);
    // secure 引用与系统功能键仍 fail-closed(与 manifest 无关的安全拒绝)。
    let expectations: [CooperativeErrorCode?] = [
        .backgroundActionUnsupported,   // a + secure 引用
        .backgroundActionUnsupported,   // f1 + function 系统键
        nil,                            // space + option → 升级 takeover
        nil,                            // a + caps_lock → 升级 takeover
    ]
    for (action, expected) in zip(actions, expectations) {
        let plan = try dispatcher.plan(actions: [action], context: context)
        #expect(plan.cooperativeError == expected)
        if expected == nil {
            #expect(plan.requiresTakeover)
        }
        #expect(plan.lastAcknowledgedAction == -1)
    }
    let mixed = try dispatcher.plan(
        actions: [.click(elementRef: "press"), .keypress(key: "a", elementRef: "secure")],
        context: context
    )
    #expect(mixed.cooperativeError == .backgroundActionUnsupported)
    #expect(mixed.lastAcknowledgedAction == -1)
    #expect(performer.performed.isEmpty)
}

@Test func unsafeInputMethodConsumesWhitelistedTextThroughKeyboardChannel() throws {
    // 实机（2026-09-03 14:55）：门号来自刚加的 stage 诊断 —— 接管授权已经建立，抛出点在
    // consumeForegroundPlan 的 IME 复核。那道复核是**一刀切**（布局不安全就拒），但探针实测
    // unicode 形态免疫输入法（拼音激活下「测试」「abc」都进的了 Safari 网页输入框），而 plan 侧
    // 那道闸已经改成"只管会不会投真实按键码"。两半必须同一把尺子，否则计划合法、投递被拦。
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy(),
        backgroundTextInputSafety: InputDispatcherTextSafety(.imeOrCandidate)
    )
    let context = DispatchContext(
        guardValue: inputDispatcherForegroundGuard(
            keyboardFocus: inputDispatcherKeyboardFocus("snapshot-focus-text")
        )
    )
    let actions = [NativeAction.type(text: "marker", elementRef: "text")]
    let plan = try dispatcher.plan(actions: actions, context: context)
    #expect(plan.cooperativeError == nil)

    let entries = try dispatcher.consumeForegroundPlan(
        plan,
        authority: foregroundConsumptionAuthority(plan: plan, context: context, actions: actions)
    )

    #expect(entries.count == 1)
    #expect(
        entries[0].backend != .axSelectedText,
        "IME-unsafe consumption must not deliver text through an AX write"
    )
    // consume 只产出待执行条目，执行发生在 executeForegroundActions ⇒ 此处必须还没动过目标。
    #expect(performer.performed.isEmpty)

    // 反向锁更新（P1）：没点名元素 ⇒ 形态不可知。兼容表已批 textEntry 的 app 在接管下
    // 委托给激活后的前台系统焦点（放行）；未批 app 的拒绝边界见
    // unapproved/unavailable-policy 断言（foregroundKeyboardPlanWithoutFocusAuthority...）。
    let unnamed = try dispatcher.plan(
        actions: [NativeAction.type(text: "marker")],
        context: context
    )
    #expect(
        unnamed.cooperativeError == nil && unnamed.requiresTakeover,
        "a compat-approved unnamed text target defers focus to the OS after activation"
    )
}

@Test func namedCommandChordsPreserveExactElementFocusSemantics() throws {
    for key in ["a", "s"] {
        let performer = InputDispatcherPerformerSpy()
        let dispatcher = InputDispatcher(
            performer: performer,
            application: inputDispatcherApplication,
            syntheticPolicy: inputDispatcherSyntheticPolicy(),
            backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
        )
        let snapshotFocus = inputDispatcherKeyboardFocus("snapshot-focus-\(key)")
        let context = DispatchContext(guardValue: inputDispatcherForegroundGuard(keyboardFocus: snapshotFocus))
        let actions = [NativeAction.keypress(key: key, modifiers: ["command"], elementRef: "text")]
        let plan = try dispatcher.plan(actions: actions, context: context)
        #expect(plan.cooperativeError == nil, "\(key) was rejected before foreground consumption")
        let entries = try dispatcher.consumeForegroundPlan(
            plan,
            authority: foregroundConsumptionAuthority(plan: plan, context: context, actions: actions)
        )

        #expect(entries.count == 1)
        #expect(entries[0].backend == .foregroundKeyboard)
        #expect(entries[0].targetKeyboardFocus?.identityToken == performer.element(reference: "text", snapshotID: "snapshot")?.identityToken)
        #expect(entries[0].targetKeyboardFocus?.role == "AXTextField")
        #expect(performer.performed.isEmpty)

        // Without a ref these remain ordinary window commands.
        let unnamedActions = [NativeAction.keypress(key: key, modifiers: ["command"])]
        let unnamed = try dispatcher.plan(actions: unnamedActions, context: context)
        let unnamedEntries = try dispatcher.consumeForegroundPlan(
            unnamed, authority: foregroundConsumptionAuthority(plan: unnamed, context: context, actions: unnamedActions)
        )
        #expect(unnamedEntries[0].targetKeyboardFocus == nil)
        #expect(unnamedEntries[0].backend == .pidKeyboard)
    }

    let rejectionPerformer = InputDispatcherPerformerSpy()
    let rejectionDispatcher = InputDispatcher(
        performer: rejectionPerformer,
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy(),
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let rejectionContext = DispatchContext(guardValue: inputDispatcherForegroundGuard(
        keyboardFocus: inputDispatcherKeyboardFocus("snapshot-focus-rejections")
    ))
    let unapproved = try rejectionDispatcher.plan(
        actions: [.keypress(key: "p", modifiers: ["command"], elementRef: "text")],
        context: rejectionContext
    )
    #expect(unapproved.cooperativeError == nil)
    #expect(unapproved.requiresTakeover)
    #expect(unapproved.lastAcknowledgedAction == -1)

    let secure = try rejectionDispatcher.plan(
        actions: [.keypress(key: "a", modifiers: ["command"], elementRef: "secure")],
        context: rejectionContext
    )
    #expect(secure.cooperativeError == .backgroundActionUnsupported)
    #expect(secure.lastAcknowledgedAction == -1)
    #expect(rejectionPerformer.performed.isEmpty)
}

@Test func keyboardFallbackAndPointerFamiliesRequireTakeoverBeforeInput() throws {
    let performer = InputDispatcherPerformerSpy(textPreflight: .unsupported)
    let dispatcher = InputDispatcher(
        performer: performer,
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    let batches: [[NativeAction]] = [
        [.click(x: 15, y: 15, within: "fallback")],
        [.click(elementRef: "fallback")],
        [inputDispatcherSafeDoubleClick()],
        [.scroll(deltaY: 1, elementRef: "fallback")],
        [inputDispatcherSafeDrag()],
        [.keypress(key: "a")],
        [.type(text: "fallback", elementRef: "text")],
    ]

    for actions in batches {
        let plan = try dispatcher.plan(actions: actions, context: context)
        #expect(plan.requiresTakeover)
        #expect(plan.lastAcknowledgedAction == -1)
    }
    #expect(performer.performed.isEmpty)
}

@Test func publicActionClassesOmitWaitAndFollowTheResolvedBackend() throws {
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let plan = try dispatcher.plan(
        actions: [.click(elementRef: "fallback"), .wait(durationMS: 0)],
        context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    )

    #expect(plan.requiresTakeover)
    #expect(plan.actionClasses == [.click])
    #expect(performer.performed.isEmpty)
}

@Test func mixedAXPressAndPIDScrollExposeFullAndPIDOnlyActionClasses() throws {
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let plan = try dispatcher.plan(
        actions: [.click(elementRef: "press"), .scroll(deltaY: 10)],
        context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    )

    #expect(plan.actionClasses == [.press, .scroll])
    #expect(plan.pidActionClasses == [.scroll])
    #expect(plan.summary.actionClasses == [.press, .scroll])
    #expect(plan.summary.pidActionClasses == [.scroll])
}

@Test func backgroundDeliveryFamilyPlansPidScrollWithoutTakeover() throws {
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy(backgroundDeliveryEnabled: true),
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let plan = try dispatcher.plan(
        actions: [.scroll(deltaY: 1, elementRef: "fallback")],
        context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    )

    #expect(!plan.requiresTakeover)
    #expect(plan.backends == [.pidPointer])
    #expect(plan.pidActionClasses == [.scroll])
    #expect(performer.performed.isEmpty)
}

@Test func backgroundDeliveryFamilyPlansPidClickWithoutTakeover() throws {
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy(backgroundDeliveryEnabled: true),
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let plan = try dispatcher.plan(
        actions: [.click(elementRef: "fallback")],
        context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    )

    #expect(!plan.requiresTakeover)
    #expect(plan.backends == [.pidPointer])
    #expect(plan.actionClasses == [.click])
    #expect(performer.performed.isEmpty)
}

@Test func rightClickAlwaysResolvesToPidPointerNeverAXPress() throws {
    // Right-click has no AXPress semantics: even an element advertising
    // AXPress ("press" fixture) must resolve to pidPointer pointer delivery
    // (takeover by default), never the accessibility press path.
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy(),
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let plan = try dispatcher.plan(
        actions: [.rightClick(elementRef: "press")],
        context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    )

    #expect(plan.requiresTakeover)
    #expect(plan.backends == [.pidPointer])
    #expect(plan.actionClasses == [.click])
    #expect(performer.performed.isEmpty)
}

@Test func backgroundDeliveryFamilyPlansPidRightClickWithoutTakeover() throws {
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy(backgroundDeliveryEnabled: true),
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let plan = try dispatcher.plan(
        actions: [.rightClick(elementRef: "fallback")],
        context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    )

    #expect(!plan.requiresTakeover)
    #expect(plan.backends == [.pidPointer])
    #expect(plan.actionClasses == [.click])
    #expect(performer.performed.isEmpty)
}

@Test func pointerXYWithinUnknownReferenceRejectsStaleReference() throws {
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy(),
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try dispatcher.plan(
            actions: [.rightClick(x: 15, y: 15, within: "unknown")],
            context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
        )
    }
    #expect(performer.performed.isEmpty)
}

@Test func pointerXYWithStaleReferenceNeverFallsBackEvenOutsideWindow() throws {
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy(),
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try dispatcher.plan(
            actions: [.rightClick(x: 100_000, y: 100_000, within: "unknown")],
            context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
        )
    }
}

@Test func backgroundDeliveryFamilyStaysOffByDefaultForUnmanifestedApps() throws {
    let performer = InputDispatcherPerformerSpy()
    // Default policy: backgroundDeliveryEnabled == false, even when the
    // registry cell would allow the action — the family must be explicit.
    let dispatcher = InputDispatcher(
        performer: performer,
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy(),
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let plan = try dispatcher.plan(
        actions: [.scroll(deltaY: 1, elementRef: "fallback")],
        context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    )

    #expect(plan.requiresTakeover)
    #expect(plan.backends == [.pidPointer])
}

@Test func enabledNilElementsRemainPlanableAndFallBackToPidDelivery() throws {
    // AX exposes no enabled attribute for many elements (Finder rows,
    // document scroll regions). They must stay planable (pid delivery or
    // takeover) instead of being rejected as helperFailed. Only an explicit
    // enabled=false stays a hard failure.
    let performer = InputDispatcherPerformerSpy()
    performer.fallbackEnabled = nil
    let dispatcher = InputDispatcher(
        performer: performer,
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy(),
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let plan = try dispatcher.plan(
        actions: [.click(elementRef: "fallback")],
        context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    )

    #expect(plan.requiresTakeover)
    #expect(plan.backends == [.pidPointer])

    let disabledPerformer = InputDispatcherPerformerSpy()
    disabledPerformer.fallbackEnabled = false
    let disabledDispatcher = InputDispatcher(
        performer: disabledPerformer,
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy(),
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    #expect(throws: ActionExecutionError.helperFailed) {
        _ = try disabledDispatcher.plan(
            actions: [.click(elementRef: "fallback")],
            context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
        )
    }
}

@Test func AXPressPlanHasNoPIDActionClasses() throws {
    let performer = InputDispatcherPerformerSpy()
    let plan = try InputDispatcher(performer: performer).plan(
        actions: [.click(elementRef: "press"), .wait(durationMS: 0)],
        context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    )

    #expect(plan.backends == [.axPress, .wait])
    #expect(plan.pidActionClasses.isEmpty)
    #expect(!plan.requiresTakeover)
}

@Test func everyPointerOnlyFamilyExposesItsPIDActionClass() throws {
    let cases: [(NativeAction, DispatchActionClass)] = [
        (.click(x: 15, y: 15, within: "fallback"), .click),
        (.click(elementRef: "fallback"), .click),
        (inputDispatcherSafeDoubleClick(), .doubleClick),
        (.scroll(deltaY: 1), .scroll),
        (inputDispatcherSafeDrag(), .drag),
    ]
    for (action, expected) in cases {
        let plan = try InputDispatcher(performer: InputDispatcherPerformerSpy()).plan(
            actions: [action],
            context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
        )
        #expect(plan.pidActionClasses == [expected])
    }
}

@Test func foregroundPlanConsumptionReturnsOneAlignedEntryPerOriginalActionAndIsOnceOnly() throws {
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy(),
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let context = DispatchContext(guardValue: inputDispatcherForegroundGuard())
    let actions: [NativeAction] = [
        .click(elementRef: "press"),
        .scroll(deltaY: 10, x: 15, y: 15, targetElementRef: "fallback"),
    ]
    let plan = try dispatcher.plan(actions: actions, context: context)

    let entries = try dispatcher.consumeForegroundPlan(
        plan,
        authority: foregroundConsumptionAuthority(plan: plan, context: context, actions: actions)
    )

    #expect(entries.map(\.sourceIndex) == [0, 1])
    #expect(entries.map(\.source) == actions)
    #expect(entries.map(\.backend) == [.axPress, .pidPointer])
    #expect(entries.map(\.actionClass) == [.press, .scroll])
    #expect(entries[0].resolved?.method == .accessibilityPress)
    #expect(entries[1].resolved == nil)
    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try dispatcher.consumeForegroundPlan(
            plan,
            authority: foregroundConsumptionAuthority(plan: plan, context: context, actions: actions)
        )
    }
    #expect(performer.performed.isEmpty)
}

@Test func foregroundPlanPreflightRejectsEveryPredictableMismatchBeforeActionZero() throws {
    enum Mismatch: CaseIterable {
        case unresolvedAXElement
        case countMismatch
        case backendMismatch
        case staleDigest
        case staleSnapshot
        case wrongMode
        case changedTarget
        case foreignDispatcherAuthority
    }

    for mismatch in Mismatch.allCases {
        let performer = InputDispatcherPerformerSpy()
        let dispatcher = InputDispatcher(
            performer: performer,
            application: inputDispatcherApplication,
            syntheticPolicy: inputDispatcherSyntheticPolicy(),
            backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
        )
        let context = DispatchContext(guardValue: inputDispatcherForegroundGuard())
        let actions: [NativeAction] = [
            .click(elementRef: "press"),
            .scroll(deltaY: 10, x: 15, y: 15, targetElementRef: "fallback"),
        ]
        let plan = try dispatcher.plan(actions: actions, context: context)
        if mismatch == .unresolvedAXElement { performer.unavailableReferences.insert("press") }
        if mismatch == .changedTarget {
            performer.state = ActionTargetState(
                pid: 11,
                windowID: 23,
                bounds: context.guardValue.bounds,
                axIdentity: context.guardValue.axIdentity
            )
        }
        let authority = foregroundConsumptionAuthority(
            plan: plan,
            context: context,
            actions: mismatch == .staleDigest
                ? [
                    .click(elementRef: "press"),
                    .scroll(deltaY: 11, x: 15, y: 15, targetElementRef: "fallback"),
                ]
                : actions,
            backends: mismatch == .countMismatch
                ? Array(plan.backends.dropLast())
                : mismatch == .backendMismatch
                    ? plan.backends.map { $0 == .pidPointer ? .axPress : $0 }
                    : plan.backends,
            snapshotID: mismatch == .staleSnapshot ? "stale-snapshot" : context.guardValue.snapshotID,
            interactionMode: mismatch == .wrongMode ? .background : .foregroundTakeover
        )

        if mismatch == .foreignDispatcherAuthority {
            let other = InputDispatcher(performer: performer, processNonce: UUID())
            #expect(throws: ActionExecutionError.staleSnapshot) {
                _ = try other.consumeForegroundPlan(plan, authority: authority)
            }
        } else {
            #expect(throws: ActionExecutionError.self) {
                _ = try dispatcher.consumeForegroundPlan(plan, authority: authority)
            }
        }
        #expect(performer.performed.isEmpty)
    }
}

@Test func foregroundKeyboardPlanIsConsumableOnlyForTheGuardedKeyboardExecutor() throws {
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy(),
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let context = DispatchContext(guardValue: inputDispatcherForegroundGuard(
        keyboardFocus: inputDispatcherKeyboardFocus("ax:focus")
    ))
    let actions: [NativeAction] = [.keypress(key: "a", modifiers: ["command"]), .type(text: "marker")]
    let plan = try dispatcher.plan(actions: actions, context: context)

    let entries = try dispatcher.consumeForegroundPlan(
        plan,
        authority: foregroundConsumptionAuthority(plan: plan, context: context, actions: actions)
    )

    #expect(entries.map(\.backend) == [.pidKeyboard, .foregroundKeyboard])
    #expect(entries.allSatisfy { $0.resolved == nil && $0.actionClass == .text })
    #expect(performer.performed.isEmpty)
}

@Test func foregroundKeyboardPlanWithoutFocusAuthorityFailsClosedBeforeRecord() throws {
    let performer = InputDispatcherPerformerSpy()
    // P1 后「无焦点权威」的 fail-closed 边界只对**未批 textEntry** 的 app 生效
    // （已批 app 降级为前台系统焦点投递，见 foregroundTakeoverTypePlan... 盲投断言）。
    // 用空 registry（未批）policy 保住「计划拒 + 不入 records」的行为验证。
    let dispatcher = InputDispatcher(
        performer: performer,
        application: inputDispatcherApplication,
        syntheticPolicy: SyntheticInputPlanningPolicy(
            pointerCapability: .experimentalAvailable,
            keyboardCapability: .available,
            registry: PIDInputCompatibilityRegistry(cells: [])
        ),
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let context = DispatchContext(guardValue: inputDispatcherForegroundGuard())
    let actions: [NativeAction] = [.keypress(key: "a", modifiers: ["command"]), .type(text: "marker")]

    let plan = try dispatcher.plan(actions: actions, context: context)

    #expect(plan.cooperativeError == .backgroundActionUnsupported)
    #expect(plan.lastAcknowledgedAction == -1)
    #expect(performer.performed.isEmpty)
    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try dispatcher.consumeForegroundPlan(
            plan,
            authority: foregroundConsumptionAuthority(plan: plan, context: context, actions: actions)
        )
    }

    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try dispatcher.plan(
            actions: [.keypress(key: "a", modifiers: ["command"]), .click(elementRef: "missing")],
            context: context
        )
    }
}

@Test func planIsSingleUseAndExactGuardBound() throws {
    let performer = InputDispatcherPerformerSpy()
    let dispatcher = InputDispatcher(
        performer: performer,
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    let plan = try dispatcher.plan(actions: [.wait(durationMS: 0)], context: context)
    let changed = DispatchContext(
        guardValue: ActionGuard(
            pid: 11,
            windowID: 23,
            bounds: CGRect(x: 0, y: 0, width: 100, height: 100),
            axIdentity: 33,
            snapshotID: "snapshot",
            interactionMode: .background
        )
    )

    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try dispatcher.execute(plan, context: changed)
    }
    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try dispatcher.execute(plan, context: context)
    }
    #expect(performer.performed.isEmpty)
}

@Test func executionPreservesSuccessfulPrefixAndUnknownOutcomeBoundary() throws {
    let performer = InputDispatcherPerformerSpy(failures: [1: ActionPerformFailure(error: .helperFailed, inputStarted: true)])
    let dispatcher = InputDispatcher(
        performer: performer,
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    let plan = try dispatcher.plan(actions: [.wait(durationMS: 0), .click(elementRef: "press")], context: context)

    let result = try dispatcher.execute(plan, context: context)

    #expect(result.lastAcknowledgedAction == 0)
    #expect(result.error == .unknownOutcome)
    #expect(result.outcomes == [
        ActionOutcome(index: 0, ok: true, error: nil, effectVerification: .unverified),
        ActionOutcome(index: 1, ok: false, error: .unknownOutcome),
    ])
}

private func inputDispatcherBackgroundGuard() -> ActionGuard {
    ActionGuard(
        pid: 11,
        windowID: 22,
        bounds: CGRect(x: 0, y: 0, width: 100, height: 100),
        axIdentity: 33,
        snapshotID: "snapshot",
        interactionMode: .background
    )
}

private let inputDispatcherApplication = PIDTargetApplication(
    bundleIdentifier: "com.example.editor",
    version: "1.2.3"
)

private func inputDispatcherSyntheticPolicy(
    pointerCapability: PIDPointerDeliveryCapability = .experimentalAvailable,
    keyboardCapability: ForegroundKeyboardDeliveryCapability = .available,
    backgroundDeliveryEnabled: Bool = false
) -> SyntheticInputPlanningPolicy {
    let commandA = ApprovedKeyChord(rawValue: "command+a")!
    let commandS = ApprovedKeyChord(rawValue: "command+s")!
    return SyntheticInputPlanningPolicy(
        pointerCapability: pointerCapability,
        keyboardCapability: keyboardCapability,
        registry: PIDInputCompatibilityRegistry(cells: [
            PIDInputCompatibilityCell(
                bundleIdentifier: inputDispatcherApplication.bundleIdentifier,
                version: inputDispatcherApplication.version,
                backend: .pidPointer,
                action: .click
            ),
            PIDInputCompatibilityCell(
                bundleIdentifier: inputDispatcherApplication.bundleIdentifier,
                version: inputDispatcherApplication.version,
                backend: .pidPointer,
                action: .doubleClick
            ),
            PIDInputCompatibilityCell(
                bundleIdentifier: inputDispatcherApplication.bundleIdentifier,
                version: inputDispatcherApplication.version,
                backend: .pidPointer,
                action: .scroll
            ),
            PIDInputCompatibilityCell(
                bundleIdentifier: inputDispatcherApplication.bundleIdentifier,
                version: inputDispatcherApplication.version,
                backend: .pidPointer,
                action: .drag
            ),
            PIDInputCompatibilityCell(
                bundleIdentifier: inputDispatcherApplication.bundleIdentifier,
                version: inputDispatcherApplication.version,
                backend: .foregroundKeyboard,
                action: .text,
                allowedKeyChords: [commandA, commandS],
                allowTextEntry: true
            ),
        ]),
        backgroundDeliveryEnabled: backgroundDeliveryEnabled
    )
}

private func inputDispatcherSafeDoubleClick() -> NativeAction {
    NativeAction(
        kind: .doubleClick,
        x: 15,
        y: 15,
        endX: nil,
        endY: nil,
        text: nil,
        key: nil,
        deltaX: nil,
        deltaY: nil,
        durationMS: nil,
        elementRef: nil,
        targetElementRef: "fallback",
        modifiers: []
    )
}

private func inputDispatcherSafeDrag() -> NativeAction {
    NativeAction(
        kind: .drag,
        x: 15,
        y: 15,
        endX: 20,
        endY: 20,
        text: nil,
        key: nil,
        deltaX: nil,
        deltaY: nil,
        durationMS: nil,
        elementRef: nil,
        targetElementRef: "fallback",
        modifiers: []
    )
}

private func inputDispatcherForegroundGuard(
    keyboardFocus: KeyboardFocusAuthority? = nil
) -> ActionGuard {
    ActionGuard(
        pid: 11,
        windowID: 22,
        bounds: CGRect(x: 0, y: 0, width: 100, height: 100),
        axIdentity: 33,
        keyboardFocus: keyboardFocus,
        snapshotID: "snapshot",
        interactionMode: .foregroundTakeover
    )
}

private func inputDispatcherKeyboardFocus(_ identity: String) -> KeyboardFocusAuthority {
    KeyboardFocusAuthority(
        identityToken: identity,
        bounds: CGRect(x: 10, y: 10, width: 50, height: 40),
        role: "AXTextArea",
        subrole: nil
    )
}

private func foregroundConsumptionAuthority(
    plan: DispatchPlan,
    context: DispatchContext,
    actions: [NativeAction],
    backends: [DispatchBackend]? = nil,
    snapshotID: String? = nil,
    interactionMode: InteractionMode? = nil
) -> ForegroundPlanConsumptionAuthority {
    ForegroundPlanConsumptionAuthority(
        planRef: plan.planRef,
        snapshotID: snapshotID ?? context.guardValue.snapshotID,
        interactionMode: interactionMode ?? context.guardValue.interactionMode,
        actions: actions,
        backends: backends ?? plan.backends,
        guardValue: context.guardValue
    )
}

private final class InputDispatcherSyntheticPosterSpy: SyntheticInputPosting {
    var preflightCalls = 0
    var events: [SyntheticInputEvent] = []

    func preflight() -> Bool {
        preflightCalls += 1
        return true
    }

    func post(_ event: SyntheticInputEvent) throws {
        events.append(event)
    }
}

private final class InputDispatcherPerformerSpy: ActionProviding {
    var performed: [ResolvedAction] = []
    var state = ActionTargetState(
        pid: 11,
        windowID: 22,
        bounds: CGRect(x: 0, y: 0, width: 100, height: 100),
        axIdentity: 33
    )
    var unavailableReferences: Set<String> = []
    var fallbackIdentity = "fallback"
    var fallbackBounds = CGRect(x: 10, y: 10, width: 20, height: 20)
    var fallbackEnabled: Bool? = true
    var scrollElement = AXUIElementCreateApplication(12)
    var scrollIdentity = "scroll"
    var scrollBounds = CGRect(x: 10, y: 10, width: 20, height: 60)
    var scrollEnabled: Bool? = true
    var scrollActionNames = ActionNameResults.complete([
        kAXIncrementAction as String,
        kAXDecrementAction as String,
    ])
    var scrollPressDirection: AXScrollDirection?
    var scrollPressOwner = AXUIElementCreateApplication(12)
    var scrollPressOwnerIdentity = "scroll-owner"
    var scrollPressButton = AXUIElementCreateApplication(14)
    var scrollPressButtonIdentity = "scroll-button"
    var scrollPressButtonSubrole: String?
    var scrollPressButtonEnabled: Bool? = true
    var pressElement = AXUIElementCreateApplication(11)
    var pressIdentity = "press"
    var pressRole = kAXButtonRole as String
    var pressSubrole: String?
    let textPreflight: AXTextMutationPreflight
    let failures: [Int: ActionPerformFailure]
    /// 实机条件注入：app 在后台时 macOS 根本不报 kAXFocusedUIElement（实测 -25212），
    /// 于是计划期的焦点回读必然失败。用它来复现"接管还没发生 ⇒ 读不到焦点"。
    var focusedKeyboardElementError: ActionExecutionError?

    init(
        textPreflight: AXTextMutationPreflight = .settable,
        failures: [Int: ActionPerformFailure] = [:],
        focusedKeyboardElementError: ActionExecutionError? = nil
    ) {
        self.textPreflight = textPreflight
        self.failures = failures
        self.focusedKeyboardElementError = focusedKeyboardElementError
    }

    func currentTargetState() throws -> ActionTargetState {
        state
    }

    func element(reference: String, snapshotID: String) -> ActionElement? {
        guard snapshotID == "snapshot", !unavailableReferences.contains(reference) else { return nil }
        if reference == "secure" {
            return ActionElement(
                element: AXUIElementCreateApplication(11),
                bounds: CGRect(x: 10, y: 10, width: 20, height: 20),
                role: "AXSecureTextField",
                subrole: nil,
                actions: []
            )
        }
        if reference == "text" {
            return ActionElement(
                element: AXUIElementCreateApplication(11),
                bounds: CGRect(x: 10, y: 10, width: 20, height: 20),
                role: kAXTextFieldRole as String,
                subrole: nil,
                actions: []
            )
        }
        if reference == "fallback" {
            return ActionElement(
                element: AXUIElementCreateApplication(11),
                identityToken: fallbackIdentity,
                bounds: fallbackBounds,
                roleResult: .init(value: kAXGroupRole as String, status: .complete),
                subroleResult: .init(value: nil, status: .complete),
                enabled: fallbackEnabled,
                actionNames: .complete([])
            )
        }
        if reference == "scroll" {
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
        guard reference == "press" else { return nil }
        return ActionElement(
            element: pressElement,
            identityToken: pressIdentity,
            bounds: CGRect(x: 10, y: 10, width: 20, height: 20),
            roleResult: .init(value: pressRole, status: .complete),
            subroleResult: .init(value: pressSubrole, status: .complete),
            enabled: true,
            actionNames: .complete([kAXPressAction as String])
        )
    }

    func pointerElement(reference: String, snapshotID: String) -> ActionElement? {
        element(reference: reference, snapshotID: snapshotID)
    }

    func scrollPressTarget(
        reference: String,
        snapshotID: String,
        direction: AXScrollDirection
    ) -> AXScrollPressTarget? {
        guard reference == "scroll", snapshotID == "snapshot", direction == scrollPressDirection else {
            return nil
        }
        return AXScrollPressTarget(
            owner: ActionElement(
                element: scrollPressOwner,
                identityToken: scrollPressOwnerIdentity,
                bounds: scrollBounds,
                roleResult: .init(value: kAXScrollBarRole as String, status: .complete),
                subroleResult: .init(value: nil, status: .complete),
                enabled: true,
                actionNames: .complete([])
            ),
            button: ActionElement(
                element: scrollPressButton,
                identityToken: scrollPressButtonIdentity,
                bounds: CGRect(x: 10, y: 10, width: 20, height: 10),
                roleResult: .init(value: kAXButtonRole as String, status: .complete),
                subroleResult: .init(value: scrollPressButtonSubrole ?? direction.pageSubrole, status: .complete),
                enabled: scrollPressButtonEnabled,
                actionNames: .complete([kAXPressAction as String])
            )
        )
    }

    func preflightAXTextMutation(_ element: ActionElement) -> AXTextMutationPreflight {
        textPreflight
    }

    func focusedKeyboardElement(matching expected: ActionElement?) throws -> ActionElement {
        if let focusedKeyboardElementError { throw focusedKeyboardElementError }
        guard let expected else { throw ActionExecutionError.targetNotFrontmost }
        return expected
    }

    func perform(_ action: ResolvedAction) throws -> ActionPerformance {
        let index = performed.count
        performed.append(action)
        if let failure = failures[index] { throw failure }
        return ActionPerformance(inputStarted: action.method != .wait)
    }
}

private final class InputDispatcherEffectReader: AXEffectReading {
    private var observations: [AXEffectObservation?]
    private let beforeRead: ((Int, TimeInterval) -> Void)?
    var elements: [AXUIElement] = []
    var remainingBudgets: [TimeInterval] = []

    init(
        _ observations: [AXEffectObservation?],
        beforeRead: ((Int, TimeInterval) -> Void)? = nil
    ) {
        self.observations = observations
        self.beforeRead = beforeRead
    }

    func read(
        _ element: AXUIElement,
        remainingBudget: TimeInterval
    ) -> AXEffectObservation? {
        let readIndex = elements.count
        elements.append(element)
        remainingBudgets.append(remainingBudget)
        beforeRead?(readIndex, remainingBudget)
        guard !observations.isEmpty else { return nil }
        return observations.removeFirst()
    }
}

private struct InputDispatcherTextSafety: BackgroundTextInputSafetyDetecting {
    let value: BackgroundTextInputSafety
    init(_ value: BackgroundTextInputSafety) { self.value = value }
    func detect() -> BackgroundTextInputSafety { value }
}

private final class MutableInputDispatcherTextSafety: BackgroundTextInputSafetyDetecting {
    var value: BackgroundTextInputSafety
    init(_ value: BackgroundTextInputSafety) { self.value = value }
    func detect() -> BackgroundTextInputSafety { value }
}

private final class InputDispatcherTextWriter: AXSelectedTextWriting {
    var preflightCalls = 0
    var writeCalls = 0

    func preflightSelectedText(to _: AXUIElement) -> AXTextMutationPreflight {
        preflightCalls += 1
        return .settable
    }

    func writeSelectedText(_: String, to _: AXUIElement) -> AXSelectedTextWriteResult {
        writeCalls += 1
        return .written
    }
}

private final class InputDispatcherElementState {
    var value: ActionElement
    init(_ value: ActionElement) { self.value = value }
}

@Test func requiresActivePointerPlanRoutesToActiveForegroundTakeoverReason() throws {
    let performer = InputDispatcherPerformerSpy()
    let registry = PIDInputCompatibilityRegistry(cells: [
        PIDInputCompatibilityCell(
            bundleIdentifier: inputDispatcherApplication.bundleIdentifier,
            version: inputDispatcherApplication.version,
            backend: .pidPointer,
            action: DispatchActionClass.click,
            requiresActive: true
        ),
    ])
    let policy = SyntheticInputPlanningPolicy(
        pointerCapability: .experimentalAvailable,
        keyboardCapability: .available,
        registry: registry,
        backgroundDeliveryEnabled: true
    )
    let dispatcher = InputDispatcher(
        performer: performer,
        application: inputDispatcherApplication,
        syntheticPolicy: policy,
        fragmentDraftClock: { 0 }
    )
    let context = DispatchContext(guardValue: inputDispatcherBackgroundGuard())
    let plan = try dispatcher.plan(
        actions: [NativeAction.click(x: 15, y: 15, within: "fallback")],
        context: context
    )

    // 计划阶段分流: requires_active 的 app pointer 动作强制走 takeover 激活路径,
    // reason 用独立标识(与普通 foreground_takeover_required 区分)
    #expect(plan.reason == CooperativeErrorCode.requiresActiveForegroundTakeover.rawValue)
    #expect(plan.requiresTakeover)
    #expect(plan.cooperativeError == nil)
    #expect(performer.performed.isEmpty)
}

@Test func unsafeInputMethodStillPlansTextForUnicodeKeyboardDelivery() throws {
    // 实测（2026-09-03 13:23 / 13:26，探针进程用与生产同形态的事件）：SCIM 拼音正激活时，
    // virtualKey 0 + keyboardSetUnicodeString + postToPid(1237) 把「测试」与「abc」都送进了 Safari
    // 网页输入框（AX value 可见、发送按钮由灰变黑）⇒ 输入法不安全**不构成**拒发文本的理由。
    // 会被截走候选的是真实按键码，AX 写另有不变量守着（见 ActionTests 的 perform 侧用例）。
    // 所以闸的条件必须等于"这一发最终会不会投真实按键码"：role 在 AX 写白名单 ⇒ 投 unicode ⇒ 放行。
    let dispatcher = InputDispatcher(
        performer: InputDispatcherPerformerSpy(textPreflight: .unsupported),
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy(),
        backgroundTextInputSafety: InputDispatcherTextSafety(.imeOrCandidate)
    )
    let plan = try dispatcher.plan(
        actions: [.type(text: "marker", elementRef: "text")],
        context: DispatchContext(guardValue: inputDispatcherForegroundGuard())
    )
    #expect(plan.cooperativeError == nil, "an active IME must not veto unicode text delivery outright")
    #expect(plan.requiresTakeover)
    #expect(!plan.backends.contains(.axSelectedText), "IME-unsafe planning must not pick the AX write route")

    // 真正的漏洞在这一发：原生控件 AX 写**可用**（.settable）时，若仍按白名单去走 AX 写，
    // 就等于在输入法不安全时做了文本 mutation —— 那条不变量守的正是它。窄化后的闸必须把这发
    // 也改路由到键盘 unicode 通道。
    let settableDispatcher = InputDispatcher(
        performer: InputDispatcherPerformerSpy(textPreflight: .settable),
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy(),
        backgroundTextInputSafety: InputDispatcherTextSafety(.imeOrCandidate)
    )
    let settablePlan = try settableDispatcher.plan(
        actions: [.type(text: "marker", elementRef: "text")],
        context: DispatchContext(guardValue: inputDispatcherForegroundGuard())
    )
    #expect(
        !settablePlan.backends.contains(.axSelectedText),
        "an unsafe input method must never reach the AX text mutation path, even when it is settable"
    )
    #expect(settablePlan.cooperativeError == nil)

    // 没点名目标元素 ⇒ 形态不可知。旧语义最保守处理照旧拒；P1 降级授权后：
    // 兼容表已批 textEntry 的 app 在接管下委托给激活后的前台系统焦点，放行。
    let unnamed = try dispatcher.plan(
        actions: [.type(text: "marker")],
        context: DispatchContext(guardValue: inputDispatcherForegroundGuard())
    )
    #expect(unnamed.cooperativeError == nil && unnamed.requiresTakeover)
}

@Test(arguments: ["type", "keypress"])
func namedKeyboardPlanRetainsIdentityAcrossPreActivationFailure(kind: String) throws {
    let performer = InputDispatcherPerformerSpy(textPreflight: .unsupported, focusedKeyboardElementError: .inputFocusRequired)
    let windowBounds = CGRect(x: -300, y: 180, width: 100, height: 100)
    performer.state = ActionTargetState(pid: 11, windowID: 22, bounds: windowBounds, axIdentity: 33)
    let dispatcher = InputDispatcher(
        performer: performer, application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy(),
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let context = DispatchContext(guardValue: ActionGuard(
        pid: 11, windowID: 22, bounds: windowBounds, axIdentity: 33,
        keyboardFocus: nil, snapshotID: "snapshot", interactionMode: .foregroundTakeover
    ))
    let action = kind == "type" ? NativeAction.type(text: "marker", elementRef: "text")
        : .keypress(key: "a", modifiers: ["command"], elementRef: "text")
    let plan = try dispatcher.plan(actions: [action], context: context)
    #expect(plan.cooperativeError == nil && plan.requiresTakeover)
    // Consumption happens after takeover activation; exact focus is now
    // available. Leaving it unavailable correctly rejects before any posting.
    performer.focusedKeyboardElementError = nil
    let entries = try dispatcher.consumeForegroundPlan(
        plan, authority: foregroundConsumptionAuthority(plan: plan, context: context, actions: [action])
    )
    #expect(entries[0].targetKeyboardFocus?.identityToken == performer.element(reference: "text", snapshotID: "snapshot")?.identityToken)
    #expect(entries[0].targetKeyboardFocus?.bounds == CGRect(x: -290, y: 190, width: 20, height: 20))
    #expect(performer.performed.isEmpty)
}

@Test func foregroundTakeoverTypePlanDefersFocusVerificationToDelivery() throws {
    // 复现实机条件（2026-09-03 12:18，Safari oMLX 聊天框）：WebKit 输入框不报 AXSelectedText 可写
    // ⇒ preflight 判 .unsupported ⇒ 只能走 foregroundKeyboard；而 macOS 只在 app 前台时才报
    // kAXFocusedUIElement（只读探针实测：后台 -25212 kAXErrorNoValue，前台 0 且 role=AXTextArea、
    // enabled=1），"前台"恰恰是这份计划要去批准的结果 ⇒ 计划期回读焦点必然失败（实机
    // FOCUS-ACQUIRE-EARLY unverified → AX-MAP raw=-25212 → PLAN-THROW helperFailed）。
    // 焦点复验不许取消，只许挪到激活之后、投递之前：deliveryKeyboardFocusGuard 取激活后一刻的
    // 真实焦点作 expected，validateEnvironment 再要求 currentFocus 与之一致。
    let dispatcher = InputDispatcher(
        performer: InputDispatcherPerformerSpy(
            textPreflight: .unsupported,
            focusedKeyboardElementError: .inputFocusRequired
        ),
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy(),
        backgroundTextInputSafety: InputDispatcherTextSafety(.safeASCIIKeyboardLayout)
    )
    let plan = try dispatcher.plan(
        actions: [.type(text: "marker", elementRef: "text")],
        context: DispatchContext(guardValue: inputDispatcherForegroundGuard())
    )
    #expect(
        plan.cooperativeError == nil,
        "a named text target must not be vetoed by a focus read that cannot succeed pre-activation"
    )
    #expect(plan.requiresTakeover, "delivery still has to go through takeover")

    // 没点名元素 ⇒ 激活之后"谁在焦点上就投给谁"。旧语义为盲投失败关闭；
    // P1：兼容表已批 textEntry 的 app 在接管下降级为"投给激活后的前台系统焦点"。
    let blind = try dispatcher.plan(
        actions: [.type(text: "marker")],
        context: DispatchContext(guardValue: inputDispatcherForegroundGuard())
    )
    #expect(
        blind.cooperativeError == nil && blind.requiresTakeover,
        "compat-approved blind type defers focus to the OS focus after activation"
    )

    // 后台请求没有"激活必然先于投递"这个前提 ⇒ 不许降级，照旧当场抛出（旧行为一字不改）。
    #expect(throws: ActionExecutionError.inputFocusRequired) {
        _ = try dispatcher.plan(
            actions: [.type(text: "marker", elementRef: "text")],
            context: DispatchContext(guardValue: ActionGuard(
                pid: 11,
                windowID: 22,
                bounds: CGRect(x: 0, y: 0, width: 100, height: 100),
                axIdentity: 33,
                keyboardFocus: nil,
                snapshotID: "snapshot",
                interactionMode: .background
            ))
        )
    }
}

@Test func imeActiveBlindTypeUnderTakeoverDelegatesToOSFocusWhenCompatApproved() throws {
    // P1 第二环：IME 激活 + 无元素（形态不可知）曾是 pre-preflight 一刀切保守拒；
    // 兼容表已批 textEntry 的 app 在接管下与焦点 gate 同规则降级——投给激活后
    // 的前台系统焦点。未批 app 仍由同门拒绝（见 background 家族旧断言）。
    let dispatcher = InputDispatcher(
        performer: InputDispatcherPerformerSpy(
            textPreflight: .unsupported,
            focusedKeyboardElementError: .inputFocusRequired
        ),
        application: inputDispatcherApplication,
        syntheticPolicy: inputDispatcherSyntheticPolicy(),
        backgroundTextInputSafety: InputDispatcherTextSafety(.imeOrCandidate)
    )
    let plan = try dispatcher.plan(
        actions: [.type(text: "marker")],
        context: DispatchContext(guardValue: inputDispatcherForegroundGuard())
    )
    #expect(
        plan.cooperativeError == nil && plan.requiresTakeover,
        "compat-approved blind type under active IME defers to the OS focus after activation"
    )
}

@Test(arguments: ["click", "right_click", "double_click", "scroll", "drag"])
func genericForegroundPlansUnknownAppPointerWithoutAXDescendants(kind: String) throws {
    let dispatcher = InputDispatcher(
        performer: InputDispatcherPerformerSpy(),
        application: PIDTargetApplication(bundleIdentifier: "test.unlisted", version: "99"),
        syntheticPolicy: SyntheticInputPlanningPolicy(
            pointerCapability: .available, keyboardCapability: .available,
            registry: PIDInputCompatibilityRegistry(), genericForegroundEnabled: true
        )
    )
    var fields: [String: JSONValue] = ["type": .string(kind), "x": .number(15), "y": .number(15)]
    if kind == "scroll" { fields["delta_y"] = .number(600) }
    if kind == "drag" { fields["end_x"] = .number(30); fields["end_y"] = .number(30) }
    let plan = try dispatcher.plan(
        actions: [try NativeAction.parse(.object(fields))],
        context: DispatchContext(guardValue: inputDispatcherForegroundGuard())
    )
    #expect(plan.cooperativeError == nil)
    #expect(plan.requiresTakeover)
    #expect(plan.backends.map(\.rawValue) == ["foreground_pointer"])
    #expect(plan.summary.pidActionClasses.isEmpty)
}

@Test func genericForegroundRejectsMissingExplicitPointerReference() throws {
    let dispatcher = InputDispatcher(
        performer: InputDispatcherPerformerSpy(),
        application: inputDispatcherApplication,
        syntheticPolicy: SyntheticInputPlanningPolicy(
            pointerCapability: .available, keyboardCapability: .available,
            registry: PIDInputCompatibilityRegistry(), genericForegroundEnabled: true
        )
    )
    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try dispatcher.plan(
            actions: [NativeAction.click(x: 15, y: 15, within: "gone")],
            context: DispatchContext(guardValue: inputDispatcherForegroundGuard())
        )
    }
    #expect(throws: ActionExecutionError.invalidAction) {
        _ = try dispatcher.plan(
            actions: [NativeAction.click(x: 15, y: 15)],
            context: DispatchContext(guardValue: inputDispatcherBackgroundGuard())
        )
    }
}

@Test func genericForegroundKeypressUsesFragmentCompatibleKeyboardBackend() throws {
    let dispatcher = InputDispatcher(
        performer: InputDispatcherPerformerSpy(),
        application: PIDTargetApplication(bundleIdentifier: "test.unlisted", version: "99"),
        syntheticPolicy: SyntheticInputPlanningPolicy(
            pointerCapability: .available, keyboardCapability: .available,
            registry: PIDInputCompatibilityRegistry(), genericForegroundEnabled: true
        )
    )
    let actions = [NativeAction.keypress(key: "tab")]
    let plan = try dispatcher.plan(actions: actions, context: DispatchContext(guardValue: inputDispatcherForegroundGuard()))
    #expect(plan.cooperativeError == nil)
    #expect(plan.backends == [.foregroundKeyboard])
    #expect(plan.foregroundFragmentRequirements(actions: actions)?.first?.backend == .foregroundKeyboard)
}
