import ApplicationServices
import CoreGraphics
import Foundation
import Testing

@testable import AstraMacComputerHelperCore

@Suite struct AppshotCaptureTests {
  @Test func dimensionsAreBoundedBeforeAllocation() throws {
    try AppshotCaptureImage.validateDimensions(width: 8000, height: 4000)
    #expect(throws: AppshotCaptureError.resourceLimit) {
      try AppshotCaptureImage.validateDimensions(width: 8001, height: 4000)
    }
    #expect(throws: AppshotCaptureError.resourceLimit) {
      try AppshotCaptureImage.validateDimensions(width: 16385, height: 1)
    }
    #expect(throws: AppshotCaptureError.resourceLimit) {
      try AppshotCaptureImage.validateDimensions(width: 0, height: 1)
    }
  }
  @Test func sharedDeadlineCannotRestart() throws {
    var now = 10.0
    let deadline = AppshotCaptureDeadline(duration: 5, clock: { now })
    now = 14
    #expect(try deadline.remaining() == 1)
    now = 15
    #expect(throws: AppshotCaptureError.timedOut) { try deadline.remaining() }
  }
}

private func recipient() -> AppshotRecipientBinding {
  .init(
    requestID: UUID().uuidString, connectionID: "c", sessionID: "s",
    identity: .init(pid: 1, uid: 501, processStart: "1"), instanceID: "b")
}
private func target(
  id: Int = 7, pid: Int = 42, start: String = "9", width: Double = 1, root: AXUIElement? = nil
) -> AppshotCaptureTarget {
  .init(
    source: .init(
      pid: pid, processStart: start, bundleId: "app", appLabel: "App", windowTitle: "Window",
      windowId: id, bounds: .init(x: 0, y: 0, width: width, height: 1)),
    axRoot: root ?? AXUIElementCreateApplication(42), window: nil)
}
private func blackImage(alpha: UInt8 = 255) -> CGImage {
  let context = CGContext(
    data: nil, width: 1, height: 1, bitsPerComponent: 8, bytesPerRow: 4,
    space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
  context.data!.assumingMemoryBound(to: UInt8.self)[3] = alpha
  return context.makeImage()!
}
private final class TargetFixture: AppshotTargetProviding {
  var reads = 0
  var targets: [AppshotCaptureTarget]
  var error: Error?
  init(_ targets: [AppshotCaptureTarget]) { self.targets = targets }
  func read(deadline: AppshotCaptureDeadline) throws -> AppshotCaptureTarget {
    if let error { throw error }
    let value = targets[min(reads, targets.count - 1)]
    reads += 1
    return value
  }
}
private struct ImageFixture: AppshotWindowImageProviding {
  var run: (@escaping (Result<CGImage, Error>) -> Void) -> Void = { $0(.success(blackImage())) }
  func capture(target: AppshotCaptureTarget, completion: @escaping (Result<CGImage, Error>) -> Void)
  { run(completion) }
}
private func capture(_ coordinator: AppshotCaptureCoordinator, duration: Double = 5) async
  -> Result<AppshotCaptureResult, Error>
{
  await withCheckedContinuation { continuation in
    coordinator.capture(for: recipient(), deadline: .init(duration: duration)) {
      continuation.resume(returning: $0)
    }
  }
}

extension AppshotCaptureTests {
  @Test(.enabled(if: ProcessInfo.processInfo.environment["ASTRA_MACOS_APPSHOT_E2E"] == "1"))
  func liveEdgeSourceBindingAndCapture() async throws {
    let coordinator = AppshotCaptureCoordinator()
    let result = try await capture(coordinator).get()
    #expect(result.source.bundleId == "com.microsoft.edgemac")
    #expect(result.source.windowId > 0)
    #expect(result.width > 0 && result.height > 0 && !result.png.isEmpty)
    print("APPSHOT_EDGE_CAPTURE_OK width=\(result.width) height=\(result.height)")
  }
  @Test func opaqueBlackIsValidTransparentIsRejected() throws {
    #expect(try AppshotCaptureImage.png(blackImage()).count > 0)
    #expect(throws: (any Error).self) { try AppshotCaptureImage.png(blackImage(alpha: 0)) }
    try AppshotCaptureImage.validatePNGSize(10 * 1024 * 1024)
    #expect(throws: AppshotCaptureError.resourceLimit) {
      try AppshotCaptureImage.validatePNGSize(10 * 1024 * 1024 + 1)
    }
  }
  @Test func exactFilterSelectionHasOnlyWindowOperation() {
    enum Filter: Equatable { case desktopIndependentWindow(Int) }
    var selected: [Int] = []
    let filter = appshotWindowFilter(77) { id in
      selected.append(id)
      return Filter.desktopIndependentWindow(id)
    }
    #expect(filter == .desktopIndependentWindow(77))
    #expect(selected == [77])
  }
  @Test(arguments: [0, 1, 2, 3, 4]) func everyIdentityChangeDiscardsPNG(change: Int) async {
    let before = target()
    let after = [
      target(id: 8), target(pid: 43), target(start: "10"), target(width: 2),
      target(root: AXUIElementCreateApplication(43)),
    ][change]
    let coordinator = AppshotCaptureCoordinator(
      targets: TargetFixture([before, after]), images: ImageFixture())
    let result = await capture(coordinator)
    #expect(throws: AppshotCaptureError.sourceWindowChanged) { try result.get() }
  }
  @Test func finalRevalidationAfterAXUsesSameDeadlineAndRoot() async throws {
    let before = target()
    let fixture = TargetFixture([before, before, target(id: 8)])
    let coordinator = AppshotCaptureCoordinator(targets: fixture, images: ImageFixture())
    let result = try await capture(coordinator).get()
    #expect(CFEqual(result.axRoot, before.axRoot))
    #expect(throws: AppshotCaptureError.sourceWindowChanged) { try coordinator.revalidate(result) }
    result.deadline.cancel()
    #expect(throws: AppshotCaptureError.cancelled) { try coordinator.revalidate(result) }
  }
  @Test func neverCompletingCallbackTimesOutAndRetainsBusyOwnership() async {
    let coordinator = AppshotCaptureCoordinator(
      targets: TargetFixture([target()]), images: ImageFixture(run: { _ in }))
    let start = ProcessInfo.processInfo.systemUptime
    let result = await capture(coordinator, duration: 0.03)
    #expect(throws: AppshotCaptureError.timedOut) { try result.get() }
    #expect(ProcessInfo.processInfo.systemUptime - start < 1)
    let again = await capture(coordinator)
    #expect(throws: AppshotCaptureError.busy) { try again.get() }
  }
  @Test func lateAndDuplicateCallbackCannotDeliverAgain() async throws {
    let fixture = TargetFixture([target()])
    let coordinator = AppshotCaptureCoordinator(
      targets: fixture,
      images: ImageFixture(run: { callback in
        DispatchQueue.global().asyncAfter(deadline: .now() + 0.05) {
          callback(.success(blackImage()))
          callback(.success(blackImage()))
        }
      }))
    let result = await capture(coordinator, duration: 0.01)
    #expect(throws: AppshotCaptureError.timedOut) { try result.get() }
    try await Task.sleep(nanoseconds: 100_000_000)
    // Late image never enters post-capture target read/PNG publication path.
    #expect(fixture.reads == 1)
  }
  @Test(arguments: [AppshotCaptureError.permissionDenied, .protectedContext, .indeterminateTarget])
  func safetyFailurePreventsImageRequest(error: AppshotCaptureError) async {
    let targets = TargetFixture([target()])
    targets.error = error
    let images = ImageFixture(run: { _ in Issue.record("unsafe image request") })
    let result = await capture(AppshotCaptureCoordinator(targets: targets, images: images))
    #expect(throws: error) { try result.get() }
  }
  @Test func realProviderRejectsMissingScreenPermission() {
    var provider = AppshotSystemTargetProvider()
    provider.screenRecording = { false }
    provider.frontmost = {
      Issue.record("must not read target without screenshot permission")
      return nil
    }
    #expect(throws: AppshotCaptureError.permissionDenied) { try provider.read(deadline: .init()) }
  }
  @Test func realProviderRejectsLockedSession() {
    var provider = AppshotSystemTargetProvider()
    provider.accessibility = { true }
    provider.screenRecording = { true }
    for session: [String: Any]? in [
      nil, [:],
      [
        kCGSessionOnConsoleKey as String: true, kCGSessionLoginDoneKey as String: true,
        "CGSSessionScreenIsLocked": true,
      ],
    ] {
      provider.session = { session }
      #expect(throws: AppshotCaptureError.protectedContext) { try provider.read(deadline: .init()) }
    }
  }
}

