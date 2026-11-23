import Foundation
import Testing

// Inspect the opt-in harness without linking or running any desktop fixture.
@Test func optionalForegroundHarnessRequiresSeparateSetupAndPerPlanHumanConsent() throws {
    let tests = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
    let source = try String(contentsOf: tests.appendingPathComponent("AstraMacComputerE2EHarness/main.swift"), encoding: .utf8)
    let setup = try #require(source.range(of: "runExperimentalPIDPointerMatrix ? try confirmFixtureSetup() : nil"))
    let build = try #require(source.range(of: "let package = packageRoot()"))
    #expect(setup.upperBound <= build.lowerBound)
    let calls = source.components(separatedBy: ".takeoverBegin(").dropFirst()
    #expect(calls.count == 2 && calls.allSatisfy { $0.prefix(180).contains("focusConsent: consent") })
    #expect(source.contains("try approveFixtureBorrow(caseName: \"forced_activation_regression\""))
    #expect(source.contains("try approveFixtureBorrow(caseName: scenario.name"))
    let gate = try String(contentsOf: tests.appendingPathComponent("AstraMacComputerE2EHarness/InteractiveConsent.swift"), encoding: .utf8)
    #expect(gate.contains("isatty(STDIN_FILENO)") && gate.contains("tcgetpgrp(STDIN_FILENO) == getpgrp()"))
    #expect(gate.contains("UUID()") && gate.contains("keyState(.hidSystemState") && gate.contains("liveFrontmostPID()"))
    #expect(!gate.contains("ProcessInfo.processInfo.environment"))
}
