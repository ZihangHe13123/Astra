import Darwin
import Foundation
import Testing

@testable import AstraMacComputerHelperCore

final class AppshotTestProcesses: AppshotProcessProviding {
  var now: UInt64 = 10_000_000_000
  var identities: [Int32: AppshotProcessIdentity] = [:]
  func identity(pid: Int32) -> AppshotProcessIdentity? { identities[pid] }
  func monotonicNS() -> UInt64 { now }
}

@Suite @MainActor struct AppshotBrokerTests {
  func fixture() -> (AppshotClientRegistry, AppshotTestProcesses) {
    let processes = AppshotTestProcesses()
    processes.identities[10] = .init(pid: 10, uid: getuid(), processStart: "100")
    processes.identities[20] = .init(pid: 20, uid: getuid(), processStart: "200")
    return (
      AppshotClientRegistry(instanceID: "broker", nonce: "nonce", processes: processes), processes
    )
  }
  func connect(_ registry: AppshotClientRegistry, _ pid: Int32, session: String, connection: String)
    throws
  {
    try registry.authenticate(
      connectionID: connection,
      peer: .init(pid: pid, uid: getuid(), processStart: String(pid * 10)),
      hello: .init(
        type: "hello", version: 1, sessionId: session, pid: Int(pid),
        processStart: String(pid * 10), clientNonce: "nonce"))
  }
  func state(
    _ registry: AppshotClientRegistry, _ connection: String, session: String, activity: UInt64,
    count: Int = 0, requestID: String = "state"
  ) throws {
    try registry.update(
      connectionID: connection,
      state: .init(
        type: "client_state", version: 1, requestId: requestID, brokerId: "broker",
        sessionId: session, activityNs: String(activity), appshotCount: count, canAccept: true))
  }

  @Test func newestEligibleClientIsFrozenAndNoBroadcast() throws {
    let (registry, _) = fixture()
    try connect(registry, 10, session: "a", connection: "ca")
    try connect(registry, 20, session: "b", connection: "cb")
    try state(registry, "ca", session: "a", activity: 10)
    try state(registry, "cb", session: "b", activity: 20, count: 3)
    let binding = try registry.reserveCapture()
    #expect(binding.sessionID == "b")
    try state(registry, "ca", session: "a", activity: 30)
    #expect(try registry.revalidate(binding).connectionID == "cb")
    #expect(throws: AppshotBrokerError.captureBusy) { try registry.reserveCapture() }
    registry.cancelCapture(binding)
  }

  @Test func noInputTiesCapacityAndDeadProcessFailClosed() throws {
    let (registry, processes) = fixture()
    #expect(throws: AppshotBrokerError.noReceivingSession) { try registry.reserveCapture() }
    try connect(registry, 10, session: "a", connection: "ca")
    try connect(registry, 20, session: "b", connection: "cb")
    #expect(throws: AppshotBrokerError.noReceivingSession) { try registry.reserveCapture() }
    try state(registry, "ca", session: "a", activity: 20)
    try state(registry, "cb", session: "b", activity: 20)
    #expect(throws: AppshotBrokerError.receivingSessionAmbiguous) { try registry.reserveCapture() }
    try state(registry, "cb", session: "b", activity: 30, count: 4)
    #expect(throws: AppshotBrokerError.attachmentLimitReached) { try registry.reserveCapture() }
    processes.identities.removeValue(forKey: 20)
    #expect(try registry.reserveCapture().sessionID == "a")
  }

  @Test func identityNonceSessionAndClockChecks() throws {
    let (registry, processes) = fixture()
    let hello = AppshotHello(
      type: "hello", version: 1, sessionId: "a", pid: 10, processStart: "100", clientNonce: "wrong")
    #expect(throws: AppshotBrokerError.unauthorized) {
      try registry.authenticate(connectionID: "ca", peer: processes.identities[10]!, hello: hello)
    }
    try connect(registry, 10, session: "a", connection: "ca")
    #expect(throws: AppshotBrokerError.unauthorized) {
      try connect(registry, 10, session: "a", connection: "cb")
    }
    #expect(throws: AppshotBrokerError.invalidActivity) {
      try state(registry, "ca", session: "a", activity: processes.now + 1_000_000_001)
    }
    try state(registry, "ca", session: "a", activity: 20)
    #expect(throws: AppshotBrokerError.invalidActivity) {
      try state(registry, "ca", session: "a", activity: 19)
    }
    let binding = try registry.reserveCapture()
    processes.identities[10] = .init(pid: 10, uid: getuid(), processStart: "101")
    #expect(throws: AppshotBrokerError.recipientDisconnected) { try registry.revalidate(binding) }
  }

  @Test func offerTimeoutLateAckReleaseAndDisconnectCustody() throws {
    let (registry, processes) = fixture()
    try connect(registry, 10, session: "a", connection: "ca")
    try state(registry, "ca", session: "a", activity: 10)
    var destroyed = 0
    let binding = try registry.reserveCapture()
    try registry.stage(
      binding,
      artifact: .init(
        manifestPath: "/private/tmp/example", byteCount: 100, cleanup: { destroyed += 1 }))
    processes.now += 3_000_000_001
    #expect(registry.expire().count == 1)
    #expect(destroyed == 1)
    #expect(throws: AppshotBrokerError.unknownAttachment) {
      try registry.acknowledge(connectionID: "ca", requestID: binding.requestID, accepted: true)
    }
    let next = try registry.reserveCapture()
    try registry.stage(
      next,
      artifact: .init(
        manifestPath: "/private/tmp/example", byteCount: 100, cleanup: { destroyed += 1 }))
    _ = try registry.acknowledge(connectionID: "ca", requestID: next.requestID, accepted: true)
    #expect(registry.release(connectionID: "other", requestID: next.requestID) == false)
    #expect(registry.release(connectionID: "ca", requestID: next.requestID))
    #expect(registry.release(connectionID: "ca", requestID: next.requestID))
    #expect(destroyed == 2)
    let last = try registry.reserveCapture()
    try registry.stage(
      last,
      artifact: .init(
        manifestPath: "/private/tmp/example", byteCount: 100, cleanup: { destroyed += 1 }))
    registry.disconnect(connectionID: "ca")
    #expect(destroyed == 3)
    #expect(registry.connectedCount == 0)
  }
}

