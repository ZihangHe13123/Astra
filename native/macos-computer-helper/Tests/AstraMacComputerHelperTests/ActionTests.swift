@testable import AstraMacComputerHelperCore
import ApplicationServices
import CoreGraphics
import Foundation
import Testing

@Test func replacementWirePreservesIntentAndBindsDigestAndReference() throws {
    for text in ["", "A中文🙂"] {
        let action = try NativeAction.parse(.object([
            "type": .string("type"), "text": .string(text), "element_ref": .string("field"), "replace": .bool(true),
        ]))
        #expect(action != NativeAction.type(text: text, elementRef: "field"))
        #expect(InputDispatcher.digest([action]) != InputDispatcher.digest([.type(text: text, elementRef: "field")]))
        let rebound = try NativeAction.parse(.object([
            "type": .string("type"), "text": .string(text), "element_ref": .string("other"), "replace": .bool(true),
        ]))
        #expect(action.resolvingElementReference("other") == rebound)
    }
}

@Test func replacementExecutionUsesValueWriterAndPreservesVerification() throws {
    let element = AXUIElementCreateApplication(11)
    let target = ActionElement(element: element, bounds: CGRect(x: 10, y: 10, width: 30, height: 20),
        role: kAXTextFieldRole as String, subrole: nil, actions: [])
    let action = try NativeAction.parse(.object([
        "type": .string("type"), "text": .string(""), "element_ref": .string("field"), "replace": .bool(true),
    ]))
    for verification in [ActionEffectVerification.verified, .unverified, .noop] {
        let writer = ReplacementValueWriterSpy(verification: verification)
        let performer = SystemActionPerformer(state: {
            ActionTargetState(pid: 11, windowID: 22, bounds: CGRect(x: 0, y: 0, width: 100, height: 100), axIdentity: 33)
        }, lookup: { _, _ in target }, textValueReplacer: writer,
        replacementTargetValidation: { current in #expect(CFEqual(current.element!, element)) },
        focusedKeyboard: { _ in target })
        let result = try performer.performReplacement(ResolvedAction(source: action, method: .accessibilityText,
            screenPoint: nil, endScreenPoint: nil, element: element, verifiedElement: target), validateMutation: {})
        #expect(writer.writes == [""] && result.effectVerification == verification)
        #expect(result.observationRequired == (verification == .unverified))
        #expect(result.inputStarted == (verification != .noop))
        #expect(performer.preflightAXTextReplacement(target) == .settable)
        do {
            _ = try performer.perform(ResolvedAction(source: action, method: .unicodeText,
                screenPoint: nil, endScreenPoint: nil, element: element, verifiedElement: target))
            Issue.record("replacement must never use legacy unicode/selected-text fallback")
        } catch let error as ActionPerformFailure { #expect(!error.inputStarted) }
    }
}

@Test func replacementExecutionWithoutInvocationActivityGuardIsRejected() throws {
    let ax = AXUIElementCreateApplication(11)
    let target = ActionElement(element: ax, bounds: CGRect(x: 10, y: 10, width: 30, height: 20),
        role: kAXTextFieldRole as String, subrole: nil, actions: [])
    let source = try NativeAction.parse(.object([
        "type": .string("type"), "text": .string(""), "element_ref": .string("field"), "replace": .bool(true),
    ]))
    let writer = ReplacementValueWriterSpy(verification: .verified)
    let performer = SystemActionPerformer(state: {
        ActionTargetState(pid: 11, windowID: 22, bounds: CGRect(x: 0, y: 0, width: 100, height: 100), axIdentity: 33)
    }, lookup: { _, _ in target }, textValueReplacer: writer,
        replacementTargetValidation: { _ in }, focusedKeyboard: { _ in target })
    do {
        _ = try performer.perform(ResolvedAction(source: source, method: .accessibilityText,
            screenPoint: nil, endScreenPoint: nil, element: ax, verifiedElement: target))
        Issue.record("replacement without the invocation lease guard was accepted")
    } catch let error as ActionPerformFailure {
        #expect(!error.inputStarted)
    }
    #expect(writer.writes.isEmpty)
}

private final class ReplacementValueWriterSpy: AXTextValueReplacing {
    let verification: ActionEffectVerification
    var writes: [String] = []
    init(verification: ActionEffectVerification) { self.verification = verification }
    func preflight(to _: AXUIElement) -> AXTextMutationPreflight { .settable }
    func replace(_ text: String, to _: AXUIElement, validateBeforeMutation: () throws -> Void) throws -> TextReplacementResult {
        try validateBeforeMutation()
        writes.append(text)
        return TextReplacementResult(inputStarted: verification != .noop,
            effectVerification: verification, observationRequired: verification == .unverified)
    }
}

@Test func replacementWireRejectsMissingTargetFalseAndMixedFields() {
    let base: [String: JSONValue] = ["type": .string("type"), "text": .string(""), "element_ref": .string("field"), "replace": .bool(true)]
    let invalid: [[String: JSONValue]] = [
        ["replace": .bool(false)], ["replace": .null], ["replace": .number(1)],
        ["element_ref": .null], ["type": .string("keypress"), "key": .string("a")],
        ["modifiers": .array([])], ["x": .number(1)], ["key": .null], ["element_index": .number(1)],
    ]
    for extra in invalid {
        #expect(throws: ActionExecutionError.invalidAction) {
            _ = try NativeAction.parse(.object(base.merging(extra) { _, new in new }))
        }
    }
}

@Test func protocolV2CoordinatePointerUsesIndependentTargetElementReference() throws {
    let action = try NativeAction.parse(.object([
        "type": .string("click"),
        "x": .number(10),
        "y": .number(20),
        "target_element_ref": .string("snapshot:canvas"),
    ]))

    #expect(action.elementRef == nil)
    #expect(action.targetElementRef == "snapshot:canvas")
}

@Test func protocolV2RejectsCoordinatePointerUsingSemanticElementReference() {
    #expect(throws: ActionExecutionError.invalidAction) {
        _ = try NativeAction.parse(.object([
            "type": .string("click"),
            "x": .number(10),
            "y": .number(20),
            "element_ref": .string("snapshot:canvas"),
        ]))
    }
}

