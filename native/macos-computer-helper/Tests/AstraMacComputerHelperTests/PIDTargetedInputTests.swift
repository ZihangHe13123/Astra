@testable import AstraMacComputerHelperCore
import AppKit
import CoreGraphics
import Darwin
import Foundation
import Testing

@Test func productionPIDPathUsesOnlyPIDDeliveryAndNeverWarpsTheCursor() throws {
    let packageRoot = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
    let pidSource = try String(contentsOf: packageRoot.appendingPathComponent("Sources/AstraMacComputerHelper/PIDTargetedInput.swift"), encoding: .utf8)
    let activitySource = try String(contentsOf: packageRoot.appendingPathComponent("Sources/AstraMacComputerHelper/UserActivityArbiter.swift"), encoding: .utf8)

    #expect(pidSource.contains("postToPid"))
    #expect(!pidSource.contains("CGWarpMouseCursorPosition"))
    #expect(!pidSource.contains(".post(tap:"))
    #expect(activitySource.contains("options: .listenOnly"))
}

@Test func compatibilityRegistryIsExactAndDefaultDeny() {
    let identity = PIDTargetApplication(bundleIdentifier: "com.example.Editor", version: "1.2.3")
    let empty = PIDInputCompatibilityRegistry()
    #expect(!empty.allows(application: identity, action: .click))

    let registry = PIDInputCompatibilityRegistry(cells: [
        .init(bundleIdentifier: identity.bundleIdentifier, version: identity.version, action: .click),
    ])
    #expect(registry.allows(application: identity, action: .click))
    #expect(!registry.allows(application: .init(bundleIdentifier: identity.bundleIdentifier, version: "1.2.4"), action: .click))
    #expect(!registry.allows(application: .init(bundleIdentifier: "com.example.Other", version: identity.version), action: .click))
    #expect(!registry.allows(application: identity, action: .scroll))
}

@Test func unavailablePIDPointerCapabilityOverridesExactRegistryCell() {
    let application = PIDTargetApplication(bundleIdentifier: "dev.astra.fixture", version: "1.0.0")
    let registry = PIDInputCompatibilityRegistry(cells: [
        PIDInputCompatibilityCell(bundleIdentifier: application.bundleIdentifier, version: application.version, action: .click),
    ])
    let policy = PIDPointerPlanningPolicy(capability: .unavailable, registry: registry)

    #expect(!policy.allows(application: application, actions: [.click]))
}

@Test func availablePIDPointerCapabilityRequiresExactIdentityAndEveryActionCell() {
    let application = PIDTargetApplication(bundleIdentifier: "dev.astra.fixture", version: "1.0.0")
    let registry = PIDInputCompatibilityRegistry(cells: [
        PIDInputCompatibilityCell(bundleIdentifier: application.bundleIdentifier, version: application.version, action: .click),
        PIDInputCompatibilityCell(bundleIdentifier: application.bundleIdentifier, version: application.version, action: .scroll),
    ])
    let policy = PIDPointerPlanningPolicy(capability: .experimentalAvailable, registry: registry)

    #expect(policy.allows(application: application, actions: [.click, .scroll]))
    #expect(!policy.allows(application: nil, actions: [.click]))
    #expect(!policy.allows(application: application, actions: [.drag]))
    #expect(!policy.allows(
        application: PIDTargetApplication(bundleIdentifier: application.bundleIdentifier, version: "1.0.1"),
        actions: [.click]
    ))
}

@Test func AXOnlyPlanNeedsNoPIDPointerCapability() {
    let policy = PIDPointerPlanningPolicy(capability: .unavailable, registry: PIDInputCompatibilityRegistry())
    #expect(policy.allows(application: nil, actions: []))
}

