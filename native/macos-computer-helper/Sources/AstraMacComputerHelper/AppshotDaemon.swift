import AppKit
import Darwin
import Foundation
import AstraAppshotCore

public enum AppshotHelperMode: Equatable {
  case computerUse, daemon, clientIdentity, brokerIdentity, calendar, calendarChild, unsupported
  public init(arguments: [String]) {
    switch arguments {
    case []: self = .computerUse
    case ["--appshot-daemon"]: self = .daemon
    case ["--appshot-client-identity"]: self = .clientIdentity
    case ["--appshot-broker-identity"]: self = .brokerIdentity
    case ["--calendar"]: self = .calendar
    case ["--calendar-child"]: self = .calendarChild
    default: self = .unsupported
    }
  }
}

/// Read-only identity of the actual parent; no runtime/settings/AppKit initialization.
public func appshotClientIdentityJSON() throws -> Data {
  let processes = AppshotSystemProcesses()
  guard let identity = processes.identity(pid: getppid()), identity.uid == getuid() else {
    throw AppshotErrorCode.brokerUnavailable
  }
  struct Response: Encodable {
    let pid: Int32
    let uid: UInt32
    let process_start: String
    let monotonic_ns: String
  }
  let encoder = JSONEncoder()
  encoder.outputFormatting = [.sortedKeys]
  return try encoder.encode(
    Response(
      pid: identity.pid, uid: identity.uid,
      process_start: identity.processStart, monotonic_ns: String(processes.monotonicNS())))
}

/// One retained coordinator, one absolute deadline and one callback claim for all phases.
// Dependencies are immutable. Begin runs on MainActor; projection/revalidation run only on
// this serial worker. The retained native coordinator and runtime synchronize their own state.
final class AppshotCapturePipeline: @unchecked Sendable {
  typealias Begin = (
    AppshotRecipientBinding, AppshotCaptureDeadline,
    @escaping (Result<AppshotCaptureResult, Error>) -> Void
  ) -> (() -> Void)
  private let runtime: AppshotRuntimeDirectory
  private let begin: Begin
  private let projection: (AppshotCaptureResult) throws -> AppshotProjection
  private let revalidate: (AppshotCaptureResult) throws -> Void
  private let worker = DispatchQueue(label: "com.astra.appshot.publication", qos: .userInitiated)
  init(
    runtime: AppshotRuntimeDirectory, begin: @escaping Begin,
    projection: @escaping (AppshotCaptureResult) throws -> AppshotProjection,
    revalidate: @escaping (AppshotCaptureResult) throws -> Void
  ) {
    self.runtime = runtime
    self.begin = begin
    self.projection = projection
    self.revalidate = revalidate
  }
  convenience init(runtime: AppshotRuntimeDirectory) {
    let coordinator = AppshotCaptureCoordinator()
    self.init(
      runtime: runtime,
      begin: { coordinator.capture(for: $0, deadline: $1, completion: $2) },
      projection: { result in
        guard let root = result.axRoot else { return try AppshotProjectionBuilder.unavailable() }
        // Leave time for exact-window revalidation and publication after optional AX.
        let duration = min(0.75, max(0, try result.deadline.remaining() - 1))
        let detailDeadline = try result.deadline.limited(to: duration)
        let detail = try AppshotProjectionBuilder().build(root: root, deadline: detailDeadline)
        return try detail.metadata.nodeCount == 0 ? AppshotProjectionBuilder.unavailable() : detail
      },
      revalidate: { try coordinator.revalidate($0) })
  }
  @MainActor func capture(
    _ recipient: AppshotRecipientBinding,
    completion: @escaping @MainActor (Result<AppshotCapturedArtifact, Error>) -> Void
  ) -> (() -> Void) {
    let deadline = AppshotCaptureDeadline()
    let claim = AppshotPipelineClaim()
    let cancel = begin(recipient, deadline) { [self] result in
      guard claim.take() else { return }
      worker.async { [self] in
        let published: Result<AppshotPublishedBundle, Error>
        do {
          let result = try result.get()
          _ = try deadline.remaining()
          guard result.deadline === deadline, result.authority.recipient == recipient else {
            throw AppshotErrorCode.artifactUnsafe
          }
          let detail: AppshotProjection
          do {
            detail = try projection(result)
          } catch {
            // Only optional AX failure is downgraded. Capture, identity, custody,
            // cancellation and the original overall deadline still gate delivery.
            _ = try deadline.remaining()
            detail = try AppshotProjectionBuilder.unavailable()
          }
          _ = try deadline.remaining()
          published = .success(
            try AppshotArtifactPublisher(runtime: runtime).publish(
              result, projection: detail, revalidate: revalidate))
        } catch { published = .failure(AppshotErrorCode.map(error)) }
        Task { @MainActor in
          do {
            // Bundle retains custody until this exact handoff, checking original deadline again.
            let bundle = try published.get()
            completion(.success(try bundle.makeCapturedArtifact()))
          } catch { completion(.failure(AppshotErrorCode.map(error))) }
        }
      }
    }
    return {
      deadline.cancel()
      cancel()
    }
  }
}

