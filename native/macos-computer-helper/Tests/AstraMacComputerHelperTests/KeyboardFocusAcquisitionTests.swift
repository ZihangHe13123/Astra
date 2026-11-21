@testable import AstraMacComputerHelperCore
import ApplicationServices
import CoreGraphics
import Foundation
import Testing

// docs/macos-computer-use.md#input-delivery-contracts
//
// 锁四件事：
// ① 焦点不匹配时，只有"本次就要打字进去的文本元素"可以被获取焦点，且获取后必须复核；
// ② 获取不到（或被禁用）时照旧 inputFocusRequired，**绝不写入**（不盲投）；
// ③ 能力在装配处显式开启：没喂 focusAcquisition 就永远不获取（单测不碰真实 AX）；
// ④ 安全字段/非文本角色一律拒绝，与注入闭包无关。

private func makeTarget(role: String, element: AXUIElement) -> ActionElement {
    ActionElement(
        element: element,
        bounds: CGRect(x: 10, y: 10, width: 50, height: 20),
        role: role,
        subrole: nil,
        actions: []
    )
}

private func makeState() -> ActionTargetState {
    ActionTargetState(
        pid: 11,
        windowID: 22,
        bounds: CGRect(x: 0, y: 0, width: 100, height: 100),
        axIdentity: 33
    )
}

private func typeAction(target element: AXUIElement, verified: ActionElement) -> ResolvedAction {
    ResolvedAction(
        source: .type(text: "hi", elementRef: "text"),
        method: .accessibilityText,
        screenPoint: nil,
        endScreenPoint: nil,
        element: element,
        verifiedElement: verified
    )
}

@Test(arguments: [false, true])
func backgroundWriterNeverTakesKeyboardFocus(drift: Bool) throws {
    let ax = AXUIElementCreateApplication(11)
    let b = makeTarget(role: "AXTextField", element: ax)
    let a = makeTarget(role: "AXTextField", element: AXUIElementCreateApplication(12))
    var caret = b, focusWrites = 0, textWrites = 0
    let writer = SystemAXSelectedTextWriter(isSettable: { _ in
        if drift { caret = a } // The person switches fields after background preflight.
        return (.success, true)
    }, setValue: { _, _ in textWrites += 1; return .success })
    let performer = SystemActionPerformer(state: makeState, lookup: { _, _ in b }, selectedTextWriter: writer,
        focusedKeyboard: { _ in caret }, focusAcquisition: { _, field in focusWrites += 1; caret = field; return field })
    do {
        _ = try performer.perform(typeAction(target: ax, verified: b))
        #expect(!drift)
    } catch let failure as ActionPerformFailure {
        #expect(drift && failure.error == .inputFocusRequired && !failure.inputStarted)
    }
    #expect(focusWrites == 0)
    #expect(textWrites == (drift ? 0 : 1))
    #expect(caret.identityToken == (drift ? a.identityToken : b.identityToken))
}

@Test func authorizedTypeAcquiresFocusForTargetFieldThenWritesExactlyOnce() {
    let targetAX = AXUIElementCreateApplication(11)
    let otherAX = AXUIElementCreateApplication(12)
    let target = makeTarget(role: kAXTextAreaRole as String, element: targetAX)
    let other = makeTarget(role: kAXTextAreaRole as String, element: otherAX)

    var setCalls = 0
    var acquireCalls = 0
    let writer = SystemAXSelectedTextWriter(
        isSettable: { _ in (.success, true) },
        setValue: { _, _ in
            setCalls += 1
            return .success
        }
    )
    let performer = SystemActionPerformer(
        state: makeState,
        lookup: { _, _ in target },
        selectedTextWriter: writer, keyboardTargetValidation: { _ in },
        focusedKeyboard: { _ in other },
        focusAcquisition: { _, expected in
            acquireCalls += 1
            return expected
        }
    )

    var leaseChecks = 0
    #expect(throws: Never.self) {
        _ = try performer.perform(typeAction(target: targetAX, verified: target), validateMutation: { leaseChecks += 1 })
    }
    #expect(leaseChecks >= acquireCalls)
    #expect(acquireCalls >= 1)
    #expect(setCalls == 1)
}

