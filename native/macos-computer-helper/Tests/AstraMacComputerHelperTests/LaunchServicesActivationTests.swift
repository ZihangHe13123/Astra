@testable import AstraMacComputerHelperCore
@preconcurrency import ApplicationServices
import CoreGraphics
import Foundation
import Testing

@Test func exactApplicationVersionReadsOnlyTheSelectedBundleMetadata() throws {
    let root = FileManager.default.temporaryDirectory
        .appendingPathComponent("astra-version-\(UUID().uuidString)")
    let contents = root.appendingPathComponent("Fixture.app/Contents")
    try FileManager.default.createDirectory(at: contents, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: root) }
    let plist: [String: Any] = [
        "CFBundleIdentifier": "dev.astra.fixture",
        "CFBundleExecutable": "Fixture",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.2.3",
    ]
    let data = try PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
    try data.write(to: contents.appendingPathComponent("Info.plist"))

    #expect(exactApplicationVersion(bundleURL: contents.deletingLastPathComponent()) == "1.2.3")
    #expect(exactApplicationVersion(bundleURL: nil) == nil)
}

@Test func foregroundActivationUsesExactBundlePathAndVerifiesResult() throws {
    let target = activationTarget()
    let runtime = ActivationRuntimeSpy(
        identity: ApplicationLaunchIdentity(
            bundleURL: URL(fileURLWithPath: "/Applications/TextEdit.app"),
            localizedName: "TextEdit"
        ),
        expected: target
    )
    let launcher = ProcessLauncherSpy { runtime.markVerified() }
    let controller = LaunchServicesApplicationActivationController(runtime: runtime, launcher: launcher)

    try controller.activate(target)

    #expect(launcher.invocations == [
        ProcessLaunchInvocation(executable: "/usr/bin/open", arguments: ["/Applications/TextEdit.app"]),
    ])
    #expect(!launcher.invocations[0].arguments.contains("-n"))
    #expect(runtime.unhideCount == 1)
    #expect(runtime.setFocusedWindowCount == 1)
    #expect(runtime.raiseCount == 1)
}

@Test func foregroundActivationUsesLocalizedNameOnlyWhenBundleURLIsAbsent() throws {
    let target = activationTarget()
    let runtime = ActivationRuntimeSpy(
        identity: ApplicationLaunchIdentity(bundleURL: nil, localizedName: "访达"),
        expected: target
    )
    let launcher = ProcessLauncherSpy { runtime.markVerified() }

    try LaunchServicesApplicationActivationController(runtime: runtime, launcher: launcher).activate(target)

    #expect(launcher.invocations == [
        ProcessLaunchInvocation(executable: "/usr/bin/open", arguments: ["-a", "访达"]),
    ])
}

@Test func foregroundActivationFailsClosedWithoutBundlePathOrLocalizedName() {
    let target = activationTarget()
    let runtime = ActivationRuntimeSpy(
        identity: ApplicationLaunchIdentity(bundleURL: nil, localizedName: nil),
        expected: target
    )
    let launcher = ProcessLauncherSpy()

    #expect(throws: WindowObservationError.self) {
        try LaunchServicesApplicationActivationController(runtime: runtime, launcher: launcher).activate(target)
    }
    #expect(launcher.invocations.isEmpty)
}

@Test func zeroOpenExitDoesNotSucceedWithoutFrontmostAndExactFocusedWindowChecks() {
    let target = activationTarget()
    let runtime = ActivationRuntimeSpy(
        identity: ApplicationLaunchIdentity(
            bundleURL: URL(fileURLWithPath: "/Applications/TextEdit.app"),
            localizedName: "TextEdit"
        ),
        expected: target
    )
    let launcher = ProcessLauncherSpy(status: 0)
    var now = Date(timeIntervalSince1970: 0)
    let controller = LaunchServicesApplicationActivationController(
        runtime: runtime,
        launcher: launcher,
        now: { now },
        sleep: { interval in now = now.addingTimeInterval(interval) },
        timeout: 0.05,
        retryInterval: 0.05
    )

    expectTargetNotFrontmost { try controller.activate(target) }
    #expect(!launcher.invocations.isEmpty)
}

