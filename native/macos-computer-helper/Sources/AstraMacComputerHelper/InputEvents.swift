import CoreGraphics
import Foundation

enum SyntheticInputEvent: Equatable {
    case mouseDown(point: CGPoint, clickCount: Int, button: PointerButton = .left)
    case mouseUp(point: CGPoint, clickCount: Int, button: PointerButton = .left)
    case mouseDragged(point: CGPoint)
    case unicodeKeyDown([UInt16])
    case unicodeKeyUp([UInt16])
    case virtualKeyDown(CGKeyCode, CGEventFlags)
    case virtualKeyUp(CGKeyCode, CGEventFlags)
    case scroll(point: CGPoint, deltaX: Int32, deltaY: Int32)
}

struct SyntheticInputFailure: Error {
    let error: ActionExecutionError
    let inputStarted: Bool
    var userActivityPaused = false
}

protocol SyntheticInputPosting: AnyObject {
    func preflight() -> Bool
    func post(_ event: SyntheticInputEvent) throws
}

final class UnavailableSyntheticInputPoster: SyntheticInputPosting {
    func preflight() -> Bool { true }

    func post(_: SyntheticInputEvent) throws {
        throw SyntheticInputFailure(error: .helperFailed, inputStarted: false)
    }
}

enum HeldInputScope: Hashable {
    case legacy
    case cooperative(marker: UInt64, generation: UUID)
}

struct HeldInputCleanupResult: Equatable {
    let attempted: Int
    let released: Int
    let failed: Int

    var succeeded: Bool { failed == 0 }
}

final class HeldInputRegistry {
    static let shared = HeldInputRegistry()

    private struct Held {
        let scope: HeldInputScope
        let release: () throws -> Void
        let cleanupRelease: () throws -> Void
    }

    private let lock = NSLock()
    private var held: [UUID: Held] = [:]

    func begin(token: UUID, poster: any SyntheticInputPosting, down: SyntheticInputEvent, release: SyntheticInputEvent) throws {
        lock.lock()
        defer { lock.unlock() }
        do {
            try poster.post(down)
            held[token] = Held(
                scope: .legacy,
                release: { try poster.post(release) },
                cleanupRelease: { try poster.post(release) }
            )
        } catch let failure as SyntheticInputFailure {
            if failure.inputStarted {
                held[token] = Held(
                    scope: .legacy,
                    release: { try poster.post(release) },
                    cleanupRelease: { try poster.post(release) }
                )
            }
            throw failure
        } catch {
            throw SyntheticInputFailure(error: .helperFailed, inputStarted: false)
        }
    }

    func begin(token: UUID, postDown: () throws -> Void, release: @escaping () throws -> Void) throws {
        try begin(
            token: token,
            scope: .legacy,
            postDown: postDown,
            release: release,
            cleanupRelease: release
        )
    }

    func begin(
        token: UUID,
        scope: HeldInputScope,
        postDown: () throws -> Void,
        release: @escaping () throws -> Void,
        cleanupRelease: @escaping () throws -> Void
    ) throws {
        lock.lock()
        defer { lock.unlock() }
        guard held[token] == nil else {
            throw SyntheticInputFailure(error: .helperFailed, inputStarted: false)
        }
        held[token] = Held(scope: scope, release: release, cleanupRelease: cleanupRelease)
        do {
            try postDown()
        } catch let failure as SyntheticInputFailure {
            if !failure.inputStarted { held.removeValue(forKey: token) }
            throw failure
        } catch {
            throw SyntheticInputFailure(error: .helperFailed, inputStarted: true)
        }
    }

    func finish(token: UUID) throws {
        lock.lock()
        defer { lock.unlock() }
        guard let value = held[token] else { return }
        do {
            try value.release()
            held.removeValue(forKey: token)
        } catch let failure as SyntheticInputFailure {
            throw SyntheticInputFailure(error: failure.error, inputStarted: true)
        } catch {
            throw SyntheticInputFailure(error: .helperFailed, inputStarted: true)
        }
    }

    @discardableResult
    func cleanup(token: UUID) -> Bool {
        cleanup(token: token, requiring: nil).succeeded
    }

    @discardableResult
    func cleanup(token: UUID, scope: HeldInputScope) -> HeldInputCleanupResult {
        cleanup(token: token, requiring: scope)
    }

    private func cleanup(token: UUID, requiring scope: HeldInputScope?) -> HeldInputCleanupResult {
        lock.lock()
        defer { lock.unlock() }
        guard let value = held[token], scope == nil || value.scope == scope else {
            return HeldInputCleanupResult(attempted: 0, released: 0, failed: 0)
        }
        do {
            try value.cleanupRelease()
            held.removeValue(forKey: token)
            return HeldInputCleanupResult(attempted: 1, released: 1, failed: 0)
        } catch {
            return HeldInputCleanupResult(attempted: 1, released: 0, failed: 1)
        }
    }

    @discardableResult
    func cleanupAll() -> Bool {
        cleanupAll(where: { _ in true }).succeeded
    }

    @discardableResult
    func cleanupAll(scope: HeldInputScope) -> HeldInputCleanupResult {
        cleanupAll(where: { $0 == scope })
    }

    private func cleanupAll(where includes: (HeldInputScope) -> Bool) -> HeldInputCleanupResult {
        lock.lock()
        defer { lock.unlock() }
        var attempted = 0
        var released = 0
        var failed = 0
        for token in Array(held.keys) {
            guard let value = held[token], includes(value.scope) else { continue }
            attempted += 1
            do {
                try value.cleanupRelease()
                held.removeValue(forKey: token)
                released += 1
            } catch {
                failed += 1
            }
        }
        return HeldInputCleanupResult(attempted: attempted, released: released, failed: failed)
    }

    var heldCount: Int {
        lock.lock()
        defer { lock.unlock() }
        return held.count
    }

    func heldCount(scope: HeldInputScope) -> Int {
        lock.lock()
        defer { lock.unlock() }
        return held.values.lazy.filter { $0.scope == scope }.count
    }
}
