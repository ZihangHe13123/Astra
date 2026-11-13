import Darwin
import Foundation
import Testing

@testable import AstraMacComputerHelperCore

@Suite @MainActor struct AppshotDaemonTests {
  @Test func modesAreExactAndIdentityIsReadOnlyParentIdentity() throws {
    #expect(AppshotHelperMode(arguments: []) == .computerUse)
    #expect(AppshotHelperMode(arguments: ["--appshot-daemon"]) == .daemon)
    #expect(AppshotHelperMode(arguments: ["--appshot-client-identity"]) == .clientIdentity)
    for args in [
      ["--appshot-daemon", "extra"], ["--appshot-client-identity", "--capture"], ["--capture"],
      [""], ["--appshot-daemon=true"],
    ] {
      #expect(AppshotHelperMode(arguments: args) == .unsupported)
    }
    let data = try appshotClientIdentityJSON()
    let json = try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
    #expect(Set(json.keys) == ["pid", "uid", "process_start", "monotonic_ns"])
    let expected = try #require(AppshotSystemProcesses().identity(pid: getppid()))
    #expect(json["pid"] as? Int32 == expected.pid)
    #expect(json["uid"] as? UInt32 == expected.uid)
    #expect(json["process_start"] as? String == expected.processStart)
    #expect(UInt64(json["monotonic_ns"] as? String ?? "") != nil)
    #expect(data.count < 256)
  }
  @Test func nativeErrorsAreBoundedAndCanonical() throws {
    #expect(AppshotErrorCode.map(AppshotCaptureError.permissionDenied) == .permissionUnavailable)
    #expect(AppshotErrorCode.map(AppshotCaptureError.protectedContext) == .protectedUI)
    #expect(AppshotErrorCode.map(AppshotCaptureError.timedOut) == .captureTimeout)
    #expect(AppshotErrorCode.map(AppshotCaptureError.cancelled) == .captureCancelled)
    #expect(AppshotErrorCode.map(AppshotBrokerError.quotaExceeded) == .resourceLimitReached)
    #expect(
      AppshotErrorCode.map(AXTextDetailSerializationError.attributeReadFailed)
        == .axObservationFailed)
    #expect(AppshotErrorCode.map(NSError(domain: "SECRET /path/title", code: 42)) == .captureFailed)
    let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
      .deletingLastPathComponent().appendingPathComponent("Fixtures/appshot_error_codes_v1.json")
    let codes = try JSONDecoder().decode([String].self, from: Data(contentsOf: url))
    #expect(Set(codes) == Set(AppshotErrorCode.allCases.map(\.rawValue)))
  }
}