private struct CaptureProcesses: AppshotProcessProviding {
  func identity(pid: Int32) -> AppshotProcessIdentity? {
    .init(pid: pid, uid: 501, processStart: "9")
  }
  func monotonicNS() -> UInt64 { 0 }
}
private func native(
  id: UInt32 = 7, pid: Int32 = 42, bounds: CGRect = CGRect(x: 0, y: 0, width: 1, height: 1),
  visible: Bool = true, alpha: Double = 1, layer: Int = 0
) -> AppshotNativeWindow {
  .init(id: id, pid: pid, bounds: bounds, onScreen: visible, layer: layer, alpha: alpha)
}
private func safeSystemProvider() -> AppshotSystemTargetProvider {
  var provider = AppshotSystemTargetProvider()
  provider.accessibility = { true }
  provider.screenRecording = { true }
  provider.session = {
    [kCGSessionOnConsoleKey as String: true, kCGSessionLoginDoneKey as String: true]
  }
  provider.frontmost = { .init(pid: 42, bundle: "example.app", label: "App") }
  provider.processes = CaptureProcesses()
  let root = AXUIElementCreateApplication(42)
  provider.focusedWindow = { _, _ in
    .init(root: root, bounds: CGRect(x: 0, y: 0, width: 1, height: 1))
  }
  provider.nativeWindows = { [native()] }
  provider.enumerator = AppshotShareableWindowEnumerator { $0(.success(shareable())) }
  return provider
}
extension AppshotCaptureTests {
  @Test func publicIdentityBindingMatchesUniqueAXCGSCWindow() throws {
    let provider = safeSystemProvider()
    let result = try provider.read(deadline: .init())
    #expect(result.source.pid == 42)
    #expect(result.source.windowId == 7)
    #expect(result.source.processStart == "9")
  }
  @Test(arguments: [0, 1, 2, 3, 4, 5]) func nativeCandidatesFailClosed(caseIndex: Int) {
    var provider = safeSystemProvider()
    let records: [[AppshotNativeWindow]] = [
      [], [native(), native(id: 8)], [native(visible: false)], [native(alpha: 0)],
      [native(pid: 43)],
      [native(bounds: CGRect(x: 0, y: 0, width: 2, height: 1))],
    ]
    provider.nativeWindows = { records[caseIndex] }
    #expect(throws: AppshotCaptureError.indeterminateTarget) {
      try provider.read(deadline: .init())
    }
  }
  @Test(arguments: ["other page", "Source", "", "Source - Microsoft Edge"])
  func equalBoundsRequireOnePositiveTitleMatch(siblingTitle: String) throws {
    var provider = safeSystemProvider()
    let root = AXUIElementCreateApplication(42)
    provider.focusedWindow = { _, _ in
      .init(root: root, bounds: native().bounds, title: "Source - Microsoft Edge")
    }
    var target = native(); target.title = "Source"
    var sibling = native(id: 8); sibling.title = siblingTitle
    provider.nativeWindows = { [sibling, target] }
    if siblingTitle != "other page" {
      #expect(throws: AppshotCaptureError.indeterminateTarget) { try provider.read(deadline: .init()) }
    } else {
      #expect(try provider.read(deadline: .init()).source.windowId == 7)
    }
  }
  @Test func disambiguatingTitleMustSurviveFinalAXRead() {
    var provider = safeSystemProvider()
    let root = AXUIElementCreateApplication(42)
    var calls = 0
    provider.focusedWindow = { _, _ in
      calls += 1
      return .init(root: root, bounds: native().bounds, title: calls == 1 ? "Source" : "Changed")
    }
    var target = native(); target.title = "Source"
    var sibling = native(id: 8); sibling.title = "Other"
    provider.nativeWindows = { [target, sibling] }
    #expect(throws: AppshotCaptureError.sourceWindowChanged) { try provider.read(deadline: .init()) }
  }
  @Test(arguments: [0, 1, 2, 3, 4, 5]) func shareableIdentityAndOffscreenFailClosed(caseIndex: Int)
  {
    var provider = safeSystemProvider()
    let records: [[AppshotNativeWindow]] = [
      [], [native(), native()], [native(id: 8)], [native(pid: 43)], [native(visible: false)],
      [native()],
    ]
    provider.enumerator = AppshotShareableWindowEnumerator { callback in
      callback(
        .success(
          .init(
            windows: records[caseIndex],
            displays: caseIndex == 5 ? [] : [CGRect(x: 0, y: 0, width: 100, height: 100)])))
    }
    #expect(throws: AppshotCaptureError.sourceWindowChanged) {
      try provider.read(deadline: .init())
    }
  }
  @Test(arguments: [
    "com.apple.systempreferences", "com.apple.ActivityMonitor", "com.apple.SecurityAgent",
    "com.apple.controlcenter", "com.apple.notificationcenterui", "example.app",
  ])
  func userCaptureDoesNotDenyApplicationIdentity(bundle: String) throws {
    var provider = safeSystemProvider()
    provider.frontmost = { .init(pid: 42, bundle: bundle, label: "Any title") }
    let result = try provider.read(deadline: .init())
    #expect(result.source.bundleId == bundle)
    #expect(result.source.windowId == 7)
  }
  @Test func ordinaryDialogsSheetsAndMissingMinimizedAttributeAreEligible() throws {
    for role in [kAXWindowRole, kAXSheetRole] {
      for minimized in [false, nil] as [Bool?] {
        try AppshotSystemTargetProvider.validateAXWindow(role: role, minimized: minimized)
      }
    }
    for role in [nil, kAXButtonRole] {
      #expect(throws: AppshotCaptureError.indeterminateTarget) {
        try AppshotSystemTargetProvider.validateAXWindow(role: role, minimized: false)
      }
    }
    #expect(throws: AppshotCaptureError.indeterminateTarget) {
      try AppshotSystemTargetProvider.validateAXWindow(role: kAXWindowRole, minimized: true)
    }
  }
  @Test(arguments: [true, false]) func unavailableAXCanUseUniqueVisibleWindow(permission: Bool) throws {
    var provider = safeSystemProvider()
    provider.accessibility = { permission }
    provider.focusedWindow = { _, _ in
      #expect(permission)
      throw AppshotCaptureError.indeterminateTarget
    }
    let result = try provider.read(deadline: .init())
    #expect(result.source.windowId == 7)
    #expect(result.axRoot == nil)
    provider.nativeWindows = { [native(), native(id: 8)] }
    #expect(throws: AppshotCaptureError.indeterminateTarget) { try provider.read(deadline: .init()) }
  }
  @Test func visibleFloatingWindowIsEligible() throws {
    var provider = safeSystemProvider()
    provider.nativeWindows = { [native(layer: 8)] }
    #expect(try provider.read(deadline: .init()).source.windowId == 7)
  }
  @Test func lockChangeDuringShareableReadPreventsCapture() {
    var provider = safeSystemProvider()
    var locked = false
    provider.session = {
      [kCGSessionOnConsoleKey as String: true, kCGSessionLoginDoneKey as String: true,
       "CGSSessionScreenIsLocked": locked]
    }
    provider.enumerator = AppshotShareableWindowEnumerator { callback in
      locked = true
      callback(.success(shareable()))
    }
    #expect(throws: AppshotCaptureError.protectedContext) { try provider.read(deadline: .init()) }
  }
  @Test func permissionChangeAfterImageDiscardsBytes() async {
    var provider = safeSystemProvider()
    var permitted = true
    provider.screenRecording = { permitted }
    let images = ImageFixture(run: { callback in
      permitted = false
      callback(.success(blackImage()))
    })
    let result = await capture(AppshotCaptureCoordinator(targets: provider, images: images))
    #expect(throws: AppshotCaptureError.permissionDenied) { try result.get() }
  }
  @Test func cancellationInvalidatesAuthorityAndRejectsLaterImage() async throws {
    let coordinator = AppshotCaptureCoordinator(
      targets: TargetFixture([target()]),
      images: ImageFixture(run: { callback in
        DispatchQueue.global().asyncAfter(deadline: .now() + 0.05) {
          callback(.success(blackImage()))
        }
      }))
    let result: Result<AppshotCaptureResult, Error> = await withCheckedContinuation {
      continuation in
      let cancel = coordinator.capture(for: recipient()) { continuation.resume(returning: $0) }
      cancel()
      cancel()
    }
    #expect(throws: AppshotCaptureError.cancelled) { try result.get() }
    try await Task.sleep(nanoseconds: 80_000_000)
  }
}
extension AppshotCaptureTests {
  @Test func oldDuplicateCannotReleaseNewCaptureOwnership() async throws {
    let (stream, continuation) = AsyncStream<((Result<CGImage, Error>) -> Void)>.makeStream()
    var iterator = stream.makeAsyncIterator()
    let coordinator = AppshotCaptureCoordinator(
      targets: TargetFixture([target()]), images: ImageFixture(run: { continuation.yield($0) }))
    let first = Task { await capture(coordinator) }
    let firstCallback = await iterator.next()!
    firstCallback(.success(blackImage()))
    _ = try await first.value.get()
    let second = Task { await capture(coordinator) }
    let secondCallback = await iterator.next()!
    firstCallback(.success(blackImage()))
    // A dispatch marker in the same worker isn't exposed: allow the stale callback to be rejected.
    try await Task.sleep(nanoseconds: 20_000_000)
    let third = await capture(coordinator)
    #expect(throws: AppshotCaptureError.busy) { try third.get() }
    secondCallback(.success(blackImage()))
    _ = try await second.value.get()
    continuation.finish()
  }
  @Test func exactWindowGeometryRejectsCroppedAndNonuniformPixels() throws {
    let geometry = WindowGeometry(
      bounds: CGRect(x: 0, y: 0, width: 200, height: 100), backingScale: 2)
    _ = try resolvedWindowImageGeometry(requested: geometry, imageWidth: 400, imageHeight: 200)
    _ = try resolvedWindowImageGeometry(requested: geometry, imageWidth: 200, imageHeight: 100)
    for size in [(199, 100), (400, 100), (400, 199)] {
      #expect(throws: (any Error).self) {
        try resolvedWindowImageGeometry(
          requested: geometry, imageWidth: size.0, imageHeight: size.1)
      }
    }
  }
}
extension AppshotCaptureTests {
  @Test func AXBudgetTracksSameAbsoluteClockAndCancellation() throws {
    var now = 10.0
    let deadline = AppshotCaptureDeadline(clock: { now })
    let firstBudget = try deadline.axBudget()
    now = 14
    let secondBudget = try deadline.axBudget()
    #expect(try firstBudget.phaseTimeout(maximum: 5) == 1)
    #expect(try secondBudget.phaseTimeout(maximum: 5) == 1)
    deadline.cancel()
    #expect(!firstBudget.available)
    #expect(!secondBudget.available)
  }
}

