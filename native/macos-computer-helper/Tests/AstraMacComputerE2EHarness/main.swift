@testable import AstraMacComputerHelperCore
import AppKit
import CoreGraphics
import Darwin
import Foundation

private enum E2EFailure: Error, CustomStringConvertible {
    case assertion(String)
    var description: String { switch self { case let .assertion(message): return message } }
}

private enum E2EExit: Error {
    case notSupported
}

private struct FixtureSelection {
    let appRef: String
    let windowRef: String
    let bundleID: String
    let appVersion: String
}

private struct MatrixScenario {
    let name: String
    let actionClass: DispatchActionClass
    let action: NativeAction
    let effectPrefix: String
}

private final class ForcedActivationFailure: ApplicationActivationControlling {
    func activate(_: WindowTarget) throws { throw WindowObservationError.targetNotFrontmost }
    func restore(pid _: pid_t) throws {}
}

private final class RecordingActivationRuntime: ApplicationActivationRuntime {
    private let system = SystemApplicationActivationRuntime()
    private(set) var trace: [String] = []
    func applicationIdentity(pid: pid_t) -> ApplicationLaunchIdentity? {
        let value = system.applicationIdentity(pid: pid)
        trace.append("identity=\(value != nil)")
        return value
    }
    func unhide(pid: pid_t) { trace.append("unhide=\(pid)"); system.unhide(pid: pid) }
    func setFocusedWindow(_ target: WindowTarget) -> Bool {
        let value = system.setFocusedWindow(target)
        trace.append("set_focused=\(value)")
        return value
    }
    func raiseWindow(_ target: WindowTarget) -> Bool {
        let value = system.raiseWindow(target)
        trace.append("raise=\(value)")
        return value
    }
    func frontmostPID() -> pid_t? {
        let value = system.frontmostPID()
        trace.append("frontmost=\(value ?? -1)")
        return value
    }
    func focusedWindowMatches(_ target: WindowTarget) -> Bool {
        let value = system.focusedWindowMatches(target)
        trace.append("exact_window=\(value)")
        return value
    }
}

private func require(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    guard condition() else { throw E2EFailure.assertion(message) }
}

private func packageRoot() -> URL {
    URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent()
}

private func command(_ arguments: [String], capture: Bool = false) throws -> String {
    let process = Process()
    let output = Pipe()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
    process.arguments = arguments
    if capture { process.standardOutput = output }
    try process.run()
    process.waitUntilExit()
    try require(process.terminationStatus == 0, "command failed: \(arguments.joined(separator: " "))")
    return capture ? String(decoding: output.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self) : ""
}

private func runOpen(arguments: [String]) throws {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/open")
    process.arguments = arguments
    try process.run()
    process.waitUntilExit()
    try require(process.terminationStatus == 0, "/usr/bin/open failed")
}

private func makeFixtureApp(executable: URL, root: URL) throws -> URL {
    let fixtureApp = root.appendingPathComponent("AstraComputerFixture.app")
    let contents = fixtureApp.appendingPathComponent("Contents")
    let macOS = contents.appendingPathComponent("MacOS")
    try FileManager.default.createDirectory(at: macOS, withIntermediateDirectories: true)
    let installed = macOS.appendingPathComponent("AstraComputerFixture")
    try FileManager.default.copyItem(at: executable, to: installed)
    try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: installed.path)
    let plist: [String: Any] = [
        "CFBundleIdentifier": "dev.astra.computer-fixture",
        "CFBundleExecutable": "AstraComputerFixture",
        "CFBundleName": "AstraComputerFixture",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0.0",
        "LSMinimumSystemVersion": "14.0",
    ]
    let data = try PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
    try data.write(to: contents.appendingPathComponent("Info.plist"), options: .atomic)
    _ = try command(["/usr/bin/codesign", "--force", "--sign", "-", "--deep", "--strict", fixtureApp.path])
    _ = try command(["/usr/bin/codesign", "--verify", "--deep", "--strict", fixtureApp.path])
    return fixtureApp
}

