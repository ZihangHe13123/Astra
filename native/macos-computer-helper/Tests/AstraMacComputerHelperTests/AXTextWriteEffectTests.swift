import ApplicationServices
import CoreGraphics
import Testing
@testable import AstraMacComputerHelperCore

// Live Edge 153 (2026-09-23): AXSelectedText on a web search field reported success, the
// receipt said delivered, yet the field and page stayed empty. An accepted-but-ignored
// write must not count as delivered text.

private final class RecordingEffectPoster: SyntheticInputPosting {
    var events: [SyntheticInputEvent] = []
    func preflight() -> Bool { true }
    func post(_ event: SyntheticInputEvent) throws { events.append(event) }
}

private let effectAX = AXUIElementCreateApplication(11)
private let effectField = ActionElement(element: effectAX, bounds: CGRect(x: 10, y: 10, width: 50, height: 20),
    role: "AXTextField", subrole: nil, actions: [])

private func effectProbe(values: [String?], selected: String? = nil,
                         sleeps: @escaping (Int) -> Void = { _ in }) -> AXTextWriteEffectProbe {
    var remaining = values
    return AXTextWriteEffectProbe(
        readValue: { _ in remaining.count > 1 ? remaining.removeFirst() : remaining.first ?? nil },
        readSelectedText: { _ in selected },
        settleMilliseconds: 100, pollMilliseconds: 25, sleepMilliseconds: sleeps)
}

private func effectPerformer(poster: RecordingEffectPoster, writer: SystemAXSelectedTextWriter,
                             memory: AXTextWriteEffectMemory? = nil,
                             webContent: ((AXUIElement) -> Bool)? = nil) -> SystemActionPerformer {
    SystemActionPerformer(
        state: { ActionTargetState(pid: 11, windowID: 22, bounds: CGRect(x: 0, y: 0, width: 100, height: 100),
                                   axIdentity: 33) },
        lookup: { _, _ in effectField }, inputPoster: poster, selectedTextWriter: writer,
        focusedKeyboard: { _ in effectField }, webContentProbe: webContent, axTextWriteMemory: memory)
}

private func typeAction(_ method: ResolvedActionMethod) -> ResolvedAction {
    ResolvedAction(source: .type(text: "abc", elementRef: "text"), method: method,
        screenPoint: nil, endScreenPoint: nil, element: effectAX, verifiedElement: effectField)
}

@Test func AXSelectedTextWriteThatLeavesTheValueUnchangedIsIneffective() throws {
    var slept = 0
    let writer = SystemAXSelectedTextWriter(isSettable: { _ in (.success, true) }, setValue: { _, _ in .success },
        effectProbe: effectProbe(values: [""], sleeps: { slept += $0 }))
    #expect(try writer.writeSelectedText("Lyra", to: effectAX, validateBeforeMutation: {}) == .ineffective)
    // The whole bounded settle window is spent before calling it ineffective, and no more.
    #expect(slept >= 100 && slept <= 125)
}

@Test func AXSelectedTextWriteIsWrittenWhenTheEffectIsSeenOrCannotBeJudged() throws {
    func write(_ values: [String?], text: String = "Lyra", selected: String? = nil) throws -> AXSelectedTextWriteResult {
        try SystemAXSelectedTextWriter(isSettable: { _ in (.success, true) }, setValue: { _, _ in .success },
            effectProbe: effectProbe(values: values, selected: selected))
            .writeSelectedText(text, to: effectAX, validateBeforeMutation: {})
    }
    #expect(try write(["", "Lyra"]) == .written)
    // An asynchronous AX update inside the settle window still counts.
    #expect(try write(["", "", "", "Lyra"]) == .written)
    // Unreadable values prove nothing either way.
    #expect(try write([nil]) == .written)
    #expect(try write(["", nil]) == .written)
    // Replacing a selection with identical text legitimately keeps the value.
    #expect(try write(["abc"], text: "b", selected: "b") == .written)
    // Without a probe the legacy behavior is unchanged.
    #expect(try SystemAXSelectedTextWriter(isSettable: { _ in (.success, true) }, setValue: { _, _ in .success })
        .writeSelectedText("Lyra", to: effectAX, validateBeforeMutation: {}) == .written)
}