@Test func compatibilityRegistryLoadsStrictInjectedBytesAndFailsWhollyClosed() {
    let application = PIDTargetApplication(bundleIdentifier: "com.example.editor", version: "1.2.3")
    let empty = PIDInputCompatibilityRegistry(bytes: Data(#"{"schema_version":2,"applications":[]}"#.utf8))
    #expect(!empty.allows(application: application, action: .scroll))

    let positive = PIDInputCompatibilityRegistry(bytes: Data(#"{"schema_version":2,"applications":[{"bundle_id":"com.example.editor","app_version":"1.2.3","capabilities":[{"backend":"pid_pointer","enabled_actions":["scroll"]}]}]}"#.utf8))
    #expect(positive.allows(application: application, action: .scroll))
    #expect(!positive.allows(application: application, action: .click))

    let invalidPayloads: [Data?] = [
        nil,
        Data(#"{"schema_version":2,"applications":}"#.utf8),
        Data(#"{"schema_version":2,"applications":[],"extra":true}"#.utf8),
        Data(#"{"schema_version":2,"applications":[{"bundle_id":"com.example.editor","app_version":"1.2.3","capabilities":[{"backend":"pid_pointer","enabled_actions":["scroll"],"extra":true}]}]}"#.utf8),
        Data(#"{"schema_version":1,"applications":[]}"#.utf8),
        Data(#"{"schema_version":2,"applications":[{"bundle_id":"com.example.editor","app_version":"1.2.3","capabilities":[{"backend":"pid_pointer","enabled_actions":["scroll"]}]},{"bundle_id":"com.example.editor","app_version":"1.2.3","capabilities":[{"backend":"pid_pointer","enabled_actions":["click"]}]}]}"#.utf8),
        Data(#"{"schema_version":2,"applications":[{"bundle_id":"com.example.editor","app_version":"1.2.3","capabilities":[{"backend":"pid_pointer","enabled_actions":["scroll","scroll"]}]}]}"#.utf8),
        Data(#"{"schema_version":2,"applications":[{"bundle_id":"com.example.editor","app_version":"1.2.3","capabilities":[{"backend":"pid_pointer","enabled_actions":["keypress"]}]}]}"#.utf8),
        Data("{\"schema_version\":2,\"applications\":[{\"bundle_id\":\"com.\(String(repeating: "a", count: 256))\",\"app_version\":\"1.2.3\",\"capabilities\":[{\"backend\":\"pid_pointer\",\"enabled_actions\":[\"scroll\"]}]}]}".utf8),
        Data("{\"schema_version\":2,\"applications\":[{\"bundle_id\":\"com.example.editor\",\"app_version\":\"1.\(String(repeating: "0", count: 64))\",\"capabilities\":[{\"backend\":\"pid_pointer\",\"enabled_actions\":[\"scroll\"]}]}]}".utf8),
        Data(("{\"schema_version\":2,\"applications\":[" + Array(repeating: "{\"bundle_id\":\"com.example.editor\",\"app_version\":\"1.2.3\",\"capabilities\":[{\"backend\":\"pid_pointer\",\"enabled_actions\":[\"scroll\"]}]}", count: 129).joined(separator: ",") + "]}").utf8),
    ]
    for payload in invalidPayloads {
        let rejected = PIDInputCompatibilityRegistry(bytes: payload)
        #expect(!rejected.allows(application: application, action: .scroll))
        #expect(!rejected.allows(application: application, action: .click))
    }
}

@Test func systemPIDPosterCarriesMarkerLocationAndExactPID() throws {
    var receivedPID: pid_t?
    var receivedMarker: UInt64?
    var receivedLocation: CGPoint?
    let poster = CGPIDTargetedInputPoster(
        preflightAccess: { true },
        deliver: { event, pid in
            receivedPID = pid
            receivedMarker = UInt64(bitPattern: event.getIntegerValueField(.eventSourceUserData))
            receivedLocation = event.location
        }
    )
    let marker = UInt64.max - 7

    #expect(poster.preflight(targetPID: 777, marker: marker))
    try poster.post(.scroll(point: CGPoint(x: 30, y: 40), deltaX: 1, deltaY: -2), to: 777, marker: marker)

    #expect(receivedPID == 777)
    #expect(receivedMarker == marker)
    #expect(receivedLocation == CGPoint(x: 30, y: 40))
}

@Test func systemPIDPosterBuildsUnicodeAndVirtualKeyboardEventsForOnlyTheExactPID() throws {
    var delivered: [(event: CGEvent, pid: pid_t)] = []
    let poster = CGPIDTargetedInputPoster(
        preflightAccess: { true },
        deliver: { event, pid in delivered.append((event, pid)) }
    )
    let marker: UInt64 = 0x1234_5678
    let unicode = Array("🙂".utf16)

    try poster.post(.unicodeKeyDown(unicode), to: 777, marker: marker)
    try poster.post(.virtualKeyUp(9, .maskCommand), to: 777, marker: marker)

    #expect(delivered.count == 2)
    #expect(delivered.allSatisfy { $0.pid == 777 })
    #expect(delivered.allSatisfy {
        UInt64(bitPattern: $0.event.getIntegerValueField(.eventSourceUserData)) == marker
    })
    #expect(delivered[0].event.type == .keyDown)
    var actualLength = 0
    var units = [UniChar](repeating: 0, count: unicode.count)
    delivered[0].event.keyboardGetUnicodeString(
        maxStringLength: units.count,
        actualStringLength: &actualLength,
        unicodeString: &units
    )
    #expect(Array(units.prefix(actualLength)) == unicode)
    #expect(delivered[1].event.type == .keyUp)
    #expect(delivered[1].event.getIntegerValueField(.keyboardEventKeycode) == 9)
    #expect(delivered[1].event.flags.contains(.maskCommand))
    try poster.post(.unicodeKeyDown(unicode), to: 777, marker: marker)
    try poster.post(.unicodeKeyUp(unicode), to: 777, marker: marker)
    #expect(delivered[2].event.flags.isEmpty)
    #expect(delivered[3].event.flags.isEmpty)
}

@Test func sharedPIDPointerGuardDoesNotBindKeyboardFocusAcrossAFocusChangingClick() throws {
    let focus = KeyboardFocusAuthority(
        identityToken: "ax:focus",
        bounds: CGRect(x: 120, y: 230, width: 200, height: 100),
        role: "AXTextArea",
        subrole: nil
    )
    let guardValue = ActionGuard(
        pid: 11,
        windowID: 22,
        bounds: CGRect(x: 100, y: 200, width: 300, height: 200),
        axIdentity: 33,
        keyboardFocus: focus,
        snapshotID: "snapshot",
        interactionMode: .foregroundTakeover
    )
    let valid = PIDActionTargetState(
        target: ActionTargetState(
            pid: guardValue.pid,
            windowID: guardValue.windowID,
            bounds: guardValue.bounds,
            axIdentity: guardValue.axIdentity,
            keyboardFocus: focus
        ),
        snapshotID: guardValue.snapshotID,
        isFrontmost: true,
        isKeyWindow: true
    )
    try ExactPIDActionGuardValidator(state: { valid }).revalidate(expected: guardValue, point: nil)

    let changed = PIDActionTargetState(
        target: ActionTargetState(
            pid: guardValue.pid,
            windowID: guardValue.windowID,
            bounds: guardValue.bounds,
            axIdentity: guardValue.axIdentity,
            keyboardFocus: KeyboardFocusAuthority(
                identityToken: "ax:changed",
                bounds: focus.bounds,
                role: focus.role,
                subrole: focus.subrole
            )
        ),
        snapshotID: guardValue.snapshotID,
        isFrontmost: true,
        isKeyWindow: true
    )
    try ExactPIDActionGuardValidator(state: { changed }).revalidate(expected: guardValue, point: nil)
}

@Test func exactPIDGuardValidatorRejectsEveryChangedAuthorityFieldAndContainment() throws {
    let guardValue = pidGuard()
    let valid = PIDActionTargetState(
        target: ActionTargetState(pid: 11, windowID: 22, bounds: guardValue.bounds, axIdentity: 33),
        snapshotID: guardValue.snapshotID,
        isFrontmost: true,
        isKeyWindow: true
    )
    try ExactPIDActionGuardValidator(state: { valid }).revalidate(expected: guardValue, point: CGPoint(x: 110, y: 210))

    let invalidStates: [PIDActionTargetState] = [
        .init(target: .init(pid: 12, windowID: 22, bounds: guardValue.bounds, axIdentity: 33), snapshotID: guardValue.snapshotID, isFrontmost: true, isKeyWindow: true),
        .init(target: .init(pid: 11, windowID: 23, bounds: guardValue.bounds, axIdentity: 33), snapshotID: guardValue.snapshotID, isFrontmost: true, isKeyWindow: true),
        .init(target: .init(pid: 11, windowID: 22, bounds: guardValue.bounds, axIdentity: 34), snapshotID: guardValue.snapshotID, isFrontmost: true, isKeyWindow: true),
        .init(target: .init(pid: 11, windowID: 22, bounds: CGRect(x: 101, y: 200, width: 300, height: 200), axIdentity: 33), snapshotID: guardValue.snapshotID, isFrontmost: true, isKeyWindow: true),
        .init(target: .init(pid: 11, windowID: 22, bounds: guardValue.bounds, axIdentity: 33, focusedAXIdentity: 34), snapshotID: guardValue.snapshotID, isFrontmost: true, isKeyWindow: true),
        .init(target: .init(pid: 11, windowID: 22, bounds: guardValue.bounds, axIdentity: 33, focusedAXBounds: CGRect(x: 101, y: 200, width: 300, height: 200)), snapshotID: guardValue.snapshotID, isFrontmost: true, isKeyWindow: true),
        .init(target: .init(pid: 11, windowID: 22, bounds: guardValue.bounds, axIdentity: 33, focusedRootPreference: .containedOverlay), snapshotID: guardValue.snapshotID, isFrontmost: true, isKeyWindow: true),
        .init(target: valid.target, snapshotID: "other", isFrontmost: true, isKeyWindow: true),
        .init(target: valid.target, snapshotID: guardValue.snapshotID, isFrontmost: false, isKeyWindow: true),
        .init(target: valid.target, snapshotID: guardValue.snapshotID, isFrontmost: true, isKeyWindow: false),
    ]
    for state in invalidStates {
        #expect(throws: ActionExecutionError.self) {
            try ExactPIDActionGuardValidator(state: { state }).revalidate(expected: guardValue, point: CGPoint(x: 110, y: 210))
        }
    }
    #expect(throws: ActionExecutionError.outOfBounds) {
        try ExactPIDActionGuardValidator(state: { valid }).revalidate(expected: guardValue, point: CGPoint(x: 99, y: 210))
    }
}

@Test func compatibilityDisabledPIDActionProducesZeroInput() {
    let fixture = PIDExecutorFixture(compatibility: PIDInputCompatibilityRegistry())
    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.lease,
        actions: pidPlanned([.click(x: 10, y: 10)])
    )

    #expect(result.cooperativeError == .backgroundActionUnsupported)
    #expect(fixture.poster.events.isEmpty)
    #expect(fixture.validator.calls == 0)
}

@Test func executorRejectsMissingSafeRegionAuthorityBeforePosterPreflight() {
    let fixture = PIDExecutorFixture(
        enabled: [.click],
        evidence: true,
        elementLookup: { reference, snapshotID in
            guard reference == "canvas", snapshotID == "snapshot" else { return nil }
            return safeElement(bounds: CGRect(x: 0, y: 0, width: 300, height: 200))
        }
    )
    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.lease,
        actions: [PlannedDispatchEntry(
            sourceIndex: 0,
            source: safeClick(x: 10, y: 10),
            backend: .pidPointer,
            actionClass: .click,
            resolved: nil,
            pointerSafeRegion: nil
        )]
    )

    #expect(result.error == .invalidAction)
    #expect(result.lastAcknowledgedAction == -1)
    #expect(fixture.poster.preflightCalls == 0)
    #expect(fixture.poster.events.isEmpty)
    #expect(fixture.activity.fragmentsStarted == 0)
}

@Test func executorRejectsMissingSafeRegionAuthorityBeforeCompatibilityDecision() {
    let fixture = PIDExecutorFixture(compatibility: PIDInputCompatibilityRegistry())
    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.lease,
        actions: [PlannedDispatchEntry(
            sourceIndex: 0,
            source: safeClick(x: 10, y: 10),
            backend: .pidPointer,
            actionClass: .click,
            resolved: nil,
            pointerSafeRegion: nil
        )]
    )

    #expect(result.error == .invalidAction)
    #expect(result.cooperativeError == nil)
    #expect(result.lastAcknowledgedAction == -1)
    #expect(fixture.poster.preflightCalls == 0)
    #expect(fixture.poster.events.isEmpty)
    #expect(fixture.activity.fragmentsStarted == 0)
}

@Test func executorValidatesEverySafeRegionAuthorityBeforeAnyCompatibilityDecision() {
    let fixture = PIDExecutorFixture(compatibility: PIDInputCompatibilityRegistry())
    let valid = pidPlanned([.click(x: 10, y: 10)])[0]
    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.lease,
        actions: [
            valid,
            PlannedDispatchEntry(
                sourceIndex: 1,
                source: safeClick(x: 20, y: 20),
                backend: .pidPointer,
                actionClass: .click,
                resolved: nil,
                pointerSafeRegion: nil
            ),
        ]
    )

    #expect(result.error == .invalidAction)
    #expect(result.cooperativeError == nil)
    #expect(result.lastAcknowledgedAction == -1)
    #expect(fixture.poster.preflightCalls == 0)
    #expect(fixture.poster.events.isEmpty)
    #expect(fixture.activity.fragmentsStarted == 0)
}

@Test func executorRejectsPointerSourceAndAuthorityReferenceMismatchBeforePosterPreflight() {
    let fixture = PIDExecutorFixture(enabled: [.click], evidence: true)
    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.lease,
        actions: [PlannedDispatchEntry(
            sourceIndex: 0,
            source: safeClick(x: 10, y: 10, elementRef: "canvas"),
            backend: .pidPointer,
            actionClass: .click,
            resolved: nil,
            pointerSafeRegion: PointerSafeRegionAuthority(
                reference: "different-element",
                identityToken: "different-element",
                bounds: CGRect(x: 0, y: 0, width: 300, height: 200)
            )
        )]
    )

    #expect(result.error == .staleSnapshot)
    #expect(result.lastAcknowledgedAction == -1)
    #expect(fixture.poster.preflightCalls == 0)
    #expect(fixture.poster.events.isEmpty)
    #expect(fixture.activity.fragmentsStarted == 0)
}

@Test(arguments: [UInt64(0xfeed_beef_dead_beef), UInt64(0x1234)])
func staleFragmentLeaseCannotBorrowRearmedSessionForClickOrScroll(nextMarker: UInt64) throws {
    let activity = PIDActivityMonitor()
    let oldLease = activity.currentLeaseForTest()
    activity.rearm(marker: nextMarker)

    let click = PIDExecutorFixture(enabled: [.click], activity: activity, evidence: true)
    let clickResult = click.executor.run(
        expected: click.guardValue,
        application: click.application,
        lease: oldLease,
        actions: pidPlanned([.click(x: 10, y: 10)])
    )
    let scroll = PIDExecutorFixture(enabled: [.scroll], activity: activity, evidence: true)
    let scrollResult = scroll.executor.run(
        expected: scroll.guardValue,
        application: scroll.application,
        lease: oldLease,
        actions: pidPlanned([.scroll(deltaY: 10, x: 10, y: 10)])
    )

    #expect(clickResult.cooperativeError == .userActivityPaused)
    #expect(scrollResult.cooperativeError == .userActivityPaused)
    #expect(click.poster.events.isEmpty)
    #expect(scroll.poster.events.isEmpty)
    #expect(click.validator.calls == 0)
    #expect(scroll.validator.calls == 0)
}

@Test func PIDClickRevalidatesBeforeEveryDownAndUpAndRequiresFreshEvidence() {
    let fixture = PIDExecutorFixture(enabled: [.click], evidence: false)
    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.lease,
        actions: pidPlanned([.click(x: 10, y: 10), .click(x: 20, y: 20)])
    )

    #expect(result.error == nil)
    #expect(result.cooperativeError == .observationRequired)
    #expect(result.lastAcknowledgedAction == 0)
    #expect(result.outcomes == [ActionOutcome(index: 0, ok: true, error: nil, observationRequired: true)])
    #expect(fixture.poster.events.count == 2)
    #expect(fixture.poster.events.allSatisfy { $0.pid == 11 && $0.marker == fixture.marker })
    #expect(fixture.validator.calls == 2)
    #expect(fixture.activity.assertions == 8)
}

@Test func plannedPIDActionUsesTheApprovedClassWithoutRawKindReclassification() {
    let source = NativeAction(
        kind: .click,
        x: 10,
        y: 10,
        endX: nil,
        endY: nil,
        text: nil,
        key: nil,
        deltaX: 0,
        deltaY: 7,
        durationMS: nil,
        elementRef: nil,
        targetElementRef: "pointer-4",
        modifiers: []
    )
    let fixture = PIDExecutorFixture(enabled: [.scroll], evidence: true)
    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.lease,
        actions: [PlannedDispatchEntry(
            sourceIndex: 4,
            source: source,
            backend: .pidPointer,
            actionClass: .scroll,
            resolved: nil,
            pointerSafeRegion: PointerSafeRegionAuthority(
                reference: "pointer-4",
                identityToken: "pointer-4",
                bounds: CGRect(x: 0, y: 0, width: 300, height: 200)
            )
        )]
    )

    #expect(result.error == nil)
    #expect(result.lastAcknowledgedAction == 4)
    #expect(result.outcomes == [ActionOutcome(index: 4, ok: true, error: nil)])
    #expect(fixture.poster.events.map(\.event) == [
        .scroll(point: CGPoint(x: 110, y: 210), deltaX: 0, deltaY: 7),
    ])
}