@Test func protocolV2RejectsMixedSemanticAndPointerReferences() {
    #expect(throws: ActionExecutionError.invalidAction) {
        _ = try NativeAction.parse(.object([
            "type": .string("click"),
            "x": .number(10),
            "y": .number(20),
            "element_ref": .string("snapshot:semantic"),
            "target_element_ref": .string("snapshot:safe-region"),
        ]))
    }
}

@Test func protocolV2PreservesSemanticElementReferenceClick() throws {
    let action = try NativeAction.parse(.object([
        "type": .string("click"),
        "element_ref": .string("snapshot:button"),
    ]))

    #expect(action.elementRef == "snapshot:button")
    #expect(action.targetElementRef == nil)
    #expect(action.x == nil)
    #expect(action.y == nil)
}

@Test func protocolV2RejectsCoordinateLessReferenceLessScroll() {
    #expect(throws: ActionExecutionError.invalidAction) {
        _ = try NativeAction.parse(.object([
            "type": .string("scroll"),
            "delta_y": .number(10),
        ]))
    }
}

@Test func protocolV2PreservesAbsentHorizontalDeltaForSemanticVerticalScroll() throws {
    let action = try NativeAction.parse(.object([
        "type": .string("scroll"),
        "delta_y": .number(1),
        "element_ref": .string("snapshot:scrollbar"),
    ]))

    #expect(action.deltaX == nil)
    #expect(action.deltaY == 1)
    #expect(action.elementRef == "snapshot:scrollbar")
}

