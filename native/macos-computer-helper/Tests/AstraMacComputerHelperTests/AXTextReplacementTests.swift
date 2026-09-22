@testable import AstraMacComputerHelperCore
import ApplicationServices
import Foundation
import Testing

@Test func AXTextReplacementRequiresSameOwnedNonsecureEditableField() throws {
    let ax = AXUIElementCreateApplication(11)
    func field(_ element: AXUIElement, role: String = "AXTextField", enabled: Bool? = true,
               bounds: CGRect = CGRect(x: 10, y: 10, width: 30, height: 20)) -> ActionElement {
        ActionElement(element: element, identityToken: "retained", bounds: bounds,
            roleResult: .init(value: role, status: .complete), subroleResult: .init(value: nil, status: .complete),
            enabled: enabled, actionNames: .complete([]))
    }
    let expected = field(ax)
    try validateReplacementField(expected: expected, current: expected, belongs: { CFEqual($0, ax) })
    for current in [nil, field(AXUIElementCreateApplication(12)), field(ax, role: "AXSecureTextField"),
                    field(ax, role: "AXButton"), field(ax, enabled: false), field(ax, enabled: nil),
                    field(ax, bounds: CGRect(x: 11, y: 10, width: 30, height: 20))] {
        #expect(throws: ActionExecutionError.staleSnapshot) {
            try validateReplacementField(expected: expected, current: current, belongs: { _ in true })
        }
    }
    #expect(throws: ActionExecutionError.staleSnapshot) {
        try validateReplacementField(expected: expected, current: expected, belongs: { _ in false })
    }
}

@Test func AXTextReplacementCapabilityIsPublishedInBothSnapshotScopes() throws {
    let node = SerializedAXNode(index: 1, role: "AXWindow", subrole: nil, label: nil, title: nil, help: nil,
        value: nil, enabled: true, focused: true, actions: [], bounds: CGRect(x: 0, y: 0, width: 100, height: 100),
        elementRef: ElementReference(snapshotID: "snapshot", ordinal: 1), children: [], sourceElement: nil)
    for subtree in [false, true] {
        let json = snapshotAXTreeJSON(SerializedAXTree(root: node, nodeCount: 1, jsonByteCount: 0), subtree: subtree)
        guard case let .object(fields) = json,
              case let .array(capabilities)? = fields["observation_capabilities"] else {
            Issue.record("missing capabilities"); continue
        }
        #expect(capabilities.contains(.string("replace_text_v1")))
    }
}

@Test func AXTextReplacementWritesOnceAndVerifiesCompleteValue() throws {
    for text in ["", "A中文🙂", String(repeating: "x", count: 20_000)] {
        let f = TextReplacementFixture()
        f.reads = ["old" as CFString, text as CFString]
        let result = try f.replacer.replace(text, to: f.element, validateBeforeMutation: {})
        #expect(result.inputStarted && result.effectVerification == .verified)
        #expect(!result.observationRequired)
        #expect(f.writes == [text])
        #expect(f.timeouts.last == 0)
    }
}

@Test func AXTextReplacementNoopRequiresExactReadbackWithoutWriting() throws {
    let f = TextReplacementFixture()
    f.reads = ["" as CFString]
    var validations = 0
    let result = try f.replacer.replace("", to: f.element, validateBeforeMutation: { validations += 1 })
    #expect(!result.inputStarted && result.effectVerification == .noop)
    #expect(f.writes.isEmpty && validations >= 2)
}

@Test func AXTextReplacementDoesNotNormalizeReadback() throws {
    let f = TextReplacementFixture()
    f.reads = ["e\u{301}" as CFString, "e\u{301}" as CFString]
    let result = try f.replacer.replace("é", to: f.element, validateBeforeMutation: {})
    #expect(f.writes.count == 1)
    #expect(result.effectVerification == .unverified && result.observationRequired)
}

@Test func AXTextReplacementUnreliableReadbackStopsWithoutSecondWrite() throws {
    for raw: CFTypeRef? in [nil, "wrong" as CFString, NSNumber(value: 42),
                           String(repeating: "x", count: 40_001) as CFString] {
        let f = TextReplacementFixture()
        f.reads = ["old" as CFString, raw]
        let result = try f.replacer.replace("target", to: f.element, validateBeforeMutation: {})
        #expect(result.inputStarted && result.effectVerification == .unverified && result.observationRequired)
        #expect(f.writes == ["target"])
    }
}

