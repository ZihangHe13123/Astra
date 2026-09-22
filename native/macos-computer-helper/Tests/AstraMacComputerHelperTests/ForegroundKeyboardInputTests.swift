@testable import AstraMacComputerHelperCore
import CoreGraphics
import Foundation
import Testing

@Test func foregroundKeyboardFailureReceiptSerializesOriginalCauseAndPauseWithoutEffectClaim() {
    let diagnostics = KeyboardFailureDiagnostics(
        stage: .beforeKeyUp, cause: .staleSnapshot, userActivityPaused: false,
        inputMayHaveStarted: true, cleanupFailed: false
    )
    #expect(ActionOutcome(index: 0, ok: false, error: .unknownOutcome,
        inputDiagnostics: diagnostics).asJSON() == .object([
            "index": .number(0), "ok": .bool(false), "error_code": .string("unknown_outcome"),
            "input_diagnostics": .object([
                "stage": .string("before_key_up"), "cause": .string("stale_snapshot"),
                "input_may_have_started": .bool(true), "cleanup_failed": .bool(false),
            ]),
        ]))
    let pause = KeyboardFailureDiagnostics(
        stage: .beforeKeyDown, cause: nil, userActivityPaused: true,
        inputMayHaveStarted: false, cleanupFailed: false
    )
    #expect(ActionOutcome(index: 0, ok: false, error: nil,
        inputDiagnostics: pause).asJSON() == .object([
            "index": .number(0), "ok": .bool(false), "error_code": .string("user_activity_paused"),
            "input_diagnostics": .object([
                "stage": .string("before_key_down"), "cause": .string("user_activity_paused"),
                "input_may_have_started": .bool(false), "cleanup_failed": .bool(false),
            ]),
        ]))
}

@Test func foregroundKeyboardPreflightRequiresExactFocusNonsecureStateAndRegistry() throws {
    let fixture = KeyboardExecutorFixture()
    let entry = keyboardEntry(.keypress(key: "a", modifiers: ["command"]))

    _ = try fixture.executor.preflight(
        expected: fixture.guardValue,
        application: fixture.application,
        marker: fixture.lease.marker,
        entries: [entry]
    )

    fixture.focus = KeyboardFocusAuthority(
        identityToken: "ax:changed",
        bounds: fixture.focus.bounds,
        role: fixture.focus.role,
        subrole: fixture.focus.subrole
    )
    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try fixture.executor.preflight(
            expected: fixture.guardValue,
            application: fixture.application,
            marker: fixture.lease.marker,
            entries: [entry]
        )
    }

    fixture.focus = fixture.expectedFocus
    fixture.secureInput.enabled = true
    #expect(throws: ActionExecutionError.secureTarget) {
        _ = try fixture.executor.preflight(
            expected: fixture.guardValue,
            application: fixture.application,
            marker: fixture.lease.marker,
            entries: [entry]
        )
    }

    let secure = KeyboardExecutorFixture(role: "AXSecureTextField")
    #expect(throws: ActionExecutionError.secureTarget) {
        _ = try secure.executor.preflight(
            expected: secure.guardValue,
            application: secure.application,
            marker: secure.lease.marker,
            entries: [entry]
        )
    }

    let secureSubrole = KeyboardExecutorFixture(subrole: "AXSecureInputSubrole")
    #expect(throws: ActionExecutionError.secureTarget) {
        _ = try secureSubrole.executor.preflight(
            expected: secureSubrole.guardValue,
            application: secureSubrole.application,
            marker: secureSubrole.lease.marker,
            entries: [entry]
        )
    }

    let disabled = KeyboardExecutorFixture(keyChords: [])
    #expect(throws: ForegroundKeyboardFailure.compatibilityDisabled) {
        _ = try disabled.executor.preflight(
            expected: disabled.guardValue,
            application: disabled.application,
            marker: disabled.lease.marker,
            entries: [entry]
        )
    }
    #expect(fixture.poster.events.isEmpty)
}

@Test(arguments: ["type", "keypress"])
func foregroundTargetedInputFocusMismatchStopsBeforePosting(kind: String) throws {
    let fixture = KeyboardExecutorFixture()
    let entry = keyboardEntry(
        kind == "type" ? .type(text: "must-not-post", elementRef: "target")
            : .keypress(key: "a", modifiers: ["command"], elementRef: "target"),
        targetKeyboardFocus: KeyboardFocusAuthority(
            identityToken: "ax:different-target",
            bounds: fixture.expectedFocus.bounds,
            role: fixture.expectedFocus.role,
            subrole: fixture.expectedFocus.subrole
        )
    )

    #expect(throws: ActionExecutionError.inputFocusRequired) {
        _ = try fixture.executor.preflight(
            expected: fixture.guardValue,
            application: fixture.application,
            marker: fixture.lease.marker,
            entries: [entry]
        )
    }
    #expect(fixture.poster.events.isEmpty)
}

@Test func foregroundMixedTargetedTextAndWindowChordRejectsSnapshotFocusMismatchBeforePosting() throws {
    let fixture = KeyboardExecutorFixture()
    let targetFocus = KeyboardFocusAuthority(
        identityToken: "ax:target",
        bounds: fixture.expectedFocus.bounds,
        role: fixture.expectedFocus.role,
        subrole: fixture.expectedFocus.subrole
    )
    fixture.focus = targetFocus
    let entries = [
        keyboardEntry(
            .type(text: "must-not-post", elementRef: "target"),
            targetKeyboardFocus: targetFocus
        ),
        keyboardEntry(.keypress(key: "a", modifiers: ["command"]), sourceIndex: 1),
    ]

    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try fixture.executor.preflight(
            expected: fixture.guardValue,
            application: fixture.application,
            marker: fixture.lease.marker,
            entries: entries
        )
    }
    #expect(fixture.poster.events.isEmpty)
}

@Test(arguments: ["type", "keypress"])
func foregroundNamedInputWithoutTargetAuthorityCannotBecomeWindowInput(kind: String) throws {
    let fixture = KeyboardExecutorFixture()
    let entry = keyboardEntry(
        kind == "type" ? .type(text: "must-not-post", elementRef: "target")
            : .keypress(key: "a", modifiers: ["command"], elementRef: "target")
    )

    #expect(throws: ActionExecutionError.inputFocusRequired) {
        _ = try fixture.executor.preflight(
            expected: fixture.guardValue,
            application: fixture.application,
            marker: fixture.lease.marker,
            entries: [entry]
        )
    }
    #expect(fixture.poster.events.isEmpty)
}

@Test func foregroundTargetedTextValidatorFailureStaysStaleBeforePosting() throws {
    let fixture = KeyboardExecutorFixture()
    fixture.validator.failureAt = 1
    let entry = keyboardEntry(
        .type(text: "must-not-post", elementRef: "target"),
        targetKeyboardFocus: fixture.expectedFocus
    )

    #expect(throws: ActionExecutionError.staleSnapshot) {
        _ = try fixture.executor.preflight(
            expected: fixture.guardValue,
            application: fixture.application,
            marker: fixture.lease.marker,
            entries: [entry]
        )
    }
    #expect(fixture.poster.events.isEmpty)
}