private func shareable() -> AppshotShareableWindows {
  .init(windows: [native()], displays: [CGRect(x: 0, y: 0, width: 100, height: 100)])
}

extension AppshotCaptureTests {
  @Test func enumerationNeverCompletesRetainsOwnership() throws {
    var calls = 0
    // The budget runs out only once the enumeration has started, however slow this run is.
    let clock = ManualCaptureClock()
    let enumerator = AppshotShareableWindowEnumerator { _ in
      calls += 1
      clock.advance(by: 1)
    }
    #expect(throws: AppshotCaptureError.timedOut) {
      try enumerator.read(deadline: .init(duration: 0.02, clock: { clock.now }))
    }
    #expect(throws: AppshotCaptureError.busy) { try enumerator.read(deadline: .init()) }
    #expect(calls == 1)
  }

  @Test(arguments: [false, true]) func enumerationDoesNotStartForUnavailableDeadline(cancel: Bool) {
    let enumerator = AppshotShareableWindowEnumerator { _ in Issue.record("enumeration started") }
    let deadline = AppshotCaptureDeadline(duration: cancel ? 5 : 0)
    if cancel { deadline.cancel() }
    #expect(throws: cancel ? AppshotCaptureError.cancelled : .timedOut) {
      try enumerator.read(deadline: deadline)
    }
  }

  @Test func enumerationPreservesImmediateFrameworkError() {
    let failure = NSError(domain: "SCStreamErrorDomain", code: -3801)
    let enumerator = AppshotShareableWindowEnumerator { $0(.failure(failure)) }
    do {
      _ = try enumerator.read(deadline: .init())
      Issue.record("framework error was discarded")
    } catch {
      #expect((error as NSError).domain == failure.domain)
      #expect((error as NSError).code == failure.code)
    }
    #expect(!enumerator.hasOutstandingRead)
  }
}