@Test func deliveredPIDActionRequiresFreshExactEvidenceForEveryGuardedFieldAndNeverReplays() {
    let expected = pidGuard()
    let valid = PIDActionTargetState(
        target: ActionTargetState(
            pid: expected.pid,
            windowID: expected.windowID,
            bounds: expected.bounds,
            axIdentity: expected.axIdentity,
            focusedAXIdentity: expected.focusedAXIdentity,
            focusedAXBounds: expected.focusedAXBounds,
            focusedRootPreference: expected.focusedRootPreference
        ),
        snapshotID: expected.snapshotID,
        isFrontmost: true,
        isKeyWindow: true
    )
    let changed: [PIDActionTargetState] = [
        .init(target: .init(pid: 12, windowID: expected.windowID, bounds: expected.bounds, axIdentity: expected.axIdentity), snapshotID: expected.snapshotID, isFrontmost: true, isKeyWindow: true),
        .init(target: .init(pid: expected.pid, windowID: 23, bounds: expected.bounds, axIdentity: expected.axIdentity), snapshotID: expected.snapshotID, isFrontmost: true, isKeyWindow: true),
        .init(target: .init(pid: expected.pid, windowID: expected.windowID, bounds: expected.bounds, axIdentity: 34, focusedAXIdentity: expected.focusedAXIdentity, focusedAXBounds: expected.focusedAXBounds), snapshotID: expected.snapshotID, isFrontmost: true, isKeyWindow: true),
        .init(target: .init(pid: expected.pid, windowID: expected.windowID, bounds: CGRect(x: 101, y: 200, width: 300, height: 200), axIdentity: expected.axIdentity, focusedAXIdentity: expected.focusedAXIdentity, focusedAXBounds: expected.focusedAXBounds), snapshotID: expected.snapshotID, isFrontmost: true, isKeyWindow: true),
        .init(target: .init(pid: expected.pid, windowID: expected.windowID, bounds: expected.bounds, axIdentity: expected.axIdentity, focusedAXIdentity: 34), snapshotID: expected.snapshotID, isFrontmost: true, isKeyWindow: true),
        .init(target: .init(pid: expected.pid, windowID: expected.windowID, bounds: expected.bounds, axIdentity: expected.axIdentity, focusedAXBounds: CGRect(x: 100, y: 201, width: 300, height: 200)), snapshotID: expected.snapshotID, isFrontmost: true, isKeyWindow: true),
        .init(target: .init(pid: expected.pid, windowID: expected.windowID, bounds: expected.bounds, axIdentity: expected.axIdentity, focusedRootPreference: .containedOverlay), snapshotID: expected.snapshotID, isFrontmost: true, isKeyWindow: true),
        .init(target: valid.target, snapshotID: "stale-snapshot", isFrontmost: true, isKeyWindow: true),
        .init(target: valid.target, snapshotID: expected.snapshotID, isFrontmost: false, isKeyWindow: true),
        .init(target: valid.target, snapshotID: expected.snapshotID, isFrontmost: true, isKeyWindow: false),
    ]

    var acceptedEvidenceCalls = 0
    let accepted = PIDExecutorFixture(
        enabled: [.scroll],
        evidenceCheck: { _, guardValue in
            acceptedEvidenceCalls += 1
            return (try? ExactPIDActionGuardValidator(state: { valid }).revalidate(expected: guardValue, point: nil)) != nil
        }
    )
    let acceptedResult = accepted.executor.run(
        expected: accepted.guardValue,
        application: accepted.application,
        lease: accepted.lease,
        actions: pidPlanned([.scroll(deltaY: 10, x: 10, y: 10)])
    )
    #expect(acceptedResult.error == nil)
    #expect(acceptedEvidenceCalls == 1)
    #expect(accepted.poster.events.count == 1)

    for changedState in changed {
        var evidenceCalls = 0
        let fixture = PIDExecutorFixture(
            enabled: [.scroll],
            evidenceCheck: { _, guardValue in
                evidenceCalls += 1
                return (try? ExactPIDActionGuardValidator(state: { changedState }).revalidate(expected: guardValue, point: nil)) != nil
            }
        )
        let result = fixture.executor.run(
            expected: fixture.guardValue,
            application: fixture.application,
            lease: fixture.lease,
            actions: pidPlanned([
                .scroll(deltaY: 10, x: 10, y: 10),
                .scroll(deltaY: 20, x: 20, y: 20),
            ])
        )

        #expect(result.error == nil)
        #expect(result.cooperativeError == .observationRequired)
        #expect(result.lastAcknowledgedAction == 0)
        #expect(result.outcomes == [ActionOutcome(index: 0, ok: true, error: nil, observationRequired: true)])
        #expect(evidenceCalls == 1)
        #expect(fixture.poster.events.count == 1)
        #expect(fixture.activity.fragmentsStarted == 1)
        #expect(fixture.activity.fragmentsEnded == 1)

        let retry = fixture.executor.run(
            expected: fixture.guardValue,
            application: fixture.application,
            lease: fixture.lease,
            actions: pidPlanned([.scroll(deltaY: 10, x: 10, y: 10)])
        )
        #expect(retry.cooperativeError == .userActivityPaused)
        #expect(fixture.poster.events.count == 1)
    }
}