@Test func matchingAXHashDoesNotReplaceExactFocusedWindowVerification() {
    let target = activationTarget()
    let runtime = ActivationRuntimeSpy(
        identity: ApplicationLaunchIdentity(
            bundleURL: URL(fileURLWithPath: "/Applications/TextEdit.app"),
            localizedName: "TextEdit"
        ),
        expected: target,
        exactFocusedWindowMatches: false
    )
    let launcher = ProcessLauncherSpy { runtime.markVerified() }
    var now = Date(timeIntervalSince1970: 0)
    let controller = LaunchServicesApplicationActivationController(
        runtime: runtime,
        launcher: launcher,
        now: { now },
        sleep: { interval in now = now.addingTimeInterval(interval) },
        timeout: 0.05,
        retryInterval: 0.05
    )

    expectTargetNotFrontmost { try controller.activate(target) }
}

@Test func popupPointerActivationRequiresItsExplicitPointProof() throws {
    let target = activationTarget()
    let point = CGPoint(x: 180, y: 122)
    let runtime = ActivationRuntimeSpy(
        identity: ApplicationLaunchIdentity(bundleURL: nil, localizedName: "Test"),
        expected: target,
        exactFocusedWindowMatches: false,
        popupPointerMatches: true
    )
    runtime.markVerified()
    let clock = ActivationTestClock()
    let launcher = ProcessLauncherSpy()
    let controller = LaunchServicesApplicationActivationController(
        runtime: runtime, launcher: launcher,
        now: { clock.now() }, sleep: { clock.sleep($0) },
        timeout: 0.05, retryInterval: 0.05
    )

    try controller.activatePopupPointerOnly(target, at: point)
    #expect(runtime.popupPointerMatchCount == 1)
    #expect(runtime.lastPopupPoint == point)
    #expect(launcher.invocations.isEmpty)
    #expect(runtime.unhideCount == 0)
    #expect(runtime.setFocusedWindowCount == 0)
    #expect(runtime.raiseCount == 0)
    #expect(runtime.focusedWindowMatchCount == 0)
    // A regular activation for the same target still requires AX focus.
    expectTargetNotFrontmost { try controller.activate(target) }
    #expect(runtime.popupPointerMatchCount == 1)
}

@Test func popupPointerActivationFailsClosedWithoutFrontmostOrProof() {
    let target = activationTarget()
    let point = CGPoint(x: 180, y: 122)
    for (frontmost, proof) in [(false, true), (true, false)] {
        let runtime = ActivationRuntimeSpy(
            identity: ApplicationLaunchIdentity(bundleURL: nil, localizedName: "Test"),
            expected: target, exactFocusedWindowMatches: false,
            frontmostMatches: frontmost, popupPointerMatches: proof
        )
        runtime.markVerified()
        let clock = ActivationTestClock()
        let launcher = ProcessLauncherSpy()
        let controller = LaunchServicesApplicationActivationController(
            runtime: runtime, launcher: launcher,
            now: { clock.now() }, sleep: { clock.sleep($0) },
            timeout: 0.05, retryInterval: 0.05
        )
        expectTargetNotFrontmost { try controller.activatePopupPointerOnly(target, at: point) }
        #expect(runtime.popupPointerMatchCount == (frontmost ? 1 : 0))
        #expect(launcher.invocations.isEmpty)
        #expect(runtime.unhideCount == 0)
        #expect(runtime.setFocusedWindowCount == 0)
        #expect(runtime.raiseCount == 0)
    }
}