/// Time that moves only when the test advances it. A loaded parallel run once spent the whole
/// 0.15 s budget before the withheld read began, so the capture timed out without that read and
/// the test waited for it forever.
private final class ManualCaptureClock: @unchecked Sendable {
  private let lock = NSLock()
  private var value: TimeInterval = 1_000
  var now: TimeInterval { lock.withLock { value } }
  func advance(by seconds: TimeInterval) { lock.withLock { value += seconds } }
}

extension AppshotCaptureTests {
  @Test(arguments: [0, 1, 2], [false, true])
  func enumerationOwnershipCoversInitialPostImageAndFinalRead(phase: Int, cancel: Bool) async throws
  {
    let (stream, continuation) = AsyncStream<AppshotShareableWindowEnumerator.Completion>
      .makeStream()
    var iterator = stream.makeAsyncIterator()
    var reads = 0
    var provider = safeSystemProvider()
    provider.enumerator = AppshotShareableWindowEnumerator { callback in
      reads += 1
      if reads <= phase { callback(.success(shareable())) } else { continuation.yield(callback) }
    }
    let coordinator = AppshotCaptureCoordinator(targets: provider, images: ImageFixture())
    let clock = ManualCaptureClock()
    let deadline = AppshotCaptureDeadline(duration: 0.15, clock: { clock.now })
    let captureTask = Task {
      await withCheckedContinuation { continuation in
        coordinator.capture(for: recipient(), deadline: deadline) {
          continuation.resume(returning: $0)
        }
      }
    }
    var finalTask: Task<Void, Error>?
    if phase == 2 {
      let result = try await captureTask.value.get()
      finalTask = Task.detached { try coordinator.revalidate(result) }
    }
    let callback = await iterator.next()!
    // Concurrent admission is refused even when the operation is final revalidation.
    await #expect(throws: AppshotCaptureError.busy) { try await capture(coordinator).get() }
    if cancel { deadline.cancel() } else { clock.advance(by: 1) }
    let expected = cancel ? AppshotCaptureError.cancelled : .timedOut
    if let finalTask {
      await #expect(throws: expected) { try await finalTask.value }
    } else {
      await #expect(throws: expected) { try await captureTask.value.get() }
    }
    #expect(provider.enumerator.hasOutstandingRead)
    await #expect(throws: AppshotCaptureError.busy) { try await capture(coordinator).get() }
    #expect(reads == phase + 1)
    callback(.success(shareable()))
    #expect(!provider.enumerator.hasOutstandingRead)
    continuation.finish()
  }