@Test func leaseRevokedAfterDownStopsAtCleanupOnlyRelease() {
    let fixture = PIDExecutorFixture(enabled: [.click], evidence: true)
    let lease = fixture.lease
    fixture.poster.onPost = { event in
        if case .mouseDown = event { fixture.activity.rearm(marker: fixture.marker) }
    }
    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: lease,
        actions: pidPlanned([.click(x: 10, y: 10)])
    )

    #expect(result.cooperativeError == .userActivityPaused)
    #expect(result.error == .unknownOutcome)
    #expect(fixture.poster.events.map(\.event) == [
        .mouseDown(point: CGPoint(x: 110, y: 210), clickCount: 1),
        .mouseUp(point: CGPoint(x: 110, y: 210), clickCount: 1),
    ])
    #expect(fixture.activity.fragmentsStarted == 1)
    #expect(fixture.activity.fragmentsEnded == 1)
    #expect(fixture.heldInputs.heldCount == 0)
}

@Test func leaseRevokedAfterDragIntermediateStopsBeforeTheNextPoint() {
    let fixture = PIDExecutorFixture(enabled: [.drag], evidence: true)
    let lease = fixture.lease
    fixture.poster.onPost = { event in
        if case .mouseDragged = event, fixture.poster.events.count == 2 {
            fixture.activity.rearm(marker: fixture.marker)
        }
    }
    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: lease,
        actions: pidPlanned([.drag(x: 10, y: 10, endX: 30, endY: 30, durationMS: 30)])
    )
    let lastPoint = CGPoint(x: 110 + 20 * CGFloat(1) / 3, y: 210 + 20 * CGFloat(1) / 3)

    #expect(result.error == .unknownOutcome)
    #expect(fixture.poster.events.map(\.event) == [
        .mouseDown(point: CGPoint(x: 110, y: 210), clickCount: 1),
        .mouseDragged(point: lastPoint),
        .mouseUp(point: lastPoint, clickCount: 1),
    ])
}

@Test func leaseRevokedDuringEvidenceCannotAcknowledgeCompletedInput() {
    let activity = PIDActivityMonitor()
    let fixture = PIDExecutorFixture(
        enabled: [.click],
        activity: activity,
        evidenceCheck: { _, _ in
            activity.rearm(marker: 0xfeed_beef_dead_beef)
            return true
        }
    )
    let lease = fixture.lease
    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: lease,
        actions: pidPlanned([.click(x: 10, y: 10)])
    )

    #expect(result.cooperativeError == .userActivityPaused)
    #expect(result.error == .unknownOutcome)
    #expect(result.lastAcknowledgedAction == -1)
    #expect(fixture.poster.events.count == 2)
}

@Test func guardFailureBeforeMouseDownRemainsPreInputAndIsNotUnknownOutcome() {
    let fixture = PIDExecutorFixture(enabled: [.click], evidence: true)
    fixture.validator.failureAt = 1
    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.lease,
        actions: pidPlanned([.click(x: 10, y: 10)])
    )

    #expect(result.error == .staleSnapshot)
    #expect(fixture.poster.events.isEmpty)
    #expect(fixture.heldInputs.heldCount == 0)
}

@Test func doubleClickGuardFailureAfterFirstClickIsUnknownOutcome() {
    let fixture = PIDExecutorFixture(enabled: [.doubleClick], evidence: true)
    fixture.validator.failureAt = 3
    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.lease,
        actions: pidPlanned([.doubleClick(x: 10, y: 10)])
    )

    #expect(result.error == .unknownOutcome)
    #expect(fixture.poster.events.count == 2)
}

@Test func pauseAfterMouseDownPostsPairedReleaseAndStopsLaterActions() {
    let activity = PIDActivityMonitor(pauseAfterAssertion: 5)
    let fixture = PIDExecutorFixture(enabled: [.click], activity: activity, evidence: true)
    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.lease,
        actions: pidPlanned([.click(x: 10, y: 10), .click(x: 20, y: 20)])
    )

    #expect(result.cooperativeError == .userActivityPaused)
    #expect(result.error == .unknownOutcome)
    #expect(result.lastAcknowledgedAction == -1)
    #expect(fixture.poster.events.map(\.event) == [
        .mouseDown(point: CGPoint(x: 110, y: 210), clickCount: 1),
        .mouseUp(point: CGPoint(x: 110, y: 210), clickCount: 1),
    ])
    #expect(fixture.heldInputs.heldCount == 0)
}

@Test func releaseGuardFailureUsesCleanupOnlyUpAndReportsUnknownOutcome() {
    let fixture = PIDExecutorFixture(enabled: [.click], evidence: true)
    fixture.validator.failureFrom = 2
    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.lease,
        actions: pidPlanned([.click(x: 10, y: 10)])
    )

    #expect(result.error == .unknownOutcome)
    #expect(!result.cleanupFailed)
    #expect(fixture.poster.events.map(\.event) == [
        .mouseDown(point: CGPoint(x: 110, y: 210), clickCount: 1),
        .mouseUp(point: CGPoint(x: 110, y: 210), clickCount: 1),
    ])
    #expect(fixture.validator.calls == 2)
    #expect(fixture.heldInputs.heldCount == 0)
}

@Test func targetChangeAfterDragDownUsesCleanupOnlyUpAndStopsIntermediates() {
    let fixture = PIDExecutorFixture(enabled: [.drag], evidence: true)
    fixture.validator.failureFrom = 2
    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.lease,
        actions: pidPlanned([.drag(x: 10, y: 10, endX: 30, endY: 30, durationMS: 20)])
    )

    #expect(result.error == .unknownOutcome)
    #expect(fixture.poster.events.map(\.event) == [
        .mouseDown(point: CGPoint(x: 110, y: 210), clickCount: 1),
        .mouseUp(point: CGPoint(x: 110, y: 210), clickCount: 1),
    ])
    #expect(fixture.heldInputs.heldCount == 0)
}

@Test func dragFailureAfterIntermediateReleasesAtLastSuccessfullyPostedPoint() {
    let fixture = PIDExecutorFixture(enabled: [.drag], evidence: true)
    fixture.validator.failureFrom = 3
    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.lease,
        actions: pidPlanned([.drag(x: 10, y: 10, endX: 30, endY: 30, durationMS: 30)])
    )
    let lastPoint = CGPoint(x: 110 + 20 * CGFloat(1) / 3, y: 210 + 20 * CGFloat(1) / 3)

    #expect(result.error == .unknownOutcome)
    #expect(fixture.poster.events.map(\.event) == [
        .mouseDown(point: CGPoint(x: 110, y: 210), clickCount: 1),
        .mouseDragged(point: lastPoint),
        .mouseUp(point: lastPoint, clickCount: 1),
    ])
}

@Test func dragCleanupFailureAtLastPointIsExplicitAndRetainsToken() {
    let fixture = PIDExecutorFixture(enabled: [.drag], evidence: true)
    fixture.validator.failureFrom = 2
    fixture.poster.failingCalls = [1]
    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.lease,
        actions: pidPlanned([.drag(x: 10, y: 10, endX: 30, endY: 30, durationMS: 20)])
    )

    #expect(result.error == .unknownOutcome)
    #expect(result.cleanupFailed)
    #expect(fixture.poster.events.last?.event == .mouseUp(point: CGPoint(x: 110, y: 210), clickCount: 1))
    #expect(fixture.heldInputs.heldCount(scope: fixture.activity.heldInputScopeValue) == 1)
}

@Test func cleanupOnlyPostFailureIsExplicitAndRemainsHeldForLaterCleanup() {
    let fixture = PIDExecutorFixture(enabled: [.click], evidence: true)
    fixture.validator.failureFrom = 2
    fixture.poster.failingCalls = [1]
    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.lease,
        actions: pidPlanned([.click(x: 10, y: 10)])
    )

    #expect(result.error == .unknownOutcome)
    #expect(result.cleanupFailed)
    #expect(fixture.poster.events.count == 2)
    #expect(fixture.heldInputs.heldCount(scope: fixture.activity.heldInputScopeValue) == 1)
}

@Test func dragChecksPauseAndGuardBeforeEveryIntermediateAndRelease() {
    let fixture = PIDExecutorFixture(enabled: [.drag], evidence: true)
    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.lease,
        actions: pidPlanned([.drag(x: 10, y: 10, endX: 30, endY: 30, durationMS: 20)])
    )

    #expect(result.error == nil)
    #expect(fixture.poster.events.count == 4)
    #expect(fixture.validator.calls == 4)
    #expect(fixture.activity.assertions == 12)
}

@Test func activityImmediatelyAfterScrollPostingPausesWithUnknownOutcome() {
    let activity = PIDActivityMonitor(pauseAfterAssertion: 4)
    let fixture = PIDExecutorFixture(enabled: [.scroll], activity: activity, evidence: true)
    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.lease,
        actions: pidPlanned([.scroll(deltaY: 10, x: 10, y: 10)])
    )

    #expect(result.cooperativeError == .userActivityPaused)
    #expect(result.error == .unknownOutcome)
    #expect(fixture.poster.events.count == 1)
}