final class BrokerTestStore: AppshotSettingsStoring {
  var settings = AppshotSettings.default
  func load() throws -> AppshotSettingsLoadResult { .init(settings: settings, warning: nil) }
  func save(_ settings: AppshotSettings) throws { self.settings = settings }
}
final class BrokerTestRegistrar: AppshotHotKeyRegistering {
  var handler: AppshotCaptureHandler?
  var registered = 0
  var unregistered = 0
  var failure: AppshotError?
  func register(_ chord: AppshotChord, handler: @escaping AppshotCaptureHandler) throws
    -> AppshotHotKeyToken
  {
    if let failure { throw failure }
    self.handler = handler
    registered += 1
    return AppshotHotKeyToken(chord: chord) {}
  }
  func unregister(_ token: AppshotHotKeyToken) {
    unregistered += 1
    handler = nil
    token.cancel()
  }
}
final class BrokerTestClock: AppshotProcessProviding {
  var offset: UInt64 = 0
  func identity(pid: Int32) -> AppshotProcessIdentity? {
    AppshotSystemProcesses().identity(pid: pid)
  }
  func monotonicNS() -> UInt64 { AppshotSystemProcesses().monotonicNS() &+ offset }
}

private struct BrokerTestFailure: Error, CustomStringConvertible {
  let description: String
}

private struct BrokerReadDiagnostics: CustomStringConvertible {
  var lastCount = 0
  var lastErrno: Int32 = 0
  var eofReads = 0
  var wouldBlockReads = 0

  mutating func record(_ count: Int, error: Int32) {
    lastCount = count
    lastErrno = count < 0 ? error : 0
    if count == 0 { eofReads += 1 }
    if count < 0 && (error == EAGAIN || error == EWOULDBLOCK) { wouldBlockReads += 1 }
  }

  var description: String {
    "last_read=\(lastCount) errno=\(lastErrno) EOF_reads=\(eofReads) EAGAIN_reads=\(wouldBlockReads)"
  }
}

@MainActor final class BrokerSocketClient {
  let fd: Int32
  var decoder = AppshotFrameDecoder()
  var pendingMessages: [AppshotMessage] = []
  init(fd: Int32) { self.fd = fd }
  init(path: String) throws {
    // Own the descriptor locally until it is usable. Throwing after `fd` is set would also run
    // deinit, closing a number that another parallel test may already have reused.
    let descriptor = socket(AF_UNIX, SOCK_STREAM, 0)
    guard descriptor >= 0 else { throw AppshotBrokerError.systemFailure }
    var address = sockaddr_un()
    address.sun_family = sa_family_t(AF_UNIX)
    address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
    withUnsafeMutableBytes(of: &address.sun_path) {
      $0.copyBytes(from: Array((path + "/broker.sock").utf8) + [0])
    }
    let result = withUnsafePointer(to: &address) { pointer in
      pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
        Darwin.connect(descriptor, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
      }
    }
    do {
      guard result == 0 else { throw AppshotBrokerError.systemFailure }
      try AppshotRuntimeDirectory.configureSocket(descriptor)
    } catch {
      Darwin.close(descriptor)
      throw error
    }
    fd = descriptor
  }
  deinit { Darwin.close(fd) }
  func send(_ message: AppshotMessage) throws { try sendBytes(message.encodeFrame()) }
  func sendBytes(_ data: Data) throws {
    let count = data.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
    #expect(count == data.count)
  }
  func receive() async throws -> AppshotMessage {
    var diagnostics = BrokerReadDiagnostics()
    for _ in 0..<100 {
      if !pendingMessages.isEmpty { return pendingMessages.removeFirst() }
      var buffer = [UInt8](repeating: 0, count: 65536)
      let count = read(fd, &buffer, buffer.count)
      diagnostics.record(count, error: errno)
      if count > 0 {
        pendingMessages.append(contentsOf: try decoder.feed(Data(buffer.prefix(count))))
        if !pendingMessages.isEmpty { return pendingMessages.removeFirst() }
      }
      try await Task.sleep(nanoseconds: 5_000_000)
    }
    throw BrokerTestFailure(description: "socket receive exhausted 100 polls: \(diagnostics)")
  }
  func hello(_ descriptor: AppshotRuntimeDescriptor, nonce: String? = nil, start: String? = nil)
    async throws -> AppshotMessage
  {
    let identity = AppshotSystemProcesses().identity(pid: getpid())!
    try send(
      .hello(
        .init(
          type: "hello", version: 1, sessionId: "session", pid: Int(getpid()),
          processStart: start ?? identity.processStart, clientNonce: nonce ?? descriptor.brokerNonce
        )))
    return try await receive()
  }
}

extension BrokerSocketClient {
  /// True once the broker has closed this connection. Parallel test load can delay the broker's
  /// socket loop far beyond a fixed sleep, so the wait is bounded rather than assumed.
  func closedByPeer(timeout: TimeInterval = 2) async -> Bool {
    let deadline = Date().addingTimeInterval(timeout)
    var buffer = [UInt8](repeating: 0, count: 4096)
    while Date() < deadline {
      let count = read(fd, &buffer, buffer.count)
      if count == 0 { return true }
      if count < 0, errno != EAGAIN, errno != EWOULDBLOCK, errno != EINTR { return true }
      try? await Task.sleep(nanoseconds: 5_000_000)
    }
    return false
  }
}

/// Polls a broker-side count until it holds or a bound expires.
@MainActor private func eventually(timeout: TimeInterval = 2, _ condition: () -> Bool) async -> Bool {
  let deadline = Date().addingTimeInterval(timeout)
  while !condition() {
    if Date() >= deadline { return false }
    try? await Task.sleep(nanoseconds: 5_000_000)
  }
  return true
}