@Test func IneffectiveForegroundAXTextWriteFallsBackToKeyboardAndIsRemembered() throws {
    let poster = RecordingEffectPoster()
    let memory = AXTextWriteEffectMemory()
    var writes = 0
    let performer = effectPerformer(poster: poster, writer: SystemAXSelectedTextWriter(
        isSettable: { _ in (.success, true) }, setValue: { _, _ in writes += 1; return .success },
        effectProbe: effectProbe(values: [""])), memory: memory)
    #expect(try performer.perform(typeAction(.unicodeText)).inputStarted)
    #expect(writes == 1)
    #expect(!poster.events.isEmpty)
    #expect(memory.ignoresSelectedTextWrites(pid: 11))
    // Later actions in this process skip the ignored write entirely.
    poster.events.removeAll()
    #expect(try performer.perform(typeAction(.unicodeText)).inputStarted)
    #expect(writes == 1)
    #expect(!poster.events.isEmpty)
    #expect(performer.preflightAXTextMutation(effectField) == .unsupported)
}

@Test func IneffectiveBackgroundAXTextWriteReportsNothingSentAndReroutesNextPlan() {
    let poster = RecordingEffectPoster()
    let memory = AXTextWriteEffectMemory()
    let performer = effectPerformer(poster: poster, writer: SystemAXSelectedTextWriter(
        isSettable: { _ in (.success, true) }, setValue: { _, _ in .success },
        effectProbe: effectProbe(values: [""])), memory: memory)
    #expect(performer.preflightAXTextMutation(effectField) == .settable)
    do {
        _ = try performer.perform(typeAction(.accessibilityText))
        Issue.record("an ignored background write must not be reported as delivered")
    } catch let failure as ActionPerformFailure {
        #expect(failure.error == .inputFocusRequired)
        #expect(failure.inputStarted == false)
    } catch {
        Issue.record("unexpected error: \(error)")
    }
    #expect(poster.events.isEmpty)
    #expect(memory.ignoresSelectedTextWrites(pid: 11))
    #expect(performer.preflightAXTextMutation(effectField) == .unsupported)
}

@Test func WebContentTextFieldsNeverAttemptAXSelectedTextWrites() throws {
    let poster = RecordingEffectPoster()
    var writes = 0
    let performer = effectPerformer(poster: poster, writer: SystemAXSelectedTextWriter(
        isSettable: { _ in (.success, true) }, setValue: { _, _ in writes += 1; return .success }),
        webContent: { _ in true })
    #expect(performer.preflightAXTextMutation(effectField) == .unsupported)
    #expect(try performer.perform(typeAction(.unicodeText)).inputStarted)
    #expect(writes == 0)
    #expect(!poster.events.isEmpty)
}

@Test func WebAreaProbeWalksABoundedParentChain() {
    let chain = (101...104).map { AXUIElementCreateApplication(pid_t($0)) }
    func parent(_ element: AXUIElement) -> AXUIElement? {
        guard let index = chain.firstIndex(where: { CFEqual($0, element) }), index + 1 < chain.count else { return nil }
        return chain[index + 1]
    }
    func roles(_ values: [String]) -> (AXUIElement) -> String? {
        { element in chain.firstIndex(where: { CFEqual($0, element) }).map { values[$0] } }
    }
    #expect(axElementIsInsideWebArea(chain[0], parent: parent,
        role: roles(["AXTextField", "AXGroup", "AXWebArea", "AXWindow"])))
    #expect(!axElementIsInsideWebArea(chain[0], parent: parent,
        role: roles(["AXTextField", "AXGroup", "AXSplitGroup", "AXWindow"])))
    // The field itself is not its own web area, and cycles or deep chains stop.
    #expect(!axElementIsInsideWebArea(chain[0], parent: { _ in nil }, role: { _ in "AXWebArea" }))
    #expect(!axElementIsInsideWebArea(chain[0], parent: { $0 }, role: { _ in "AXGroup" }, maximumDepth: 8))
}