@Test func scrollElementReferenceUsesVerifiedCenterAndMissingCoordinatesAreRejected() {
    let fixture = PIDExecutorFixture(enabled: [.scroll], evidence: true)
    let referenced = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.lease,
        actions: pidPlanned([.scroll(deltaY: 10, elementRef: "scroll-area")])
    )
    #expect(referenced.error == nil)
    #expect(fixture.poster.events.first?.event == .scroll(point: CGPoint(x: 140, y: 250), deltaX: 0, deltaY: 10))

    let noPoint = PIDExecutorFixture(enabled: [.scroll], evidence: true)
    let rejected = noPoint.executor.run(
        expected: noPoint.guardValue,
        application: noPoint.application,
        lease: noPoint.lease,
        actions: [PlannedDispatchEntry(
            sourceIndex: 0,
            source: .scroll(deltaY: 10),
            backend: .pidPointer,
            actionClass: .scroll,
            resolved: nil,
            pointerSafeRegion: nil
        )]
    )
    #expect(rejected.error == .invalidAction)
    #expect(noPoint.poster.events.isEmpty)
}

@Test func rawCoordinatePayloadCanReachForegroundWindowAuthorityPlanning() throws {
    let payloads: [JSONValue] = [
        .object(["type": .string("click"), "x": .number(10), "y": .number(10)]),
        .object(["type": .string("double_click"), "x": .number(10), "y": .number(10)]),
        .object(["type": .string("scroll"), "x": .number(10), "y": .number(10), "delta_y": .number(3)]),
        .object([
            "type": .string("drag"),
            "x": .number(10),
            "y": .number(10),
            "end_x": .number(20),
            "end_y": .number(20),
        ]),
    ]

    for payload in payloads {
        let action = try NativeAction.parse(payload)
        #expect(action.targetElementRef == nil)
    }
}

@Test func staleSafeElementReferenceRejectsBeforePIDInput() {
    let fixture = PIDExecutorFixture(enabled: [.click], evidence: true, elementLookup: { _, _ in nil })
    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.lease,
        actions: safePlanned(safeClick(x: 15, y: 20), bounds: CGRect(x: 10, y: 10, width: 40, height: 40))
    )

    #expect(result.error == .staleSnapshot)
    #expect(fixture.poster.events.isEmpty)
}

@Test func rawCoordinateMustBeStrictlyInsideDeclaredSafeElement() {
    let fixture = PIDExecutorFixture(
        enabled: [.click],
        evidence: true,
        elementLookup: { _, _ in safeElement(bounds: CGRect(x: 10, y: 10, width: 40, height: 40)) }
    )
    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.lease,
        actions: safePlanned(safeClick(x: 10, y: 20), bounds: CGRect(x: 10, y: 10, width: 40, height: 40))
    )

    #expect(result.error == .outOfBounds)
    #expect(fixture.poster.events.isEmpty)
}

@Test func declaredSafeElementMustBeWhollyInsideExactWindow() {
    let fixture = PIDExecutorFixture(
        enabled: [.click],
        evidence: true,
        elementLookup: { _, _ in safeElement(bounds: CGRect(x: 290, y: 190, width: 20, height: 20)) }
    )
    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.lease,
        actions: safePlanned(safeClick(x: 295, y: 195), bounds: CGRect(x: 290, y: 190, width: 20, height: 20))
    )

    #expect(result.error == .outOfBounds)
    #expect(fixture.poster.events.isEmpty)
}

@Test func movedSafeElementBoundsRejectBeforeMouseDown() {
    let fixture = PIDExecutorFixture(
        enabled: [.click],
        evidence: true,
        elementLookup: { _, _ in safeElement(bounds: CGRect(x: 11, y: 10, width: 40, height: 40)) }
    )
    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.lease,
        actions: safePlanned(safeClick(x: 20, y: 20), bounds: CGRect(x: 10, y: 10, width: 40, height: 40))
    )

    #expect(result.error == .staleSnapshot)
    #expect(fixture.poster.events.isEmpty)
}

@Test func validSafeRegionClickPreservesExactDeclaredCoordinate() {
    let fixture = PIDExecutorFixture(
        enabled: [.click],
        evidence: true,
        elementLookup: { _, _ in safeElement(bounds: CGRect(x: 10, y: 10, width: 40, height: 40)) }
    )
    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.lease,
        actions: safePlanned(safeClick(x: 15, y: 20), bounds: CGRect(x: 10, y: 10, width: 40, height: 40))
    )

    #expect(result.error == nil)
    #expect(fixture.poster.events.map(\.event) == [
        .mouseDown(point: CGPoint(x: 115, y: 220), clickCount: 1),
        .mouseUp(point: CGPoint(x: 115, y: 220), clickCount: 1),
    ])
}

@Test func safeElementReplacementAfterMouseDownIsUnknownAndUsesMatchingCleanup() {
    var identity = "canvas-original"
    let fixture = PIDExecutorFixture(
        enabled: [.click],
        evidence: true,
        elementLookup: { _, _ in
            safeElement(identity: identity, bounds: CGRect(x: 10, y: 10, width: 40, height: 40))
        }
    )
    fixture.poster.onPost = { event in
        if case .mouseDown = event { identity = "canvas-replacement" }
    }
    let result = fixture.executor.run(
        expected: fixture.guardValue,
        application: fixture.application,
        lease: fixture.lease,
        actions: safePlanned(safeClick(x: 15, y: 20), bounds: CGRect(x: 10, y: 10, width: 40, height: 40))
    )

    #expect(result.error == .unknownOutcome)
    #expect(result.lastAcknowledgedAction == -1)
    #expect(!result.cleanupFailed)
    #expect(fixture.poster.events.map(\.event) == [
        .mouseDown(point: CGPoint(x: 115, y: 220), clickCount: 1),
        .mouseUp(point: CGPoint(x: 115, y: 220), clickCount: 1),
    ])
    #expect(fixture.heldInputs.heldCount == 0)
}

private func pidGuard() -> ActionGuard {
    ActionGuard(
        pid: 11,
        windowID: 22,
        bounds: CGRect(x: 100, y: 200, width: 300, height: 200),
        axIdentity: 33,
        snapshotID: "snapshot",
        interactionMode: .foregroundTakeover
    )
}

private func safeClick(x: CGFloat, y: CGFloat, elementRef: String = "canvas") -> NativeAction {
    NativeAction(
        kind: .click,
        x: x,
        y: y,
        endX: nil,
        endY: nil,
        text: nil,
        key: nil,
        deltaX: nil,
        deltaY: nil,
        durationMS: nil,
        elementRef: nil,
        targetElementRef: elementRef,
        modifiers: []
    )
}

private func safeElement(
    identity: String = "canvas-original",
    bounds: CGRect
) -> ActionElement {
    ActionElement(
        element: nil,
        identityToken: identity,
        bounds: bounds,
        roleResult: .init(value: "AXGroup", status: .complete),
        subroleResult: .init(value: nil, status: .complete),
        enabled: true,
        actionNames: .complete([])
    )
}

private func safePlanned(
    _ action: NativeAction,
    identity: String = "canvas-original",
    bounds: CGRect
) -> [PlannedDispatchEntry] {
    [PlannedDispatchEntry(
        sourceIndex: 0,
        source: action,
        backend: .pidPointer,
        actionClass: .click,
        resolved: nil,
        pointerSafeRegion: PointerSafeRegionAuthority(
            reference: action.targetElementRef ?? "",
            identityToken: identity,
            bounds: bounds
        )
    )]
}

private func pidPlanned(_ actions: [NativeAction]) -> [PlannedDispatchEntry] {
    actions.enumerated().map { index, action in
        let actionClass: DispatchActionClass
        switch action.kind {
        case .click, .rightClick: actionClass = .click
        case .doubleClick: actionClass = .doubleClick
        case .scroll: actionClass = .scroll
        case .drag: actionClass = .drag
        case .type, .keypress, .wait:
            fatalError("PID test helper accepts only pointer actions")
        }
        let hasCoordinates = action.x != nil || action.y != nil || action.endX != nil || action.endY != nil
        let reference: String
        let source: NativeAction
        if hasCoordinates {
            reference = action.targetElementRef ?? "pointer-\(index)"
            source = NativeAction(
                kind: action.kind,
                x: action.x,
                y: action.y,
                endX: action.endX,
                endY: action.endY,
                text: action.text,
                key: action.key,
                deltaX: action.deltaX,
                deltaY: action.deltaY,
                durationMS: action.durationMS,
                elementRef: nil,
                targetElementRef: reference,
                modifiers: action.modifiers
            )
        } else {
            guard let elementRef = action.elementRef else {
                fatalError("PID test helper requires explicit semantic element authority")
            }
            reference = elementRef
            source = action
        }
        let bounds = reference == "scroll-area"
            ? CGRect(x: 20, y: 30, width: 40, height: 40)
            : CGRect(x: 0, y: 0, width: 300, height: 200)
        return PlannedDispatchEntry(
            sourceIndex: index,
            source: source,
            backend: .pidPointer,
            actionClass: actionClass,
            resolved: nil,
            pointerSafeRegion: PointerSafeRegionAuthority(
                reference: reference,
                identityToken: reference,
                bounds: bounds
            )
        )
    }
}