@Test func activationFailureLogsTheLastExactWindowChecksOnce() {
    let target = activationTarget()
    let runtime = ActivationRuntimeSpy(
        identity: ApplicationLaunchIdentity(bundleURL: nil, localizedName: "Test"),
        expected: target,
        exactFocusedWindowMatches: false
    )
    runtime.markVerified()
    let clock = ActivationTestClock()
    var lines: [String] = []
    let controller = LaunchServicesApplicationActivationController(
        runtime: runtime,
        launcher: ProcessLauncherSpy(),
        now: { clock.now() },
        sleep: { clock.sleep($0) },
        diagnostic: { lines.append($0) },
        timeout: 0.05,
        retryInterval: 0.05
    )

    expectTargetNotFrontmost { try controller.activate(target) }

    #expect(lines.count == 1)
    #expect(lines[0].contains("ACTIVATE-FAIL pid=42 windowID=24 attempts=1"))
    #expect(lines[0].contains("frontmost=true setFocusedWindow=true raiseWindow=true focusedWindowMatches=false"))
    #expect(!lines[0].contains("Document"))
}

@Test func activationFailureNamesTheApplicationSeenInFrontAndEveryLaunch() {
    let target = activationTarget()
    // The launch succeeds but another application (999) stays in front: the live WeChat shape.
    let runtime = ActivationRuntimeSpy(
        identity: ApplicationLaunchIdentity(bundleURL: nil, localizedName: "Test"),
        expected: target
    )
    let clock = ActivationTestClock()
    var lines: [String] = []
    let launcher = ProcessLauncherSpy()
    let controller = LaunchServicesApplicationActivationController(
        runtime: runtime,
        launcher: launcher,
        now: { clock.now() },
        sleep: { clock.sleep($0) },
        diagnostic: { lines.append($0) },
        timeout: 1,
        retryInterval: 0.05,
        activationSettleInterval: 0.3
    )

    expectTargetNotFrontmost { try controller.activate(target) }

    #expect(lines.count == 1)
    #expect(lines[0].contains("attempts=0 frontmost=false"))
    #expect(lines[0].contains("lastFrontmostPID=999 launches=\(launcher.invocations.count) launchFailures=0"))
    #expect(launcher.invocations.count > 1)
}

@Test func activationFailureCountsRefusedLaunches() {
    let target = activationTarget()
    let runtime = ActivationRuntimeSpy(
        identity: ApplicationLaunchIdentity(bundleURL: nil, localizedName: "Test"),
        expected: target
    )
    let clock = ActivationTestClock()
    var lines: [String] = []
    let launcher = ProcessLauncherSpy(status: 1)
    let controller = LaunchServicesApplicationActivationController(
        runtime: runtime,
        launcher: launcher,
        now: { clock.now() },
        sleep: { clock.sleep($0) },
        diagnostic: { lines.append($0) },
        timeout: 1,
        retryInterval: 0.05,
        activationSettleInterval: 0.3
    )

    expectTargetNotFrontmost { try controller.activate(target) }

    let launches = launcher.invocations.count
    #expect(launches > 1)
    #expect(lines.count == 1)
    #expect(lines[0].contains("launches=\(launches) launchFailures=\(launches)"))
}

@Test func activationFailureLogsSkippedFocusedMatchAfterSetRefusal() {
    let target = activationTarget()
    let runtime = ActivationRuntimeSpy(
        identity: ApplicationLaunchIdentity(bundleURL: nil, localizedName: "Test"),
        expected: target,
        setFocusedWindowSucceeds: false
    )
    runtime.markVerified()
    let clock = ActivationTestClock()
    var lines: [String] = []
    let controller = LaunchServicesApplicationActivationController(
        runtime: runtime,
        launcher: ProcessLauncherSpy(),
        now: { clock.now() },
        sleep: { clock.sleep($0) },
        diagnostic: { lines.append($0) },
        timeout: 0.05,
        retryInterval: 0.05
    )

    expectTargetNotFrontmost { try controller.activate(target) }

    #expect(lines.count == 1)
    #expect(lines[0].contains("setFocusedWindow=false raiseWindow=true focusedWindowMatches=not_checked"))
    #expect(runtime.focusedWindowMatchCount == 0)
}

