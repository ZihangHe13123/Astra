import Darwin
import Foundation

extension AppStateObservation {
    var helperResultJSON: JSONValue {
        .object([
            "app_ref": .string(target.appRef),
            "window_ref": .string(target.windowRef),
            "catalog_generation": .number(Double(catalogGeneration)),
            "interaction_mode": .string(target.interactionMode.rawValue),
        ])
    }
}

enum InputRecord {
    case line(Data)
    case tooLarge
}

private final class BoundedLineReader {
    private let input: FileHandle
    private var buffer = Data()
    private var pending: [InputRecord] = []
    private var discardingOversizedLine = false
    private var reachedEOF = false

    init(input: FileHandle = .standardInput) { self.input = input }

    func next() throws -> InputRecord? {
        while pending.isEmpty && !reachedEOF {
            guard let chunk = try readChunk() else {
                reachedEOF = true
                if discardingOversizedLine {
                    discardingOversizedLine = false
                    pending.append(.tooLarge)
                } else if !buffer.isEmpty {
                    pending.append(.line(buffer))
                    buffer.removeAll(keepingCapacity: false)
                }
                break
            }
            consume(chunk)
        }
        return pending.isEmpty ? nil : pending.removeFirst()
    }

    private func readChunk() throws -> Data? {
        var bytes = [UInt8](repeating: 0, count: 64 * 1024)
        while true {
            let count = bytes.withUnsafeMutableBytes { buffer in
                Darwin.read(input.fileDescriptor, buffer.baseAddress, buffer.count)
            }
            if count > 0 { return Data(bytes[..<count]) }
            if count == 0 { return nil }
            if errno != EINTR { throw ReaderError.readFailed(errno) }
        }
    }

    private func consume(_ chunk: Data) {
        for byte in chunk {
            if byte == 0x0A {
                if discardingOversizedLine {
                    discardingOversizedLine = false
                    pending.append(.tooLarge)
                } else {
                    if buffer.last == 0x0D { buffer.removeLast() }
                    pending.append(.line(buffer))
                    buffer.removeAll(keepingCapacity: true)
                }
                continue
            }
            guard !discardingOversizedLine else { continue }
            if buffer.count == maxRequestLineBytes {
                buffer.removeAll(keepingCapacity: false)
                discardingOversizedLine = true
            } else {
                buffer.append(byte)
            }
        }
    }
}

private enum ReaderError: Error { case readFailed(Int32) }
private enum WriterError: Error { case writeFailed(Int32) }
private var terminationCleanupSource: DispatchSourceSignal?

func installHelperBrokenPipeHandling() {
    signal(SIGPIPE, SIG_IGN)
}

@discardableResult
func installTerminationInputCleanup(
    _ cleanup: @escaping () -> Void,
    terminate: @escaping (Int32) -> Void = { Darwin._exit($0) }
) -> DispatchSourceSignal {
    signal(SIGTERM, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: SIGTERM, queue: DispatchQueue.global(qos: .userInitiated))
    source.setEventHandler {
        cleanup()
        terminate(128 + SIGTERM)
    }
    source.resume()
    terminationCleanupSource = source
    return source
}

func writeResponse(
    _ response: HelperResponse,
    fileDescriptor: Int32 = STDOUT_FILENO
) throws {
    let line = try encodeResponse(response)
    try writeAll(Data((line + "\n").utf8), fileDescriptor: fileDescriptor)
}

func writeHelperDiagnostic(
    _ message: String,
    fileDescriptor: Int32 = STDERR_FILENO
) {
    try? writeAll(Data((message + "\n").utf8), fileDescriptor: fileDescriptor)
}

private func writeAll(_ data: Data, fileDescriptor: Int32) throws {
    try data.withUnsafeBytes { buffer in
        guard let baseAddress = buffer.baseAddress else { return }
        var written = 0
        while written < buffer.count {
            let count = Darwin.write(
                fileDescriptor,
                baseAddress.advanced(by: written),
                buffer.count - written
            )
            if count < 0, errno == EINTR { continue }
            guard count > 0 else {
                throw WriterError.writeFailed(count < 0 ? errno : EIO)
            }
            written += count
        }
    }
}