extension AppshotBrokerTests {
  @Test func realSocketHandshakeStatusAsyncCompletionAndGrace() async throws {
    let path = "/private/tmp/as-broker-\(UUID().uuidString.prefix(10))"
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    let registrar = BrokerTestRegistrar()
    let clock = BrokerTestClock()
    let controller = try AppshotShortcutController(registrar: registrar, store: BrokerTestStore())
    var captured: ((Result<AppshotCapturedArtifact, Error>) -> Void)?
    var binding: AppshotRecipientBinding?
    var stopped = 0
    var completed = 0
    var destroyed = 0
    var outcomes: [AppshotTransactionOutcome] = []
    let broker = AppshotBroker(
      runtime: runtime, controller: controller, processes: clock,
      capture: { recipient, done in
        binding = recipient
        captured = done
        return {}
      }, onStop: { stopped += 1 }, onOutcome: { _, result in outcomes.append(result) },
      permission: { "ready" })
    try broker.start()
    defer { broker.stop() }
    #expect(registrar.registered == 0)
    let descriptor = try AppshotRuntimeDirectory.readDescriptor(at: path)
    let client = try BrokerSocketClient(path: path)
    let hello = try await client.hello(descriptor)
    #expect(
      hello
        == .helloAck(
          .init(
            type: "hello_ack", version: 1, instanceId: descriptor.instanceID,
            brokerNonce: descriptor.brokerNonce, sessionId: "session")))
    #expect(registrar.registered == 1)
    try client.send(
      .command(
        .init(
          type: "command", version: 1, requestId: "status", brokerId: descriptor.instanceID,
          sessionId: "session", name: "status", argument: "")))
    guard case .status(let status) = try await client.receive() else {
      Issue.record("expected status")
      return
    }
    #expect(status.connectedTuis == 1 && status.registration == "registered")
    #expect(status.permission == "ready")
    #expect(captured == nil)
    try client.send(
      .clientState(
        .init(
          type: "client_state", version: 1, requestId: "state", brokerId: descriptor.instanceID,
          sessionId: "session", activityNs: String(clock.monotonicNS()), appshotCount: 3,
          canAccept: true)))
    _ = try await client.receive()
    registrar.handler? { completed += 1 }
    try await Task.sleep(nanoseconds: 10_000_000)
    #expect(completed == 0)
    let recipient = try #require(binding)
    captured?(
      .success(
        .init(
          manifestPath: path + "/appshot-test.manifest.json", byteCount: 100,
          cleanup: { destroyed += 1 })))
    guard case .attachOffer(let offer) = try await client.receive() else {
      Issue.record("expected offer")
      return
    }
    #expect(offer.requestId == recipient.requestID)
    #expect(completed == 0)
    try client.send(
      .attachAck(
        .init(
          type: "attach_ack", version: 1, requestId: recipient.requestID,
          brokerId: descriptor.instanceID, sessionId: "session", accepted: true, reason: "")))
    guard case .attachCommit = try await client.receive() else {
      Issue.record("expected commit")
      return
    }
    #expect(completed == 0 && destroyed == 0 && outcomes.isEmpty)
    // A nonmatching state is not proof of incorporation.
    try client.send(
      .clientState(
        .init(
          type: "client_state", version: 1,
          requestId: "old-state", brokerId: descriptor.instanceID, sessionId: "session",
          activityNs: String(clock.monotonicNS()), appshotCount: 3, canAccept: true)))
    _ = try await client.receive()
    #expect(completed == 0 && outcomes.isEmpty)
    // Staged offers are excluded from client count; update to four only AFTER commit.
    try client.send(
      .clientState(
        .init(
          type: "client_state", version: 1, requestId: recipient.requestID,
          brokerId: descriptor.instanceID,
          sessionId: "session", activityNs: String(clock.monotonicNS()), appshotCount: 4,
          canAccept: false)))
    _ = try await client.receive()
    registrar.handler? { completed += 1 }
    try await Task.sleep(nanoseconds: 10_000_000)
    #expect(completed == 2)
    #expect(outcomes == [.incorporated, .failed(.attachmentLimitReached)])
    #expect(shutdown(client.fd, SHUT_RDWR) == 0)
    try await Task.sleep(nanoseconds: 20_000_000)
    #expect(broker.authenticatedClientCount == 0 && registrar.unregistered == 1)
    #expect(destroyed == 1 && stopped == 0)
    let reconnect = try BrokerSocketClient(path: path)
    _ = try await reconnect.hello(descriptor)
    #expect(registrar.registered == 2)
    clock.offset += 3_000_000_000
    broker.poll()
    #expect(stopped == 0)
    #expect(shutdown(reconnect.fd, SHUT_RDWR) == 0)
    try await Task.sleep(nanoseconds: 20_000_000)
    clock.offset += 2_000_000_001
    broker.poll()
    #expect(stopped == 1 && registrar.unregistered == 2)
    #expect(!FileManager.default.fileExists(atPath: path + "/broker.sock"))
    #expect(!FileManager.default.fileExists(atPath: path + "/broker.json"))
  }

  @Test func realSocketRejectsNonceStartOversizeAndBoundsUnauthenticatedClients() async throws {
    let path = "/private/tmp/as-broker-\(UUID().uuidString.prefix(10))"
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    let registrar = BrokerTestRegistrar()
    let clock = BrokerTestClock()
    let broker = AppshotBroker(
      runtime: runtime,
      controller: try AppshotShortcutController(registrar: registrar, store: BrokerTestStore()),
      processes: clock,
      capture: { _, _ in
        Issue.record("must not capture")
        return {}
      })
    try broker.start()
    defer { broker.stop() }
    let descriptor = try #require(runtime.descriptor)
    let identity = AppshotSystemProcesses().identity(pid: getpid())!
    for (nonce, start) in [("wrong", identity.processStart), (descriptor.brokerNonce, "1")] {
      let client = try BrokerSocketClient(path: path)
      try client.send(
        .hello(
          .init(
            type: "hello", version: 1, sessionId: "session", pid: Int(getpid()),
            processStart: start, clientNonce: nonce)))
      #expect(await client.closedByPeer())
      #expect(await eventually { broker.socketClientCount == 0 })
    }
    let oversized = try BrokerSocketClient(path: path)
    for _ in 0..<16 {
      try oversized.sendBytes(Data(repeating: 32, count: 4096))
      try await Task.sleep(nanoseconds: 5_000_000)
    }
    try oversized.sendBytes(Data([32]))
    #expect(await oversized.closedByPeer())
    #expect(await eventually { broker.socketClientCount == 0 })
    var raw: [BrokerSocketClient] = []
    for _ in 0..<17 {
      raw.append(try BrokerSocketClient(path: path))
      try await Task.sleep(nanoseconds: 5_000_000)
    }
    // The seventeenth unauthenticated client is refused; the first sixteen stay connected.
    #expect(await raw[16].closedByPeer())
    #expect(await eventually { broker.socketClientCount == 16 })
    #expect(broker.authenticatedClientCount == 0 && registrar.registered == 0)
    clock.offset += 2_000_000_001
    broker.poll()
    #expect(broker.socketClientCount == 0)
    withExtendedLifetime(raw) {}
  }

  @Test func reservationsEnforceQuotaAndAllSixteenClients() throws {
    let processes = AppshotTestProcesses()
    let registry = AppshotClientRegistry(instanceID: "broker", nonce: "nonce", processes: processes)
    for pid: Int32 in 1...16 {
      processes.identities[pid] = .init(pid: pid, uid: getuid(), processStart: String(pid * 10))
      try connect(registry, pid, session: "s\(pid)", connection: "c\(pid)")
    }
    processes.identities[17] = .init(pid: 17, uid: getuid(), processStart: "170")
    #expect(throws: AppshotBrokerError.tooManyClients) {
      try connect(registry, 17, session: "s17", connection: "c17")
    }
    var captures = 0
    outer: for pid in 1...16 {
      try state(registry, "c\(pid)", session: "s\(pid)", activity: UInt64(pid))
      for index in 0..<4 {
        do {
          let binding = try registry.reserveCapture()
          try registry.stage(
            binding,
            artifact: .init(
              manifestPath: "/private/tmp/test",
              byteCount: AppshotClientRegistry.captureReservationBytes, cleanup: {}))
          _ = try registry.acknowledge(
            connectionID: "c\(pid)", requestID: binding.requestID, accepted: true)
          try state(
            registry, "c\(pid)", session: "s\(pid)", activity: UInt64(pid),
            count: index + 1, requestID: binding.requestID)
          captures += 1
        } catch AppshotBrokerError.quotaExceeded { break outer }
      }
    }
    #expect(
      captures == AppshotClientRegistry.maximumLiveBytes
        / AppshotClientRegistry.captureReservationBytes)
    #expect(registry.liveBytes <= AppshotClientRegistry.maximumLiveBytes)
    #expect(throws: AppshotBrokerError.quotaExceeded) { try registry.reserveCapture() }
  }

  @Test func captureDeadlineRejectsAndDestroysLateResult() throws {
    let (registry, processes) = fixture()
    try connect(registry, 10, session: "a", connection: "ca")
    try state(registry, "ca", session: "a", activity: 10)
    let binding = try registry.reserveCapture()
    processes.now += 5_000_000_000
    #expect(registry.expire() == [binding])
    var destroyed = 0
    #expect(throws: AppshotBrokerError.unknownAttachment) {
      try registry.stage(
        binding,
        artifact: .init(
          manifestPath: "/private/tmp/test", byteCount: 100, cleanup: { destroyed += 1 }))
    }
    #expect(destroyed == 1 && registry.liveBytes == 0)
  }
}