@Test func nonzeroOpenExitRemainsTargetNotFrontmostAfterVerificationSignalsMatch() {
    let target = activationTarget()
    let runtime = ActivationRuntimeSpy(
        identity: ApplicationLaunchIdentity(
            bundleURL: URL(fileURLWithPath: "/Applications/TextEdit.app"),
            localizedName: "TextEdit"
        ),
        expected: target
    )
    let launcher = ProcessLauncherSpy(status: 7) { runtime.markVerified() }
    let controller = expiringActivationController(runtime: runtime, launcher: launcher)

    expectTargetNotFrontmost { try controller.activate(target) }
}

@Test func wrongFrontmostPIDRemainsTargetNotFrontmostAfterOpenSucceeds() {
    let target = activationTarget()
    let runtime = ActivationRuntimeSpy(
        identity: ApplicationLaunchIdentity(
            bundleURL: URL(fileURLWithPath: "/Applications/TextEdit.app"),
            localizedName: "TextEdit"
        ),
        expected: target,
        frontmostMatches: false
    )
    let launcher = ProcessLauncherSpy { runtime.markVerified() }

    expectTargetNotFrontmost {
        try expiringActivationController(runtime: runtime, launcher: launcher).activate(target)
    }
}

@Test func processLaunchErrorsRetryThenBecomeTargetNotFrontmost() {
    let target = activationTarget()
    let runtime = ActivationRuntimeSpy(
        identity: ApplicationLaunchIdentity(
            bundleURL: URL(fileURLWithPath: "/Applications/TextEdit.app"),
            localizedName: "TextEdit"
        ),
        expected: target
    )
    let launcher = ThrowingProcessLauncherSpy()

    expectTargetNotFrontmost {
        try expiringActivationController(runtime: runtime, launcher: launcher).activate(target)
    }
    #expect(launcher.invocationCount > 0)
}

@Test func stalledOpenChildStopsAtDeadlineWithoutAXFocusOrRaise() {
    let target = activationTarget()
    let runtime = ActivationRuntimeSpy(
        identity: ApplicationLaunchIdentity(
            bundleURL: URL(fileURLWithPath: "/Applications/TextEdit.app"),
            localizedName: "TextEdit"
        ),
        expected: target
    )
    let process = StalledApplicationProcess()
    var now = Date(timeIntervalSince1970: 0)
    let launcher = SystemApplicationProcessLauncher(
        processFactory: { process },
        now: { now },
        sleep: { interval in now = now.addingTimeInterval(interval) },
        pollInterval: 0.01
    )
    let controller = LaunchServicesApplicationActivationController(
        runtime: runtime,
        launcher: launcher,
        now: { now },
        sleep: { interval in now = now.addingTimeInterval(interval) },
        timeout: 0.05,
        retryInterval: 0.01
    )

    expectTargetNotFrontmost { try controller.activate(target) }

    #expect(now <= Date(timeIntervalSince1970: 0.05))
    #expect(process.executable?.path == "/usr/bin/open")
    #expect(process.arguments == ["/Applications/TextEdit.app"])
    #expect(process.terminateCount == 1)
    #expect(process.forceTerminateCount == 1)
    #expect(process.waitUntilExitCount == 0)
    #expect(!process.isRunning)
    #expect(runtime.setFocusedWindowCount == 0)
    #expect(runtime.raiseCount == 0)
}

@Test func completedOpenChildIsReapedBeforeTerminationStatusIsRead() throws {
    let process = CompletedApplicationProcess()
    let result = try SystemApplicationProcessLauncher(processFactory: { process }).run(
        executable: "/usr/bin/open",
        arguments: ["/Applications/TextEdit.app"],
        timeout: 1
    )

    #expect(result == .exited(0))
    let waitIndex = try #require(process.events.firstIndex(of: "waitUntilExit"))
    let statusIndex = try #require(process.events.firstIndex(of: "terminationStatus"))
    #expect(waitIndex < statusIndex)
    #expect(process.events.filter { $0 == "waitUntilExit" }.count == 1)
}

