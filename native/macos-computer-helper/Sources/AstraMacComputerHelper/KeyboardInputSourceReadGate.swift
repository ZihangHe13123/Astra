import CoreFoundation
import Foundation

/// TIS refresh/read boundary for the synchronous, long-lived helper.
final class KeyboardInputSourceReadGate {
    static let shared = KeyboardInputSourceReadGate()

    private let isMainThread: () -> Bool
    private let refresh: () throws -> Void
    private var reading = false

    init(
        isMainThread: @escaping () -> Bool = { Thread.isMainThread },
        refresh: (() throws -> Void)? = nil
    ) {
        self.isMainThread = isMainThread
        self.refresh = refresh ?? Self.refreshNotifications
    }

    func read<Value>(_ value: () -> Value?) -> Value? {
        // The thread check precedes access to the main-thread-only state. Do
        // not dispatch/sync to main from a background caller: fail closed.
        guard isMainThread(), !reading else { return nil }
        reading = true
        defer { reading = false }
        do {
            try refresh()
            return value()
        } catch {
            return nil
        }
    }

    private enum RefreshError: Error { case unavailable }

    private static func refreshNotifications() throws {
        // No sleep/wait or input delivery. TIS's process cache is invalidated
        // by distributed notifications delivered on the main default run loop.
        switch CFRunLoopRunInMode(.defaultMode, 0, false) {
        case .finished, .timedOut, .handledSource:
            return
        case .stopped:
            throw RefreshError.unavailable
        @unknown default:
            throw RefreshError.unavailable
        }
    }
}