extension AppshotBrokerTests {
  @Test func duplicateCaptureCompletionDoesNotRevokeFirstOfferAndTimeoutReleasesGate() async throws
  {
    let path = "/private/tmp/as-broker-\(UUID().uuidString.prefix(10))"
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    let registrar = BrokerTestRegistrar()
    let clock = BrokerTestClock()
    var captured: ((Result<AppshotCapturedArtifact, Error>) -> Void)?
    var destroyed = 0
    var completed = 0
    let broker = AppshotBroker(
      runtime: runtime,
      controller: try AppshotShortcutController(registrar: registrar, store: BrokerTestStore()),
      processes: clock,
      capture: { _, done in
        captured = done
        return {}
      })
    try broker.start()
    defer { broker.stop() }
    let descriptor = try #require(runtime.descriptor)
    let client = try BrokerSocketClient(path: path)
    _ = try await client.hello(descriptor)
    try client.send(
      .clientState(
        .init(
          type: "client_state", version: 1, requestId: "state", brokerId: descriptor.instanceID,
          sessionId: "session", activityNs: String(clock.monotonicNS()), appshotCount: 0,
          canAccept: true)))
    _ = try await client.receive()
    registrar.handler? { completed += 1 }
    try await Task.sleep(nanoseconds: 10_000_000)
    let artifact = AppshotCapturedArtifact(
      manifestPath: path + "/test", byteCount: 100, cleanup: { destroyed += 1 })
    captured?(.success(artifact))
    guard case .attachOffer(let offer) = try await client.receive() else {
      Issue.record("expected offer")
      return
    }
    captured?(.success(artifact))
    #expect(destroyed == 0 && completed == 0)
    clock.offset += 3_000_000_001
    broker.poll()
    guard case .attachRevoke = try await client.receive() else {
      Issue.record("expected revoke")
      return
    }
    #expect(destroyed == 1 && completed == 1)
    try client.send(
      .attachAck(
        .init(
          type: "attach_ack", version: 1, requestId: offer.requestId,
          brokerId: descriptor.instanceID, sessionId: "session", accepted: true, reason: "")))
    guard case .attachRevoke = try await client.receive() else {
      Issue.record("late ACK must not commit")
      return
    }
    #expect(destroyed == 1 && completed == 1)
    registrar.handler? { completed += 1 }
    try await Task.sleep(nanoseconds: 10_000_000)
    clock.offset += 5_000_000_001
    broker.poll()
    #expect(completed == 2)
    captured?(
      .success(.init(manifestPath: path + "/late", byteCount: 100, cleanup: { destroyed += 1 })))
    #expect(destroyed == 2 && completed == 2)
  }