@Test func foregroundKeyboardPostsExactPIDMarkerAndBalancesKeyState() throws {
    let fixture = KeyboardExecutorFixture()
    let entry = keyboardEntry(.keypress(key: "a", modifiers: ["command"]))
    let prepared = try fixture.executor.preflight(
        expected: fixture.guardValue,
        application: fixture.application,
        marker: fixture.lease.marker,
        entries: [entry]
    )

    let result = fixture.executor.executePrepared(
        sourceIndex: 0,
        from: prepared,
        expected: fixture.guardValue,
        lease: fixture.lease
    )

    #expect(result.error == nil)
    #expect(result.cooperativeError == nil)
    #expect(result.lastAcknowledgedAction == 0)
    #expect(fixture.poster.events.map(\.event) == [
        .virtualKeyDown(0, .maskCommand),
        .virtualKeyUp(0, .maskCommand),
    ])
    #expect(fixture.poster.events.allSatisfy { $0.pid == fixture.guardValue.pid && $0.marker == fixture.lease.marker })
    #expect(fixture.heldInputs.heldCount == 0)
    #expect(fixture.validator.calls == 3)
}

@Test func foregroundKeyboardAliasesUseTheSameApprovedChordAndPhysicalKey() throws {
    for (alias, key, code) in [
        ("ArrowLeft", "left", CGKeyCode(123)), ("ArrowRight", "right", CGKeyCode(124)),
        ("ArrowDown", "down", CGKeyCode(125)), ("ArrowUp", "up", CGKeyCode(126)),
        ("Esc", "escape", CGKeyCode(53)), ("Enter", "return", CGKeyCode(36)),
    ] {
        let fixture = KeyboardExecutorFixture(keyChords: [ApprovedKeyChord(rawValue: key)!])
        let action = try NativeAction.parse(.object(["type": .string("keypress"), "key": .string(alias)]))
        #expect(ApprovedKeyChord(action: action)?.rawValue == key)
        let plan = try fixture.executor.preflight(
            expected: fixture.guardValue, application: fixture.application,
            marker: fixture.lease.marker, entries: [keyboardEntry(action)]
        )
        let result = fixture.executor.executePrepared(
            sourceIndex: 0, from: plan, expected: fixture.guardValue, lease: fixture.lease
        )
        #expect(result.error == nil)
        #expect(fixture.poster.events.map(\.event) == [.virtualKeyDown(code, []), .virtualKeyUp(code, [])])
    }
}

@Test func foregroundKeyboardOrdinaryFocusTransitionAcknowledgesPairAndRequiresObservation() throws {
    // Escape opens/closes a menu, Tab changes controls, Return opens a dialog.
    // None of these transitions requires user input or a secure target.
    for (key, code) in [("escape", CGKeyCode(53)), ("tab", CGKeyCode(48)), ("return", CGKeyCode(36))] {
        let fixture = KeyboardExecutorFixture(genericForegroundEnabled: true)
        fixture.poster.afterPost = { call in
            if call == 0 {
                fixture.focusObservation = .authority(KeyboardFocusAuthority(
                    identityToken: "ax:new-control", bounds: fixture.expectedFocus.bounds,
                    role: "AXButton", subrole: nil
                ))
            }
        }
        let prepared = try fixture.executor.preflight(
            expected: fixture.guardValue, application: fixture.application,
            marker: fixture.lease.marker, entries: [keyboardEntry(.keypress(key: key))]
        )
        let result = fixture.executor.executePrepared(
            sourceIndex: 0, from: prepared, expected: fixture.guardValue, lease: fixture.lease
        )
        #expect(result.error == nil)
        #expect(result.lastAcknowledgedAction == 0)
        #expect(result.outcomes == [ActionOutcome(index: 0, ok: true, error: nil, observationRequired: true)])
        #expect(result.outcomes.first?.asJSON() == .object([
            "index": .number(0), "ok": .bool(true), "observation_required": .bool(true),
        ]))
        #expect(fixture.poster.events.map(\.event) == [
            .virtualKeyDown(code, []), .virtualKeyUp(code, []),
        ])
        #expect(fixture.heldInputs.heldCount == 0)
    }
}

@Test func foregroundKeyboardUncertainFocusAfterDownNeverBecomesCheckpoint() throws {
    for observation: KeyboardFocusObservation in [
        .secure, .secureOrIndeterminate, .stale,
        .authority(KeyboardFocusAuthority(identityToken: "new", bounds: CGRect(x: 20, y: 30, width: 200, height: 100),
            role: "AXUnknown", subrole: nil)),
    ] {
        let fixture = KeyboardExecutorFixture()
        fixture.poster.afterPost = { if $0 == 0 { fixture.focusObservation = observation } }
        let plan = try fixture.executor.preflight(expected: fixture.guardValue, application: fixture.application,
            marker: fixture.lease.marker, entries: [keyboardEntry(.keypress(key: "return"))])
        let result = fixture.executor.executePrepared(sourceIndex: 0, from: plan,
            expected: fixture.guardValue, lease: fixture.lease)
        #expect(result.error == .unknownOutcome)
        #expect(result.outcomes.first?.observationRequired == false)
        #expect(result.outcomes.first?.inputDiagnostics?.stage == .afterKeyUp)
        #expect(fixture.poster.events.map(\.event) == [.virtualKeyDown(36, []), .virtualKeyUp(36, [])])
        #expect(fixture.heldInputs.heldCount == 0)
    }
}

@Test func foregroundKeyboardTextFocusChangeReleasesFirstCharacterAndStopsBeforeNext() throws {
    for targeted in [false, true] {
        let fixture = KeyboardExecutorFixture()
        fixture.poster.afterPost = { call in
            if call == 0 {
                fixture.focus = KeyboardFocusAuthority(identityToken: "new", bounds: fixture.expectedFocus.bounds,
                    role: "AXTextField", subrole: nil)
            }
        }
        let entry = keyboardEntry(.type(text: "AB"), targetKeyboardFocus: targeted ? fixture.expectedFocus : nil)
        let plan = try fixture.executor.preflight(expected: fixture.guardValue, application: fixture.application,
            marker: fixture.lease.marker, entries: [entry])
        let result = fixture.executor.executePrepared(sourceIndex: 0, from: plan,
            expected: fixture.guardValue, lease: fixture.lease)
        #expect(result.error == .unknownOutcome)
        #expect(result.outcomes.first?.inputDiagnostics?.cause == (targeted ? .inputFocusRequired : .staleSnapshot))
        #expect(result.outcomes.first?.inputDiagnostics?.stage == .afterKeyUp)
        #expect(result.outcomes.first?.observationRequired == false)
        #expect(fixture.poster.events.map(\.event) == [.unicodeKeyDown([65]), .unicodeKeyUp([65])])
        #expect(fixture.heldInputs.heldCount == 0)
    }
}

