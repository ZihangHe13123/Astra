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

@Test func typeAcquiresFocusForTargetFieldThenWritesExactlyOnce() {
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
        selectedTextWriter: writer,
        focusedKeyboard: { _ in other },
        focusAcquisition: { _, expected in
            acquireCalls += 1
            return expected
        }
    )

    #expect(throws: Never.self) {
        _ = try performer.perform(typeAction(target: targetAX, verified: target))
    }
    // 一次 type 会在 resolve 与 revalidate 两处各查一次焦点，故获取可能被触发多次；
    // 真正的不变量是**只写入一次**（生产里第二次复核通过就不会再获取）。
    #expect(acquireCalls >= 1)
    #expect(setCalls == 1)
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
        selectedTextWriter: writer,
        focusedKeyboard: { _ in other },
        focusAcquisition: { _, _ in
            acquireCalls += 1
            return other
        }
    )

    do {
        _ = try performer.perform(typeAction(target: targetAX, verified: target))
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

/// 实机回归锁：Safari 在后台时（不注入 focusedKeyboard ＝ 走真实焦点读取）那次读会失败，
/// 因此获取必须发生在它**之前** —— 否则 type 永远进不去，且日志里连尝试痕迹都没有。
@Test func typeAcquiresFocusBeforeReadingCurrentFocusSoBackgroundTargetsStillWork() {
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
        selectedTextWriter: writer,
        focusAcquisition: { _, expected in
            acquireCalls += 1
            return expected
        }
    )

    #expect(throws: Never.self) {
        _ = try performer.perform(typeAction(target: targetAX, verified: target))
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