extension AppshotDaemonTests {
  @Test func daemonHidesPriorHUDBeforeCaptureAndStopsAfterUnregisterAndCleanup() async throws {
    let path = "/private/tmp/as-daemon-\(UUID().uuidString.prefix(10))"
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    let registrar = BrokerTestRegistrar()
    var messages: [AppshotHUDMessage?] = []
    let hud = AppshotHUD(render: { messages.append($0) }, schedule: { _, _ in })
    var callback: AppshotBroker.Capture?
    var captured: (@MainActor (Result<AppshotCapturedArtifact, Error>) -> Void)?
    var finished = 0
    var cancelled = 0
    var exits = 0
    callback = { binding, completion in
      #expect(binding.sessionID == "session")
      #expect(messages.last! == nil)
      captured = completion
      return { cancelled += 1 }
    }
    let daemon = AppshotDaemon(
      runtime: runtime,
      controller: try AppshotShortcutController(registrar: registrar, store: BrokerTestStore()),
      hud: hud, capture: callback!,
      onStop: {
        #expect(registrar.unregistered == 1)
        #expect(!FileManager.default.fileExists(atPath: path + "/broker.sock"))
        #expect(messages.last! == nil)
        exits += 1
      })
    try daemon.start()
    let client = try BrokerSocketClient(path: path)
    let descriptor = try #require(runtime.descriptor)
    _ = try await client.hello(descriptor)
    try client.send(
      .clientState(
        .init(
          type: "client_state", version: 1,
          requestId: "state", brokerId: descriptor.instanceID, sessionId: "session",
          activityNs: "1", appshotCount: 0, canAccept: true)))
    _ = try await client.receive()
    registrar.handler? { finished += 1 }
    try await Task.sleep(nanoseconds: 10_000_000)
    #expect(captured != nil && finished == 0)
    daemon.stop()
    #expect(cancelled == 1 && finished == 1 && exits == 1)
    var destroyed = 0
    captured?(.success(.init(manifestPath: "opaque", byteCount: 1, cleanup: { destroyed += 1 })))
    #expect(destroyed == 1)
    #expect(!messages.contains(.success))
    daemon.stop()
    #expect(exits == 1)
  }
  @Test(arguments: [false, true])
  func pipelineKeepsScreenshotWhenOptionalProjectionFails(axFails: Bool) async throws {
    let path = "/private/tmp/as-pipe-\(UUID().uuidString.prefix(10))"
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    #expect(try runtime.elect())
    _ = try runtime.publishListener()
    defer { runtime.close() }
    let fixture = try AppshotArtifactTests().captured(runtime)
    let pipeline = AppshotCapturePipeline(
      runtime: runtime,
      begin: { recipient, deadline, done in
        #expect(recipient == fixture.authority.recipient)
        done(
          .success(
            .init(
              authority: fixture.authority, png: fixture.png,
              width: fixture.width, height: fixture.height, deadline: deadline)))
        return { deadline.cancel() }
      },
      projection: { result in
        #expect(!Thread.isMainThread)
        if axFails { throw AXTextDetailSerializationError.attributeReadFailed }
        return try AppshotProjectionBuilder().build(
          provider: AppshotAXFixture(), deadline: result.deadline)
      },
      revalidate: { result in
        #expect(!Thread.isMainThread)
        _ = try result.deadline.remaining()
      })
    let result: Result<AppshotCapturedArtifact, Error> = await withCheckedContinuation {
      continuation in
      _ = pipeline.capture(fixture.authority.recipient) { result in
        MainActor.assertIsolated()
        continuation.resume(returning: result)
      }
    }
    let artifact = try result.get()
    #expect(FileManager.default.fileExists(atPath: artifact.manifestPath))
    let manifest = try AppshotManifest.decodeStrict(Data(contentsOf: URL(fileURLWithPath: artifact.manifestPath)))
    #expect(manifest.ax.coverage == (axFails ? .unavailable : .reportedAXSubtree))
    #expect(manifest.png.size == fixture.png.count)
    if axFails { #expect(manifest.ax.nodeCount == 0 && manifest.ax.truncated) }
    artifact.destroy()
    #expect(runtime.descriptor?.entries.count == 1)
  }

  @Test func pipelinePreservesDeliveredTimeoutAfterSharedDeadlineIsCancelled() async throws {
    let path = "/private/tmp/as-pipe-timeout-\(UUID().uuidString.prefix(10))"
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    #expect(try runtime.elect())
    _ = try runtime.publishListener()
    defer { runtime.close() }
    let recipient = AppshotRecipientBinding(
      requestID: "request", connectionID: "connection", sessionID: "session",
      identity: .init(pid: getpid(), uid: getuid(), processStart: "1"),
      instanceID: runtime.descriptor!.instanceID)
    let pipeline = AppshotCapturePipeline(
      runtime: runtime,
      begin: { _, deadline, done in
        deadline.cancel()
        done(.failure(AppshotCaptureError.timedOut))
        done(.failure(AppshotCaptureError.timedOut))
        return {}
      },
      projection: { _ in
        Issue.record("failed capture must not enter projection")
        throw AppshotCaptureError.captureFailed
      },
      revalidate: { _ in Issue.record("failed capture must not enter revalidation") })
    let result: Result<AppshotCapturedArtifact, Error> = await withCheckedContinuation {
      continuation in
      _ = pipeline.capture(recipient) { continuation.resume(returning: $0) }
    }
    #expect(throws: AppshotErrorCode.captureTimeout) { try result.get() }
    #expect(runtime.descriptor?.entries.count == 1)
  }
}

extension AppshotDaemonTests {
  @Test(arguments: [
    "success", "reject", "offer-timeout", "disconnect", "release", "commit-timeout",
    "capture-error",
  ])
  func terminalOutcomesNeverShowPrematureSuccess(mode: String) async throws {
    let path = "/private/tmp/as-outcome-\(UUID().uuidString.prefix(10))"
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    let registrar = BrokerTestRegistrar()
    let clock = BrokerTestClock()
    var outcomes: [AppshotTransactionOutcome] = []
    var finished = 0
    var destroyed = 0
    var captures = 0
    let hud = AppshotHUD(
      render: { message in
        if let message, case .success = message { #expect(mode == "success") }
      }, schedule: { _, _ in })
    let broker = AppshotBroker(
      runtime: runtime,
      controller: try AppshotShortcutController(registrar: registrar, store: BrokerTestStore()),
      processes: clock,
      capture: { binding, done in
        #expect(binding.sessionID == "session")
        captures += 1
        if mode == "capture-error" {
          done(.failure(AppshotCaptureError.protectedContext))
        } else {
          done(
            .success(
              .init(manifestPath: path + "/owned", byteCount: 1, cleanup: { destroyed += 1 })))
        }
        return {}
      },
      onOutcome: { _, outcome in
        outcomes.append(outcome)
        switch outcome {
        case .incorporated: hud.show(.success)
        case .failed(let code): hud.show(.failure(code))
        }
      }, permission: { "unavailable" })
    try broker.start()
    defer { broker.stop() }
    let descriptor = try #require(runtime.descriptor)
    let client = try BrokerSocketClient(path: path)
    _ = try await client.hello(descriptor)
    try client.send(
      .clientState(
        .init(
          type: "client_state", version: 1,
          requestId: "state", brokerId: descriptor.instanceID, sessionId: "session",
          activityNs: "1", appshotCount: 0, canAccept: true)))
    _ = try await client.receive()
    registrar.handler? { finished += 1 }
    if mode == "capture-error" {
      try await Task.sleep(nanoseconds: 10_000_000)
      #expect(outcomes == [.failed(.protectedUI)] && finished == 1 && destroyed == 0)
      return
    }
    guard case .attachOffer(let offer) = try await client.receive() else {
      Issue.record("expected offer")
      return
    }
    #expect(outcomes.isEmpty && finished == 0 && captures == 1)
    if mode == "disconnect" {
      #expect(shutdown(client.fd, SHUT_RDWR) == 0)
      #expect(await eventually { !outcomes.isEmpty })
      #expect(outcomes == [.failed(.recipientDisconnected)])
    } else if mode == "offer-timeout" {
      clock.offset += 3_000_000_001
      broker.poll()
      _ = try await client.receive()
      #expect(outcomes == [.failed(.captureTimeout)])
    } else {
      try client.send(
        .attachAck(
          .init(
            type: "attach_ack", version: 1,
            requestId: offer.requestId, brokerId: descriptor.instanceID, sessionId: "session",
            accepted: mode != "reject", reason: "")))
      _ = try await client.receive()
      if mode == "reject" {
        #expect(outcomes == [.failed(.attachmentRejected)])
      } else {
        #expect(outcomes.isEmpty && finished == 0)
        if mode == "release" {
          try client.send(
            .release(
              .init(
                type: "release", version: 1, requestId: offer.requestId,
                brokerId: descriptor.instanceID, sessionId: "session")))
          _ = try await client.receive()
          #expect(outcomes == [.failed(.attachmentRejected)])
        } else if mode == "commit-timeout" {
          clock.offset += 3_000_000_001
          broker.poll()
          #expect(outcomes == [.failed(.captureTimeout)])
        } else {
          try client.send(
            .clientState(
              .init(
                type: "client_state", version: 1,
                requestId: offer.requestId, brokerId: descriptor.instanceID, sessionId: "session",
                activityNs: "1", appshotCount: 1, canAccept: true)))
          _ = try await client.receive()
          #expect(outcomes == [.incorporated])
        }
      }
    }
    #expect(finished == 1)
    #expect(destroyed == (mode == "success" ? 0 : 1))
    broker.stop()
    #expect(finished == 1 && destroyed == 1)
  }

  @Test(arguments: ["ax-error", "revalidate-error", "cancel-handoff", "ax-error-cancelled"])
  func workerFailureAndCancelledHandoffCleanExactPublication(mode: String) async throws {
    let path = "/private/tmp/as-cancel-\(UUID().uuidString.prefix(10))"
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    #expect(try runtime.elect())
    _ = try runtime.publishListener()
    defer { runtime.close() }
    let fixture = try AppshotArtifactTests().captured(runtime)
    let pipeline = AppshotCapturePipeline(
      runtime: runtime,
      begin: { _, deadline, done in
        let result = AppshotCaptureResult(
          authority: fixture.authority, png: fixture.png,
          width: fixture.width, height: fixture.height, deadline: deadline)
        done(.success(result))
        done(.success(result))  // duplicate cannot publish a second bundle
        return { deadline.cancel() }
      },
      projection: { result in
        if mode == "ax-error-cancelled" {
          result.deadline.cancel()
          throw AXTextDetailSerializationError.attributeReadFailed
        }
        if mode == "ax-error" { throw AXTextDetailSerializationError.attributeReadFailed }
        return try AppshotProjectionBuilder().build(
          provider: AppshotAXFixture(), deadline: result.deadline)
      },
      revalidate: { _ in
        if mode == "revalidate-error" { throw AppshotCaptureError.sourceWindowChanged }
      })
    let result: Result<AppshotCapturedArtifact, Error> = await withCheckedContinuation {
      continuation in
      let cancel = pipeline.capture(fixture.authority.recipient) {
        continuation.resume(returning: $0)
      }
      if mode == "cancel-handoff" {
        // Occupy MainActor only in this deterministic test so publication completes before its handoff.
        let end = ProcessInfo.processInfo.systemUptime + 1
        while (try? AppshotRuntimeDirectory.readDescriptor(at: path).entries.count) != 4
          && ProcessInfo.processInfo.systemUptime < end
        {
          usleep(1000)
        }
        #expect((try? AppshotRuntimeDirectory.readDescriptor(at: path).entries.count) == 4)
        cancel()
      }
    }
    if mode == "ax-error" {
      let artifact = try result.get()
      let manifest = try AppshotManifest.decodeStrict(Data(contentsOf: URL(fileURLWithPath: artifact.manifestPath)))
      #expect(manifest.ax.coverage == .unavailable)
      artifact.destroy()
    } else {
      let expected: AppshotErrorCode = mode == "revalidate-error" ? .sourceWindowChanged : .captureCancelled
      #expect(throws: expected) { try result.get() }
    }
    #expect(runtime.descriptor?.entries.count == 1)
    #expect(
      try FileManager.default.contentsOfDirectory(atPath: path).filter { $0.hasPrefix("appshot-") }
        .isEmpty)
  }
}