@Test func foregroundKeyboardTextReflowKeepsIdentityAfterInputStarts() throws {
    for targeted in [false, true] {
        for beforeInput in [false, true] {
            let fixture = KeyboardExecutorFixture()
            let changed = KeyboardFocusAuthority(
                identityToken: fixture.expectedFocus.identityToken,
                bounds: CGRect(x: 20, y: 25, width: 200, height: 95),
                role: fixture.expectedFocus.role, subrole: fixture.expectedFocus.subrole
            )
            let entry = keyboardEntry(.type(text: "AB"), targetKeyboardFocus: targeted ? fixture.expectedFocus : nil)
            let plan = try fixture.executor.preflight(expected: fixture.guardValue, application: fixture.application,
                marker: fixture.lease.marker, entries: [entry])
            if beforeInput {
                fixture.focus = changed
            } else {
                fixture.poster.afterPost = { if $0 == 0 { fixture.focus = changed } }
            }
            let result = fixture.executor.executePrepared(sourceIndex: 0, from: plan,
                expected: fixture.guardValue, lease: fixture.lease)
            if beforeInput {
                // An old snapshot never authorizes the initial keystroke.
                #expect(result.error == (targeted ? .inputFocusRequired : .staleSnapshot))
                #expect(fixture.poster.events.isEmpty)
            } else {
                #expect(result.error == nil)
                #expect(result.lastAcknowledgedAction == 0)
                #expect(fixture.poster.events.map(\.event) == [
                    .unicodeKeyDown([65]), .unicodeKeyUp([65]), .unicodeKeyDown([66]), .unicodeKeyUp([66]),
                ])
            }
        }
    }
}

@Test func foregroundKeyboardChunkingKeepsEditorControlCharactersSeparate() throws {
    let fixture = KeyboardExecutorFixture(experimentalUnicodeChunkGraphemes: 8)
    let plan = try fixture.executor.preflight(expected: fixture.guardValue, application: fixture.application,
        marker: fixture.lease.marker, entries: [keyboardEntry(.type(text: "AB\n\n中文\tCD\r\nEF"))])
    let result = fixture.executor.executePrepared(sourceIndex: 0, from: plan,
        expected: fixture.guardValue, lease: fixture.lease)
    #expect(result.error == nil)
    #expect(fixture.poster.events.map(\.event) == [
        .unicodeKeyDown(Array("AB".utf16)), .unicodeKeyUp(Array("AB".utf16)),
        .virtualKeyDown(36, .maskShift), .virtualKeyUp(36, .maskShift),
        .virtualKeyDown(36, .maskShift), .virtualKeyUp(36, .maskShift),
        .unicodeKeyDown(Array("中文".utf16)), .unicodeKeyUp(Array("中文".utf16)),
        .unicodeKeyDown([9]), .unicodeKeyUp([9]),
        .unicodeKeyDown(Array("CD".utf16)), .unicodeKeyUp(Array("CD".utf16)),
        .virtualKeyDown(36, .maskShift), .virtualKeyUp(36, .maskShift),
        .unicodeKeyDown(Array("EF".utf16)), .unicodeKeyUp(Array("EF".utf16)),
    ])
}

@Test func foregroundKeyboardTextReflowStillRejectsChangedRoleOrUntrustedBounds() throws {
    for targeted in [false, true] {
        for invalidRole in [false, true] {
            let fixture = KeyboardExecutorFixture()
            fixture.poster.afterPost = { call in
                if call == 0 {
                    fixture.focus = KeyboardFocusAuthority(
                        identityToken: fixture.expectedFocus.identityToken,
                        bounds: invalidRole ? fixture.expectedFocus.bounds : CGRect(x: -1, y: 25, width: 200, height: 95),
                        role: invalidRole ? "AXButton" : fixture.expectedFocus.role,
                        subrole: fixture.expectedFocus.subrole
                    )
                }
            }
            let entry = keyboardEntry(.type(text: "AB"), targetKeyboardFocus: targeted ? fixture.expectedFocus : nil)
            let plan = try fixture.executor.preflight(expected: fixture.guardValue, application: fixture.application,
                marker: fixture.lease.marker, entries: [entry])
            let result = fixture.executor.executePrepared(sourceIndex: 0, from: plan,
                expected: fixture.guardValue, lease: fixture.lease)
            #expect(result.error == .unknownOutcome)
            #expect(fixture.poster.events.count == 2)
            #expect(fixture.heldInputs.heldCount == 0)
        }
    }
}

@Test func foregroundKeyboardUserActivityAfterDownReleasesBeforeStopping() throws {
    let fixture = KeyboardExecutorFixture()
    fixture.poster.afterPost = { if $0 == 0 { fixture.activity.paused = true } }
    let plan = try fixture.executor.preflight(expected: fixture.guardValue, application: fixture.application,
        marker: fixture.lease.marker, entries: [keyboardEntry(.keypress(key: "return"))])
    let result = fixture.executor.executePrepared(sourceIndex: 0, from: plan,
        expected: fixture.guardValue, lease: fixture.lease)
    #expect(result.cooperativeError == .userActivityPaused)
    #expect(result.error == .unknownOutcome)
    #expect(result.outcomes.first?.inputDiagnostics?.stage == .afterKeyUp)
    #expect(fixture.poster.events.map(\.event) == [.virtualKeyDown(36, []), .virtualKeyUp(36, [])])
    #expect(fixture.heldInputs.heldCount == 0)
}

@Test func foregroundKeyboardRegistersHeldStateBeforePossiblyStartedKeyDown() throws {
    let fixture = KeyboardExecutorFixture()
    fixture.poster.startedFailingCalls = [0]
    let entry = keyboardEntry(.keypress(key: "return"))
    let prepared = try fixture.executor.preflight(
        expected: fixture.guardValue,
        application: fixture.application,
        marker: fixture.lease.marker,
        entries: [entry]
    )

    let result = fixture.executor.executePrepared(
        sourceIndex: 0,
        from: prepared,
        expected: fixture.guardValue,
        lease: fixture.lease
    )

    #expect(result.error == .unknownOutcome)
    #expect(result.outcomes.first?.inputDiagnostics?.stage == .keyDown)
    #expect(result.outcomes.first?.inputDiagnostics?.cause == .helperFailed)
    #expect(result.outcomes.first?.inputDiagnostics?.inputMayHaveStarted == true)
    #expect(fixture.poster.events.map(\.event) == [
        .virtualKeyDown(36, []),
        .virtualKeyUp(36, []),
    ])
    #expect(fixture.heldInputs.heldCount == 0)
}

@Test func foregroundKeyboardTreatsUnknownDownFailureAsPossiblyStartedAndCleansUp() throws {
    let fixture = KeyboardExecutorFixture()
    fixture.poster.unknownFailingCalls = [0]
    let entry = keyboardEntry(.keypress(key: "return"))
    let prepared = try fixture.executor.preflight(
        expected: fixture.guardValue,
        application: fixture.application,
        marker: fixture.lease.marker,
        entries: [entry]
    )

    let result = fixture.executor.executePrepared(
        sourceIndex: 0,
        from: prepared,
        expected: fixture.guardValue,
        lease: fixture.lease
    )

    #expect(result.error == .unknownOutcome)
    #expect(fixture.poster.events.map(\.event) == [
        .virtualKeyDown(36, []),
        .virtualKeyUp(36, []),
    ])
    #expect(fixture.heldInputs.heldCount == 0)
}