@Test(arguments: ["lostFront", "reparented", "moved", "secure", "stoppedDuringProof", "noProof", "alreadyFocused"])
func foregroundFocusWriteRequiresCurrentWindowAndField(probe: String) throws {
    let ax = AXUIElementCreateApplication(11)
    let b = makeTarget(role: "AXTextField", element: ax)
    let a = makeTarget(role: "AXTextField", element: AXUIElementCreateApplication(12))
    var caret = probe == "alreadyFocused" ? b : a, writes = 0, stopped = false
    let current = ActionElement(element: ax, identityToken: b.identityToken,
        bounds: probe == "moved" ? b.bounds.offsetBy(dx: 20, dy: 0) : b.bounds,
        roleResult: b.roleResult, subroleResult: probe == "secure" ? .init(value: "AXSecureTextField", status: .complete) : b.subroleResult,
        enabled: true, actionNames: b.actionNames)
    let proof: ((ActionElement) throws -> Void)? = probe == "noProof" ? nil : { expected in
        if probe == "lostFront" { throw ActionExecutionError.targetNotFrontmost }
        try validateReplacementField(expected: expected, current: current, belongs: { _ in probe != "reparented" })
        if probe == "stoppedDuringProof" { stopped = true }
    }
    let performer = SystemActionPerformer(state: makeState, lookup: { _, _ in current },
        keyboardTargetValidation: proof, focusedKeyboard: { _ in caret },
        focusAcquisition: { _, field in writes += 1; caret = field; return field })
    do {
        _ = try performer.acquireOrVerifyKeyboardFocus(matching: b, validateFocusMutation: {
            if stopped { throw ActionExecutionError.actionTimeout }
        })
        #expect(probe == "alreadyFocused")
    } catch let error as ActionExecutionError {
        #expect(probe != "alreadyFocused")
        #expect(error == (probe == "lostFront" ? .targetNotFrontmost : probe == "stoppedDuringProof" ? .actionTimeout : .staleSnapshot))
    }
    #expect(writes == 0)
    #expect(caret.identityToken == (probe == "alreadyFocused" ? b.identityToken : a.identityToken))
}

@Test func typeRefusesToWriteWhenAcquisitionCannotVerifyFocus() {
    let targetAX = AXUIElementCreateApplication(11)
    let otherAX = AXUIElementCreateApplication(12)
    let target = makeTarget(role: kAXTextAreaRole as String, element: targetAX)
    let other = makeTarget(role: kAXTextAreaRole as String, element: otherAX)

    var setCalls = 0
    var acquireCalls = 0
    let writer = SystemAXSelectedTextWriter(
        isSettable: { _ in (.success, true) },
        setValue: { _, _ in
            setCalls += 1
            return .success
        }
    )
    // 获取"成功"但复核回来仍是别的元素 —— 必须仍然拒绝写入。
    let performer = SystemActionPerformer(
        state: makeState,
        lookup: { _, _ in target },
        selectedTextWriter: writer, keyboardTargetValidation: { _ in },
        focusedKeyboard: { _ in other },
        focusAcquisition: { _, _ in
            acquireCalls += 1
            return other
        }
    )

    do {
        _ = try performer.perform(typeAction(target: targetAX, verified: target), validateMutation: {})
        Issue.record("expected inputFocusRequired")
    } catch let failure as ActionPerformFailure {
        #expect(failure.error == .inputFocusRequired)
        #expect(failure.inputStarted == false)
    } catch {
        Issue.record("unexpected error: \(error)")
    }
    #expect(acquireCalls == 1)
    #expect(setCalls == 0)
}

@Test func typeWithoutExplicitAcquisitionCapabilityNeverAttemptsFocus() {
    let targetAX = AXUIElementCreateApplication(11)
    let otherAX = AXUIElementCreateApplication(12)
    let target = makeTarget(role: kAXTextAreaRole as String, element: targetAX)
    let other = makeTarget(role: kAXTextAreaRole as String, element: otherAX)

    var setCalls = 0
    let writer = SystemAXSelectedTextWriter(
        isSettable: { _ in (.success, true) },
        setValue: { _, _ in
            setCalls += 1
            return .success
        }
    )
    // 没喂 focusAcquisition ⇒ 能力未开启 ⇒ 行为与改动前逐字一致（既有契约）。
    let performer = SystemActionPerformer(
        state: makeState,
        lookup: { _, _ in target },
        selectedTextWriter: writer,
        focusedKeyboard: { _ in other }
    )

    // perform() 会把底层错误包成 ActionPerformFailure，直接 expect 错误码会漏包一层。
    do {
        _ = try performer.perform(typeAction(target: targetAX, verified: target))
        Issue.record("expected inputFocusRequired")
    } catch let failure as ActionPerformFailure {
        #expect(failure.error == .inputFocusRequired)
        #expect(failure.inputStarted == false)
    } catch {
        Issue.record("unexpected error: \(error)")
    }
    #expect(setCalls == 0)
}