  @Test func cancelledEnumerationReturnsPromptlyButRetainsLeaseUntilLateCallback() async throws {
    let (stream, continuation) = AsyncStream<AppshotShareableWindowEnumerator.Completion>
      .makeStream()
    var iterator = stream.makeAsyncIterator()
    let enumerator = AppshotShareableWindowEnumerator { continuation.yield($0) }
    let deadline = AppshotCaptureDeadline()
    let request = Task.detached { try enumerator.read(deadline: deadline) }
    let callback = await iterator.next()!
    let start = ProcessInfo.processInfo.systemUptime
    deadline.cancel()
    await #expect(throws: AppshotCaptureError.cancelled) { try await request.value }
    #expect(ProcessInfo.processInfo.systemUptime - start < 0.5)
    #expect(throws: AppshotCaptureError.busy) { try enumerator.read(deadline: .init()) }
    callback(.success(shareable()))
    #expect(!enumerator.hasOutstandingRead)
    continuation.finish()
  }

  @Test func lateDuplicateEnumerationCannotReleaseNewOperationOrReplaceResult() async throws {
    let (stream, continuation) = AsyncStream<AppshotShareableWindowEnumerator.Completion>
      .makeStream()
    var iterator = stream.makeAsyncIterator()
    let enumerator = AppshotShareableWindowEnumerator { continuation.yield($0) }
    let clock = ManualCaptureClock()
    let first = Task.detached { try enumerator.read(deadline: .init(duration: 0.02, clock: { clock.now })) }
    let old = await iterator.next()!
    clock.advance(by: 1)
    await #expect(throws: AppshotCaptureError.timedOut) { try await first.value }
    old(.success(shareable()))
    let second = Task.detached { try enumerator.read(deadline: .init()) }
    let current = await iterator.next()!
    old(.failure(AppshotCaptureError.permissionDenied))
    #expect(enumerator.hasOutstandingRead)
    #expect(throws: AppshotCaptureError.busy) { try enumerator.read(deadline: .init()) }
    current(.success(shareable()))
    current(.failure(AppshotCaptureError.permissionDenied))
    #expect(try await second.value.windows.count == 1)
    #expect(!enumerator.hasOutstandingRead)
    continuation.finish()
  }

  @Test(arguments: [false, true])
  func nativeReadCannotStartEnumerationAfterDeadlineBecameUnavailable(cancel: Bool) {
    var now = 0.0
    let deadline = AppshotCaptureDeadline(clock: { now })
    var provider = safeSystemProvider()
    provider.nativeWindows = {
      if cancel { deadline.cancel() } else { now = 6 }
      return [native()]
    }
    provider.enumerator = AppshotShareableWindowEnumerator { _ in
      Issue.record("enumeration started")
    }
    #expect(throws: cancel ? AppshotCaptureError.cancelled : .timedOut) {
      try provider.read(deadline: deadline)
    }
  }
}

extension AppshotCaptureTests {
  @Test func successfulDeliveryCanImmediatelyPerformFinalRevalidation() async {
    let coordinator = AppshotCaptureCoordinator(
      targets: TargetFixture([target()]), images: ImageFixture())
    let result: Result<Void, Error> = await withCheckedContinuation { continuation in
      coordinator.capture(for: recipient()) { outcome in
        continuation.resume(returning: Result { try coordinator.revalidate(outcome.get()) })
      }
    }
    #expect(throws: Never.self) { try result.get() }
  }
}