@Test func foregroundKeyboardSecureTransitionCannotBlockMatchingCleanupKeyUp() throws {
    let fixture = KeyboardExecutorFixture()
    fixture.poster.afterPost = { call in
        if call == 0 { fixture.secureInput.enabled = true }
    }
    let entry = keyboardEntry(.keypress(key: "return"))
    let prepared = try fixture.executor.preflight(
        expected: fixture.guardValue,
        application: fixture.application,
        marker: fixture.lease.marker,
        entries: [entry]
    )

    let result = fixture.executor.executePrepared(
        sourceIndex: 0,
        from: prepared,
        expected: fixture.guardValue,
        lease: fixture.lease
    )

    #expect(result.error == .unknownOutcome)
    #expect(fixture.poster.events.map(\.event) == [
        .virtualKeyDown(36, []),
        .virtualKeyUp(36, []),
    ])
    #expect(fixture.heldInputs.heldCount == 0)
}

@Test func foregroundKeyboardClassifiesSecureAndIndeterminateFocusBeforeAuthorityMismatch() throws {
    let transitions: [KeyboardFocusObservation] = [
        .authority(KeyboardFocusAuthority(
            identityToken: "ax:secure-role",
            bounds: CGRect(x: 20, y: 30, width: 200, height: 100),
            role: "AXSecureTextField",
            subrole: nil
        )),
        .authority(KeyboardFocusAuthority(
            identityToken: "ax:secure-subrole",
            bounds: CGRect(x: 20, y: 30, width: 200, height: 100),
            role: "AXTextField",
            subrole: "AXSecureTextField"
        )),
        .secureOrIndeterminate,
    ]

    for transition in transitions {
        let fixture = KeyboardExecutorFixture()
        let entry = keyboardEntry(
            .type(text: "must-not-post", elementRef: "target"),
            targetKeyboardFocus: fixture.expectedFocus
        )
        let prepared = try fixture.executor.preflight(
            expected: fixture.guardValue,
            application: fixture.application,
            marker: fixture.lease.marker,
            entries: [entry]
        )
        fixture.focusObservation = transition

        let result = fixture.executor.executePrepared(
            sourceIndex: 0,
            from: prepared,
            expected: fixture.guardValue,
            lease: fixture.lease
        )

        #expect(result.error == .secureTarget)
        #expect(result.lastAcknowledgedAction == -1)
        #expect(fixture.poster.events.isEmpty)
    }
}

@Test func foregroundTargetedTextFocusLossAfterKeyDownIsUnknownOutcome() throws {
    let fixture = KeyboardExecutorFixture()
    let entry = keyboardEntry(
        .type(text: "A", elementRef: "target"),
        targetKeyboardFocus: fixture.expectedFocus
    )
    let prepared = try fixture.executor.preflight(
        expected: fixture.guardValue,
        application: fixture.application,
        marker: fixture.lease.marker,
        entries: [entry]
    )
    fixture.poster.afterPost = { call in
        guard call == 0 else { return }
        fixture.focus = KeyboardFocusAuthority(
            identityToken: "ax:focus-lost",
            bounds: fixture.expectedFocus.bounds,
            role: fixture.expectedFocus.role,
            subrole: fixture.expectedFocus.subrole
        )
    }

    let result = fixture.executor.executePrepared(
        sourceIndex: 0,
        from: prepared,
        expected: fixture.guardValue,
        lease: fixture.lease
    )

    #expect(result.error == .unknownOutcome)
    #expect(result.lastAcknowledgedAction == -1)
    #expect(fixture.poster.events.map(\.event) == [
        .unicodeKeyDown(Array("A".utf16)),
        .unicodeKeyUp(Array("A".utf16)),
    ])
}

@Test func foregroundKeyboardRevalidatesAfterMatchingKeyUpAndKeepsTargetLossUncertain() throws {
    let fixture = KeyboardExecutorFixture()
    let entry = keyboardEntry(.keypress(key: "return"))
    let prepared = try fixture.executor.preflight(
        expected: fixture.guardValue,
        application: fixture.application,
        marker: fixture.lease.marker,
        entries: [entry]
    )
    fixture.validator.failureAt = 3

    let result = fixture.executor.executePrepared(
        sourceIndex: 0,
        from: prepared,
        expected: fixture.guardValue,
        lease: fixture.lease
    )

    #expect(result.error == .unknownOutcome)
    #expect(result.lastAcknowledgedAction == -1)
    #expect(fixture.poster.events.map(\.event) == [
        .virtualKeyDown(36, []),
        .virtualKeyUp(36, []),
    ])
    #expect(fixture.heldInputs.heldCount == 0)
}

@Test func foregroundKeyboardTextUsesBalancedUnicodePairsAndPausesBetweenCharacters() throws {
    let activity = KeyboardActivity(pauseOnEventCall: 3)
    let fixture = KeyboardExecutorFixture(activity: activity)
    let entry = keyboardEntry(.type(text: "A🙂B"))
    let prepared = try fixture.executor.preflight(
        expected: fixture.guardValue,
        application: fixture.application,
        marker: fixture.lease.marker,
        entries: [entry]
    )

    let result = fixture.executor.executePrepared(
        sourceIndex: 0,
        from: prepared,
        expected: fixture.guardValue,
        lease: fixture.lease
    )

    #expect(result.error == .unknownOutcome)
    #expect(result.cooperativeError == .userActivityPaused)
    #expect(fixture.poster.events.map(\.event) == [
        .unicodeKeyDown(Array("A".utf16)),
        .unicodeKeyUp(Array("A".utf16)),
    ])
    #expect(fixture.heldInputs.heldCount == 0)
}

@Test func foregroundKeyboardTargetLossBeforeInputIsStaleButAfterDownIsUnknown() throws {
    let before = KeyboardExecutorFixture()
    let entry = keyboardEntry(.keypress(key: "return"))
    let beforePlan = try before.executor.preflight(
        expected: before.guardValue,
        application: before.application,
        marker: before.lease.marker,
        entries: [entry]
    )
    before.validator.failureAt = 2
    let beforeResult = before.executor.executePrepared(
        sourceIndex: 0,
        from: beforePlan,
        expected: before.guardValue,
        lease: before.lease
    )
    #expect(beforeResult.error == .staleSnapshot)
    #expect(beforeResult.outcomes.first?.inputDiagnostics?.stage == .beforeKeyDown)
    #expect(beforeResult.outcomes.first?.inputDiagnostics?.inputMayHaveStarted == false)
    #expect(before.poster.events.isEmpty)

    let after = KeyboardExecutorFixture()
    let afterPlan = try after.executor.preflight(
        expected: after.guardValue,
        application: after.application,
        marker: after.lease.marker,
        entries: [entry]
    )
    after.validator.failureAt = 3
    let afterResult = after.executor.executePrepared(
        sourceIndex: 0,
        from: afterPlan,
        expected: after.guardValue,
        lease: after.lease
    )
    #expect(afterResult.error == .unknownOutcome)
    #expect(afterResult.outcomes.first?.inputDiagnostics?.stage == .afterKeyUp)
    #expect(afterResult.outcomes.first?.inputDiagnostics?.cause == .staleSnapshot)
    #expect(after.poster.events.count == 2)
}