private final class AppshotPipelineClaim: @unchecked Sendable {
  private let lock = NSLock()
  private var claimed = false
  func take() -> Bool {
    lock.lock()
    defer { lock.unlock() }
    guard !claimed else { return false }
    claimed = true
    return true
  }
}

@MainActor final class AppshotDaemon {
  private let hud: any AppshotHUDPresenting
  private let onStop: () -> Void
  private var stopping = false
  private var broker: AppshotBroker!
  init(
    runtime: AppshotRuntimeDirectory, controller: AppshotShortcutController,
    hud: any AppshotHUDPresenting, capture: @escaping AppshotBroker.Capture,
    onStop: @escaping () -> Void = {}
  ) {
    self.hud = hud
    self.onStop = onStop
    broker = AppshotBroker(
      runtime: runtime, controller: controller,
      capture: { [weak self] recipient, completion in
        self?.hud.hide()
        return capture(recipient, completion)
      }, onStop: { [weak self] in self?.didStop() },
      onOutcome: { [weak self] _, outcome in
        guard let self, !self.stopping else { return }
        switch outcome {
        case .incorporated: self.hud.show(.success)
        case .failed(let code): self.hud.show(.failure(code))
        }
      })
  }
  static func production(onStop: @escaping () -> Void) throws -> AppshotDaemon {
    let runtime = try AppshotRuntimeDirectory()
    let controller = try AppshotShortcutController(
      registrar: CarbonAppshotHotKeyRegistrar(),
      store: AppshotSettingsStore())
    let pipeline = AppshotCapturePipeline(runtime: runtime)
    return AppshotDaemon(
      runtime: runtime, controller: controller, hud: AppshotHUD(),
      capture: { pipeline.capture($0, completion: $1) }, onStop: onStop)
  }
  func start() throws { try broker.start() }
  func stop() {
    guard !stopping else { return }
    stopping = true
    broker.stop()
  }
  private func didStop() {
    stopping = true
    hud.hide()
    onStop()
  }
}

/// Explicit CLI mode only. SIGTERM/SIGINT and final-client grace share exact broker cleanup.
@MainActor public func runAppshotDaemon() -> Int32 {
  let application = NSApplication.shared
  application.setActivationPolicy(.accessory)
  do {
    let daemon = try AppshotDaemon.production {
      application.stop(nil)
      if let event = NSEvent.otherEvent(
        with: .applicationDefined, location: .zero,
        modifierFlags: [], timestamp: 0, windowNumber: 0, context: nil,
        subtype: 0, data1: 0, data2: 0)
      {
        application.postEvent(event, atStart: true)
      }
    }
    try daemon.start()
    let signals = [SIGINT, SIGTERM].map { number -> DispatchSourceSignal in
      signal(number, SIG_IGN)
      let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
      source.setEventHandler { daemon.stop() }
      source.resume()
      return source
    }
    application.run()
    daemon.stop()
    for source in signals { source.cancel() }
    return 0
  } catch {
    FileHandle.standardError.write(Data((AppshotErrorCode.map(error).rawValue + "\n").utf8))
    return 69
  }
}
