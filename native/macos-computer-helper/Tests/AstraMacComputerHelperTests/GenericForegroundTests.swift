@testable import AstraMacComputerHelperCore
import CoreGraphics
import Testing

@Test func genericForegroundPolicyDoesNotWidenBackgroundRegistry() {
    let policy = SyntheticInputPlanningPolicy(
        pointerCapability: .available, keyboardCapability: .available,
        registry: PIDInputCompatibilityRegistry(), genericForegroundEnabled: true
    )
    let app = PIDTargetApplication(bundleIdentifier: "test.unlisted", version: "99")
    #expect(policy.allowsForeground(application: app, intents: [.pointer(.click), .textEntry]))
    #expect(!policy.allows(application: app, intents: [.pointer(.click), .textEntry]))
    #expect(!policy.allowsBackgroundDelivery(application: app, backend: .pidPointer, action: .click))
    #expect(!policy.allowsForeground(application: nil, intents: [.textEntry]))
}

@Test func genericForegroundPosterMarksEventsAndRejectsInactiveTarget() throws {
    var active: Int32 = 42
    var delivered: [CGEvent] = []
    let poster = CGForegroundInputPoster(
        preflightAccess: { true }, frontmostPID: { active }, pointIsInTargetWindow: { _, _ in true },
        deliver: { delivered.append($0) }
    )
    #expect(poster.preflight(targetPID: 42, marker: 123))
    try poster.post(.mouseDown(point: CGPoint(x: 20, y: 30), clickCount: 1), to: 42, marker: 123)
    #expect(delivered.count == 1)
    #expect(delivered[0].getIntegerValueField(.eventSourceUserData) == 123)
    active = 7
    #expect(throws: (any Error).self) {
        try poster.post(.unicodeKeyDown([65]), to: 42, marker: 123)
    }
    #expect(delivered.count == 1)
    // A held button must still be released after focus changes.
    try poster.post(.mouseUp(point: CGPoint(x: 20, y: 30), clickCount: 1), to: 42, marker: 123)
    #expect(delivered.count == 2)
}

@Test func genericForegroundPosterRejectsOverlayAndMovesBeforeScroll() throws {
    var ownsPoint = false
    var events: [CGEvent] = []
    let poster = CGForegroundInputPoster(
        preflightAccess: { true }, frontmostPID: { 42 },
        pointIsInTargetWindow: { _, _ in ownsPoint }, deliver: { events.append($0) }
    )
    #expect(throws: (any Error).self) {
        try poster.post(.scroll(point: CGPoint(x: 20, y: 30), deltaX: 0, deltaY: 80), to: 42, marker: 123)
    }
    #expect(events.isEmpty)
    ownsPoint = true
    try poster.post(.scroll(point: CGPoint(x: 20, y: 30), deltaX: 0, deltaY: 80), to: 42, marker: 123)
    #expect(events.map(\.type) == [.mouseMoved, .scrollWheel])
    #expect(events.allSatisfy { $0.getIntegerValueField(.eventSourceUserData) == 123 })
}

@Test func genericForegroundPosterRechecksDeadlineAfterHitTest() throws {
    var time: Double = 0
    var events: [CGEvent] = []
    let poster = CGForegroundInputPoster(preflightAccess: { true }, frontmostPID: { 42 },
        pointIsInTargetWindow: { _, _ in time = 13; return true },
        deliver: { events.append($0) }, now: { time })
    #expect(throws: (any Error).self) {
        try poster.post(.mouseDown(point: CGPoint(x: 10, y: 20), clickCount: 1), to: 42, marker: 123, deadline: 12)
    }
    #expect(events.isEmpty)
}

@Test(arguments: [false, true]) func foregroundLoanCannotPostAfterSlowFinalFocusRead(scroll: Bool) throws {
    var reads = 0, clock = 119.0
    var events: [CGEventType] = []
    let poster = CGForegroundInputPoster(preflightAccess: { true }, frontmostPID: {
        reads += 1
        if reads >= 2 { clock = 122 }
        return 42
    }, pointIsInTargetWindow: { _, _ in true }, deliver: { events.append($0.type) }, now: { clock })
    let input: SyntheticInputEvent = scroll ? .scroll(point: .zero, deltaX: 0, deltaY: 2) : .unicodeKeyDown([65])
    #expect(throws: SyntheticInputFailure.self) { try poster.post(input, to: 42, marker: 123, deadline: 120) }
    #expect(events.isEmpty)
    try poster.post(.unicodeKeyUp([65]), to: 42, marker: 123, deadline: 120)
    #expect(events == [.keyUp])
}