private func node(_ value: JSONValue, where predicate: ([String: JSONValue]) -> Bool) -> (String, CGRect)? {
    guard case let .object(fields) = value else { return nil }
    if predicate(fields), case let .string(reference)? = fields["element_ref"], let bounds = rect(fields["bounds"]) {
        return (reference, bounds)
    }
    if case let .array(children)? = fields["children"] {
        for child in children { if let match = node(child, where: predicate) { return match } }
    }
    return nil
}

private func containsValue(_ value: JSONValue, prefix: String) -> Bool {
    guard case let .object(fields) = value else { return false }
    if case let .string(candidate)? = fields["value"], candidate.hasPrefix(prefix) { return true }
    if case let .array(children)? = fields["children"] {
        return children.contains { containsValue($0, prefix: prefix) }
    }
    return false
}

private func visibleAXDescriptions(_ value: JSONValue) -> [String] {
    guard case let .object(fields) = value else { return [] }
    let own = ["role", "title", "label", "value", "identifier"].compactMap { key -> String? in
        if case let .string(candidate)? = fields[key], !candidate.isEmpty { return "\(key)=\(candidate)" }
        return nil
    }.joined(separator: ",")
    let children: [String]
    if case let .array(values)? = fields["children"] {
        children = values.flatMap(visibleAXDescriptions)
    } else {
        children = []
    }
    return (own.isEmpty ? [] : [own]) + children
}

private func firstIncompleteAXNode(pid: pid_t) -> String {
    let app = AXUIElementCreateApplication(pid)
    guard let windows = completeAXElementArray(app, attribute: kAXWindowsAttribute, maximum: 64) else {
        return "application AXWindows incomplete"
    }
    var queue = windows.map { ($0, "AXWindow") }
    var remaining = 128
    while !queue.isEmpty, remaining > 0 {
        remaining -= 1
        let (candidate, path) = queue.removeFirst()
        let role = AXNodeReader.stringAttribute(candidate, kAXRoleAttribute)
        let subrole = AXNodeReader.stringAttribute(candidate, kAXSubroleAttribute)
        if role.status != .complete || role.value == nil || subrole.status != .complete {
            return "path=\(path),role=\(String(describing: role.value))/\(role.status),subrole=\(String(describing: subrole.value))/\(subrole.status)"
        }
        guard let children = completeAXElementArray(
            candidate,
            attribute: kAXChildrenAttribute,
            maximum: remaining,
            unsupportedMeansEmpty: knownAXLeafRole(role: role, subrole: subrole)
        ) else {
            let title = AXNodeReader.stringAttribute(candidate, kAXTitleAttribute).value ?? ""
            let identifier = AXNodeReader.stringAttribute(candidate, kAXIdentifierAttribute).value ?? ""
            var count: CFIndex = 0
            let countError = AXUIElementGetAttributeValueCount(
                candidate,
                kAXChildrenAttribute as CFString,
                &count
            )
            return "children incomplete at path=\(path)/\(role.value ?? "nil"),subrole=\(subrole.value ?? "nil")/\(subrole.status),child_error=\(countError.rawValue),child_count=\(count),title=\(title),identifier=\(identifier)"
        }
        queue.append(contentsOf: children.enumerated().map { index, child in
            (child, "\(path)/\(role.value ?? "nil")[\(index)]")
        })
    }
    return remaining == 0 ? "AX traversal exceeded diagnostic bound" : "none"
}

private func axWindowEnumerationRoles(pid: pid_t) -> String {
    let app = AXUIElementCreateApplication(pid)
    let complete = completeAXElementArray(app, attribute: kAXWindowsAttribute, maximum: 64) ?? []
    let bounded = AXNodeReader.elementArrayAttribute(app, kAXWindowsAttribute, remaining: 64)
    var copiedValue: CFTypeRef?
    let copiedError = AXUIElementCopyAttributeValue(app, kAXWindowsAttribute as CFString, &copiedValue)
    let copied = copiedValue as? [AXUIElement] ?? []
    var focusedValue: CFTypeRef?
    let focusedError = AXUIElementCopyAttributeValue(app, kAXFocusedWindowAttribute as CFString, &focusedValue)
    let focused = decodeAXElement(focusedValue)
    func roles(_ elements: [AXUIElement]) -> [String] {
        elements.map { AXNodeReader.stringAttribute($0, kAXRoleAttribute).value ?? "nil" }
    }
    let completeEqualsApp = complete.map { CFEqual($0, app) }
    let copiedEqualsApp = copied.map { CFEqual($0, app) }
    return "complete=\(roles(complete))/equals_app=\(completeEqualsApp),bounded=\(roles(bounded)),copied_error=\(copiedError.rawValue),copied=\(roles(copied))/equals_app=\(copiedEqualsApp),focused_error=\(focusedError.rawValue),focused_role=\(focused.map { roles([$0]) } ?? []),focused_equals_app=\(focused.map { CFEqual($0, app) } ?? false)"
}