@Test func systemPerformerUsesTheExactSingleAXScrollAction() throws {
    let element = AXUIElementCreateApplication(11)
    var calls: [String] = []
    let performer = SystemActionPerformer(
        state: {
            ActionTargetState(
                pid: 11,
                windowID: 22,
                bounds: CGRect(x: 0, y: 0, width: 100, height: 100),
                axIdentity: 33
            )
        },
        lookup: { _, _ in nil },
        performAXAction: { _, action in
            calls.append(action as String)
            return .success
        }
    )
    let cases: [(ResolvedActionMethod, NativeAction, String)] = [
        (.accessibilityIncrement, .scroll(deltaY: 999, elementRef: "scroll"), kAXIncrementAction as String),
        (.accessibilityDecrement, .scroll(deltaY: -999, elementRef: "scroll"), kAXDecrementAction as String),
    ]

    for (method, source, expectedAction) in cases {
        calls.removeAll()
        _ = try performer.perform(ResolvedAction(
            source: source,
            method: method,
            screenPoint: nil,
            endScreenPoint: nil,
            element: element
        ))
        #expect(calls == [expectedAction])
    }
}

@Test func systemPerformerReportsAXScrollFailureAfterOneDispatchAttempt() {
    let element = AXUIElementCreateApplication(11)
    var calls = 0
    let performer = SystemActionPerformer(
        state: {
            ActionTargetState(
                pid: 11,
                windowID: 22,
                bounds: CGRect(x: 0, y: 0, width: 100, height: 100),
                axIdentity: 33
            )
        },
        lookup: { _, _ in nil },
        performAXAction: { _, _ in
            calls += 1
            return .cannotComplete
        }
    )
    let action = ResolvedAction(
        source: .scroll(deltaY: 1, elementRef: "scroll"),
        method: .accessibilityIncrement,
        screenPoint: nil,
        endScreenPoint: nil,
        element: element
    )

    do {
        _ = try performer.perform(action)
        Issue.record("expected AX action failure")
    } catch let failure as ActionPerformFailure {
        #expect(failure.inputStarted)
    } catch {
        Issue.record("unexpected error: \(error)")
    }
    #expect(calls == 1)
}

@Test func selectedTextRechecksExactFocusAfterSettableProbeBeforeMutation() {
    let targetAX = AXUIElementCreateApplication(11)
    let otherAX = AXUIElementCreateApplication(12)
    let target = ActionElement(
        element: targetAX,
        bounds: CGRect(x: 10, y: 10, width: 50, height: 20),
        role: kAXTextFieldRole as String,
        subrole: nil,
        actions: []
    )
    let other = ActionElement(
        element: otherAX,
        bounds: target.bounds,
        role: kAXTextFieldRole as String,
        subrole: nil,
        actions: []
    )
    var focused = target
    var setCalls = 0
    let writer = SystemAXSelectedTextWriter(
        isSettable: { _ in
            focused = other
            return (.success, true)
        },
        setValue: { _, _ in
            setCalls += 1
            return .success
        }
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
        lookup: { _, _ in target },
        selectedTextWriter: writer,
        focusedKeyboard: { _ in focused }
    )
    let action = ResolvedAction(
        source: .type(text: "must-not-write", elementRef: "text"),
        method: .accessibilityText,
        screenPoint: nil,
        endScreenPoint: nil,
        element: targetAX,
        verifiedElement: target
    )

    do {
        _ = try performer.perform(action)
        Issue.record("expected final focused-element mismatch")
    } catch let failure as ActionPerformFailure {
        #expect(failure.error == .inputFocusRequired)
        #expect(failure.inputStarted == false)
    } catch {
        Issue.record("unexpected error: \(error)")
    }
    #expect(setCalls == 0)
}

@Test func productionInputSurfaceExcludesGlobalSyntheticInputCapabilities() throws {
    let packageRoot = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
    let sourceDirectory = packageRoot.appendingPathComponent("Sources/AstraMacComputerHelper")
    let productionSource = try ["InputEvents.swift", "Actions.swift", "Windows.swift"]
        .map { try String(contentsOf: sourceDirectory.appendingPathComponent($0), encoding: .utf8) }
        .joined(separator: "\n")

    #expect(!productionSource.contains("CGWarpMouseCursorPosition"))
    #expect(!productionSource.contains("CGSyntheticInputPoster"))
    #expect(!productionSource.contains("legacyForegroundActionAllowed"))
    #expect(!productionSource.contains(".post(tap:"))
}