@Test func focusAcquisitionGateRejectsNonTextRolesAndSecureOrDisabledTargets() {
    let element = AXUIElementCreateApplication(11)
    #expect(SystemActionPerformer.keyboardFocusAcquisitionAllowed(
        for: makeTarget(role: kAXTextAreaRole as String, element: element)
    ))
    #expect(SystemActionPerformer.keyboardFocusAcquisitionAllowed(
        for: makeTarget(role: kAXTextFieldRole as String, element: element)
    ))
    #expect(SystemActionPerformer.keyboardFocusAcquisitionAllowed(
        for: makeTarget(role: kAXComboBoxRole as String, element: element)
    ))
    // 非文本角色：不给焦点（哪怕它可聚焦）。
    #expect(!SystemActionPerformer.keyboardFocusAcquisitionAllowed(
        for: makeTarget(role: kAXButtonRole as String, element: element)
    ))
    #expect(!SystemActionPerformer.keyboardFocusAcquisitionAllowed(
        for: makeTarget(role: "AXWebArea", element: element)
    ))
}

/// A field that did not report focus pre-activation is still usable after approval.
@Test func authorizedTypeCanAcquireFocusWhenCurrentFocusIsUnavailable() {
    let targetAX = AXUIElementCreateApplication(11)
    let target = makeTarget(role: kAXTextAreaRole as String, element: targetAX)
    var setCalls = 0
    var acquireCalls = 0
    let writer = SystemAXSelectedTextWriter(
        isSettable: { _ in (.success, true) },
        setValue: { _, _ in
            setCalls += 1
            return .success
        }
    )
    let performer = SystemActionPerformer(
        state: makeState,
        lookup: { _, _ in target },
        selectedTextWriter: writer, keyboardTargetValidation: { _ in },
        focusedKeyboard: { _ in throw ActionExecutionError.inputFocusRequired },
        focusAcquisition: { _, expected in
            acquireCalls += 1
            return expected
        }
    )

    #expect(throws: Never.self) {
        _ = try performer.perform(typeAction(target: targetAX, verified: target), validateMutation: {})
    }
    #expect(acquireCalls >= 1)
    #expect(setCalls == 1)
}

// Live Edge 153: after pressing a page button, AXFocused=true on a web field was applied
// asynchronously and the immediate AXFocusedUIElement read still returned the button.
@Test func KeyboardFocusAcquisitionWaitsBrieflyForAnAsynchronousFocusChange() {
    let wanted = AXUIElementCreateApplication(201)
    let previous = AXUIElementCreateApplication(202)
    var reads = 0
    let acquired = acquireKeyboardFocus(expected: makeTarget(role: "AXTextField", element: wanted),
        setFocused: { _ in true },
        readFocused: { reads += 1; return reads < 4 ? previous : wanted },
        identity: { makeTarget(role: "AXTextField", element: $0) },
        settleMilliseconds: 300, pollMilliseconds: 25, sleepMilliseconds: { _ in })
    #expect(acquired?.element.map { CFEqual($0, wanted) } == true)
    #expect(reads == 4)
}

@Test func KeyboardFocusAcquisitionStillReportsTheRealFocusAfterTheSettleWindow() {
    let wanted = AXUIElementCreateApplication(201)
    let previous = AXUIElementCreateApplication(202)
    var reads = 0
    var slept = 0
    let acquired = acquireKeyboardFocus(expected: makeTarget(role: "AXTextField", element: wanted),
        setFocused: { _ in true },
        readFocused: { reads += 1; return previous },
        identity: { makeTarget(role: "AXTextField", element: $0) },
        settleMilliseconds: 300, pollMilliseconds: 25, sleepMilliseconds: { slept += $0 })
    // The caller compares this identity and still refuses to type into the wrong element.
    #expect(acquired?.element.map { CFEqual($0, previous) } == true)
    #expect(slept == 300 && reads == 13)
    #expect(acquireKeyboardFocus(expected: makeTarget(role: "AXTextField", element: wanted),
        setFocused: { _ in false }, readFocused: { wanted },
        identity: { makeTarget(role: "AXTextField", element: $0) }) == nil)
}