@Test func foregroundKeyboardTimeoutAndPosterFailureRespectPossibleInputBoundary() throws {
    let timedOut = KeyboardExecutorFixture(clockValues: [0, 13])
    let entry = keyboardEntry(.keypress(key: "return"))
    let timedOutPlan = try timedOut.executor.preflight(
        expected: timedOut.guardValue,
        application: timedOut.application,
        marker: timedOut.lease.marker,
        entries: [entry]
    )
    let timedOutResult = timedOut.executor.executePrepared(
        sourceIndex: 0,
        from: timedOutPlan,
        expected: timedOut.guardValue,
        lease: timedOut.lease
    )
    #expect(timedOutResult.error == .actionTimeout)
    #expect(timedOut.poster.events.isEmpty)

    let failedUp = KeyboardExecutorFixture()
    failedUp.poster.failingCalls = [1]
    let failedPlan = try failedUp.executor.preflight(
        expected: failedUp.guardValue,
        application: failedUp.application,
        marker: failedUp.lease.marker,
        entries: [entry]
    )
    let failedResult = failedUp.executor.executePrepared(
        sourceIndex: 0,
        from: failedPlan,
        expected: failedUp.guardValue,
        lease: failedUp.lease
    )
    #expect(failedResult.error == .unknownOutcome)
    #expect(failedResult.outcomes.first?.inputDiagnostics?.stage == .keyUp)
    #expect(failedResult.outcomes.first?.inputDiagnostics?.cleanupFailed == false)
    #expect(failedUp.poster.events.map(\.event) == [
        .virtualKeyDown(36, []),
        .virtualKeyUp(36, []),
        .virtualKeyUp(36, []),
    ])
}

@Test func foregroundKeyboardRetainsExactHeldReleaseUntilTerminalCleanupCanRetry() throws {
    let fixture = KeyboardExecutorFixture()
    fixture.poster.failingCalls = [1, 2]
    let entry = keyboardEntry(.keypress(key: "return"))
    let prepared = try fixture.executor.preflight(
        expected: fixture.guardValue,
        application: fixture.application,
        marker: fixture.lease.marker,
        entries: [entry]
    )

    let result = fixture.executor.executePrepared(
        sourceIndex: 0,
        from: prepared,
        expected: fixture.guardValue,
        lease: fixture.lease
    )

    #expect(result.error == .unknownOutcome)
    #expect(result.cleanupFailed)
    #expect(result.outcomes.first?.inputDiagnostics?.cleanupFailed == true)
    #expect(result.outcomes.first?.inputDiagnostics?.stage == .keyUp)
    #expect(fixture.heldInputs.heldCount == 1)

    fixture.poster.failingCalls = []
    let cleanup = fixture.heldInputs.cleanupAll(scope: fixture.activity.scope)

    #expect(cleanup == HeldInputCleanupResult(attempted: 1, released: 1, failed: 0))
    #expect(fixture.heldInputs.heldCount == 0)
    #expect(fixture.poster.events.map(\.event) == [
        .virtualKeyDown(36, []),
        .virtualKeyUp(36, []),
        .virtualKeyUp(36, []),
        .virtualKeyUp(36, []),
    ])
    #expect(fixture.poster.events.allSatisfy {
        $0.pid == fixture.guardValue.pid && $0.marker == fixture.lease.marker
    })
}

@Test func foregroundTakeoverBlindTypeWithoutFocusAuthorityDelegatesToOSFocus() throws {
    // WPS 型场景：takeover 下应用无可读 AX 焦点（guard 无焦点权威 + 活读 .stale）。
    // 无焦点目标的盲打字委托 OS 焦点投递，不再在 preflight 撞 staleSnapshot
    //（2026-09-04 WPS helper 端到端卡点；Python 侧 imeActiveBlindTypeUnderTakeover
    // DelegatesToOSFocusWhenCompatApproved 已定语义，执行层漏跟）。
    let fixture = KeyboardExecutorFixture()
    fixture.focusObservation = .stale
    let blindGuard = fixture.guardValue.replacingKeyboardFocus(nil)
    let entry = keyboardEntry(.type(text: "hi"))
    let prepared = try fixture.executor.preflight(
        expected: blindGuard,
        application: fixture.application,
        marker: fixture.lease.marker,
        entries: [entry]
    )
    let result = fixture.executor.executePrepared(
        sourceIndex: 0,
        from: prepared,
        expected: blindGuard,
        lease: fixture.lease
    )
    #expect(result.error == nil)
    #expect(result.cooperativeError == nil)
    #expect(fixture.poster.events.map(\.event) == [
        .unicodeKeyDown(Array("h".utf16)),
        .unicodeKeyUp(Array("h".utf16)),
        .unicodeKeyDown(Array("i".utf16)),
        .unicodeKeyUp(Array("i".utf16)),
    ])
}

@Test func foregroundTakeoverBlindTypeStillRefusesSecureInputEnabled() throws {
    // 委托 OS 焦点不得绕过系统级安全输入闸：SecureEventInput 开启时照样拒绝。
    let fixture = KeyboardExecutorFixture()
    fixture.focusObservation = .stale
    fixture.secureInput.enabled = true
    let blindGuard = fixture.guardValue.replacingKeyboardFocus(nil)
    let entry = keyboardEntry(.type(text: "hi"))
    #expect(throws: ActionExecutionError.secureTarget) {
        _ = try fixture.executor.preflight(
            expected: blindGuard,
            application: fixture.application,
            marker: fixture.lease.marker,
            entries: [entry]
        )
    }
}

@Test func takeoverRejectsObservedSecureSubroleWithoutSystemSecureInput() throws {
    let fixture = KeyboardExecutorFixture()
    let secure = makeKeyboardFocusObservation(
        expectedPID: 11, actualPID: 11, identityToken: "secure-field",
        bounds: fixture.expectedFocus.bounds,
        role: BoundedAXStringResult(value: "AXTextField", status: .complete),
        subrole: BoundedAXStringResult(value: "AXSecureTextField", status: .complete),
        enabled: true, containerBounds: fixture.guardValue.focusedAXBounds
    )
    fixture.focusObservation = secure
    let guardValue = deliveryKeyboardFocusGuard(
        base: fixture.guardValue.replacingKeyboardFocus(nil), liveFocus: { secure }
    )
    #expect(throws: ActionExecutionError.secureTarget) {
        _ = try fixture.executor.preflight(
            expected: guardValue, application: fixture.application,
            marker: fixture.lease.marker, entries: [keyboardEntry(.type(text: "no"))]
        )
    }
    #expect(fixture.poster.events.isEmpty)
}