@Test func movedWindowRejectsBeforeInput() {
    let provider = RecordingActionProvider(
        states: [.init(pid: 11, windowID: 22, bounds: CGRect(x: 2, y: 0, width: 100, height: 100), axIdentity: 33)]
    )
    let executor = ActionExecutor(provider: provider)
    let result = executor.run(
        expected: .init(pid: 11, windowID: 22, bounds: CGRect(x: 0, y: 0, width: 100, height: 100), axIdentity: 33, snapshotID: "snapshot"),
        actions: [.click(x: 10, y: 10)]
    )

    #expect(result.error == .staleSnapshot)
    #expect(result.lastAcknowledgedAction == -1)
    #expect(provider.performed.isEmpty)
}

@Test func failureStopsRemainingActions() {
    let state = ActionTargetState(pid: 11, windowID: 22, bounds: CGRect(x: 0, y: 0, width: 100, height: 100), axIdentity: 33)
    let provider = RecordingActionProvider(states: [state, state], failures: [0: .targetGone])
    let executor = ActionExecutor(provider: provider)
    let result = executor.run(
        expected: .init(pid: 11, windowID: 22, bounds: state.bounds, axIdentity: 33, snapshotID: "snapshot"),
        actions: [.click(x: 10, y: 10), .type(text: "must-not-run")]
    )

    #expect(result.outcomes.count == 1)
    #expect(result.lastAcknowledgedAction == -1)
    #expect(provider.performed.count == 1)
}

@Test func successfulBatchSettlesAccessibilityPropagationBeforeReturning() {
    let state = ActionTargetState(pid: 11, windowID: 22, bounds: CGRect(x: 0, y: 0, width: 100, height: 100), axIdentity: 33)
    let provider = RecordingActionProvider(states: [state, state])
    var settleCalls = 0
    let executor = ActionExecutor(provider: provider, settlePropagation: { settleCalls += 1 })
    let result = executor.run(
        expected: .init(pid: 11, windowID: 22, bounds: state.bounds, axIdentity: 33, snapshotID: "snapshot"),
        actions: [.click(x: 10, y: 10)]
    )

    #expect(result.error == nil)
    #expect(result.lastAcknowledgedAction == 0)
    #expect(settleCalls == 1, "a fully acknowledged batch must wait for AX propagation before the caller snapshots")
}

@Test func failedBatchDoesNotSettlePropagation() {
    let state = ActionTargetState(pid: 11, windowID: 22, bounds: CGRect(x: 0, y: 0, width: 100, height: 100), axIdentity: 33)
    let provider = RecordingActionProvider(states: [state, state], failures: [0: .targetGone])
    var settleCalls = 0
    let executor = ActionExecutor(provider: provider, settlePropagation: { settleCalls += 1 })
    let result = executor.run(
        expected: .init(pid: 11, windowID: 22, bounds: state.bounds, axIdentity: 33, snapshotID: "snapshot"),
        actions: [.click(x: 10, y: 10)]
    )

    #expect(result.error != nil)
    #expect(settleCalls == 0, "failed batches fail closed without claiming the UI has settled")
}

@Test func newlyAppearedContainedOverlayRejectsBeforeInputEvenWhenFocusedAXRootIsStillParent() {
    let bounds = CGRect(x: 0, y: 0, width: 500, height: 400)
    let provider = RecordingActionProvider(states: [
        .init(
            pid: 11,
            windowID: 22,
            bounds: bounds,
            axIdentity: 33,
            focusedAXIdentity: 33,
            focusedAXBounds: bounds,
            focusedRootPreference: .containedOverlay
        ),
    ])
    let result = ActionExecutor(provider: provider).run(
        expected: .init(
            pid: 11,
            windowID: 22,
            bounds: bounds,
            axIdentity: 33,
            focusedRootPreference: .selectedWindow,
            snapshotID: "before-overlay"
        ),
        actions: [.click(x: 10, y: 10)]
    )

    #expect(result.error == .staleSnapshot)
    #expect(provider.performed.isEmpty)
}