@Test(arguments: ["duringFocusRead", "afterScrollMove"], [false, true])
func foregroundStopAtFinalDeliveryAllowsOnlyCleanup(_ scenario: String, physical: Bool) throws {
    var reads = 0, stopped = false
    var events: [CGEventType] = []
    let poster = CGForegroundInputPoster(preflightAccess: { true }, frontmostPID: {
        reads += 1
        if scenario == "duringFocusRead", reads == 2 { stopped = true }
        return 42
    }, pointIsInTargetWindow: { _, _ in true }, deliver: {
        events.append($0.type)
        if $0.type == .mouseMoved { stopped = true }
    }, validateNewInput: {
        if stopped, physical { throw UserActivityMonitoringError.paused }
        if stopped { throw ActionExecutionError.helperFailed }
    })
    let input: SyntheticInputEvent = scenario == "duringFocusRead" ? .unicodeKeyDown([65]) : .scroll(point: .zero, deltaX: 0, deltaY: 2)
    do {
        try poster.post(input, to: 42, marker: 123)
        Issue.record("unexpected dispatch after terminal stop")
    } catch let failure as SyntheticInputFailure {
        #expect(failure.inputStarted == (scenario == "afterScrollMove"))
        #expect(failure.userActivityPaused == physical)
    }
    try poster.post(.unicodeKeyUp([65]), to: 42, marker: 123)
    #expect(events == (scenario == "afterScrollMove" ? [.mouseMoved, .keyUp] : [.keyUp]))
}

@Test func genericForegroundInvalidMarkerCannotMoveCursor() throws {
    var events: [CGEvent] = []
    let poster = CGForegroundInputPoster(preflightAccess: { true }, frontmostPID: { 42 },
        pointIsInTargetWindow: { _, _ in true }, deliver: { events.append($0) })
    #expect(throws: (any Error).self) {
        try poster.post(.scroll(point: CGPoint(x: 10, y: 20), deltaX: 0, deltaY: 10), to: 42, marker: 0)
    }
    #expect(events.isEmpty)
}

@Test func genericForegroundFocusChangeDuringScrollHitTestReportsMovement() throws {
    var active: Int32 = 42
    var checks = 0
    var events: [CGEvent] = []
    let poster = CGForegroundInputPoster(preflightAccess: { true }, frontmostPID: { active },
        pointIsInTargetWindow: { _, _ in checks += 1; if checks == 2 { active = 7 }; return true },
        deliver: { events.append($0) })
    do {
        try poster.post(.scroll(point: CGPoint(x: 10, y: 20), deltaX: 0, deltaY: 10), to: 42, marker: 123)
        Issue.record("Expected focus change failure")
    } catch let failure as SyntheticInputFailure {
        #expect(failure.inputStarted)
    }
    #expect(events.map(\.type) == [.mouseMoved])
}

@Test func cursorDecoratorPreservesDeadlineAcrossSlowWindowHitTest() throws {
    var time: Double = 0
    var events: [CGEvent] = []
    let base = CGForegroundInputPoster(preflightAccess: { true }, frontmostPID: { 42 },
        pointIsInTargetWindow: { _, _ in time = 13; return true },
        deliver: { events.append($0) }, now: { time })
    let poster: any PIDTargetedInputPosting = CursorPIDTargetedInputPoster(
        base: base, cursor: InMemoryVirtualCursorPresenter())
    #expect(throws: (any Error).self) {
        try poster.post(.mouseDown(point: CGPoint(x: 10, y: 20), clickCount: 1), to: 42, marker: 123, deadline: 12)
    }
    #expect(events.isEmpty)
    // Cleanup remains possible after the deadline.
    try poster.post(.mouseUp(point: CGPoint(x: 10, y: 20), clickCount: 1), to: 42, marker: 123, deadline: 12)
    #expect(events.map(\.type) == [.leftMouseUp])
}

@Test func capturedDragUsesTheOriginalMouseDownWindowUntilRelease() throws {
    var hitTests = 0
    var events: [CGEvent] = []
    let poster = CGForegroundInputPoster(preflightAccess: { true }, frontmostPID: { 42 },
        pointIsInTargetWindow: { _, _ in hitTests += 1; return hitTests == 1 },
        deliver: { events.append($0) })
    try poster.post(.mouseDown(point: CGPoint(x: 10, y: 20), clickCount: 1), to: 42, marker: 123)
    try poster.post(.mouseDragged(point: CGPoint(x: 12, y: 22)), to: 42, marker: 123)
    #expect(hitTests == 1)
    #expect(throws: (any Error).self) {
        try poster.post(.mouseDragged(point: CGPoint(x: 14, y: 24)), to: 42, marker: 999)
    }
    try poster.post(.mouseUp(point: CGPoint(x: 12, y: 22), clickCount: 1), to: 42, marker: 123)
    #expect(throws: (any Error).self) {
        try poster.post(.mouseDragged(point: CGPoint(x: 14, y: 24)), to: 42, marker: 123)
    }
    #expect(events.map(\.type) == [.leftMouseDown, .leftMouseDragged, .leftMouseUp])
}