@Test func takeoverBlindTypeStopsWhenFocusBecomesSecureAfterPreflight() throws {
    let fixture = KeyboardExecutorFixture()
    fixture.focusObservation = .stale
    let blindGuard = fixture.guardValue.replacingKeyboardFocus(nil)
    let prepared = try fixture.executor.preflight(
        expected: blindGuard, application: fixture.application,
        marker: fixture.lease.marker, entries: [keyboardEntry(.type(text: "no"))]
    )
    fixture.focusObservation = .authority(KeyboardFocusAuthority(
        identityToken: "secure-field", bounds: fixture.expectedFocus.bounds,
        role: "AXTextField", subrole: "AXSecureTextField"
    ))
    let result = fixture.executor.executePrepared(
        sourceIndex: 0, from: prepared, expected: blindGuard, lease: fixture.lease
    )
    #expect(result.error == .secureTarget)
    #expect(fixture.poster.events.isEmpty)
}

@Test func takeoverPreservesUnavailableAXFocusDelegation() throws {
    let fixture = KeyboardExecutorFixture()
    // Real WPS lookup currently returns this for missing AX focused element.
    fixture.focusObservation = .secureOrIndeterminate
    let blindGuard = fixture.guardValue.replacingKeyboardFocus(nil)
    let prepared = try fixture.executor.preflight(
        expected: blindGuard, application: fixture.application,
        marker: fixture.lease.marker, entries: [keyboardEntry(.type(text: "ok"))]
    )
    let result = fixture.executor.executePrepared(
        sourceIndex: 0, from: prepared, expected: blindGuard, lease: fixture.lease
    )
    #expect(result.error == nil)
    #expect(fixture.poster.events.count == 4)
}

@Test func KeyboardTextContinuityCompletesUnicodeAndRetainsObservationBoundary() throws {
    for changedAt in 0...5 {
        let changed = changedAt > 0
        var continuations = 0
        let fixture = KeyboardExecutorFixture(continuingTextFocus: { expected, wanted in
            #expect(expected.snapshotID == "snapshot")
            continuations += 1
            return KeyboardTextContinuationObservation(focus: .authority(wanted),
                observationRequired: continuations == changedAt)
        })
        let entry = keyboardEntry(.type(text: "A中B", elementRef: "field"), targetKeyboardFocus: fixture.expectedFocus)
        let plan = try fixture.executor.preflight(expected: fixture.guardValue,
            application: fixture.application, marker: fixture.lease.marker, entries: [entry])
        fixture.validator.failureAt = 3 // Old window-only guard cannot prove the popup transition.
        let result = fixture.executor.executePrepared(sourceIndex: 0, from: plan,
            expected: fixture.guardValue, lease: fixture.lease)
        #expect(result.error == nil)
        #expect(result.lastAcknowledgedAction == 0)
        #expect(result.outcomes.first?.observationRequired == changed)
        #expect(continuations == 5)
        let units: [[UInt16]] = [[65], [0x4e2d], [66]]
        #expect(fixture.poster.events.map(\.event) == units.flatMap {
            [SyntheticInputEvent.unicodeKeyDown($0), .unicodeKeyUp($0)]
        })
        #expect(fixture.poster.events.allSatisfy { $0.pid == fixture.guardValue.pid && $0.marker == fixture.lease.marker })
        #expect(fixture.validator.calls == 2) // Preflight and first down remain strict.
        #expect(fixture.heldInputs.heldCount == 0)
    }
}

@Test func KeyboardTextContinuityCannotAuthorizeFirstDownOrKeypress() throws {
    for textAction in [false, true] {
        var continuations = 0
        let fixture = KeyboardExecutorFixture(continuingTextFocus: { _, wanted in
            continuations += 1
            return KeyboardTextContinuationObservation(focus: .authority(wanted), observationRequired: true)
        })
        let entry = keyboardEntry(textAction ? .type(text: "abc") : .keypress(key: "return"))
        let plan = try fixture.executor.preflight(expected: fixture.guardValue,
            application: fixture.application, marker: fixture.lease.marker, entries: [entry])
        fixture.validator.failureAt = textAction ? 2 : 3
        let result = fixture.executor.executePrepared(sourceIndex: 0, from: plan,
            expected: fixture.guardValue, lease: fixture.lease)
        #expect(result.error == (textAction ? .staleSnapshot : .unknownOutcome))
        #expect(continuations == 0)
        #expect(fixture.poster.events.count == (textAction ? 0 : 2))
    }
}

@Test func KeyboardTextContinuityRejectsLostOrSecureFieldAfterRelease() throws {
    let other = KeyboardFocusAuthority(identityToken: "other-field",
        bounds: CGRect(x: 20, y: 30, width: 200, height: 100), role: "AXTextArea", subrole: nil)
    for focus: KeyboardFocusObservation in [.stale, .secure, .secureOrIndeterminate, .authority(other)] {
        let fixture = KeyboardExecutorFixture(continuingTextFocus: { _, _ in
            KeyboardTextContinuationObservation(focus: focus, observationRequired: true)
        })
        let entry = keyboardEntry(.type(text: "abc"))
        let plan = try fixture.executor.preflight(expected: fixture.guardValue,
            application: fixture.application, marker: fixture.lease.marker, entries: [entry])
        let result = fixture.executor.executePrepared(sourceIndex: 0, from: plan,
            expected: fixture.guardValue, lease: fixture.lease)
        #expect(result.error == .unknownOutcome)
        #expect(result.lastAcknowledgedAction == -1)
        #expect(result.outcomes.first?.observationRequired == false)
        #expect(fixture.poster.events.count == 2)
        #expect(fixture.heldInputs.heldCount == 0)
    }
}

@Test func KeyboardTextContinuityCannotBorrowAnotherFieldsAncestry() throws {
    var samples = ["wanted", "other", "wanted"]
    let fixture = KeyboardExecutorFixture(continuingTextFocus: { expected, wanted in
        let proof = provenContinuingTextFocus(expectedPreference: .selectedWindow,
            observedPreference: .containedOverlay, wanted: wanted, windowBounds: expected.bounds,
            readFocused: { samples.isEmpty ? nil : samples.removeFirst() },
            observe: { _ in .authority(wanted) }, belongs: { $0 == "other" }, same: { $0 == $1 })
        return KeyboardTextContinuationObservation(focus: proof ?? .stale, observationRequired: true)
    })
    let entry = keyboardEntry(.type(text: "abc"))
    let plan = try fixture.executor.preflight(expected: fixture.guardValue,
        application: fixture.application, marker: fixture.lease.marker, entries: [entry])
    let result = fixture.executor.executePrepared(sourceIndex: 0, from: plan,
        expected: fixture.guardValue, lease: fixture.lease)
    #expect(result.error == .unknownOutcome)
    #expect(result.lastAcknowledgedAction == -1)
    #expect(fixture.poster.events.count == 2)
    #expect(fixture.heldInputs.heldCount == 0)
}