@Test func completedInputIsAcknowledgedBeforeAResultingOverlayBlocksTheNextAction() {
    let bounds = CGRect(x: 0, y: 0, width: 500, height: 400)
    let stable = ActionTargetState(
        pid: 11,
        windowID: 22,
        bounds: bounds,
        axIdentity: 33,
        focusedAXIdentity: 33,
        focusedAXBounds: bounds,
        focusedRootPreference: .selectedWindow
    )
    let resultingOverlay = ActionTargetState(
        pid: 11,
        windowID: 22,
        bounds: bounds,
        axIdentity: 33,
        focusedAXIdentity: 44,
        focusedAXBounds: CGRect(x: 50, y: 50, width: 300, height: 200),
        focusedRootPreference: .containedOverlay
    )
    let provider = RecordingActionProvider(states: [stable, resultingOverlay])
    let result = ActionExecutor(provider: provider).run(
        expected: .init(
            pid: 11,
            windowID: 22,
            bounds: bounds,
            axIdentity: 33,
            focusedAXIdentity: 33,
            focusedAXBounds: bounds,
            focusedRootPreference: .selectedWindow,
            snapshotID: "before-overlay"
        ),
        actions: [.click(x: 10, y: 10), .wait(durationMS: 1)]
    )

    #expect(result.error == .staleSnapshot)
    #expect(result.lastAcknowledgedAction == 0)
    #expect(result.outcomes.count == 2)
    #expect(provider.performed.count == 1)
}

private final class RecordingActionProvider: ActionProviding {
    var states: [ActionTargetState]
    let failures: [Int: ActionExecutionError]
    var performed: [NativeAction] = []

    init(states: [ActionTargetState], failures: [Int: ActionExecutionError] = [:]) {
        self.states = states
        self.failures = failures
    }

    func currentTargetState() throws -> ActionTargetState {
        guard !states.isEmpty else { throw ActionExecutionError.targetGone }
        return states.removeFirst()
    }

    func element(reference _: String, snapshotID _: String) -> ActionElement? { nil }

    func perform(_ action: ResolvedAction) throws -> ActionPerformance {
        let index = performed.count
        performed.append(action.source)
        if let failure = failures[index] { throw ActionPerformFailure(error: failure, inputStarted: false) }
        return ActionPerformance(inputStarted: action.method != .wait)
    }
}