@Test func PIDCompleteRightClickRequiresObservationWithoutDispatchingSuffix() {
    let rightClick = NativeAction(kind: .rightClick, x: 10, y: 10,
        endX: nil, endY: nil, text: nil, key: nil, deltaX: nil, deltaY: nil,
        durationMS: nil, elementRef: nil, targetElementRef: nil, modifiers: [])
    for hasSuffix in [false, true] {
        let fixture = PIDExecutorFixture(enabled: [.click], evidence: false)
        let actions = [rightClick] + (hasSuffix ? [.click(x: 20, y: 20)] : [])
        let result = fixture.executor.run(expected: fixture.guardValue,
            application: fixture.application, lease: fixture.lease, actions: pidPlanned(actions))

        #expect(result.error == nil)
        #expect(result.cooperativeError == (hasSuffix ? .observationRequired : nil))
        #expect(result.lastAcknowledgedAction == 0)
        #expect(result.outcomes == [ActionOutcome(index: 0, ok: true, error: nil, observationRequired: true)])
        #expect(fixture.poster.events.map(\.event) == [
            .mouseDown(point: CGPoint(x: 110, y: 210), clickCount: 1, button: .right),
            .mouseUp(point: CGPoint(x: 110, y: 210), clickCount: 1, button: .right),
        ])
        #expect(fixture.validator.calls == 2)
        #expect(fixture.heldInputs.heldCount == 0)
        #expect(fixture.activity.fragmentsEnded == 1)
    }
}

@Test func PIDCompleteBackgroundInputRetainsFailClosedReceipt() throws {
    let fixture = PIDExecutorFixture(enabled: [.click], evidence: false)
    let expected = ActionGuard(pid: 11, windowID: 22, bounds: fixture.guardValue.bounds,
        axIdentity: 33, snapshotID: "snapshot", interactionMode: .background)
    let lease = fixture.lease
    let plan = try fixture.executor.preflight(expected: expected, application: fixture.application,
        marker: lease.marker, entries: pidPlanned([.click(x: 10, y: 10)]), allowBackgroundDelivery: true)
    try fixture.activity.beginFragment(lease: lease)
    defer { fixture.activity.endFragment(lease: lease) }
    let result = fixture.executor.executePrepared(sourceIndex: 0, from: plan, expected: expected, lease: lease)

    // The background batch contract has no cooperative observation checkpoint.
    #expect(result.error == .unknownOutcome)
    #expect(result.lastAcknowledgedAction == -1)
    #expect(result.outcomes.allSatisfy { !$0.observationRequired })
    #expect(fixture.poster.events.count == 2)
}

@Test func PIDCompleteCheckpointCannotMaskPostingOrReleaseFailure() {
    for failures: Set<Int> in [[0], [1], [1, 2]] {
        let fixture = PIDExecutorFixture(enabled: [.click], evidence: false)
        fixture.poster.failingCalls = failures
        let result = fixture.executor.run(expected: fixture.guardValue,
            application: fixture.application, lease: fixture.lease,
            actions: pidPlanned([.click(x: 10, y: 10), .click(x: 20, y: 20)]))
        // The fake explicitly reports zero delivery when the very first post fails.
        #expect(result.error == (failures.contains(0) ? .helperFailed : .unknownOutcome))
        #expect(result.lastAcknowledgedAction == -1)
        #expect(result.outcomes.allSatisfy { !$0.observationRequired })
        #expect(!fixture.poster.events.contains { $0.event == .mouseDown(point: CGPoint(x: 120, y: 220), clickCount: 1) })
    }
}

private final class PIDExecutorFixture {
    let guardValue = pidGuard()
    let application = PIDTargetApplication(bundleIdentifier: "com.example.Editor", version: "1.2.3")
    let marker: UInt64 = 0xfeed_beef_dead_beef
    let poster = RecordingPIDPoster()
    let validator: RecordingPIDValidator
    let activity: PIDActivityMonitor
    let heldInputs = HeldInputRegistry()
    let executor: PIDTargetedActionExecutor
    var lease: UserActivitySessionLease { activity.currentLeaseForTest() }

    init(
        enabled: Set<DispatchActionClass> = [],
        compatibility: PIDInputCompatibilityRegistry? = nil,
        genericForegroundEnabled: Bool = false,
        activity: PIDActivityMonitor = PIDActivityMonitor(),
        evidence: Bool = true,
        evidenceCheck: PIDTargetedActionExecutor.EvidenceCheck? = nil,
        elementLookup: PIDTargetedActionExecutor.ElementLookup? = nil,
        now: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
        delay: @escaping (useconds_t) -> Void = { _ in }
    ) {
        self.activity = activity
        validator = RecordingPIDValidator()
        let targetApplication = application
        let registry = compatibility ?? PIDInputCompatibilityRegistry(cells: enabled.map {
            PIDInputCompatibilityCell(bundleIdentifier: targetApplication.bundleIdentifier, version: targetApplication.version, action: $0)
        })
        executor = PIDTargetedActionExecutor(
            poster: poster,
            compatibility: registry,
            genericForegroundEnabled: genericForegroundEnabled,
            activity: activity,
            validator: validator,
            heldInputs: heldInputs,
            element: elementLookup ?? { reference, snapshotID in
                guard snapshotID == "snapshot" else { return nil }
                let bounds: CGRect
                if reference == "scroll-area" {
                    bounds = CGRect(x: 20, y: 30, width: 40, height: 40)
                } else if reference.hasPrefix("pointer-") {
                    bounds = CGRect(x: 0, y: 0, width: 300, height: 200)
                } else {
                    return nil
                }
                return ActionElement(
                    element: nil,
                    identityToken: reference,
                    bounds: bounds,
                    roleResult: .init(value: "AXScrollArea", status: .complete),
                    subroleResult: .init(value: nil, status: .complete),
                    enabled: true,
                    actionNames: .complete([])
                )
            },
            evidence: evidenceCheck ?? { _, _ in evidence },
            delay: delay,
            now: now
        )
    }
}

private final class RecordingPIDPoster: PIDTargetedInputPosting {
    struct Posted {
        let event: SyntheticInputEvent
        let pid: pid_t
        let marker: UInt64
    }
    var events: [Posted] = []
    var preflightCalls = 0
    var failingCalls: Set<Int> = []
    var onPost: ((SyntheticInputEvent) -> Void)?
    func preflight(targetPID _: pid_t, marker _: UInt64) -> Bool {
        preflightCalls += 1
        return true
    }
    func post(_ event: SyntheticInputEvent, to targetPID: pid_t, marker: UInt64) throws {
        let call = events.count
        events.append(.init(event: event, pid: targetPID, marker: marker))
        onPost?(event)
        if failingCalls.contains(call) {
            throw SyntheticInputFailure(error: .helperFailed, inputStarted: false)
        }
    }
}

private final class RecordingPIDValidator: PIDActionGuardValidating {
    var calls = 0
    var onValidate: (() -> Void)?
    var failureAt: Int?
    var failureFrom: Int?
    func revalidate(expected _: ActionGuard, point _: CGPoint?) throws {
        calls += 1
        onValidate?()
        if calls == failureAt || calls >= failureFrom ?? .max { throw ActionExecutionError.staleSnapshot }
    }
}