  @Test func losingRuntimeAuthorityStopsBrokerAndLeavesReplacementUntouched() async throws {
    let path = "/private/tmp/as-broker-\(UUID().uuidString.prefix(10))"
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    let registrar = BrokerTestRegistrar()
    var stopped = 0
    let broker = AppshotBroker(
      runtime: runtime,
      controller: try AppshotShortcutController(registrar: registrar, store: BrokerTestStore()),
      capture: { _, _ in return {} }, onStop: { stopped += 1 })
    try broker.start()
    defer { broker.stop() }
    let client = try BrokerSocketClient(path: path)
    _ = try await client.hello(try #require(runtime.descriptor))
    #expect(unlink(path + "/broker.sock") == 0)
    try Data("replacement".utf8).write(to: URL(fileURLWithPath: path + "/broker.sock"))
    broker.poll()
    #expect(stopped == 1 && registrar.unregistered == 1)
    #expect(try String(contentsOfFile: path + "/broker.sock") == "replacement")
  }
}

@MainActor private final class BrokerChildClient {
  let process = Process()
  let input = Pipe(), output = Pipe()
  var decoder = AppshotFrameDecoder()
  init(path: String, startupDelay: TimeInterval = 0) throws {
    process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
    process.arguments = [
      "-u", "-c",
      "import socket,sys,time; time.sleep(float(sys.argv[2])); s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]+'/broker.sock'); f=s.makefile('rb'); [(s.sendall(line.encode()), sys.stdout.buffer.write(f.readline()), sys.stdout.buffer.flush()) for line in sys.stdin]",
      path, String(startupDelay),
    ]
    process.standardInput = input
    process.standardOutput = output
    try process.run()
    #expect(fcntl(output.fileHandleForReading.fileDescriptor, F_SETFL, O_NONBLOCK) == 0)
  }
  func exchange(_ message: AppshotMessage) async throws -> AppshotMessage {
    try input.fileHandleForWriting.write(contentsOf: message.encodeFrame())
    var diagnostics = BrokerReadDiagnostics()
    // This includes launching the real interpreter. Keep a bounded elapsed
    // deadline without confusing cold startup with the disconnect under test.
    let clock = ContinuousClock()
    let deadline = clock.now.advanced(by: .seconds(5))
    while clock.now < deadline {
      var buffer = [UInt8](repeating: 0, count: 65536)
      let count = read(output.fileHandleForReading.fileDescriptor, &buffer, buffer.count)
      diagnostics.record(count, error: errno)
      if count > 0, let message = try decoder.feed(Data(buffer.prefix(count))).first {
        return message
      }
      if count <= 0 && !process.isRunning { break }
      try await Task.sleep(nanoseconds: 5_000_000)
    }
    let childState = process.isRunning ? "running" : "exited(\(process.terminationStatus))"
    throw BrokerTestFailure(
      description: "child exchange did not complete within 5s: \(diagnostics) child=\(childState)")
  }
  func terminate() {
    if process.isRunning {
      process.terminate()
      process.waitUntilExit()
    }
  }
  deinit {
    if process.isRunning {
      process.terminate()
      process.waitUntilExit()
    }
  }
}

extension AppshotBrokerTests {
  @Test func frozenRecipientDisconnectFinishesAndCancelsWhileAnotherTUIStays() async throws {
    var stage = "runtime initialization"
    do {
      let path = "/private/tmp/as-broker-\(UUID().uuidString.prefix(10))"
      defer { try? FileManager.default.removeItem(atPath: path) }
      let runtime = try AppshotRuntimeDirectory(path: path)
      let registrar = BrokerTestRegistrar()
      let clock = BrokerTestClock()
      var captured: ((Result<AppshotCapturedArtifact, Error>) -> Void)?
      var binding: AppshotRecipientBinding?
      var completed = 0
      var canceled = 0
      var destroyed = 0
      let broker = AppshotBroker(
        runtime: runtime,
        controller: try AppshotShortcutController(registrar: registrar, store: BrokerTestStore()),
        processes: clock,
        capture: { recipient, done in
          binding = recipient
          captured = done
          return { canceled += 1 }
        })
      stage = "broker startup"
      try broker.start()
      defer { broker.stop() }
      let descriptor = try #require(runtime.descriptor)
      stage = "other client connect"
      let other = try BrokerSocketClient(path: path)
      stage = "other client hello"
      _ = try await other.hello(descriptor)
      stage = "other client state"
      try other.send(
        .clientState(
          .init(
            type: "client_state", version: 1, requestId: "otherstate",
            brokerId: descriptor.instanceID, sessionId: "session", activityNs: "1", appshotCount: 0,
            canAccept: true)))
      _ = try await other.receive()
      stage = "recipient child startup and identity"
      // Cold interpreter startup may exceed the old 100 x 5 ms poll loop.
      // Reproduce that delay independently of the host's current load.
      let recipient = try BrokerChildClient(path: path, startupDelay: 1)
      defer { recipient.terminate() }
      let identity = try #require(
        AppshotSystemProcesses().identity(pid: recipient.process.processIdentifier))
      stage = "recipient child hello"
      _ = try await recipient.exchange(
        .hello(
          .init(
            type: "hello", version: 1, sessionId: "recipient", pid: Int(identity.pid),
            processStart: identity.processStart, clientNonce: descriptor.brokerNonce)))
      stage = "recipient child state"
      _ = try await recipient.exchange(
        .clientState(
          .init(
            type: "client_state", version: 1, requestId: "state", brokerId: descriptor.instanceID,
            sessionId: "recipient", activityNs: String(clock.monotonicNS()), appshotCount: 0,
            canAccept: true)))
      stage = "capture trigger"
      registrar.handler? { completed += 1 }
      try await Task.sleep(nanoseconds: 10_000_000)
      #expect(binding?.sessionID == "recipient" && completed == 0)
      // Another authenticated session cannot release or complete this capture.
      stage = "other client release"
      try other.send(
        .release(
          .init(
            type: "release", version: 1, requestId: try #require(binding).requestID,
            brokerId: descriptor.instanceID, sessionId: "session")))
      guard case .releaseAck(let release) = try await other.receive() else {
        Issue.record("expected release response")
        return
      }
      #expect(!release.released && completed == 0)
      stage = "recipient disconnect"
      recipient.terminate()
      try await Task.sleep(nanoseconds: 20_000_000)
      #expect(broker.authenticatedClientCount == 1 && registrar.unregistered == 0)
      #expect(completed == 1 && canceled == 1)
      stage = "late capture cleanup"
      captured?(
        .success(.init(manifestPath: path + "/late", byteCount: 100, cleanup: { destroyed += 1 })))
      #expect(destroyed == 1 && completed == 1)
      stage = "capture trigger"
      registrar.handler? { completed += 1 }
      try await Task.sleep(nanoseconds: 10_000_000)
      #expect(binding?.sessionID == "session")
      stage = "broker shutdown"
      broker.stop()
      #expect(completed == 2 && canceled == 2)
    } catch {
      throw BrokerTestFailure(description: "phase=\(stage): \(error)")
    }
  }