@Test func activationDoesNotRaiseAfterFocusCrossesDeadline() {
    let target = activationTarget()
    var now = Date(timeIntervalSince1970: 0)
    let runtime = ActivationRuntimeSpy(
        identity: ApplicationLaunchIdentity(
            bundleURL: URL(fileURLWithPath: "/Applications/TextEdit.app"),
            localizedName: "TextEdit"
        ),
        expected: target,
        afterSetFocusedWindow: {
            now = Date(timeIntervalSince1970: 0.05)
        }
    )
    let controller = LaunchServicesApplicationActivationController(
        runtime: runtime,
        launcher: ProcessLauncherSpy(status: 0) { runtime.markVerified() },
        now: { now },
        sleep: { interval in now = now.addingTimeInterval(interval) },
        timeout: 0.05,
        retryInterval: 0.01
    )

    expectTargetNotFrontmost { try controller.activate(target) }

    #expect(runtime.setFocusedWindowCount == 1)
    #expect(runtime.raiseCount == 0)
}

@Test func activationWaitsForFrontmostBeforeMutatingTheAXWindow() throws {
    let target = activationTarget()
    let clock = ActivationTestClock()
    let runtime = PhasedActivationRuntime(
        identity: ApplicationLaunchIdentity(
            bundleURL: URL(fileURLWithPath: "/Applications/TextEdit.app"),
            localizedName: "TextEdit"
        ),
        expected: target,
        frontmostValues: [999, 999, target.pid],
        fallbackFrontmost: target.pid
    )
    let launcher = TimedProcessLauncher(now: clock.now)
    let controller = LaunchServicesApplicationActivationController(
        runtime: runtime,
        launcher: launcher,
        now: clock.now,
        sleep: clock.sleep,
        timeout: 1,
        retryInterval: 0.05
    )

    try controller.activate(target)

    let frontmostTarget = try #require(runtime.events.firstIndex(of: "frontmost:\(target.pid)"))
    let firstFocus = try #require(runtime.events.firstIndex(of: "setFocused"))
    #expect(firstFocus > frontmostTarget)
    #expect(launcher.invocationTimes.count == 1)
}

@Test func swallowedActivationRetriesOnlyAfterTheSettleWindow() throws {
    let target = activationTarget()
    let clock = ActivationTestClock()
    let runtime = PhasedActivationRuntime(
        identity: ApplicationLaunchIdentity(
            bundleURL: URL(fileURLWithPath: "/Applications/TextEdit.app"),
            localizedName: "TextEdit"
        ),
        expected: target,
        fallbackFrontmost: 999
    )
    let launcher = TimedProcessLauncher(now: clock.now) { invocation in
        if invocation == 2 { runtime.fallbackFrontmost = target.pid }
    }
    let controller = LaunchServicesApplicationActivationController(
        runtime: runtime,
        launcher: launcher,
        now: clock.now,
        sleep: clock.sleep,
        timeout: 1,
        retryInterval: 0.05
    )

    try controller.activate(target)

    #expect(launcher.invocationTimes.count == 2)
    #expect(launcher.invocationTimes[1].timeIntervalSince(launcher.invocationTimes[0]) >= 0.299)
}

@Test func exactWindowSettlementDoesNotRelaunchWhileTargetRemainsFrontmost() throws {
    let target = activationTarget()
    let clock = ActivationTestClock()
    let runtime = PhasedActivationRuntime(
        identity: ApplicationLaunchIdentity(
            bundleURL: URL(fileURLWithPath: "/Applications/TextEdit.app"),
            localizedName: "TextEdit"
        ),
        expected: target,
        fallbackFrontmost: target.pid,
        exactWindowValues: [false, true]
    )
    let launcher = TimedProcessLauncher(now: clock.now)
    let controller = LaunchServicesApplicationActivationController(
        runtime: runtime,
        launcher: launcher,
        now: clock.now,
        sleep: clock.sleep,
        timeout: 1,
        retryInterval: 0.05
    )

    try controller.activate(target)

    #expect(launcher.invocationTimes.isEmpty)
    #expect(runtime.setFocusedCount == 2)
    #expect(runtime.raiseCount == 2)
}