private final class PIDActivityMonitor: UserActivityMonitoring {
    var paused = false
    var assertions = 0
    let pauseAfterAssertion: Int?
    private(set) var heldInputScopeValue: HeldInputScope
    private var currentLease: UserActivitySessionLease
    private var fragmentActive = false
    private var fragmentConsumed = false
    private(set) var fragmentsStarted = 0
    private(set) var fragmentsEnded = 0
    init(pauseAfterAssertion: Int? = nil, marker: UInt64 = 0xfeed_beef_dead_beef) {
        self.pauseAfterAssertion = pauseAfterAssertion
        currentLease = UserActivitySessionLease(marker: marker)
        heldInputScopeValue = .cooperative(marker: marker, generation: UUID())
    }
    func rearm(marker: UInt64) {
        currentLease = UserActivitySessionLease(marker: marker)
        heldInputScopeValue = .cooperative(marker: marker, generation: UUID())
        paused = false
        fragmentActive = false
        fragmentConsumed = false
    }
    func currentLeaseForTest() -> UserActivitySessionLease { currentLease }
    func beginFragment(lease: UserActivitySessionLease) throws {
        guard lease === currentLease, !fragmentActive, !fragmentConsumed else { throw UserActivityMonitoringError.notArmed }
        fragmentActive = true
        fragmentConsumed = true
        fragmentsStarted += 1
    }
    func endFragment(lease: UserActivitySessionLease) {
        fragmentsEnded += 1
        if lease === currentLease, fragmentActive {
            fragmentActive = false
        }
    }
    func assertNotPaused(lease: UserActivitySessionLease) throws {
        guard lease === currentLease else { throw UserActivityMonitoringError.notArmed }
        try validatePauseState()
    }
    func heldInputScope(lease: UserActivitySessionLease) throws -> HeldInputScope {
        try assertNotPaused(lease: lease)
        return heldInputScopeValue
    }
    func performPIDEvent(
        lease: UserActivitySessionLease,
        validate: () throws -> Void,
        mutation: () throws -> Void
    ) throws {
        try assertNotPaused(lease: lease)
        try validate()
        try assertNotPaused(lease: lease)
        try mutation()
    }
    func performPIDCleanup(lease _: UserActivitySessionLease, _ cleanup: () throws -> Void) throws {
        try cleanup()
    }
    func cleanupHeldInputs(lease _: UserActivitySessionLease) throws -> HeldInputCleanupResult {
        HeldInputCleanupResult(attempted: 0, released: 0, failed: 0)
    }
    func arm(marker: UInt64, notification _: UserActivityPauseSignal) throws -> UserActivitySessionLease {
        rearm(marker: marker)
        return currentLease
    }
    private func validatePauseState() throws {
        assertions += 1
        if assertions >= pauseAfterAssertion ?? .max { paused = true }
        if paused { throw UserActivityMonitoringError.paused }
    }
    func disarm(lease: UserActivitySessionLease) throws -> Bool {
        guard lease === currentLease else { throw UserActivityMonitoringError.notArmed }
        return paused
    }
}

@Test func pidDragDoesNotAddFullSleepAfterSlowGuards() throws {
    var time: TimeInterval = 0
    var sleeps: [useconds_t] = []
    let fixture = PIDExecutorFixture(enabled: [.drag], now: { time }, delay: { micros in
        sleeps.append(micros)
        time += Double(micros) / 1_000_000
    })
    fixture.validator.onValidate = { time += 0.03 }
    let result = fixture.executor.run(expected: fixture.guardValue, application: fixture.application,
        lease: fixture.lease, actions: pidPlanned([.drag(x: 10, y: 10, endX: 30, endY: 30, durationMS: 300)]))
    #expect(result.error == nil)
    #expect(sleeps.reduce(0, +) < 300_000)
}

@Test func pidSlowGuardCannotPostAfterBatchDeadline() throws {
    var time: TimeInterval = 0
    let fixture = PIDExecutorFixture(enabled: [.click], now: { time })
    fixture.validator.onValidate = { time = 13 }
    let result = fixture.executor.run(expected: fixture.guardValue, application: fixture.application,
        lease: fixture.lease, actions: pidPlanned([.click(x: 10, y: 10, within: "pointer-0")]))
    #expect(result.error == .actionTimeout)
    #expect(fixture.poster.events.isEmpty)
}


private func genericForegroundWindowEntry(_ action: NativeAction = .click(x: 15, y: 20)) -> PlannedDispatchEntry {
    PlannedDispatchEntry(
        sourceIndex: 0, source: action, backend: .foregroundPointer,
        actionClass: .click, resolved: nil,
        pointerSafeRegion: PointerSafeRegionAuthority(
            reference: foregroundWindowRegionReference, identityToken: "snapshot",
            bounds: CGRect(x: 0, y: 0, width: 300, height: 200)
        )
    )
}

@Test func genericForegroundExecutorWithoutRegistryBalancesWindowCoordinates() throws {
    let fixture = PIDExecutorFixture(genericForegroundEnabled: true, elementLookup: { _, _ in nil })
    let plan = try fixture.executor.preflight(
        expected: fixture.guardValue, application: fixture.application,
        marker: fixture.lease.marker, entries: [genericForegroundWindowEntry()]
    )
    let result = fixture.executor.executePrepared(sourceIndex: 0, from: plan, expected: fixture.guardValue, lease: fixture.lease)
    #expect(result.error == nil)
    #expect(fixture.poster.events.map(\.event) == [
        .mouseDown(point: CGPoint(x: 115, y: 220), clickCount: 1),
        .mouseUp(point: CGPoint(x: 115, y: 220), clickCount: 1)
    ])
    #expect(fixture.heldInputs.heldCount == 0)
}

@Test func genericForegroundExecutorIsDisabledByDefault() {
    let fixture = PIDExecutorFixture()
    #expect(throws: PIDTargetedActionFailure.compatibilityDisabled) {
        _ = try fixture.executor.preflight(
            expected: fixture.guardValue, application: fixture.application,
            marker: fixture.lease.marker, entries: [genericForegroundWindowEntry()]
        )
    }
    #expect(fixture.poster.events.isEmpty)
}

@Test func genericForegroundRightClickPreservesMouseButton() throws {
    let fixture = PIDExecutorFixture(genericForegroundEnabled: true, elementLookup: { _, _ in nil })
    let plan = try fixture.executor.preflight(
        expected: fixture.guardValue, application: fixture.application,
        marker: fixture.lease.marker, entries: [genericForegroundWindowEntry(.rightClick(x: 15, y: 20))]
    )
    let result = fixture.executor.executePrepared(sourceIndex: 0, from: plan, expected: fixture.guardValue, lease: fixture.lease)
    #expect(result.error == nil)
    #expect(fixture.poster.events.map(\.event) == [
        .mouseDown(point: CGPoint(x: 115, y: 220), clickCount: 1, button: .right),
        .mouseUp(point: CGPoint(x: 115, y: 220), clickCount: 1, button: .right)
    ])
}

@Test func genericForegroundSlowWindowGuardCannotPostAfterDeadline() throws {
    var time: TimeInterval = 0
    let fixture = PIDExecutorFixture(genericForegroundEnabled: true, now: { time })
    let plan = try fixture.executor.preflight(expected: fixture.guardValue, application: fixture.application,
        marker: fixture.lease.marker, entries: [genericForegroundWindowEntry()])
    fixture.validator.onValidate = { time = 13 }
    let result = fixture.executor.executePrepared(sourceIndex: 0, from: plan, expected: fixture.guardValue, lease: fixture.lease)
    #expect(result.error == .actionTimeout)
    #expect(fixture.poster.events.isEmpty)
}

@Test func genericForegroundWindowChangeAfterDownCleansUpWithoutReplay() throws {
    let fixture = PIDExecutorFixture(genericForegroundEnabled: true, elementLookup: { _, _ in nil })
    let plan = try fixture.executor.preflight(expected: fixture.guardValue, application: fixture.application,
        marker: fixture.lease.marker, entries: [genericForegroundWindowEntry()])
    fixture.poster.onPost = { event in
        if case .mouseDown = event { fixture.validator.failureFrom = fixture.validator.calls + 1 }
    }
    let result = fixture.executor.executePrepared(sourceIndex: 0, from: plan, expected: fixture.guardValue, lease: fixture.lease)
    #expect(result.error == .unknownOutcome)
    #expect(fixture.poster.events.count == 2)
    #expect(fixture.heldInputs.heldCount == 0)
}

@Test func popupPointerDisappearingAfterDownReleasesPromptlyWithoutReplay() throws {
    let expected = pidGuard()
    let point = CGPoint(x: 115, y: 220)
    var popupVisible = true
    var proofCalls = 0
    var proofCallsAtDown = 0
    let validator = PopupPointerClickGuardValidator(
        sealed: expected, authorizedPoint: point, popupProof: {
            proofCalls += 1
            return popupVisible
        }
    )
    let poster = RecordingPIDPoster()
    poster.onPost = { event in
        if case .mouseDown = event {
            proofCallsAtDown = proofCalls
            popupVisible = false
        }
    }
    let activity = PIDActivityMonitor()
    let held = HeldInputRegistry()
    let executor = PIDTargetedActionExecutor(
        poster: poster, genericForegroundEnabled: true, activity: activity,
        validator: validator, heldInputs: held, element: { _, _ in nil },
        evidence: { _, _ in false }
    )
    let plan = try executor.preflight(
        expected: expected,
        application: PIDTargetApplication(bundleIdentifier: "unlisted.popup", version: "1"),
        marker: activity.currentLeaseForTest().marker,
        entries: [genericForegroundWindowEntry()]
    )
    let result = executor.executePrepared(
        sourceIndex: 0, from: plan, expected: expected,
        lease: activity.currentLeaseForTest()
    )
    #expect(result.error == nil)
    #expect(result.lastAcknowledgedAction == 0)
    #expect(result.outcomes.count == 1)
    #expect(result.outcomes[0].ok)
    #expect(result.outcomes[0].observationRequired)
    #expect(proofCallsAtDown > 0)
    #expect(proofCalls == proofCallsAtDown)
    #expect(poster.events.map(\.event) == [
        .mouseDown(point: point, clickCount: 1),
        .mouseUp(point: point, clickCount: 1)
    ])
    #expect(held.heldCount == 0)
}