  @Test func descriptorClosesAfterEveryDispatchSourceCancellation() {
    var closed: [Int32] = []
    let connection = AppshotBroker.Connection(
      fd: 42, peer: .init(pid: 10, uid: getuid(), processStart: "100"), opened: 0,
      closeFD: { closed.append($0) })
    // Read source plus one active and one already-cancelled-but-not-completed write source.
    connection.sourceCount = 3
    connection.close()
    #expect(closed.isEmpty)
    connection.sourceCancelled()
    connection.sourceCancelled()
    #expect(closed.isEmpty)
    connection.sourceCancelled()
    #expect(closed == [42])
    connection.close()
    #expect(closed == [42])
  }
}

extension AppshotBrokerTests {
  @Test func synchronousCaptureResultDoesNotRetainCancellationForCommittedAttachment() async throws
  {
    let path = "/private/tmp/as-broker-\(UUID().uuidString.prefix(10))"
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    let registrar = BrokerTestRegistrar()
    var completed = 0
    var canceled = 0
    var destroyed = 0
    let broker = AppshotBroker(
      runtime: runtime,
      controller: try AppshotShortcutController(registrar: registrar, store: BrokerTestStore()),
      capture: { _, done in
        done(
          .success(.init(manifestPath: path + "/test", byteCount: 100, cleanup: { destroyed += 1 }))
        )
        return { canceled += 1 }
      })
    try broker.start()
    defer { broker.stop() }
    let descriptor = try #require(runtime.descriptor)
    let client = try BrokerSocketClient(path: path)
    _ = try await client.hello(descriptor)
    try client.send(
      .clientState(
        .init(
          type: "client_state", version: 1, requestId: "state", brokerId: descriptor.instanceID,
          sessionId: "session", activityNs: "1", appshotCount: 0, canAccept: true)))
    _ = try await client.receive()
    registrar.handler? { completed += 1 }
    guard case .attachOffer(let offer) = try await client.receive() else {
      Issue.record("expected offer")
      return
    }
    try client.send(
      .attachAck(
        .init(
          type: "attach_ack", version: 1, requestId: offer.requestId,
          brokerId: descriptor.instanceID, sessionId: "session", accepted: true, reason: "")))
    _ = try await client.receive()
    #expect(completed == 0 && canceled == 0 && destroyed == 0)
    broker.stop()
    #expect(completed == 1 && canceled == 0 && destroyed == 1)
  }
}

extension AppshotBrokerTests {
  @Test func registrationFailureReportsUnavailableWithoutClaimingConflict() async throws {
    let path = "/private/tmp/as-broker-\(UUID().uuidString.prefix(10))"
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    let registrar = BrokerTestRegistrar()
    registrar.failure = .hotKeyRegistrationFailed
    let broker = AppshotBroker(
      runtime: runtime,
      controller: try AppshotShortcutController(registrar: registrar, store: BrokerTestStore()),
      capture: { _, _ in return {} })
    try broker.start()
    defer { broker.stop() }
    let descriptor = try #require(runtime.descriptor)
    let client = try BrokerSocketClient(path: path)
    _ = try await client.hello(descriptor)
    try client.send(
      .command(
        .init(
          type: "command", version: 1, requestId: "status", brokerId: descriptor.instanceID,
          sessionId: "session", name: "status", argument: "")))
    guard case .status(let status) = try await client.receive() else {
      Issue.record("expected status")
      return
    }
    #expect(status.registration == "unavailable")
    #expect(broker.authenticatedClientCount == 1)
  }
}

extension AppshotBrokerTests {
  @Test func socketClientRetainsCoalescedFrames() async throws {
    var pair: [Int32] = [0, 0]
    #expect(socketpair(AF_UNIX, SOCK_STREAM, 0, &pair) == 0)
    let client = BrokerSocketClient(fd: pair[0])
    defer { Darwin.close(pair[1]) }
    try AppshotRuntimeDirectory.configureSocket(pair[0])
    let first = AppshotMessage.helloAck(
      .init(
        type: "hello_ack", version: 1,
        instanceId: "broker", brokerNonce: "nonce", sessionId: "first"))
    let second = AppshotMessage.helloAck(
      .init(
        type: "hello_ack", version: 1,
        instanceId: "broker", brokerNonce: "nonce", sessionId: "second"))
    let bytes = try first.encodeFrame() + second.encodeFrame()
    #expect(bytes.withUnsafeBytes { write(pair[1], $0.baseAddress, $0.count) } == bytes.count)
    #expect(try await client.receive() == first)
    #expect(try await client.receive() == second)
  }