@Test func losingFrontmostDuringExactWindowSettlementReturnsToLaunchServices() throws {
    let target = activationTarget()
    let clock = ActivationTestClock()
    let runtime = PhasedActivationRuntime(
        identity: ApplicationLaunchIdentity(
            bundleURL: URL(fileURLWithPath: "/Applications/TextEdit.app"),
            localizedName: "TextEdit"
        ),
        expected: target,
        frontmostValues: [target.pid, target.pid, 999, 999],
        fallbackFrontmost: 999,
        exactWindowValues: [false, true]
    )
    let launcher = TimedProcessLauncher(now: clock.now) { _ in
        runtime.fallbackFrontmost = target.pid
    }
    let controller = LaunchServicesApplicationActivationController(
        runtime: runtime,
        launcher: launcher,
        now: clock.now,
        sleep: clock.sleep,
        timeout: 1,
        retryInterval: 0.05
    )

    try controller.activate(target)

    #expect(launcher.invocationTimes.count == 1)
}

@Test func restoreRetriesOnlyAfterTheSettleWindow() throws {
    let target = activationTarget()
    let clock = ActivationTestClock()
    let runtime = PhasedActivationRuntime(
        identity: ApplicationLaunchIdentity(
            bundleURL: URL(fileURLWithPath: "/Applications/TextEdit.app"),
            localizedName: "TextEdit"
        ),
        expected: target,
        fallbackFrontmost: 999
    )
    let launcher = TimedProcessLauncher(now: clock.now) { invocation in
        if invocation == 2 { runtime.fallbackFrontmost = target.pid }
    }
    let controller = LaunchServicesApplicationActivationController(
        runtime: runtime,
        launcher: launcher,
        now: clock.now,
        sleep: clock.sleep,
        timeout: 1,
        retryInterval: 0.05
    )

    try controller.restore(pid: target.pid)

    #expect(launcher.invocationTimes.count == 2)
    #expect(launcher.invocationTimes[1].timeIntervalSince(launcher.invocationTimes[0]) >= 0.299)
    #expect(runtime.setFocusedCount == 0)
    #expect(runtime.raiseCount == 0)
}

@Test func productionSourcesDoNotCallNSRunningApplicationActivate() throws {
    let testsDirectory = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
    let packageRoot = testsDirectory.deletingLastPathComponent().deletingLastPathComponent()
    let sources = packageRoot.appendingPathComponent("Sources/AstraMacComputerHelper")
    let windows = try String(contentsOf: sources.appendingPathComponent("Windows.swift"), encoding: .utf8)
    let controllers = try String(contentsOf: sources.appendingPathComponent("TargetControllers.swift"), encoding: .utf8)

    let normalized = (windows + controllers).replacingOccurrences(
        of: #"\s+"#,
        with: "",
        options: .regularExpression
    )
    #expect(!normalized.contains("NSRunningApplication.current"))
    #expect(!normalized.contains(".activate(from:"))
}

private func activationTarget() -> WindowTarget {
    WindowTarget(
        appRef: "app",
        windowRef: "window",
        pid: 42,
        windowID: 24,
        bounds: CGRect(x: 100, y: 80, width: 800, height: 600),
        title: "Document",
        axIdentity: 700,
        axElement: AXUIElementCreateApplication(42),
        interactionMode: .foregroundTakeover
    )
}

private final class ProcessLauncherSpy: ApplicationProcessLaunching {
    private(set) var invocations: [ProcessLaunchInvocation] = []
    private let status: Int32
    private let afterLaunch: () -> Void

    init(status: Int32 = 0, afterLaunch: @escaping () -> Void = {}) {
        self.status = status
        self.afterLaunch = afterLaunch
    }

    func run(
        executable: String,
        arguments: [String],
        timeout: TimeInterval
    ) throws -> ApplicationProcessLaunchResult {
        invocations.append(ProcessLaunchInvocation(executable: executable, arguments: arguments))
        afterLaunch()
        return .exited(status)
    }
}