@Test func KeyboardTextContinuityCannotMaskSecureInputActivityOrReleaseFailure() throws {
    for failure in ["secure", "activity", "release"] {
        let fixture = KeyboardExecutorFixture(
            activity: KeyboardActivity(pauseOnEventCall: failure == "activity" ? 2 : nil),
            continuingTextFocus: { _, wanted in
                KeyboardTextContinuationObservation(focus: .authority(wanted), observationRequired: true)
            })
        if failure == "release" { fixture.poster.failingCalls = [1] }
        if failure == "secure" {
            fixture.poster.afterPost = { if $0 == 0 { fixture.secureInput.enabled = true } }
        }
        let entry = keyboardEntry(.type(text: "abc"))
        let plan = try fixture.executor.preflight(expected: fixture.guardValue,
            application: fixture.application, marker: fixture.lease.marker, entries: [entry])
        let result = fixture.executor.executePrepared(sourceIndex: 0, from: plan,
            expected: fixture.guardValue, lease: fixture.lease)
        #expect(result.error == .unknownOutcome)
        #expect(result.lastAcknowledgedAction == -1)
        #expect(result.outcomes.first?.observationRequired == false)
        #expect((2...3).contains(fixture.poster.events.count))
    }
}

private final class KeyboardExecutorFixture {
    let application = PIDTargetApplication(bundleIdentifier: "com.example.Editor", version: "1.2.3")
    let expectedFocus: KeyboardFocusAuthority
    private let focusState: KeyboardFocusState
    var focus: KeyboardFocusAuthority {
        get { focusState.value }
        set { focusState.value = newValue }
    }
    var focusObservation: KeyboardFocusObservation {
        get { focusState.observation }
        set { focusState.observation = newValue }
    }
    let guardValue: ActionGuard
    let poster = RecordingKeyboardPoster()
    let validator = RecordingKeyboardValidator()
    let secureInput = KeyboardSecureInput()
    let activity: KeyboardActivity
    let heldInputs = HeldInputRegistry()
    let executor: ForegroundKeyboardExecutor
    var lease: UserActivitySessionLease { activity.lease }

    init(
        role: String = "AXTextArea",
        genericForegroundEnabled: Bool = false,
        emptyRegistry: Bool = false,
        subrole: String? = nil,
        keyChords: Set<ApprovedKeyChord> = [ApprovedKeyChord(rawValue: "command+a")!, ApprovedKeyChord(rawValue: "return")!],
        allowTextEntry: Bool = true,
        activity: KeyboardActivity = KeyboardActivity(),
        clockValues: [TimeInterval] = [0],
        clockStep: TimeInterval = 0,
        continuingTextFocus: ForegroundKeyboardExecutor.ContinuingTextFocusLookup? = nil,
        experimentalUnicodeChunkGraphemes: Int = 1
    ) {
        self.activity = activity
        expectedFocus = KeyboardFocusAuthority(
            identityToken: "ax:focus",
            bounds: CGRect(x: 20, y: 30, width: 200, height: 100),
            role: role,
            subrole: subrole
        )
        let focusState = KeyboardFocusState(expectedFocus)
        self.focusState = focusState
        guardValue = ActionGuard(
            pid: 11,
            windowID: 22,
            bounds: CGRect(x: 0, y: 0, width: 500, height: 400),
            axIdentity: 33,
            focusedAXIdentity: 33,
            focusedAXBounds: CGRect(x: 0, y: 0, width: 500, height: 400),
            keyboardFocus: expectedFocus,
            snapshotID: "snapshot",
            interactionMode: .foregroundTakeover
        )
        let registry = PIDInputCompatibilityRegistry(cells: [
            PIDInputCompatibilityCell(
                bundleIdentifier: application.bundleIdentifier,
                version: application.version,
                backend: .foregroundKeyboard,
                action: .text,
                allowedKeyChords: keyChords,
                allowTextEntry: allowTextEntry
            ),
        ])
        var clock = clockValues
        var last = clockValues.last ?? 0
        executor = ForegroundKeyboardExecutor(
            poster: poster,
            compatibility: emptyRegistry ? PIDInputCompatibilityRegistry() : registry,
            genericForegroundEnabled: genericForegroundEnabled,
            activity: activity,
            validator: validator,
            secureInput: secureInput,
            heldInputs: heldInputs,
            focus: { focusState.observation },
            continuingTextFocus: continuingTextFocus,
            now: {
                if !clock.isEmpty { last = clock.removeFirst() }
                defer { last += clockStep }
                return last
            },
            experimentalUnicodeChunkGraphemes: experimentalUnicodeChunkGraphemes
        )
    }
}

private final class KeyboardFocusState {
    var observation: KeyboardFocusObservation
    var value: KeyboardFocusAuthority {
        get { observation.authority! }
        set { observation = .authority(newValue) }
    }
    init(_ value: KeyboardFocusAuthority) { observation = .authority(value) }
}

private func keyboardEntry(
    _ action: NativeAction,
    sourceIndex: Int = 0,
    targetKeyboardFocus: KeyboardFocusAuthority? = nil
) -> PlannedDispatchEntry {
    PlannedDispatchEntry(
        sourceIndex: sourceIndex,
        source: action,
        backend: .foregroundKeyboard,
        actionClass: .text,
        resolved: nil,
        targetKeyboardFocus: targetKeyboardFocus
    )
}

private final class KeyboardSecureInput: SecureInputDetecting {
    var enabled = false
    func isSecureInputEnabled() -> Bool { enabled }
}

private final class RecordingKeyboardPoster: PIDTargetedInputPosting {
    struct Posted {
        let event: SyntheticInputEvent
        let pid: pid_t
        let marker: UInt64
    }
    var events: [Posted] = []
    var failingCalls: Set<Int> = []
    var startedFailingCalls: Set<Int> = []
    var unknownFailingCalls: Set<Int> = []
    var afterPost: ((Int) -> Void)?
    func preflight(targetPID: pid_t, marker: UInt64) -> Bool { targetPID > 0 && marker != 0 }
    func post(_ event: SyntheticInputEvent, to targetPID: pid_t, marker: UInt64) throws {
        let call = events.count
        events.append(Posted(event: event, pid: targetPID, marker: marker))
        afterPost?(call)
        if unknownFailingCalls.contains(call) {
            throw RecordingKeyboardPosterError.failed
        }
        if startedFailingCalls.contains(call) {
            throw SyntheticInputFailure(error: .helperFailed, inputStarted: true)
        }
        if failingCalls.contains(call) {
            throw SyntheticInputFailure(error: .helperFailed, inputStarted: false)
        }
    }
}

private enum RecordingKeyboardPosterError: Error {
    case failed
}

private final class RecordingKeyboardValidator: PIDActionGuardValidating {
    var calls = 0
    var failureAt: Int?
    var observation: KeyboardFocusObservation?
    func revalidateAndObserveFocus(expected: ActionGuard, point: CGPoint?) throws -> KeyboardFocusObservation? {
        try revalidate(expected: expected, point: point)
        return observation
    }
    func revalidate(expected _: ActionGuard, point _: CGPoint?) throws {
        calls += 1
        if calls == failureAt { throw ActionExecutionError.staleSnapshot }
    }
}

private final class KeyboardActivity: UserActivityMonitoring {
    let lease = UserActivitySessionLease(marker: 0xfeed_beef)
    let scope = HeldInputScope.cooperative(marker: 0xfeed_beef, generation: UUID())
    var paused = false
    var eventCalls = 0
    let pauseOnEventCall: Int?