@Test func popupPointerSlowVisualProofCrossingDeadlineCannotPostMouseDown() throws {
    let expected = pidGuard()
    let point = CGPoint(x: 115, y: 220)
    var time: TimeInterval = 0
    let validator = PopupPointerClickGuardValidator(
        sealed: expected, authorizedPoint: point, popupProof: {
            time = 13
            return true
        }
    )
    let poster = RecordingPIDPoster()
    let activity = PIDActivityMonitor()
    let held = HeldInputRegistry()
    let executor = PIDTargetedActionExecutor(
        poster: poster, genericForegroundEnabled: true, activity: activity,
        validator: validator, heldInputs: held, element: { _, _ in nil },
        now: { time }
    )
    let plan = try executor.preflight(
        expected: expected,
        application: PIDTargetApplication(bundleIdentifier: "unlisted.popup", version: "1"),
        marker: activity.currentLeaseForTest().marker,
        entries: [genericForegroundWindowEntry()]
    )
    let result = executor.executePrepared(
        sourceIndex: 0, from: plan, expected: expected,
        lease: activity.currentLeaseForTest()
    )
    #expect(result.error == .actionTimeout)
    #expect(result.lastAcknowledgedAction == -1)
    #expect(poster.events.isEmpty)
    #expect(held.heldCount == 0)
}

@Test(arguments: ["moved", "ax_lags", "cg_lags", "ax_jumps", "ax_resizes", "resized", "other_window", "jumped", "other_pid", "new_overlay"])
func genericForegroundDragAllowsOnlyItsCapturedWindowTranslation(change: String) throws {
    let expected = pidGuard()
    let poster = RecordingPIDPoster()
    let activity = PIDActivityMonitor()
    let held = HeldInputRegistry()
    var bounds = expected.bounds
    var axBounds = expected.focusedAXBounds
    var windowID = expected.windowID
    var pid = expected.pid
    var root: FocusedRootPreference = .selectedWindow
    let validator = ExactPIDActionGuardValidator(state: {
        PIDActionTargetState(target: ActionTargetState(
            pid: pid, windowID: windowID, bounds: bounds, axIdentity: expected.axIdentity,
            focusedAXBounds: axBounds, focusedRootPreference: root), snapshotID: expected.snapshotID,
            isFrontmost: true, isKeyWindow: true)
    })
    let start = CGPoint(x: 120, y: 220)
    poster.onPost = { event in
        guard case let .mouseDragged(point) = event else { return }
        bounds = expected.bounds.offsetBy(dx: point.x - start.x, dy: point.y - start.y)
        axBounds = bounds
        if change == "ax_lags" { axBounds = expected.focusedAXBounds }
        if change == "cg_lags" { bounds = expected.bounds }
        if change == "ax_jumps" { axBounds.origin.x += 20 }
        if change == "ax_resizes" { axBounds.size.width += 1 }
        switch change {
        case "resized": bounds.size.width += 1
        case "other_window": windowID += 1
        case "jumped": bounds.origin.x += 20
        case "other_pid": pid += 1
        case "new_overlay": root = .containedOverlay
        default: break
        }
    }
    let executor = PIDTargetedActionExecutor(
        poster: poster, genericForegroundEnabled: true, activity: activity,
        validator: validator, heldInputs: held, element: { _, _ in nil },
        evidence: { _, guardValue in (try? validator.revalidate(expected: guardValue, point: nil)) != nil },
        delay: { _ in }
    )
    let entry = PlannedDispatchEntry(
        sourceIndex: 0, source: .drag(x: 20, y: 20, endX: 80, endY: 80, durationMS: 300),
        backend: .foregroundPointer, actionClass: .drag, resolved: nil,
        pointerSafeRegion: PointerSafeRegionAuthority(reference: foregroundWindowRegionReference,
            identityToken: expected.snapshotID, bounds: CGRect(origin: .zero, size: expected.bounds.size)))
    let plan = try executor.preflight(expected: expected,
        application: PIDTargetApplication(bundleIdentifier: "unlisted.app", version: "99"),
        marker: activity.currentLeaseForTest().marker, entries: [entry])
    let result = executor.executePrepared(sourceIndex: 0, from: plan, expected: expected,
        lease: activity.currentLeaseForTest())
    if ["moved", "ax_lags", "cg_lags"].contains(change) {
        #expect(result.error == nil)
        #expect(result.lastAcknowledgedAction == 0)
        #expect(poster.events.count == 32)
        #expect(bounds.origin == (change == "cg_lags" ? expected.bounds.origin : CGPoint(x: 160, y: 260)))
    } else {
        #expect(result.error == .unknownOutcome)
        #expect(result.lastAcknowledgedAction == -1)
        #expect(poster.events.count == 3)
    }
    #expect(held.heldCount == 0)
    #expect(!result.cleanupFailed)
    if case .mouseUp = poster.events.last?.event {} else { Issue.record("Missing held-button release") }
}

@Test(arguments: [Int32(600), Int32(-600), Int32.min, Int32.max])
func scrollEncoderMapsPositiveContentOffsetsToDownAndRight(delta: Int32) throws {
    var delivered: CGEvent?
    let poster = CGPIDTargetedInputPoster(preflightAccess: { true }, deliver: { event, _ in delivered = event })
    if delta == Int32.min {
        #expect(throws: (any Error).self) {
            try poster.post(.scroll(point: .zero, deltaX: delta, deltaY: delta), to: 777, marker: 123)
        }
        #expect(delivered == nil)
        return
    }
    try poster.post(.scroll(point: .zero, deltaX: delta, deltaY: delta), to: 777, marker: 123)
    let event = try #require(delivered)
    #expect(event.getIntegerValueField(.scrollWheelEventPointDeltaAxis1) == -Int64(delta))
    #expect(event.getIntegerValueField(.scrollWheelEventPointDeltaAxis2) == -Int64(delta))
    let cocoa = try #require(NSEvent(cgEvent: event))
    #expect(cocoa.hasPreciseScrollingDeltas)
    #expect(cocoa.scrollingDeltaY == -CGFloat(delta))
    #expect(cocoa.scrollingDeltaX == -CGFloat(delta))
}

@Test func dragEncoderCarriesRelativeMotionForTheExactPointerSequence() throws {
    var received: [CGEvent] = []
    let poster = CGPIDTargetedInputPoster(preflightAccess: { true }, deliver: { event, _ in received.append(event) })
    try poster.post(.mouseDown(point: CGPoint(x: 550, y: 126), clickCount: 1), to: 777, marker: 123)
    try poster.post(.mouseDragged(point: CGPoint(x: 552, y: 128)), to: 777, marker: 123)
    try poster.post(.mouseDragged(point: CGPoint(x: 550, y: 132)), to: 777, marker: 123)
    try poster.post(.mouseUp(point: CGPoint(x: 550, y: 132), clickCount: 1), to: 777, marker: 123)
    try poster.post(.mouseDragged(point: CGPoint(x: 100, y: 200)), to: 777, marker: 123)
    #expect(received[1].getDoubleValueField(.mouseEventDeltaX) == 2)
    #expect(received[1].getDoubleValueField(.mouseEventDeltaY) == 2)
    #expect(received[2].getDoubleValueField(.mouseEventDeltaX) == -2)
    #expect(received[2].getDoubleValueField(.mouseEventDeltaY) == 4)
    #expect(received[4].getDoubleValueField(.mouseEventDeltaX) == 0)
    #expect(received[4].getDoubleValueField(.mouseEventDeltaY) == 0)
    try poster.post(.mouseDown(point: .zero, clickCount: 1), to: 777, marker: 123)
    try poster.post(.mouseDragged(point: CGPoint(x: 10, y: 20)), to: 778, marker: 123)
    try poster.post(.mouseDragged(point: CGPoint(x: 10, y: 20)), to: 777, marker: 124)
    #expect(received.suffix(2).allSatisfy { $0.getDoubleValueField(.mouseEventDeltaX) == 0 && $0.getDoubleValueField(.mouseEventDeltaY) == 0 })
}


// A waived status strip still must not receive a background click meant for the page below it.
@Test func backgroundPointerValidatorRefusesPointsInsideExcludedRegions() throws {
    let bounds = CGRect(x: 25, y: 30, width: 1319, height: 768)
    let guardValue = ActionGuard(pid: 42, windowID: 273, bounds: bounds, axIdentity: 5,
        snapshotID: "snapshot", interactionMode: .background)
    let validator = BackgroundPIDActionGuardValidator(state: {
        PIDActionTargetState(target: ActionTargetState(pid: 42, windowID: 273, bounds: bounds, axIdentity: 5,
                                                       focusedAXIdentity: 5, focusedAXBounds: bounds),
                             snapshotID: "snapshot", isFrontmost: false, isKeyWindow: false)
    }, pointerExclusions: { [CGRect(x: 28, y: 771, width: 437, height: 24)] })
    try validator.revalidate(expected: guardValue, point: CGPoint(x: 400, y: 400))
    try validator.revalidate(expected: guardValue, point: nil)
    #expect(throws: ActionExecutionError.outOfBounds) {
        try validator.revalidate(expected: guardValue, point: CGPoint(x: 100, y: 780))
    }
}