private func isCloseAcknowledgement(_ response: HelperResponse) -> Bool {
    guard response.ok, case let .object(result)? = response.result, case .bool(true)? = result["closed"] else { return false }
    return true
}

final class HelperTerminalCleanupCoordinator: @unchecked Sendable {
    struct Step {
        let name: String
        let action: () throws -> Void

        init(name: String, action: @escaping () throws -> Void) {
            self.name = name
            self.action = action
        }
    }

    private enum State { case idle, running, completed }
    private let steps: [Step]
    private let condition = NSCondition()
    private var state: State = .idle

    init(steps: [Step]) { self.steps = steps }

    var hasStarted: Bool {
        condition.lock()
        defer { condition.unlock() }
        return state != .idle
    }

    func run() -> [String] {
        condition.lock()
        switch state {
        case .completed:
            condition.unlock()
            return []
        case .running:
            while state == .running { condition.wait() }
            condition.unlock()
            return []
        case .idle:
            state = .running
            condition.unlock()
        }

        var failures: [String] = []
        for step in steps {
            do { try step.action() }
            catch { failures.append(step.name) }
        }

        condition.lock()
        state = .completed
        condition.broadcast()
        condition.unlock()
        return failures
    }
}

func runHelperSIGTERMCleanup(_ coordinator: HelperTerminalCleanupCoordinator) -> [String] {
    coordinator.run()
}

func runHelperLoop(
    next: () throws -> InputRecord?,
    handle: (String) -> HelperResponse,
    write: (HelperResponse) throws -> Void,
    cleanup: () -> Void,
    terminalCleanupStarted: () -> Bool = { false },
    report: (String) -> Void
) -> Int32 {
    defer { cleanup() }
    do {
        while let input = try next() {
            // SIGTERM can race a blocking read or an in-flight batch. Never
            // dispatch a buffered frame or publish success for a cancelled act.
            if terminalCleanupStarted() { return 128 + SIGTERM }
            let response: HelperResponse
            switch input {
            case let .line(data):
                if let request = String(data: data, encoding: .utf8) {
                    response = handle(request)
                } else {
                    response = Dispatcher.invalidRequest(message: "request must be valid UTF-8")
                }
            case .tooLarge:
                response = Dispatcher.invalidRequest(message: "request exceeds 4 MiB")
            }
            if terminalCleanupStarted() { return 128 + SIGTERM }
            do { try write(response) }
            catch {
                report("failed to encode or write helper response: \(error)")
                return 1
            }
            if isCloseAcknowledgement(response) { break }
        }
    } catch {
        report("failed to read helper request: \(error)")
        return 1
    }
    return terminalCleanupStarted() ? 128 + SIGTERM : 0
}

public func runHelperMain() {
    let helperURL = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL
    let cursor: any VirtualCursorPresenting = RestartableVirtualCursorPresenter {
        try VirtualCursorClient.launch(helperExecutableURL: helperURL)
    }
    let artifactSession = SnapshotArtifactSession.inherited()
    let dispatcher = Dispatcher(windows: SystemWindowObserver(
        virtualCursor: cursor,
        pidCompatibility: .bundled(),
        pidPointerCapability: .available,
        foregroundKeyboardCapability: .available,
        artifactSession: artifactSession
    ))
    let cleanupCoordinator = HelperTerminalCleanupCoordinator(steps: [
        .init(name: "dispatcher") { dispatcher.close() },
        .init(name: "artifacts") { artifactSession.close() },
        .init(name: "cursor") { cursor.close() },
        .init(name: "held_input") { _ = HeldInputRegistry.shared.cleanupAll() },
    ])
    let terminalCleanup = { _ = cleanupCoordinator.run() }
    installHelperBrokenPipeHandling()
    installTerminationInputCleanup { _ = runHelperSIGTERMCleanup(cleanupCoordinator) }
    let reader = BoundedLineReader()
    let exitCode = runHelperLoop(
        next: { try reader.next() },
        handle: { dispatcher.handle($0) },
        write: { try writeResponse($0) },
        cleanup: terminalCleanup,
        terminalCleanupStarted: { cleanupCoordinator.hasStarted },
        report: { writeHelperDiagnostic($0) }
    )
    if exitCode != 0 { Darwin.exit(exitCode) }
}
