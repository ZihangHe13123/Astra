@testable import AstraMacComputerHelperCore
import ApplicationServices
import Foundation
import Testing

@Test func focusedApplicationQueryFailsClosedOnErrorsAndMalformedValues() {
    #expect(focusedApplicationPID(error: .cannotComplete, value: AXUIElementCreateApplication(123)) == nil)
    #expect(focusedApplicationPID(error: .success, value: nil) == nil)
    #expect(focusedApplicationPID(error: .success, value: "not an AX application" as CFString) == nil)
    #expect(focusedApplicationPID(error: .success, value: AXUIElementCreateSystemWide()) == nil)
}

@Test func focusedApplicationQueryUsesEachNewObservedPID() {
    #expect(focusedApplicationPID(error: .success, value: AXUIElementCreateApplication(123)) == 123)
    #expect(focusedApplicationPID(error: .success, value: AXUIElementCreateApplication(456)) == 456)
}

@Test func launchServicesFallbackNamesTheOneActiveWindowOwner() {
    let active: Set<pid_t> = [456]
    #expect(launchServicesFrontmostPID(candidates: [123, 456, 456, 789], isActive: { active.contains($0) }) == 456)
    #expect(launchServicesFrontmostPID(candidates: [123, 789], isActive: { active.contains($0) }) == nil)
    #expect(launchServicesFrontmostPID(candidates: [], isActive: { _ in true }) == nil)
    // Two active owners, or a non-process owner, can never name the front application.
    #expect(launchServicesFrontmostPID(candidates: [123, 456], isActive: { _ in true }) == nil)
    #expect(launchServicesFrontmostPID(candidates: [0, -1], isActive: { _ in true }) == nil)
}

@Test func failureStagesNameTheErrorWithoutItsMessage() {
    #expect(observationFailureStage(WindowObservationError.targetNotFrontmost) == "target_not_frontmost")
    #expect(observationFailureStage(WindowObservationError.artifactQuotaExceeded) == "artifact_quota")
    #expect(observationFailureStage(WindowObservationError.captureFailed) == "capture")
    let other = observationFailureStage(NSError(domain: "Private detail? no", code: 7, userInfo: [
        NSLocalizedDescriptionKey: "/Users/someone/secret.txt",
    ]))
    #expect(other == "other domain=Private detail? no code=7")
    #expect(!other.contains("secret"))
}
