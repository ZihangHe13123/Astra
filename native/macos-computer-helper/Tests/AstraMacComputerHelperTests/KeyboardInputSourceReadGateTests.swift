import Carbon
import Foundation
import Testing
@testable import AstraMacComputerHelperCore

private func gateSource(ascii: Bool) -> BackgroundTextInputSourceSnapshot {
    BackgroundTextInputSourceSnapshot(
        category: kTISCategoryKeyboardInputSource as String,
        sourceType: (ascii ? kTISTypeKeyboardLayout : kTISTypeKeyboardInputMode) as String,
        isASCIICapable: true
    )
}

@Test(arguments: [false, true])
func KeyboardInputSourceReadGateRefreshesBothDirections(ascii: Bool) {
    var cached = gateSource(ascii: !ascii)
    var refreshes = 0
    let gate = KeyboardInputSourceReadGate(isMainThread: { true }, refresh: {
        refreshes += 1
        cached = gateSource(ascii: ascii)
    })
    let detector = SystemBackgroundTextInputSafetyDetector(snapshot: {
        cached
    }, readGate: gate)
    #expect(detector.detect() == (ascii ? .safeASCIIKeyboardLayout : .imeOrCandidate))
    #expect(refreshes == 1)
}

@Test func KeyboardInputSourceReadGateRefreshesBeforeEveryRead() {
    var trace: [String] = []
    let gate = KeyboardInputSourceReadGate(isMainThread: { true }, refresh: {
        trace.append("refresh")
    })
    for _ in 0..<2 {
        let result = gate.read { trace.append("read"); return 42 }
        #expect(result == 42)
    }
    #expect(trace == ["refresh", "read", "refresh", "read"])
}

@Test func KeyboardInputSourceReadGateRejectsNonMainThreadWithoutReadingOrRefreshing() {
    var refreshes = 0
    var reads = 0
    let gate = KeyboardInputSourceReadGate(isMainThread: { false }, refresh: {
        refreshes += 1
    })
    let detector = SystemBackgroundTextInputSafetyDetector(snapshot: {
        reads += 1; return gateSource(ascii: true)
    }, readGate: gate)
    #expect(detector.detect() == .unknown)
    #expect(refreshes == 0)
    #expect(reads == 0)
}

private enum GateRefreshError: Error { case unavailable }

@Test func KeyboardInputSourceReadGateDoesNotReuseSafeStateAfterRefreshFailure() {
    var fails = false
    var reads = 0
    let gate = KeyboardInputSourceReadGate(isMainThread: { true }, refresh: {
        if fails { throw GateRefreshError.unavailable }
    })
    let detector = SystemBackgroundTextInputSafetyDetector(snapshot: {
        reads += 1; return gateSource(ascii: true)
    }, readGate: gate)
    #expect(detector.detect() == .safeASCIIKeyboardLayout)
    fails = true
    #expect(detector.detect() == .unknown)
    #expect(reads == 1)
    fails = false
    #expect(detector.detect() == .safeASCIIKeyboardLayout)
    #expect(reads == 2)
}

@Test func KeyboardInputSourceReadGateResetsAfterUnavailableRead() {
    var available = false
    var refreshes = 0
    let gate = KeyboardInputSourceReadGate(isMainThread: { true }, refresh: {
        refreshes += 1
    })
    let detector = SystemBackgroundTextInputSafetyDetector(snapshot: {
        available ? gateSource(ascii: true) : nil
    }, readGate: gate)
    #expect(detector.detect() == .unknown)
    available = true
    #expect(detector.detect() == .safeASCIIKeyboardLayout)
    #expect(refreshes == 2)
}

@Test(arguments: [false, true])
func KeyboardInputSourceReadGateRejectsReentryDuringRefreshAndPropertyRead(inRefresh: Bool) {
    var gate: KeyboardInputSourceReadGate!
    var attemptingNestedRead = false
    var nestedReads = 0
    var nested: Int?
    // The flag keeps this test bounded even for the broken pass-through implementation.
    func attemptNested() {
        guard !attemptingNestedRead else { return }
        attemptingNestedRead = true
        nested = gate.read { nestedReads += 1; return 99 }
        attemptingNestedRead = false
    }
    gate = KeyboardInputSourceReadGate(isMainThread: { true }, refresh: {
        if inRefresh { attemptNested() }
    })
    let outer = gate.read { () -> Int? in
        if !inRefresh { attemptNested() }
        return 42
    }
    #expect(outer == 42)
    #expect(nested == nil)
    #expect(nestedReads == 0)
    // A rejected inner read must not permanently block subsequent independent reads.
    #expect(gate.read { 7 } == 7)
}

@Test func KeyboardInputSourceReadGateAlsoRefreshesDiagnosticReads() {
    var cachedID = "com.apple.inputmethod.SCIM.ITABC"
    let gate = KeyboardInputSourceReadGate(isMainThread: { true }, refresh: {
        cachedID = "com.apple.keylayout.ABC"
    })
    #expect(currentKeyboardInputSourceIDForDiagnostics(
        readGate: gate, readIdentifier: { cachedID }
    ) == "com.apple.keylayout.ABC")
}

@Test func KeyboardInputSourceReadGateRejectsDiagnosticReadOnNonMainThread() {
    var reads = 0
    var refreshes = 0
    let gate = KeyboardInputSourceReadGate(isMainThread: { false }, refresh: {
        refreshes += 1
    })
    #expect(currentKeyboardInputSourceIDForDiagnostics(
        readGate: gate, readIdentifier: { reads += 1; return "com.apple.keylayout.ABC" }
    ) == "unavailable-source")
    #expect(reads == 0)
    #expect(refreshes == 0)
}

@Test func KeyboardInputSourceReadGateRejectsCrossReaderReentry() {
    var gate: KeyboardInputSourceReadGate!
    var inRefresh = false
    var diagnosticReads = 0
    var nested = "not-run"
    gate = KeyboardInputSourceReadGate(isMainThread: { true }, refresh: {
        guard !inRefresh else { return }
        inRefresh = true
        nested = currentKeyboardInputSourceIDForDiagnostics(readGate: gate, readIdentifier: {
            diagnosticReads += 1
            return "com.apple.keylayout.ABC"
        })
        inRefresh = false
    })
    let detector = SystemBackgroundTextInputSafetyDetector(snapshot: { gateSource(ascii: true) }, readGate: gate)
    #expect(detector.detect() == .safeASCIIKeyboardLayout)
    #expect(nested == "unavailable-source")
    #expect(diagnosticReads == 0)
}