private func rect(_ value: JSONValue?) -> CGRect? {
    guard case let .object(fields)? = value,
          case let .number(x)? = fields["x"], case let .number(y)? = fields["y"],
          case let .number(width)? = fields["width"], case let .number(height)? = fields["height"]
    else { return nil }
    return CGRect(x: x, y: y, width: width, height: height)
}

private func snapshotFields(_ snapshot: JSONValue) throws -> (String, JSONValue) {
    guard case let .object(root) = snapshot, case let .string(id)? = root["snapshot_id"],
          case let .object(payload)? = root["payload"], let tree = payload["ax_tree"]
    else { throw E2EFailure.assertion("snapshot schema missing id/tree") }
    return (id, tree)
}

private func applicationRefs(_ catalog: JSONValue, bundleIdentifier: String, windowTitle: String? = nil) -> FixtureSelection? {
    guard case let .object(root) = catalog, case let .array(apps)? = root["apps"] else { return nil }
    for app in apps {
        guard case let .object(fields) = app,
              case let .string(bundleID)? = fields["bundle_id"], bundleID == bundleIdentifier,
              case let .string(version)? = fields["app_version"], case let .string(appRef)? = fields["app_ref"],
              case let .array(windows)? = fields["windows"] else { continue }
        for window in windows {
            guard case let .object(values) = window, case let .string(title)? = values["title"],
                  windowTitle == nil || title == windowTitle,
                  case let .string(windowRef)? = values["window_ref"]
            else { continue }
            return FixtureSelection(appRef: appRef, windowRef: windowRef, bundleID: bundleID, appVersion: version)
        }
    }
    return nil
}

private func fixtureRefs(_ catalog: JSONValue) -> FixtureSelection? {
    applicationRefs(catalog, bundleIdentifier: "dev.astra.computer-fixture", windowTitle: "Astra Computer Fixture")
}

private func cursorLocation() throws -> CGPoint {
    guard let event = CGEvent(source: nil) else { throw E2EFailure.assertion("cursor location unavailable") }
    return event.location
}

private func jsonString(_ value: String) -> String {
    let data = try! JSONSerialization.data(withJSONObject: [value])
    let encoded = String(decoding: data, as: UTF8.self)
    return String(encoded.dropFirst().dropLast())
}

private func emitRecord(
    selection: FixtureSelection, scenario: MatrixScenario,
    cursorBefore: CGPoint, cursorAfter: CGPoint,
    sentinelPID: pid_t, targetPID: pid_t,
    frontmostBefore: pid_t?, frontmostDuring: pid_t?, frontmostAfter: pid_t?,
    receivingWindow: String, exactWindow: Bool, freshEffect: Bool,
    outsideSideEffects: Bool, heldInputClean: Bool
) {
    let record = """
    {"bundle_id":\(jsonString(selection.bundleID)),"app_version":\(jsonString(selection.appVersion)),"action":\(jsonString(scenario.actionClass.rawValue)),"scenario":\(jsonString(scenario.name)),"cursor_before":[\(cursorBefore.x),\(cursorBefore.y)],"cursor_after":[\(cursorAfter.x),\(cursorAfter.y)],"exact_window":\(exactWindow),"fresh_effect":\(freshEffect),"outside_side_effects":\(outsideSideEffects),"held_input_clean":\(heldInputClean),"evidence_source":"real","target_pid":\(targetPID),"sentinel_pid":\(sentinelPID),"frontmost_before":\(frontmostBefore ?? -1),"frontmost_during":\(frontmostDuring ?? -1),"frontmost_after":\(frontmostAfter ?? -1),"receiving_window":\(jsonString(receivingWindow))}
    """
    print("ASTRA_COOPERATIVE_MATRIX_RECORD \(record)")
}