  @Test(arguments: [false, true]) func retainedPendingCommitBlocksCapture(staleState: Bool)
    async throws
  {
    let path = "/private/tmp/as-broker-\(UUID().uuidString.prefix(10))"
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    let registrar = BrokerTestRegistrar()
    let clock = BrokerTestClock()
    var captures = 0
    var destroyed = 0
    var completed = 0
    var canceled = 0
    let broker = AppshotBroker(
      runtime: runtime,
      controller: try AppshotShortcutController(registrar: registrar, store: BrokerTestStore()),
      processes: clock,
      capture: { _, done in
        captures += 1
        done(
          .success(
            .init(
              manifestPath: path + "/test", byteCount: 100,
              cleanup: { destroyed += 1 })))
        return { canceled += 1 }
      })
    try broker.start()
    defer { broker.stop() }
    let descriptor = try #require(runtime.descriptor)
    let client = try BrokerSocketClient(path: path)
    _ = try await client.hello(descriptor)
    func sendState(_ request: String, count: Int) throws {
      try client.send(
        .clientState(
          .init(
            type: "client_state", version: 1,
            requestId: request, brokerId: descriptor.instanceID, sessionId: "session",
            activityNs: "1", appshotCount: count, canAccept: count < 4)))
    }
    try sendState("before", count: 3)
    _ = try await client.receive()
    registrar.handler? { completed += 1 }
    guard case .attachOffer(let offer) = try await client.receive() else {
      Issue.record("expected offer")
      return
    }
    let ack = AppshotMessage.attachAck(
      .init(
        type: "attach_ack", version: 1,
        requestId: offer.requestId, brokerId: descriptor.instanceID, sessionId: "session",
        accepted: true, reason: ""))
    try client.send(ack)
    // This state is written before receiving commit, but processed after ACK.
    if staleState { try sendState("precommit", count: 3) }
    guard case .attachCommit = try await client.receive() else {
      Issue.record("expected commit")
      return
    }
    if staleState { _ = try await client.receive() }
    registrar.handler? { completed += 1 }
    try await Task.sleep(nanoseconds: 10_000_000)
    #expect(captures == 1 && completed == 1)
    #expect(destroyed == 0 && canceled == 0)
    if staleState {
      // The retained pending submission resolved; confirmation reports current count.
      try sendState(offer.requestId, count: 1)
      _ = try await client.receive()
      #expect(destroyed == 0)
      registrar.handler? { completed += 1 }
      try await Task.sleep(nanoseconds: 10_000_000)
      #expect(captures == 2)
    } else {
      clock.offset += 3_000_000_001
      broker.poll()
      #expect(broker.authenticatedClientCount == 0)
      #expect(destroyed == 1)
    }
  }

  @Test func commitBarrierReleaseDisconnectAndConfirmationCustody() throws {
    let (registry, processes) = fixture()
    try connect(registry, 10, session: "a", connection: "ca")
    try state(registry, "ca", session: "a", activity: 10, count: 3)
    var destroyed = 0
    func commit() throws -> AppshotRecipientBinding {
      let binding = try registry.reserveCapture()
      try registry.stage(
        binding,
        artifact: .init(
          manifestPath: "/private/tmp/test",
          byteCount: 100, cleanup: { destroyed += 1 }))
      _ = try registry.acknowledge(connectionID: "ca", requestID: binding.requestID, accepted: true)
      return binding
    }
    let first = try commit()
    #expect(registry.state(connectionID: "ca")?.canAccept == false)
    #expect(registry.release(connectionID: "other", requestID: first.requestID) == false)
    #expect(throws: AppshotBrokerError.attachmentLimitReached) { try registry.reserveCapture() }
    #expect(registry.release(connectionID: "ca", requestID: first.requestID))
    let second = try commit()
    try state(registry, "ca", session: "a", activity: 10, count: 4, requestID: second.requestID)
    #expect(throws: AppshotBrokerError.attachmentLimitReached) { try registry.reserveCapture() }
    try state(registry, "ca", session: "a", activity: 10, count: 1, requestID: second.requestID)
    processes.now += 3_000_000_001
    #expect(registry.expire().isEmpty)
    #expect(registry.confirmationExpiredConnections().isEmpty)
    #expect(destroyed == 1)
    let third = try commit()
    #expect(third.requestID != second.requestID)
    registry.disconnect(connectionID: "ca")
    #expect(destroyed == 3 && registry.liveBytes == 0)
  }
}

extension AppshotBrokerTests {
  @Test func commitConfirmationDeadlineCannotBeExtendedByStaleState() throws {
    let (registry, processes) = fixture()
    try connect(registry, 10, session: "a", connection: "ca")
    try state(registry, "ca", session: "a", activity: 10)
    let binding = try registry.reserveCapture()
    try registry.stage(
      binding,
      artifact: .init(
        manifestPath: "/private/tmp/test",
        byteCount: 100, cleanup: {}))
    _ = try registry.acknowledge(connectionID: "ca", requestID: binding.requestID, accepted: true)
    processes.now += 2_000_000_000
    try state(registry, "ca", session: "a", activity: 10, count: 0)
    #expect(throws: AppshotBrokerError.attachmentLimitReached) { try registry.reserveCapture() }
    processes.now += 1_000_000_000
    #expect(registry.confirmationExpiredConnections() == ["ca"])
    #expect(throws: AppshotBrokerError.captureExpired) {
      try state(registry, "ca", session: "a", activity: 10, count: 1, requestID: binding.requestID)
    }
    registry.disconnect(connectionID: "ca")
    #expect(registry.confirmationExpiredConnections().isEmpty)
  }

  @Test func releaseClearsCommitBarrierBeforeSynchronousCleanup() throws {
    let (registry, _) = fixture()
    try connect(registry, 10, session: "a", connection: "ca")
    try state(registry, "ca", session: "a", activity: 10, count: 3)
    let binding = try registry.reserveCapture()
    var next: AppshotRecipientBinding?
    try registry.stage(
      binding,
      artifact: .init(
        manifestPath: "/private/tmp/test",
        byteCount: 100, cleanup: { next = try? registry.reserveCapture() }))
    _ = try registry.acknowledge(connectionID: "ca", requestID: binding.requestID, accepted: true)
    #expect(registry.release(connectionID: "ca", requestID: binding.requestID))
    let reserved = try #require(next)
    #expect(reserved.requestID != binding.requestID)
    registry.cancelCapture(reserved)
  }
}