private final class ActivationRuntimeSpy: ApplicationActivationRuntime {
    private let identityValue: ApplicationLaunchIdentity?
    private let expected: WindowTarget
    private let exactFocusedWindowMatchesValue: Bool
    private let frontmostMatchesValue: Bool
    private let setFocusedWindowSucceeds: Bool
    private let popupPointerMatchesValue: Bool
    private let afterSetFocusedWindow: () -> Void
    private var verified = false
    private(set) var unhideCount = 0
    private(set) var setFocusedWindowCount = 0
    private(set) var raiseCount = 0
    private(set) var focusedWindowMatchCount = 0
    private(set) var popupPointerMatchCount = 0
    private(set) var lastPopupPoint: CGPoint?

    init(
        identity: ApplicationLaunchIdentity?,
        expected: WindowTarget,
        exactFocusedWindowMatches: Bool = true,
        frontmostMatches: Bool = true,
        setFocusedWindowSucceeds: Bool = true,
        popupPointerMatches: Bool = false,
        afterSetFocusedWindow: @escaping () -> Void = {}
    ) {
        identityValue = identity
        self.expected = expected
        exactFocusedWindowMatchesValue = exactFocusedWindowMatches
        frontmostMatchesValue = frontmostMatches
        self.setFocusedWindowSucceeds = setFocusedWindowSucceeds
        popupPointerMatchesValue = popupPointerMatches
        self.afterSetFocusedWindow = afterSetFocusedWindow
    }

    func applicationIdentity(pid: pid_t) -> ApplicationLaunchIdentity? { identityValue }
    func unhide(pid: pid_t) { unhideCount += 1 }
    func setFocusedWindow(_ target: WindowTarget) -> Bool {
        setFocusedWindowCount += 1
        afterSetFocusedWindow()
        return setFocusedWindowSucceeds && target.axIdentity == expected.axIdentity
    }
    func raiseWindow(_ target: WindowTarget) -> Bool {
        raiseCount += 1
        return target.axIdentity == expected.axIdentity
    }
    func frontmostPID() -> pid_t? { verified && frontmostMatchesValue ? expected.pid : 999 }
    func focusedWindowIdentity(pid: pid_t) -> CFHashCode? { verified ? expected.axIdentity : nil }
    func focusedWindowMatches(_ target: WindowTarget) -> Bool {
        focusedWindowMatchCount += 1
        return verified && exactFocusedWindowMatchesValue && target.axIdentity == expected.axIdentity
    }
    func popupPointerWindowMatches(_ target: WindowTarget, at point: CGPoint) -> Bool {
        popupPointerMatchCount += 1
        lastPopupPoint = point
        return verified && popupPointerMatchesValue && target.axIdentity == expected.axIdentity
    }

    func markVerified() { verified = true }
}

private enum LaunchProbeError: Error { case failed }

private final class ThrowingProcessLauncherSpy: ApplicationProcessLaunching {
    private(set) var invocationCount = 0

    func run(
        executable: String,
        arguments: [String],
        timeout: TimeInterval
    ) throws -> ApplicationProcessLaunchResult {
        invocationCount += 1
        throw LaunchProbeError.failed
    }
}

private final class StalledApplicationProcess: ApplicationManagedProcess {
    private(set) var executable: URL?
    private(set) var arguments: [String] = []
    private(set) var isRunning = false
    private(set) var terminateCount = 0
    private(set) var forceTerminateCount = 0
    private(set) var waitUntilExitCount = 0
    let terminationStatus: Int32 = 0

    func run(executable: URL, arguments: [String]) throws {
        self.executable = executable
        self.arguments = arguments
        isRunning = true
    }

    func terminate() {
        terminateCount += 1
    }

    func forceTerminate() {
        forceTerminateCount += 1
        isRunning = false
    }

    func waitUntilExit() {
        waitUntilExitCount += 1
    }
}