private var e2eStage = "startup"
private let activationRuntime = RecordingActivationRuntime()
private var pidTrace = ""
let runExperimentalPIDPointerMatrix = ProcessInfo.processInfo.environment[
    "ASTRA_MACOS_COMPUTER_PID_POINTER_EXPERIMENT"
] == "1"
do {
    try require(ProcessInfo.processInfo.environment["ASTRA_MACOS_COMPUTER_E2E"] == "1", "explicit E2E authorization is missing")
    let experimentalSetupPID = runExperimentalPIDPointerMatrix ? try confirmFixtureSetup() : nil
    let package = packageRoot()
    _ = try command(["swift", "build", "--package-path", package.path, "--product", "AstraComputerFixture"])
    if runExperimentalPIDPointerMatrix {
        _ = try command(["swift", "build", "--package-path", package.path, "--product", "AstraVirtualCursorSidecar"])
    }
    let bin = try command(["swift", "build", "--package-path", package.path, "--show-bin-path"], capture: true)
        .trimmingCharacters(in: .whitespacesAndNewlines)

    let artifactRoot = FileManager.default.temporaryDirectory.appendingPathComponent("astra-action-e2e-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: artifactRoot, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
    defer { try? FileManager.default.removeItem(at: artifactRoot) }
    let fixtureApp = try makeFixtureApp(
        executable: URL(fileURLWithPath: bin).appendingPathComponent("AstraComputerFixture"), root: artifactRoot
    )
    let sentinelPID = NSWorkspace.shared.frontmostApplication?.processIdentifier
    try require(sentinelPID != nil, "frontmost sentinel is unavailable")
    if let experimentalSetupPID, sentinelPID != experimentalSetupPID || liveFrontmostPID() != experimentalSetupPID { throw FixtureConsentNotGiven() }
    try runOpen(arguments: [fixtureApp.path])

    var fixtureApplication: NSRunningApplication?
    for _ in 0..<100 where fixtureApplication == nil {
        fixtureApplication = NSRunningApplication.runningApplications(withBundleIdentifier: "dev.astra.computer-fixture").first
        if fixtureApplication == nil { usleep(100_000) }
    }
    guard let fixtureApplication, let sentinelPID else { throw E2EFailure.assertion("fixture did not launch") }
    let targetPID = fixtureApplication.processIdentifier
    let fixtureExecutable = fixtureApp.appendingPathComponent("Contents/MacOS/AstraComputerFixture").standardizedFileURL
    let fixtureBundle = Bundle(url: fixtureApp)
    try require(fixtureApplication.bundleURL?.standardizedFileURL == fixtureApp.standardizedFileURL, "fixture runtime bundle URL is not the exact temporary .app")
    try require(fixtureApplication.bundleIdentifier == "dev.astra.computer-fixture", "fixture runtime bundle identifier changed")
    try require(fixtureBundle?.bundleIdentifier == "dev.astra.computer-fixture", "fixture Info.plist bundle identifier changed")
    try require(fixtureBundle?.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String == "1.0.0", "fixture Info.plist version changed")
    try require(fixtureApplication.activationPolicy == .regular, "fixture is not a regular foreground application")
    try require(fixtureApplication.executableURL?.standardizedFileURL == fixtureExecutable, "fixture NSRunningApplication executable URL changed")
    var fixtureAXPID = pid_t()
    try require(
        AXUIElementGetPid(AXUIElementCreateApplication(targetPID), &fixtureAXPID) == .success && fixtureAXPID == targetPID,
        "fixture AX application PID differs from NSRunningApplication PID"
    )
    pidTrace = "sentinel=\(sentinelPID),target=\(targetPID),bundle=\(fixtureApp.path),open=/usr/bin/open \(fixtureApp.path),policy=regular"
    defer {
        _ = fixtureApplication.terminate()
        if !fixtureApplication.isTerminated { _ = Darwin.kill(targetPID, SIGTERM) }
    }

    let activation = LaunchServicesApplicationActivationController(runtime: activationRuntime)
    e2eStage = "restore-sentinel-after-launch"
    try activation.restore(pid: sentinelPID)
    let initialInstances = NSRunningApplication.runningApplications(withBundleIdentifier: "dev.astra.computer-fixture").map(\.processIdentifier)
    try runOpen(arguments: [fixtureApp.path])
    usleep(200_000)
    let reusedInstances = NSRunningApplication.runningApplications(withBundleIdentifier: "dev.astra.computer-fixture").map(\.processIdentifier)
    try require(initialInstances == reusedInstances && reusedInstances == [targetPID], "/usr/bin/open did not reuse the existing fixture instance")

    let descriptor = Darwin.open(artifactRoot.path, O_RDONLY | O_DIRECTORY | O_CLOEXEC)
    try require(descriptor >= 0, "could not open E2E artifact directory")
    defer { Darwin.close(descriptor) }
    setenv("ASTRA_COMPUTER_ARTIFACT_DIR_FD", String(descriptor), 1)
    defer { unsetenv("ASTRA_COMPUTER_ARTIFACT_DIR_FD") }

    let cells = [DispatchActionClass.click, .doubleClick, .scroll, .drag].map {
        PIDInputCompatibilityCell(bundleIdentifier: "dev.astra.computer-fixture", version: "1.0.0", action: $0)
    }
    let registry = PIDInputCompatibilityRegistry(cells: cells)
    let observer: SystemWindowObserver
    if runExperimentalPIDPointerMatrix {
        let binaryDirectory = URL(fileURLWithPath: bin, isDirectory: true).standardizedFileURL
        let sidecarURL = binaryDirectory.appendingPathComponent("AstraVirtualCursorSidecar")
        try require(
            FileManager.default.isExecutableFile(atPath: sidecarURL.path),
            "virtual cursor sidecar is missing or non-executable: \(sidecarURL.path)"
        )
        let helperURL = binaryDirectory.appendingPathComponent("AstraMacComputerE2EHarness")
        let cursor = RestartableVirtualCursorPresenter {
            try VirtualCursorClient.launch(helperExecutableURL: helperURL)
        }
        observer = SystemWindowObserver(
            activation: activation, virtualCursor: cursor,
            pidCompatibility: registry,
            pidPointerCapability: .experimentalAvailable
        )
    } else {
        observer = SystemWindowObserver(
            activation: activation,
            pidCompatibility: registry,
            pidPointerCapability: .unavailable
        )
    }
    e2eStage = "discover-and-select-fixture"
    var selected: FixtureSelection?
    var selectedTarget: WindowTarget?
    var catalogMisses = 0
    var selectionSerializationFailures = 0
    for _ in 0..<50 where selectedTarget == nil {
        do {
            guard let candidate = try fixtureRefs(observer.apps()) else {
                catalogMisses += 1
                usleep(100_000)
                continue
            }
            selectedTarget = try observer.select(appRef: candidate.appRef, windowRef: candidate.windowRef)
            selected = candidate
        } catch WindowObservationError.axSerializationFailed {
            selectionSerializationFailures += 1
            usleep(100_000)
        }
    }
    guard let selected, let target = selectedTarget else {
        throw E2EFailure.assertion(
            "fixture window was not discovered and selected; catalog_misses=\(catalogMisses),select_ax_serialization_failures=\(selectionSerializationFailures),window_roles=\(axWindowEnumerationRoles(pid: targetPID)),first_incomplete_ax=\(firstIncompleteAXNode(pid: targetPID))"
        )
    }
    try require(selected.appVersion == "1.0.0", "fixture version was not copied from its exact bundle")
    e2eStage = "restore-sentinel-after-selection"
    try activation.restore(pid: sentinelPID)

    var captureNumber = 0
    func fresh(_ owner: SystemWindowObserver = observer, selection: FixtureSelection = selected, name: String? = nil) throws -> (String, JSONValue) {
        captureNumber += 1
        return try snapshotFields(owner.snapshot(
            appRef: selection.appRef, windowRef: selection.windowRef, scope: "target_window",
            artifactName: name ?? "e2e-\(captureNumber).png"
        ))
    }

    e2eStage = "initial-fixture-snapshot"
    var current = try fresh()
    func fixtureNode(label: String) throws -> (String, CGRect) {
        guard let match = node(current.1, where: {
            if case let .string(value)? = $0["label"] { return value == label }
            if case let .string(value)? = $0["title"] { return value == label }
            return false
        }) else {
            throw E2EFailure.assertion(
                "missing fixture target \(label); visible=\(visibleAXDescriptions(current.1).prefix(100))"
            )
        }
        return match
    }

    if runExperimentalPIDPointerMatrix {
        e2eStage = "forced-activation-regression"
        defer { observer.invalidateSnapshots() }
        let failing = SystemWindowObserver(
            activation: ForcedActivationFailure(),
            pidCompatibility: registry,
            pidPointerCapability: .experimentalAvailable
        )
        guard let failingSelection = try fixtureRefs(failing.apps()) else {
            throw E2EFailure.assertion("forced activation fixture target was not discovered")
        }
        let failingTarget = try failing.select(appRef: failingSelection.appRef, windowRef: failingSelection.windowRef)
        let failingSnapshot = try fresh(failing, selection: failingSelection, name: "forced-activation.png")
        let failingAction = [NativeAction.click(x: 10, y: 10)]
        guard let failingPlan = failing.planActions(
            snapshotID: failingSnapshot.0, interactionMode: .foregroundTakeover, actions: failingAction
        ).summary else { throw E2EFailure.assertion("forced activation plan failed") }
        do {
            let consent = try approveFixtureBorrow(caseName: "forced_activation_regression", action: "Attempt activation for a single fixture click; the injected activator must refuse", target: failingTarget, planRef: failingPlan.planRef, expectedPID: sentinelPID)
            _ = try failing.takeoverBegin(snapshotID: failingSnapshot.0, planRef: failingPlan.planRef, focusConsent: consent, loanDeadline: nil)
            throw E2EFailure.assertion("forced activation unexpectedly succeeded")
        } catch TakeoverError.targetNotFrontmost {
            try require(NSWorkspace.shared.frontmostApplication?.processIdentifier == sentinelPID, "failed activation changed the sentinel")
        }

        let button = try fixtureNode(label: "Fixture button").1
        let doubleClick = try fixtureNode(label: "Double click surface").1
        let scroll = try fixtureNode(label: "Scroll area").1
        let drag = try fixtureNode(label: "Drag surface").1
        let canvas = try fixtureNode(label: "Custom canvas").1
        let scenarios = [
            MatrixScenario(name: "ordinary_button", actionClass: .click, action: .click(x: button.midX, y: button.midY), effectPrefix: "astra.click_count:1"),
            MatrixScenario(name: "double_click_surface", actionClass: .doubleClick, action: .doubleClick(x: doubleClick.midX, y: doubleClick.midY), effectPrefix: "astra.double_click_count:1"),
            MatrixScenario(name: "scroll_surface", actionClass: .scroll, action: .scroll(deltaY: -30, x: scroll.midX, y: scroll.midY), effectPrefix: "astra.scroll_count:1"),
            MatrixScenario(name: "drag_surface", actionClass: .drag, action: .drag(x: drag.minX + 5, y: drag.midY, endX: drag.maxX - 5, endY: drag.midY, durationMS: 100), effectPrefix: "astra.drag_count:1"),
            MatrixScenario(name: "custom_canvas", actionClass: .click, action: .click(x: canvas.midX, y: canvas.midY), effectPrefix: "astra.canvas_count:1"),
        ]
        var matrixAccepted = true

        for scenario in scenarios {
            e2eStage = "\(scenario.name)-check-sentinel"
            guard liveFrontmostPID() == sentinelPID else { throw FixtureConsentNotGiven() }
            let actions = [scenario.action]
            let background = observer.planActions(snapshotID: current.0, interactionMode: .background, actions: actions)
            try require(background.summary?.requiresTakeover == true, "pointer action did not require takeover")
            let foreground = observer.planActions(snapshotID: current.0, interactionMode: .foregroundTakeover, actions: actions)
            guard let plan = foreground.summary else { throw E2EFailure.assertion("foreground plan failed") }
            let consent = try approveFixtureBorrow(caseName: scenario.name, action: scenario.actionClass.rawValue, target: target, planRef: plan.planRef, expectedPID: sentinelPID)
            let cursorBefore = try cursorLocation()
            let frontmostBefore = liveFrontmostPID()
            e2eStage = "\(scenario.name)-takeover-begin"
            let takeoverRef: String
            do {
                takeoverRef = try observer.takeoverBegin(snapshotID: current.0, planRef: plan.planRef, focusConsent: consent, loanDeadline: nil)
            } catch TakeoverError.targetNotFrontmost {
                let frontmostAfter = NSWorkspace.shared.frontmostApplication?.processIdentifier
                let frontmostDuring = frontmostAfter
                let cursorAfter = try cursorLocation()
                let heldInputClean = !CGEventSource.buttonState(.combinedSessionState, button: .left)
                let outsideSideEffects = frontmostAfter != sentinelPID || fixtureApplication.isTerminated
                let exactWindow = false
                let freshEffect = false
                emitRecord(
                    selection: selected, scenario: scenario,
                    cursorBefore: cursorBefore, cursorAfter: cursorAfter,
                    sentinelPID: sentinelPID, targetPID: targetPID,
                    frontmostBefore: frontmostBefore, frontmostDuring: frontmostDuring,
                    frontmostAfter: frontmostAfter, receivingWindow: String(target.windowID),
                    exactWindow: exactWindow, freshEffect: freshEffect,
                    outsideSideEffects: outsideSideEffects,
                    heldInputClean: heldInputClean
                )
                let accepted = cursorBefore == cursorAfter
                    && frontmostBefore == sentinelPID
                    && frontmostDuring == targetPID
                    && frontmostAfter == sentinelPID
                    && exactWindow
                    && freshEffect
                    && !outsideSideEffects
                    && heldInputClean
                    && !String(target.windowID).isEmpty
                matrixAccepted = matrixAccepted && accepted
                try require(cursorBefore == cursorAfter, "real cursor moved for \(scenario.name)")
                try require(heldInputClean, "held input cleanup failed for \(scenario.name)")
                current = try fresh()
                continue
            }
            let frontmostDuring = NSWorkspace.shared.frontmostApplication?.processIdentifier
            let foregroundTarget = WindowTarget(
                appRef: target.appRef, windowRef: target.windowRef, pid: target.pid,
                windowID: target.windowID, bounds: target.bounds, title: target.title,
                axIdentity: target.axIdentity, axElement: target.axElement, interactionMode: .foregroundTakeover
            )
            let exactWindow = SystemApplicationActivationRuntime().focusedWindowMatches(foregroundTarget)
            e2eStage = "\(scenario.name)-act"
            let actionResult = observer.cooperativeAct(
                snapshotID: current.0, interactionMode: .foregroundTakeover,
                planRef: plan.planRef, takeoverRef: takeoverRef, actions: actions
            )
            e2eStage = "\(scenario.name)-fresh-evidence"
            let afterAction = try fresh()
            let freshEffect = actionResult.error == nil && containsValue(afterAction.1, prefix: scenario.effectPrefix)
            e2eStage = "\(scenario.name)-takeover-end"
            let outcome = try observer.takeoverEnd(takeoverRef: takeoverRef)
            let frontmostAfter = NSWorkspace.shared.frontmostApplication?.processIdentifier
            let cursorAfter = try cursorLocation()
            let outsideSideEffects = outcome.restoration != .restored || frontmostAfter != sentinelPID || fixtureApplication.isTerminated
            let heldInputClean = !CGEventSource.buttonState(.combinedSessionState, button: .left)
            emitRecord(
                selection: selected, scenario: scenario, cursorBefore: cursorBefore, cursorAfter: cursorAfter,
                sentinelPID: sentinelPID, targetPID: targetPID, frontmostBefore: frontmostBefore,
                frontmostDuring: frontmostDuring, frontmostAfter: frontmostAfter,
                receivingWindow: String(target.windowID), exactWindow: exactWindow, freshEffect: freshEffect,
                outsideSideEffects: outsideSideEffects, heldInputClean: heldInputClean
            )
            let accepted = cursorBefore == cursorAfter
                && frontmostBefore == sentinelPID
                && frontmostDuring == targetPID
                && frontmostAfter == sentinelPID
                && exactWindow
                && freshEffect
                && !outsideSideEffects
                && heldInputClean
                && !String(target.windowID).isEmpty
            matrixAccepted = matrixAccepted && accepted
            try require(cursorBefore == cursorAfter, "real cursor moved for \(scenario.name)")
            try require(heldInputClean, "held input cleanup failed for \(scenario.name)")
            current = afterAction
        }
        if !matrixAccepted {
            print("AstraMacComputerE2EHarness: NOT_SUPPORTED experimental PID pointer matrix")
            throw E2EExit.notSupported
        }
        print("AstraMacComputerE2EHarness: EXPERIMENTAL_PASS PID pointer matrix")
    } else {
        let (buttonRef, buttonBounds) = try fixtureNode(label: "Fixture button")
        e2eStage = "release-ax-click"
        try activation.restore(pid: sentinelPID)
        let cursorBefore = try cursorLocation()
        let frontmostBefore = NSWorkspace.shared.frontmostApplication?.processIdentifier
        try require(frontmostBefore == sentinelPID, "sentinel changed before release AX click")
        let axActions = [NativeAction.click(elementRef: buttonRef)]
        let axPlanResult = observer.planActions(
            snapshotID: current.0, interactionMode: .background, actions: axActions
        )
        try require(axPlanResult.error == nil, "release AX click planning failed")
        guard let axPlan = axPlanResult.summary else { throw E2EFailure.assertion("release AX click plan missing") }
        try require(!axPlan.requiresTakeover, "release AX click unexpectedly required takeover")
        let axResult = observer.cooperativeAct(
            snapshotID: current.0, interactionMode: .background,
            planRef: axPlan.planRef, takeoverRef: nil, actions: axActions
        )
        try require(axResult.error == nil, "release AX click execution failed")
        current = try fresh()
        try require(containsValue(current.1, prefix: "astra.click_count:1"), "release AX click effect missing")
        try require(NSWorkspace.shared.frontmostApplication?.processIdentifier == sentinelPID, "release AX click changed frontmost application")
        let cursorAfterAXClick = try cursorLocation()
        try require(cursorAfterAXClick == cursorBefore, "release AX click moved physical cursor")

        e2eStage = "release-pointer-fail-closed"
        let cursorBeforePointerPlanning = try cursorLocation()
        let frontmostBeforePointerPlanning = NSWorkspace.shared.frontmostApplication?.processIdentifier
        try require(frontmostBeforePointerPlanning == sentinelPID, "sentinel changed before pointer planning")
        for mode in [InteractionMode.background, .foregroundTakeover] {
            let rejected = observer.planActions(
                snapshotID: current.0,
                interactionMode: mode,
                actions: [.click(x: buttonBounds.midX, y: buttonBounds.midY)]
            )
            try require(rejected.summary == nil, "unsupported pointer plan leaked a plan reference")
            try require(
                rejected.error == .cooperative(.backgroundActionUnsupported),
                "unsupported pointer plan did not fail closed"
            )
        }
        try require(
            NSWorkspace.shared.frontmostApplication?.processIdentifier == sentinelPID,
            "pointer planning changed frontmost application"
        )
        let cursorAfterPointerPlanning = try cursorLocation()
        try require(cursorAfterPointerPlanning == cursorBeforePointerPlanning, "pointer planning moved physical cursor")
        observer.invalidateSnapshots()
        print("AstraMacComputerE2EHarness: PASS AX-first/fail-closed")
    }
} catch is FixtureConsentNotGiven {
    print("AstraMacComputerE2EHarness: NOT_RUN remaining experimental cases (current interactive consent required)")
    exit(77)
} catch E2EExit.notSupported {
    exit(78)
} catch {
    FileHandle.standardError.write(Data("AstraMacComputerE2EHarness failed at \(e2eStage): \(error); \(pidTrace); activation=\(activationRuntime.trace.suffix(30))\n".utf8))
    exit(1)
}