@Test func AXTextReplacementSetterFailureIsUnknownAndNeverRetried() {
    let f = TextReplacementFixture()
    f.writeError = .cannotComplete
    do {
        _ = try f.replacer.replace("new", to: f.element, validateBeforeMutation: {})
        Issue.record("expected failed setter")
    } catch let failure as ActionPerformFailure {
        #expect(failure.inputStarted)
    } catch { Issue.record("wrong error: \(error)") }
    #expect(f.writes == ["new"])
}

@Test func AXTextReplacementPreflightAndLimitsFailBeforeMutation() {
    for variant in 0..<4 {
        let f = TextReplacementFixture()
        if variant == 0 { f.settable = false }
        if variant == 1 { f.preflightError = .cannotComplete }
        if variant == 2 { f.timeoutError = .cannotComplete }
        do {
            _ = try f.replacer.replace(variant == 3 ? String(repeating: "x", count: 20_001) : "new",
                to: f.element, validateBeforeMutation: {})
            Issue.record("expected preflight failure \(variant)")
        } catch let failure as ActionPerformFailure {
            #expect(!failure.inputStarted)
        } catch { Issue.record("wrong error: \(error)") }
        #expect(f.writes.isEmpty)
    }
}

@Test func AXTextReplacementChangedTargetBeforeWriteDoesNotMutate() {
    let f = TextReplacementFixture()
    var calls = 0
    do {
        _ = try f.replacer.replace("new", to: f.element, validateBeforeMutation: {
            calls += 1
            if calls == 2 { throw ActionExecutionError.secureTarget }
        })
        Issue.record("expected changed target")
    } catch let failure as ActionPerformFailure {
        #expect(!failure.inputStarted && failure.error == .secureTarget)
    } catch { Issue.record("wrong error: \(error)") }
    #expect(f.writes.isEmpty)
}

@Test func AXTextReplacementChangedTargetAfterWriteRemainsUnknown() {
    let f = TextReplacementFixture()
    do {
        _ = try f.replacer.replace("new", to: f.element, validateBeforeMutation: {
            if !f.writes.isEmpty { throw ActionExecutionError.staleSnapshot }
        })
        Issue.record("expected stale target")
    } catch let failure as ActionPerformFailure {
        #expect(failure.inputStarted && failure.error == .staleSnapshot)
    } catch { Issue.record("wrong error: \(error)") }
    #expect(f.writes.count == 1)
}

@Test func AXTextReplacementDeadlinePreventsLateMutation() {
    let f = TextReplacementFixture()
    f.onRead = { f.time = 10 }
    do {
        _ = try f.replacer.replace("new", to: f.element, validateBeforeMutation: {})
        Issue.record("expected deadline failure")
    } catch let failure as ActionPerformFailure {
        #expect(!failure.inputStarted && failure.error == .actionTimeout)
    } catch { Issue.record("wrong error: \(error)") }
    #expect(f.writes.isEmpty && f.timeouts.last == 0)
}

private final class TextReplacementFixture {
    let element = AXUIElementCreateApplication(11)
    var time: TimeInterval = 0
    var reads: [CFTypeRef?] = ["old" as CFString, "new" as CFString]
    var writes: [String] = []
    var timeouts: [Float] = []
    var settable = true
    var preflightError: AXError = .success
    var writeError: AXError = .success
    var timeoutError: AXError = .success
    var onRead: (() -> Void)?
    lazy var replacer = SystemAXTextValueReplacer(clock: { self.time },
        setMessagingTimeout: { element, timeout in
            #expect(CFEqual(element, self.element))
            self.timeouts.append(timeout)
            return self.timeoutError
        }, isSettable: { element in
            #expect(CFEqual(element, self.element))
            return (self.preflightError, self.settable)
        }, copyValue: { element in
            #expect(CFEqual(element, self.element))
            self.onRead?()
            return (.success, self.reads.isEmpty ? nil : self.reads.removeFirst())
        }, setValue: { element, value in
            #expect(CFEqual(element, self.element))
            self.writes.append(value as String)
            return self.writeError
        })
}
