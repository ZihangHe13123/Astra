@testable import AstraMacComputerHelperCore
import CoreGraphics
import Darwin
import Foundation

// Test harness only. An opt-in environment switch selects a test, never the person's decision.
struct FixtureConsentNotGiven: Error {}

private func hasForegroundTTY() -> Bool {
    isatty(STDIN_FILENO) == 1 && tcgetpgrp(STDIN_FILENO) == getpgrp()
}

private func prompt(_ text: String) { FileHandle.standardError.write(Data((text + "\n").utf8)) }

private func confirmCase(_ name: String, description: String, expectedPID: pid_t?) throws -> (stamp: Int, pid: pid_t) {
    guard hasForegroundTTY(), let before = liveFrontmostPID(), before > 0, expectedPID == nil || expectedPID == before else {
        throw FixtureConsentNotGiven()
    }
    let challenge = "approve \(name) \(UUID().uuidString.prefix(8))"
    prompt("\(description)\nTo select this case, type exactly: \(challenge)\nAny other response, EOF or timeout stops the experiment.")
    var ready = pollfd(fd: STDIN_FILENO, events: Int16(POLLIN), revents: 0)
    var reply = [UInt8](repeating: 0, count: 128)
    guard poll(&ready, 1, 60_000) == 1, ready.revents == Int16(POLLIN), hasForegroundTTY() else { throw FixtureConsentNotGiven() }
    let n = reply.withUnsafeMutableBytes { Darwin.read(STDIN_FILENO, $0.baseAddress, $0.count) }
    guard n > 0, reply.prefix(n).elementsEqual((challenge + "\n").utf8), liveFrontmostPID() == before else { throw FixtureConsentNotGiven() }

    // Return submits a line on key-down. It is NOT the approval boundary. Require a distinct
    // physical, non-typing gesture for this prompt; no debounce/grace period after approval.
    func rightShiftDown() -> Bool { CGEventSource.keyState(.hidSystemState, key: 60) } // macOS kVK_RightShift
    func flagsCount() -> UInt32 { CGEventSource.counterForEventType(.hidSystemState, eventType: .flagsChanged) }
    func otherCounts() -> [UInt32] {
        ForegroundTakeoverCoordinator.physicalInputCounts { type in
            type == .flagsChanged ? 0 : CGEventSource.counterForEventType(.hidSystemState, eventType: type)
        }
    }
    let modifiers: CGEventFlags = [.maskShift, .maskControl, .maskAlternate, .maskCommand, .maskSecondaryFn]
    func heldModifiers() -> CGEventFlags { CGEventSource.flagsState(.hidSystemState).intersection(modifiers) }
    guard !rightShiftDown() else { throw FixtureConsentNotGiven() }
    prompt("Release Return. To authorize ONLY \(name), press and release Right Shift alone within 20 seconds.")
    let deadline = ForegroundLoanClock.now() + 20
    func waitWhile(_ pending: () -> Bool) throws {
        while pending() {
            guard hasForegroundTTY(), ForegroundLoanClock.now() < deadline else { throw FixtureConsentNotGiven() }
            usleep(5_000)
        }
    }
    try waitWhile { !rightShiftDown() }
    let downFlags = flagsCount(), downOthers = otherCounts()
    guard rightShiftDown(), heldModifiers() == .maskShift else { throw FixtureConsentNotGiven() }
    try waitWhile { rightShiftDown() }
    let released = ForegroundLoanClock.now() // Before any blocking PID lookup or test preparation.
    guard released < deadline, let livePID = liveFrontmostPID(), livePID == before, hasForegroundTTY(),
          heldModifiers().isEmpty, flagsCount() == downFlags &+ 1, otherCounts() == downOthers else { throw FixtureConsentNotGiven() }
    return (Int(released * 1_000_000), livePID)
}

func confirmFixtureSetup() throws -> pid_t {
    try confirmCase("fixture_setup", description: "Build, sign, launch and inspect the temporary Astra Computer Fixture. Setup may switch focus and restore the current app. No pointer case is authorized by setup.", expectedPID: nil).pid
}

func approveFixtureBorrow(caseName: String, action: String, target: WindowTarget, planRef: String, expectedPID: pid_t) throws -> ForegroundFocusConsent {
    let decision = try confirmCase(caseName, description: "\(caseName): \(action) on Astra Computer Fixture (PID \(target.pid), window \(target.windowID), plan \(planRef)). This single plan may briefly borrow focus and conditionally restore it.", expectedPID: expectedPID)
    return ForegroundFocusConsent(decidedAtContinuousUS: decision.stamp, frontmostPID: decision.pid)
}