private final class CompletedApplicationProcess: ApplicationManagedProcess {
    private(set) var events: [String] = []
    var isRunning: Bool {
        events.append("isRunning")
        return false
    }
    var terminationStatus: Int32 {
        events.append("terminationStatus")
        return 0
    }

    func run(executable _: URL, arguments _: [String]) throws { events.append("run") }
    func waitUntilExit() { events.append("waitUntilExit") }
    func terminate() { events.append("terminate") }
    func forceTerminate() { events.append("forceTerminate") }
}

private final class ActivationTestClock {
    private(set) var value = Date(timeIntervalSince1970: 0)

    func now() -> Date { value }
    func sleep(_ interval: TimeInterval) { value = value.addingTimeInterval(interval) }
}

private final class TimedProcessLauncher: ApplicationProcessLaunching {
    private let now: () -> Date
    private let afterLaunch: (Int) -> Void
    private(set) var invocationTimes: [Date] = []

    init(now: @escaping () -> Date, afterLaunch: @escaping (Int) -> Void = { _ in }) {
        self.now = now
        self.afterLaunch = afterLaunch
    }

    func run(
        executable _: String,
        arguments _: [String],
        timeout _: TimeInterval
    ) throws -> ApplicationProcessLaunchResult {
        invocationTimes.append(now())
        afterLaunch(invocationTimes.count)
        return .exited(0)
    }
}

private final class PhasedActivationRuntime: ApplicationActivationRuntime {
    private let identityValue: ApplicationLaunchIdentity?
    private let expected: WindowTarget
    private var frontmostValues: [pid_t?]
    private var exactWindowValues: [Bool]
    var fallbackFrontmost: pid_t?
    private(set) var events: [String] = []
    private(set) var setFocusedCount = 0
    private(set) var raiseCount = 0

    init(
        identity: ApplicationLaunchIdentity?,
        expected: WindowTarget,
        frontmostValues: [pid_t?] = [],
        fallbackFrontmost: pid_t?,
        exactWindowValues: [Bool] = [true]
    ) {
        identityValue = identity
        self.expected = expected
        self.frontmostValues = frontmostValues
        self.fallbackFrontmost = fallbackFrontmost
        self.exactWindowValues = exactWindowValues
    }

    func applicationIdentity(pid _: pid_t) -> ApplicationLaunchIdentity? { identityValue }
    func unhide(pid _: pid_t) { events.append("unhide") }
    func setFocusedWindow(_ target: WindowTarget) -> Bool {
        events.append("setFocused")
        setFocusedCount += 1
        return target.axIdentity == expected.axIdentity
    }
    func raiseWindow(_ target: WindowTarget) -> Bool {
        events.append("raise")
        raiseCount += 1
        return target.axIdentity == expected.axIdentity
    }
    func frontmostPID() -> pid_t? {
        let value = frontmostValues.isEmpty ? fallbackFrontmost : frontmostValues.removeFirst()
        events.append("frontmost:\(value ?? -1)")
        return value
    }
    func focusedWindowMatches(_ target: WindowTarget) -> Bool {
        events.append("exactWindow")
        let value = exactWindowValues.isEmpty ? true : exactWindowValues.removeFirst()
        return value && target.axIdentity == expected.axIdentity
    }
}

private func expiringActivationController(
    runtime: any ApplicationActivationRuntime,
    launcher: any ApplicationProcessLaunching
) -> LaunchServicesApplicationActivationController {
    var now = Date(timeIntervalSince1970: 0)
    return LaunchServicesApplicationActivationController(
        runtime: runtime,
        launcher: launcher,
        now: { now },
        sleep: { interval in now = now.addingTimeInterval(interval) },
        timeout: 0.05,
        retryInterval: 0.05
    )
}

private func expectTargetNotFrontmost(_ operation: () throws -> Void) {
    do {
        try operation()
        Issue.record("expected target_not_frontmost")
    } catch WindowObservationError.targetNotFrontmost {
        // Expected exact failure.
    } catch {
        Issue.record("expected target_not_frontmost, got \(error)")
    }
}