    init(pauseOnEventCall: Int? = nil) { self.pauseOnEventCall = pauseOnEventCall }
    func arm(marker _: UInt64, notification _: UserActivityPauseSignal) throws -> UserActivitySessionLease { lease }
    func beginFragment(lease _: UserActivitySessionLease) throws {}
    func endFragment(lease _: UserActivitySessionLease) {}
    func assertNotPaused(lease candidate: UserActivitySessionLease) throws {
        guard candidate === lease, !paused else { throw UserActivityMonitoringError.paused }
    }
    func heldInputScope(lease candidate: UserActivitySessionLease) throws -> HeldInputScope {
        try assertNotPaused(lease: candidate)
        return scope
    }
    func performPIDEvent(
        lease candidate: UserActivitySessionLease,
        validate: () throws -> Void,
        mutation: () throws -> Void
    ) throws {
        eventCalls += 1
        if eventCalls == pauseOnEventCall { paused = true }
        try assertNotPaused(lease: candidate)
        try validate()
        try assertNotPaused(lease: candidate)
        try mutation()
    }
    func performPIDCleanup(lease _: UserActivitySessionLease, _ cleanup: () throws -> Void) throws { try cleanup() }
    func cleanupHeldInputs(lease _: UserActivitySessionLease) throws -> HeldInputCleanupResult {
        HeldInputCleanupResult(attempted: 0, released: 0, failed: 0)
    }
    func disarm(lease _: UserActivitySessionLease) throws -> Bool { paused }
}

@Test func foregroundKeyboardEarlierWaitDoesNotConsumeNewActionBudget() throws {
    let fixture = KeyboardExecutorFixture(clockValues: [0, 11, 11, 11])
    let prepared = try fixture.executor.preflight(expected: fixture.guardValue,
        application: fixture.application, marker: fixture.lease.marker,
        entries: [keyboardEntry(.keypress(key: "return"))])
    let result = fixture.executor.executePrepared(sourceIndex: 0, from: prepared,
        expected: fixture.guardValue, lease: fixture.lease)
    #expect(result.error == nil)
    #expect(fixture.poster.events.count == 2)
}

@Test func foregroundUnicodeChunksRequireExplicitInternalOptIn() throws {
    let fixture = KeyboardExecutorFixture(experimentalUnicodeChunkGraphemes: 2)
    let entry = keyboardEntry(.type(text: "a中🙂b"))
    let prepared = try fixture.executor.preflight(expected: fixture.guardValue,
        application: fixture.application, marker: fixture.lease.marker, entries: [entry])
    let result = fixture.executor.executePrepared(sourceIndex: 0, from: prepared,
        expected: fixture.guardValue, lease: fixture.lease)
    #expect(result.error == nil)
    #expect(fixture.poster.events.map(\.event) == [
        .unicodeKeyDown(Array("a中".utf16)), .unicodeKeyUp(Array("a中".utf16)),
        .unicodeKeyDown(Array("🙂b".utf16)), .unicodeKeyUp(Array("🙂b".utf16)),
    ])
}

@Test func foregroundKeyboardReusesOnlyThisGuardFocusObservation() throws {
    let fixture = KeyboardExecutorFixture()
    fixture.validator.observation = .authority(fixture.expectedFocus)
    fixture.focusObservation = .stale // Separate lookup must not be used.
    let plan = try fixture.executor.preflight(expected: fixture.guardValue,
        application: fixture.application, marker: fixture.lease.marker,
        entries: [keyboardEntry(.type(text: "a"))])
    fixture.poster.afterPost = { _ in fixture.validator.observation = .secure }
    let result = fixture.executor.executePrepared(sourceIndex: 0, from: plan,
        expected: fixture.guardValue, lease: fixture.lease)
    #expect(result.error == .unknownOutcome)
    #expect(fixture.poster.events.count == 2) // down then cleanup up, no replay
}

@Test func foregroundKeyboardSlowGuardCannotPostAfterActionDeadline() throws {
    let fixture = KeyboardExecutorFixture(clockValues: [0, 0, 0, 11])
    let plan = try fixture.executor.preflight(expected: fixture.guardValue,
        application: fixture.application, marker: fixture.lease.marker,
        entries: [keyboardEntry(.type(text: "a"))])
    let result = fixture.executor.executePrepared(sourceIndex: 0, from: plan,
        expected: fixture.guardValue, lease: fixture.lease)
    #expect(result.error == .actionTimeout)
    #expect(fixture.poster.events.isEmpty)
}


@Test func genericForegroundKeyboardUnknownAppKeepsSecureInputGuard() throws {
    let fixture = KeyboardExecutorFixture(genericForegroundEnabled: true, emptyRegistry: true)
    let entries = [keyboardEntry(.type(text: "Astra 泛用测试"))]
    let plan = try fixture.executor.preflight(expected: fixture.guardValue, application: fixture.application,
        marker: fixture.lease.marker, entries: entries)
    fixture.secureInput.enabled = true
    let result = fixture.executor.executePrepared(sourceIndex: 0, from: plan,
        expected: fixture.guardValue, lease: fixture.lease)
    #expect(result.error == .secureTarget)
    #expect(fixture.poster.events.isEmpty)
}

@Test func genericForegroundKeyboardDoesNotEnableBackgroundExecution() throws {
    let fixture = KeyboardExecutorFixture(genericForegroundEnabled: true, emptyRegistry: true)
    let foreground = fixture.guardValue
    let background = ActionGuard(pid: foreground.pid, windowID: foreground.windowID,
        bounds: foreground.bounds, axIdentity: foreground.axIdentity,
        snapshotID: foreground.snapshotID, interactionMode: .background)
    #expect(throws: ForegroundKeyboardFailure.compatibilityDisabled) {
        _ = try fixture.executor.preflight(expected: background, application: fixture.application,
            marker: fixture.lease.marker, entries: [keyboardEntry(.type(text: "Astra"))])
    }
}


@Test func foregroundChunkedLongTextFitsBudgetAndPreservesGraphemes() throws {
    let text = String(repeating: "Matrix 中文🙂e\u{301} ", count: 40)
    let fixture = KeyboardExecutorFixture(clockStep: 0.005, experimentalUnicodeChunkGraphemes: 8)
    let plan = try fixture.executor.preflight(expected: fixture.guardValue,
        application: fixture.application, marker: fixture.lease.marker,
        entries: [keyboardEntry(.type(text: text))])
    let result = fixture.executor.executePrepared(sourceIndex: 0, from: plan,
        expected: fixture.guardValue, lease: fixture.lease)
    #expect(result.error == nil)
    var delivered: [UInt16] = []
    for recorded in fixture.poster.events {
        if case let .unicodeKeyDown(units) = recorded.event {
            #expect(units.count <= 20)
            delivered.append(contentsOf: units)
        }
    }
    #expect(String(decoding: delivered, as: UTF16.self) == text)
    #expect(fixture.poster.events.count < text.count)
}