@Test func unsafeInputMethodSkipsAXWriteAndPostsUnicodeText() throws {
    // 实测反证（2026-09-03 13:23 / 13:26，探针进程投与生产同形态的事件）：SCIM 拼音正激活时，
    // virtualKey 0 + keyboardSetUnicodeString + postToPid(1237) 把「测试」和「abc」都送进了 Safari
    // 网页输入框（AX value 可见、发送按钮由灰变黑、React 状态真更新）⇒ **unicode 形态免疫输入法**。
    // 会被输入法截走候选的是真实按键码；而 AXSelectedText 写在布局不安全时必须禁止
    // （productionTextPreflightRejectsIMEAndUnknownInputSourceBeforeAXMutation 守的就是这个）。
    // 本用例取代 1322aad 那条：它断言"落到键盘通道必投真实按键码"，该假设已被上面的实测推翻。
    final class RecordingPoster: SyntheticInputPosting {
        var events: [SyntheticInputEvent] = []
        func preflight() -> Bool { true }
        func post(_ event: SyntheticInputEvent) throws { events.append(event) }
    }
    let targetAX = AXUIElementCreateApplication(11)
    let target = ActionElement(
        element: targetAX,
        bounds: CGRect(x: 10, y: 10, width: 50, height: 20),
        role: "AXTextArea",
        subrole: nil,
        actions: []
    )
    let action = ResolvedAction(
        source: .type(text: "abc", elementRef: "text"),
        method: .unicodeText,
        screenPoint: nil,
        endScreenPoint: nil,
        element: targetAX,
        verifiedElement: target
    )
    func makePerformer(
        poster: RecordingPoster,
        safety: (() -> BackgroundTextInputSafety)? = nil,
        onWriteAttempt: @escaping () -> Void
    ) -> SystemActionPerformer {
        return SystemActionPerformer(
            state: {
                ActionTargetState(
                    pid: 11,
                    windowID: 22,
                    bounds: CGRect(x: 0, y: 0, width: 100, height: 100),
                    axIdentity: 33
                )
            },
            lookup: { _, _ in target },
            inputPoster: poster,
            selectedTextWriter: SystemAXSelectedTextWriter(
                isSettable: { _ in (.success, true) },
                setValue: { _, _ in onWriteAttempt(); return .success }
            ),
            focusedKeyboard: { _ in target },
            textInputSafety: safety
        )
    }

    // 输入法不安全：既不许试 AX 写，也不许投真实按键码，只能投 unicode。
    let unsafePoster = RecordingPoster()
    var axWriteAttempts = 0
    let unsafePerformer = SystemActionPerformer(
        state: {
            ActionTargetState(
                pid: 11,
                windowID: 22,
                bounds: CGRect(x: 0, y: 0, width: 100, height: 100),
                axIdentity: 33
            )
        },
        lookup: { _, _ in target },
        inputPoster: unsafePoster,
        selectedTextWriter: SystemAXSelectedTextWriter(
            isSettable: { _ in (.success, true) },
            setValue: { _, _ in axWriteAttempts += 1; return .success }
        ),
        focusedKeyboard: { _ in target },
        textInputSafety: { .imeOrCandidate }
    )
    _ = try unsafePerformer.perform(action)
    #expect(axWriteAttempts == 0, "AX text mutation must never be attempted under an unsafe input method")
    let sawUnicode = unsafePoster.events.contains { if case .unicodeKeyDown = $0 { return true } else { return false } }
    let sawVirtual = unsafePoster.events.contains { if case .virtualKeyDown = $0 { return true } else { return false } }
    #expect(sawUnicode, "IME-unsafe text must go out as unicode events, which the IME cannot capture")
    #expect(!sawVirtual, "real key codes under an active IME get captured by candidate handling")

    // 布局安全（或不注入检测）：AX 写优先的既有行为一字不许变。
    let safePoster = RecordingPoster()
    let safePerformer = makePerformer(
        poster: safePoster,
        safety: { .safeASCIIKeyboardLayout },
        onWriteAttempt: { axWriteAttempts += 1 }
    )
    _ = try safePerformer.perform(action)
    #expect(axWriteAttempts == 1, "a safe keyboard layout keeps the existing AX-write-first behaviour")
    #expect(safePoster.events.isEmpty, "a successful AX write must not also post keystrokes")
}

@Test func keyboardFocusNeedsKnownRoleWithoutChangingPointerCompatibility() {
    func focused(_ role: String?, _ status: BoundedAXStringStatus = .complete, substatus: BoundedAXStringStatus = .complete) -> ActionElement {
        ActionElement(element: nil, identityToken: "focus", bounds: CGRect(x: 0, y: 0, width: 10, height: 10),
                      roleResult: BoundedAXStringResult(value: role, status: status),
                      subroleResult: BoundedAXStringResult(value: nil, status: substatus),
                      enabled: nil, actionNames: .complete([]))
    }
    #expect(!focused(nil).acceptsFocusedKeyboardInput)
    #expect(!focused("").acceptsFocusedKeyboardInput)
    #expect(!focused("AXTextArea", .truncated).acceptsFocusedKeyboardInput)
    #expect(!focused("AXTextArea", substatus: .truncated).acceptsFocusedKeyboardInput)
    #expect(focused("AXTextArea").acceptsFocusedKeyboardInput)
    #expect(focused("AXGroup").acceptsFocusedKeyboardInput)
    #expect(!focused(nil).isSecure)
    #expect(!focused("AXSecureTextField").acceptsFocusedKeyboardInput)
}