extension AppshotBrokerTests {
  @Test func duplicateAckPreservesPendingConfirmedAndNewerCustody() async throws {
    let path = "/private/tmp/as-dup-\(UUID().uuidString.prefix(10))"
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    let registrar = BrokerTestRegistrar()
    var destroyed = 0
    var completed = 0
    var outcomes: [AppshotTransactionOutcome] = []
    let broker = AppshotBroker(
      runtime: runtime,
      controller: try AppshotShortcutController(registrar: registrar, store: BrokerTestStore()),
      capture: { _, done in
        done(
          .success(.init(manifestPath: path + "/owned", byteCount: 1, cleanup: { destroyed += 1 })))
        return {}
      }, onOutcome: { _, result in outcomes.append(result) }, permission: { "ready" })
    try broker.start()
    defer { broker.stop() }
    let descriptor = try #require(runtime.descriptor)
    let client = try BrokerSocketClient(path: path)
    _ = try await client.hello(descriptor)
    func state(_ request: String) async throws {
      try client.send(
        .clientState(
          .init(
            type: "client_state", version: 1, requestId: request,
            brokerId: descriptor.instanceID, sessionId: "session", activityNs: "1",
            appshotCount: 0, canAccept: true)))
      _ = try await client.receive()
    }
    func ack(_ request: String) throws {
      try client.send(
        .attachAck(
          .init(
            type: "attach_ack", version: 1, requestId: request,
            brokerId: descriptor.instanceID, sessionId: "session", accepted: true, reason: "")))
    }
    func status() async throws -> Bool {
      try client.send(
        .command(
          .init(
            type: "command", version: 1, requestId: "status",
            brokerId: descriptor.instanceID, sessionId: "session", name: "status", argument: "")))
      if case .status = try await client.receive() { return true }
      return false
    }
    try await state("initial")
    registrar.handler? { completed += 1 }
    guard case .attachOffer(let first) = try await client.receive() else {
      Issue.record("expected offer")
      return
    }
    try ack(first.requestId)
    _ = try await client.receive()
    try ack(first.requestId)
    guard try await status() else {
      Issue.record("duplicate pending ACK emitted revoke")
      return
    }
    #expect(outcomes.isEmpty && completed == 0 && destroyed == 0)
    try await state(first.requestId)
    #expect(outcomes == [.incorporated] && completed == 1)
    try ack(first.requestId)
    guard try await status() else {
      Issue.record("duplicate confirmed ACK emitted revoke")
      return
    }
    #expect(outcomes == [.incorporated] && completed == 1 && destroyed == 0)
    try client.send(
      .release(
        .init(
          type: "release", version: 1, requestId: first.requestId,
          brokerId: descriptor.instanceID, sessionId: "session")))
    _ = try await client.receive()
    #expect(destroyed == 1)
    registrar.handler? { completed += 1 }
    guard case .attachOffer(let next) = try await client.receive() else {
      Issue.record("expected next offer")
      return
    }
    try ack(first.requestId)
    guard case .attachRevoke(let stale) = try await client.receive() else {
      Issue.record("expected only stale request revoke")
      return
    }
    #expect(stale.requestId == first.requestId)
    #expect(completed == 1 && destroyed == 1 && outcomes == [.incorporated])
    try ack(next.requestId)
    _ = try await client.receive()
    try await state(next.requestId)
    #expect(completed == 2 && destroyed == 1 && outcomes == [.incorporated, .incorporated])
  }
}

extension AppshotBrokerTests {
  @Test func shortcutReplacementConflictPreservesLiveRegistrationAndCanonicalError() async throws {
    let path = "/private/tmp/as-settings-\(UUID().uuidString.prefix(10))"
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    let registrar = BrokerTestRegistrar()
    let broker = AppshotBroker(
      runtime: runtime,
      controller: try AppshotShortcutController(registrar: registrar, store: BrokerTestStore()),
      capture: { _, _ in
        Issue.record("settings must never capture")
        return {}
      }, permission: { "ready" })
    try broker.start()
    defer { broker.stop() }
    let descriptor = try #require(runtime.descriptor)
    let client = try BrokerSocketClient(path: path)
    _ = try await client.hello(descriptor)
    registrar.failure = .shortcutConflict
    try client.send(
      .command(
        .init(
          type: "command", version: 1, requestId: "change",
          brokerId: descriptor.instanceID, sessionId: "session", name: "shortcut",
          argument: "Control+Shift+X")))
    guard case .commandResult(let response) = try await client.receive() else {
      Issue.record("expected command response")
      return
    }
    #expect(!response.ok && response.code == "shortcut_conflict")
    try client.send(
      .command(
        .init(
          type: "command", version: 1, requestId: "status",
          brokerId: descriptor.instanceID, sessionId: "session", name: "status", argument: "")))
    guard case .status(let status) = try await client.receive() else {
      Issue.record("expected status")
      return
    }
    #expect(status.registration == "registered" && status.chord == "Control+Shift+Z")
    #expect(registrar.unregistered == 0)
  }
}

extension AppshotBrokerTests {
  @Test func incorporationCompletionMayStopBrokerWithoutReadingRemovedClient() async throws {
    let path = "/private/tmp/as-reenter-\(UUID().uuidString.prefix(10))"
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    let registrar = BrokerTestRegistrar()
    var stopped = 0
    var destroyed = 0
    let broker = AppshotBroker(
      runtime: runtime,
      controller: try AppshotShortcutController(registrar: registrar, store: BrokerTestStore()),
      capture: { _, done in
        done(
          .success(.init(manifestPath: path + "/owned", byteCount: 1, cleanup: { destroyed += 1 })))
        return {}
      }, onStop: { stopped += 1 })
    try broker.start()
    defer { broker.stop() }
    let descriptor = try #require(runtime.descriptor)
    let client = try BrokerSocketClient(path: path)
    _ = try await client.hello(descriptor)
    func state(_ request: String) throws {
      try client.send(
        .clientState(
          .init(
            type: "client_state", version: 1, requestId: request,
            brokerId: descriptor.instanceID, sessionId: "session", activityNs: "1", appshotCount: 0,
            canAccept: true)))
    }
    try state("initial")
    _ = try await client.receive()
    registrar.handler? { broker.stop() }
    guard case .attachOffer(let offer) = try await client.receive() else {
      Issue.record("expected offer")
      return
    }
    try client.send(
      .attachAck(
        .init(
          type: "attach_ack", version: 1, requestId: offer.requestId,
          brokerId: descriptor.instanceID, sessionId: "session", accepted: true, reason: "")))
    _ = try await client.receive()
    try state(offer.requestId)
    try await Task.sleep(nanoseconds: 20_000_000)
    #expect(stopped == 1 && destroyed == 1 && registrar.unregistered == 1)
  }
}
