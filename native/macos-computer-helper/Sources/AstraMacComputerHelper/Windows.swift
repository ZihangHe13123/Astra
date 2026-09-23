@preconcurrency import ApplicationServices
import AppKit
import CoreGraphics
import CryptoKit
import Darwin
import Foundation
import ImageIO
import ScreenCaptureKit

func exactApplicationVersion(bundleURL: URL?) -> String? {
    guard let bundleURL,
          let bundle = Bundle(url: bundleURL),
          let version = bundle.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String,
          !version.isEmpty
    else { return nil }
    return version
}

func cursorOverlayExclusionWindowID(
    scope: String,
    presenter: any VirtualCursorPresenting,
    availableWindows: [VirtualCursorWindowRecord]
) -> UInt32? {
    guard scope == "display" else { return nil }
    return presenter.displayExclusionWindowID(availableWindows: availableWindows)
}

struct WindowGeometry {
    let bounds: CGRect
    let backingScale: CGFloat

    init(bounds: CGRect, backingScale: CGFloat) {
        self.bounds = bounds
        self.backingScale = max(backingScale, 1)
    }

    var pixelSize: CGSize {
        CGSize(width: (bounds.width * backingScale).rounded(), height: (bounds.height * backingScale).rounded())
    }

    func screenPoint(for point: CGPoint) -> CGPoint {
        CGPoint(x: bounds.origin.x + point.x, y: bounds.origin.y + point.y)
    }
}

// Exact-window capture can return logical-resolution pixels even when the
// display requests Retina pixels. Accept only complete-window 1x or requested
// resolution; never relabel cropped, framed, or nonuniform images as a target.
func resolvedWindowImageGeometry(
    requested: WindowGeometry,
    imageWidth: Int,
    imageHeight: Int
) throws -> WindowGeometry {
    guard requested.bounds.width.isFinite, requested.bounds.height.isFinite,
          requested.bounds.width > 0, requested.bounds.height > 0,
          imageWidth > 0, imageHeight > 0
    else { throw WindowObservationError.captureFailed }
    let actualSize = CGSize(width: imageWidth, height: imageHeight)
    if actualSize == requested.pixelSize { return requested }
    let logical = WindowGeometry(bounds: requested.bounds, backingScale: 1)
    if actualSize == logical.pixelSize { return logical }
    throw WindowObservationError.captureFailed
}

// An all-transparent result contains no observed window content. Do not publish
// action authority for it or retry through a different capture channel. Opaque
// black windows are valid; this deliberately makes no RGB-content assumptions.
func validateWindowImageContent(_ image: CGImage) throws {
    guard image.width > 0, image.height > 0,
          image.width <= 16_384, image.height <= 16_384,
          image.width * image.height <= 67_108_864
    else { throw WindowObservationError.captureFailed }
    switch image.alphaInfo {
    case .none, .noneSkipFirst, .noneSkipLast: return
    default: break
    }
    // If the bounded analysis cannot allocate, decline to publish this
    // ambiguous popup capture through either source.
    guard let context = CGContext(
        data: nil, width: image.width, height: image.height,
        bitsPerComponent: 8, bytesPerRow: image.width * 4,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ), let data = context.data else { throw WindowObservationError.captureFailed }
    let bounds = CGRect(x: 0, y: 0, width: image.width, height: image.height)
    context.clear(bounds)
    context.draw(image, in: bounds)
    let bytes = data.assumingMemoryBound(to: UInt8.self)
    for offset in stride(from: 3, to: context.bytesPerRow * image.height, by: 4) {
        if bytes[offset] != 0 { return }
    }
    throw WindowObservationError.windowContentUnavailable
}

struct DisplayCaptureCandidate: Equatable {
    let displayID: CGDirectDisplayID
    let bounds: CGRect
    let pixelSize: CGSize
}

func selectDisplayCapture(
    targetBounds: CGRect,
    candidates: [DisplayCaptureCandidate]
) throws -> DisplayCaptureCandidate {
    let intersecting = candidates.filter { candidate in
        let intersection = candidate.bounds.intersection(targetBounds)
        return !intersection.isNull && intersection.width > 0 && intersection.height > 0
    }
    guard intersecting.count == 1, let selected = intersecting.first,
          selected.bounds.contains(targetBounds),
          selected.bounds.width > 0, selected.bounds.height > 0,
          selected.pixelSize.width > 0, selected.pixelSize.height > 0,
          selected.pixelSize.width <= 16_384, selected.pixelSize.height <= 16_384,
          selected.pixelSize.width * selected.pixelSize.height <= 67_108_864
    else { throw WindowObservationError.displayUnavailable }
    return selected
}

struct WindowTarget {
    let appRef: String
    let windowRef: String
    let pid: pid_t
    let windowID: CGWindowID
    let bounds: CGRect
    let title: String
    let axIdentity: CFHashCode?
    let axElement: AXUIElement?
    let interactionMode: InteractionMode

    init(
        appRef: String,
        windowRef: String,
        pid: pid_t,
        windowID: CGWindowID,
        bounds: CGRect,
        title: String,
        axIdentity: CFHashCode? = nil,
        axElement: AXUIElement? = nil,
        interactionMode: InteractionMode = .foregroundTakeover
    ) {
        self.appRef = appRef
        self.windowRef = windowRef
        self.pid = pid
        self.windowID = windowID
        self.bounds = bounds
        self.title = title
        self.axIdentity = axIdentity
        self.axElement = axElement
        self.interactionMode = interactionMode
    }
}

let maximumCatalogGeneration = 9_007_199_254_740_991

enum CatalogGenerationError: Error {
    case invalidPriorGeneration
    case exhausted
}

func nextCatalogGeneration(after current: Int) throws -> Int {
    guard current >= 0 else { throw CatalogGenerationError.invalidPriorGeneration }
    guard current < maximumCatalogGeneration else { throw CatalogGenerationError.exhausted }
    return current + 1
}

struct WindowCatalogObservation {
    let targets: [String: WindowTarget]
    let apps: [JSONValue]
}

struct CatalogSCWindow {
    let pid: pid_t
    let windowID: CGWindowID
    let bounds: CGRect
    let isOnScreen: Bool
    let title: String
    let applicationName: String
    let bundleID: String?
    let appVersion: String?
    let isTerminated: Bool
    let isRegularApplication: Bool
}

struct CatalogWindowIdentityKey: Hashable {
    let pid: pid_t
    let windowID: CGWindowID
    let axIdentity: CFHashCode
}

struct CatalogWindowIdentityRecord {
    let element: AXUIElement
    let reference: String
}

func catalogWindowJSON(
    windowRef: String,
    title: String,
    bounds: CGRect,
    documentPath: String?,
    accessibilityTrusted: Bool,
    exactAXWindowFound: Bool,
    windowIdentityRef: String? = nil
) -> [String: JSONValue] {
    let bindingStatus: String
    if !accessibilityTrusted {
        bindingStatus = "accessibility_permission_required"
    } else if exactAXWindowFound {
        bindingStatus = "ready"
    } else {
        bindingStatus = "ax_window_unmatched"
    }

    var values: [String: JSONValue] = [
        "window_ref": .string(windowRef),
        "title": .string(String(title.prefix(maximumAXStringCharacters))),
        "bounds": CGRectJSON.encode(bounds),
        "bindable": .bool(accessibilityTrusted && exactAXWindowFound),
        "binding_status": .string(bindingStatus),
    ]
    if let documentPath {
        values["document_path"] = .string(String(documentPath.prefix(maximumAXStringCharacters)))
    }
    if accessibilityTrusted, exactAXWindowFound, let windowIdentityRef {
        values["window_identity_ref"] = .string(String(windowIdentityRef.prefix(maximumAXStringCharacters)))
    }
    return values
}

func catalogTargetsMatch(_ expected: WindowTarget, _ current: WindowTarget) -> Bool {
    guard expected.appRef == current.appRef,
          expected.windowRef == current.windowRef,
          expected.pid == current.pid,
          expected.windowID == current.windowID,
          expected.bounds == current.bounds,
          expected.axIdentity == current.axIdentity
    else { return false }
    switch (expected.axElement, current.axElement) {
    case (nil, nil): return true
    case let (expectedElement?, currentElement?): return CFEqual(expectedElement, currentElement)
    default: return false
    }
}

enum WindowObservationError: Error {
    case permissionDenied(String)
    case staleTarget
    case overlayBlocked
    case axWindowUnmatched
    case targetGone
    case targetNotFrontmost
    case invalidScope
    case displayUnavailable
    case invalidCapturePath
    case captureFailed
    case windowContentUnavailable
    case capturePublicationUncertain
    case artifactQuotaExceeded
    case artifactPublisherFailed
    case axSerializationFailed
    case observationTimedOut
}

let applicationMenuBarMaximumDepth = 4

enum PrimaryWindowCaptureError: Error { case timedOut, contentMismatch }

/// A requested output size does not prove SCK captured the same source region:
/// a compositor-owned popup may otherwise become a scaled parent plus padding.
/// Identity is checked by the caller; this checks the independent filter size.
func primaryWindowContentHasExactSize(contentRect: CGRect, expectedBounds: CGRect) -> Bool {
    let values = [contentRect.minX, contentRect.minY, contentRect.width, contentRect.height,
                  expectedBounds.minX, expectedBounds.minY, expectedBounds.width, expectedBounds.height]
    guard values.allSatisfy({ $0.isFinite }), contentRect.width > 0, contentRect.height > 0,
          expectedBounds.width > 0, expectedBounds.height > 0 else { return false }
    return abs(contentRect.width - expectedBounds.width) < 0.01
        && abs(contentRect.height - expectedBounds.height) < 0.01
}

/// A compositor-owned popup can report the popup's contentRect while SCK actually
/// scales its containing window into the requested canvas, leaving a large
/// transparent band. Only flag this distinctive shape when the visible pixels
/// match the size of another, containing window from the same application.
/// An intentionally transparent popup with that exact shape is indistinguishable
/// from the wrong capture here; the exact-window fallback can reject it too.
func windowImageHasScaledParentPadding(
    _ image: CGImage,
    expectedBounds: CGRect,
    sameApplicationWindowFrames: [CGRect]
) -> Bool {
    let parents = sameApplicationWindowFrames.filter {
        $0.width.isFinite && $0.height.isFinite && $0.width > expectedBounds.width &&
            $0.height > expectedBounds.height * 1.5 && $0.contains(expectedBounds)
    }
    guard !parents.isEmpty, image.width >= 64, image.height >= 64,
          image.alphaInfo != .none, image.alphaInfo != .noneSkipFirst,
          image.alphaInfo != .noneSkipLast
    else { return false }
    // Keep the temporary RGBA bitmap at most 16 MiB without exempting larger
    // popups from this check. Scaling both axes equally preserves the footprint.
    let scale = min(1, 2_048.0 / Double(max(image.width, image.height)))
    let sampleWidth = max(1, Int((Double(image.width) * scale).rounded()))
    let sampleHeight = max(1, Int((Double(image.height) * scale).rounded()))
    guard let context = CGContext(
        data: nil, width: sampleWidth, height: sampleHeight,
        bitsPerComponent: 8, bytesPerRow: sampleWidth * 4,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ), let data = context.data else { return true }
    let sampleRect = CGRect(x: 0, y: 0, width: sampleWidth, height: sampleHeight)
    context.interpolationQuality = .none
    context.clear(sampleRect)
    context.draw(image, in: sampleRect)
    let bytes = data.assumingMemoryBound(to: UInt8.self)
    let halfWidth = sampleWidth / 2
    // Ordinary images have content on the right. Reject them before scanning
    // the left half for the much rarer scaled-parent footprint.
    for y in 0..<sampleHeight {
        let row = y * context.bytesPerRow
        for x in halfWidth..<sampleWidth where bytes[row + x * 4 + 3] != 0 {
            return false
        }
    }
    var minX = sampleWidth
    var maxX = -1
    var minY = sampleHeight
    var maxY = -1
    for y in 0..<sampleHeight {
        let row = y * context.bytesPerRow
        for x in 0..<halfWidth where bytes[row + x * 4 + 3] != 0 {
            minX = min(minX, x)
            maxX = max(maxX, x)
            minY = min(minY, y)
            maxY = max(maxY, y)
        }
    }
    guard minX <= 2, minY <= 2, maxY >= sampleHeight - 3,
          maxX >= 0
    else { return false }
    let visibleWidth = CGFloat(maxX - minX + 1)
    let visibleHeight = CGFloat(maxY - minY + 1)
    return parents.contains { parent in
        let scaledWidth = parent.width * visibleHeight / parent.height
        return abs(scaledWidth - visibleWidth) <= max(4, scaledWidth * 0.015)
    }
}

protocol ExactWindowImageProviding {
    func primaryImage() throws -> CGImage
    func fallbackImage(for windowID: CGWindowID) -> CGImage?
}

enum ExactWindowCommandOutcome: Equatable { case exited(Int32), timedOut }

protocol ExactWindowCommandRunning {
    func run(
        executable: String,
        arguments: [String],
        environment: [String: String],
        timeout: TimeInterval
    ) -> ExactWindowCommandOutcome
}

struct SystemExactWindowCommandRunner: ExactWindowCommandRunning {
    func run(
        executable: String,
        arguments: [String],
        environment: [String: String],
        timeout: TimeInterval
    ) -> ExactWindowCommandOutcome {
        var actions: posix_spawn_file_actions_t? = nil
        var attributes: posix_spawnattr_t? = nil
        guard posix_spawn_file_actions_init(&actions) == 0,
              posix_spawn_file_actions_addopen(&actions, STDOUT_FILENO, "/dev/null", O_WRONLY, 0) == 0,
              posix_spawn_file_actions_addopen(&actions, STDERR_FILENO, "/dev/null", O_WRONLY, 0) == 0,
              posix_spawnattr_init(&attributes) == 0
        else { return .exited(-1) }
        defer {
            posix_spawn_file_actions_destroy(&actions)
            posix_spawnattr_destroy(&attributes)
        }
        let flags = Int16(POSIX_SPAWN_SETPGROUP)
        guard posix_spawnattr_setflags(&attributes, flags) == 0,
              posix_spawnattr_setpgroup(&attributes, 0) == 0
        else { return .exited(-1) }

        let argvStorage = ([executable] + arguments).map { strdup($0) }
        let environmentStorage = environment.sorted(by: { $0.key < $1.key }).map { strdup("\($0.key)=\($0.value)") }
        guard !argvStorage.contains(where: { $0 == nil }),
              !environmentStorage.contains(where: { $0 == nil })
        else {
            argvStorage.forEach { free($0) }
            environmentStorage.forEach { free($0) }
            return .exited(-1)
        }
        defer {
            argvStorage.forEach { free($0) }
            environmentStorage.forEach { free($0) }
        }
        var argv = argvStorage + [nil]
        var envp = environmentStorage + [nil]
        var pid = pid_t()
        let spawnStatus = executable.withCString { executablePointer in
            argv.withUnsafeMutableBufferPointer { argvPointer in
                envp.withUnsafeMutableBufferPointer { envPointer in
                    posix_spawn(
                        &pid,
                        executablePointer,
                        &actions,
                        &attributes,
                        argvPointer.baseAddress,
                        envPointer.baseAddress
                    )
                }
            }
        }
        guard spawnStatus == 0, pid > 0 else { return .exited(spawnStatus == 0 ? -1 : spawnStatus) }

        let deadline = DispatchTime.now().uptimeNanoseconds + UInt64(max(timeout, 0) * 1_000_000_000)
        var status: Int32 = 0
        while true {
            let waited = waitpid(pid, &status, WNOHANG)
            if waited == pid {
                return .exited((status & 0x7f) == 0 ? (status >> 8) & 0xff : -1)
            }
            if waited == -1, errno != EINTR { return .exited(-1) }
            if DispatchTime.now().uptimeNanoseconds >= deadline { break }
            usleep(10_000)
        }
        _ = Darwin.kill(-pid, SIGTERM)
        let terminationDeadline = DispatchTime.now().uptimeNanoseconds + 1_000_000_000
        while DispatchTime.now().uptimeNanoseconds < terminationDeadline {
            let waited = waitpid(pid, &status, WNOHANG)
            if waited == pid { return .timedOut }
            if waited == -1, errno != EINTR { return .timedOut }
            usleep(10_000)
        }
        _ = Darwin.kill(-pid, SIGKILL)
        while waitpid(pid, &status, 0) == -1, errno == EINTR {}
        return .timedOut
    }
}

func captureExactWindowImage(
    windowID: CGWindowID,
    provider: any ExactWindowImageProviding,
    expectedBounds: CGRect? = nil,
    sameApplicationWindowFrames: [CGRect] = []
) throws -> CGImage {
    func isWrongSource(_ image: CGImage) -> Bool {
        guard let expectedBounds else { return false }
        return windowImageHasScaledParentPadding(
            image, expectedBounds: expectedBounds,
            sameApplicationWindowFrames: sameApplicationWindowFrames
        )
    }
    do {
        let image = try provider.primaryImage()
        guard !isWrongSource(image) else {
            logActionRejected("CAPTURE-SOURCE primary=scaled_parent windowID=\(windowID) width=\(image.width) height=\(image.height)")
            throw PrimaryWindowCaptureError.contentMismatch
        }
        return image
    } catch let error as PrimaryWindowCaptureError {
        switch error {
        case .timedOut, .contentMismatch:
            guard let image = provider.fallbackImage(for: windowID) else {
                logActionRejected("CAPTURE-SOURCE fallback=unavailable windowID=\(windowID)")
                throw WindowObservationError.captureFailed
            }
            guard !isWrongSource(image) else {
                logActionRejected("CAPTURE-SOURCE fallback=scaled_parent windowID=\(windowID) width=\(image.width) height=\(image.height)")
                throw WindowObservationError.captureFailed
            }
            logActionRejected("CAPTURE-SOURCE fallback=accepted windowID=\(windowID) width=\(image.width) height=\(image.height)")
            return image
        }
    }
}

enum CapturePathError: Error {
    case invalidRoot
    case pathOutsideRoot
    case unsafeTarget
}

enum ArtifactPublicationStage: Equatable {
    case open
    case write
    case fileSync
    case close
    case publish
    case directorySync
}

struct ArtifactPublicationError: Error, Equatable {
    let stage: ArtifactPublicationStage
    let errnoCode: Int32
    let finalVisible: Bool
    let bytesComplete: Bool
    let publisherPoisoned: Bool

    init(
        stage: ArtifactPublicationStage,
        errnoCode: Int32,
        finalVisible: Bool,
        bytesComplete: Bool,
        publisherPoisoned: Bool = false
    ) {
        self.stage = stage
        self.errnoCode = errnoCode
        self.finalVisible = finalVisible
        self.bytesComplete = bytesComplete
        self.publisherPoisoned = publisherPoisoned
    }
}

enum ArtifactPublication: Equatable {
    case durable
}

enum ArtifactDirectory {
    static func writePNG(_ data: Data, named name: String) throws {
        guard isPlainName(name), let descriptor = inheritedDirectoryDescriptor() else {
            throw CapturePathError.invalidRoot
        }
        _ = try publishPNG(data, named: name, directoryFD: descriptor, syscalls: DarwinArtifactSyscalls())
    }

    static func captureExactWindow(
        windowID: CGWindowID,
        runner: any ExactWindowCommandRunning = SystemExactWindowCommandRunner()
    ) -> CGImage? {
        guard let descriptor = inheritedDirectoryDescriptor(),
              let directoryPath = ProcessInfo.processInfo.environment["ASTRA_COMPUTER_ARTIFACT_DIR_PATH"]
        else { return nil }
        return captureExactWindow(
            windowID: windowID,
            directoryFD: descriptor,
            directoryPath: directoryPath,
            runner: runner
        )
    }

    static func captureExactWindow(
        windowID: CGWindowID,
        directoryFD descriptor: Int32,
        directoryPath: String,
        runner: any ExactWindowCommandRunning
    ) -> CGImage? {
        var heldInfo = stat()
        let heldResult = fstat(descriptor, &heldInfo)
        guard heldResult == 0,
              (heldInfo.st_mode & S_IFMT) == S_IFDIR,
              heldInfo.st_uid == getuid(),
              heldInfo.st_mode & (S_IRWXG | S_IRWXO) == 0
        else { return nil }
        guard directoryPath.hasPrefix("/"), !directoryPath.contains("\0") else { return nil }
        var pathInfo = stat()
        guard lstat(directoryPath, &pathInfo) == 0,
              (pathInfo.st_mode & S_IFMT) == S_IFDIR,
              pathInfo.st_uid == getuid(),
              pathInfo.st_dev == heldInfo.st_dev,
              pathInfo.st_ino == heldInfo.st_ino
        else { return nil }
        let name = "exact-window-\(UUID().uuidString.lowercased()).png"
        var absentInfo = stat()
        errno = 0
        let absentResult = name.withCString {
            Darwin.fstatat(descriptor, $0, &absentInfo, AT_SYMLINK_NOFOLLOW)
        }
        guard absentResult == -1, errno == ENOENT else { return nil }
        defer { name.withCString { _ = Darwin.unlinkat(descriptor, $0, 0) } }
        let outputPath = URL(fileURLWithPath: directoryPath).appendingPathComponent(name).path
        let remainingTimeout: TimeInterval
        if let budget = AXObservationBudget.current {
            guard let remaining = try? budget.phaseTimeout(maximum: 5) else { return nil }
            remainingTimeout = remaining
        } else { remainingTimeout = 5 }
        let outcome = runner.run(
            executable: "/usr/sbin/screencapture",
            arguments: ["-x", "-t", "png", "-l\(windowID)", outputPath],
            environment: ["LANG": "C", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"],
            timeout: remainingTimeout
        )
        guard outcome == .exited(0) else { return nil }
        let captured = name.withCString {
            Darwin.openat(descriptor, $0, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
        }
        guard captured >= 0 else { return nil }
        defer { Darwin.close(captured) }
        var finalInfo = stat()
        var linkedInfo = stat()
        let linkedResult = name.withCString {
            Darwin.fstatat(descriptor, $0, &linkedInfo, AT_SYMLINK_NOFOLLOW)
        }
        let finalStatResult = fstat(captured, &finalInfo)
        let chmodResult = fchmod(captured, mode_t(0o600))
        let capturedData = try? FileHandle(fileDescriptor: captured, closeOnDealloc: false).readToEnd()
        let capturedSource = capturedData.flatMap { CGImageSourceCreateWithData($0 as CFData, nil) }
        guard finalStatResult == 0,
              linkedResult == 0,
              (finalInfo.st_mode & S_IFMT) == S_IFREG,
              (linkedInfo.st_mode & S_IFMT) == S_IFREG,
              finalInfo.st_uid == getuid(),
              finalInfo.st_nlink == 1,
              finalInfo.st_dev == linkedInfo.st_dev,
              finalInfo.st_ino == linkedInfo.st_ino,
              finalInfo.st_size > 0,
              finalInfo.st_size <= 64 * 1024 * 1024,
              chmodResult == 0,
              capturedData != nil,
              let source = capturedSource,
              CGImageSourceGetCount(source) == 1,
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
        else { return nil }
        return image
    }

    static func publishPNG(
        _ data: Data,
        named name: String,
        directoryFD descriptor: Int32,
        syscalls: any ArtifactSyscalls
    ) throws -> ArtifactPublication {
        guard isPlainName(name) else { throw CapturePathError.invalidRoot }
        var directoryInfo = stat()
        guard fstat(descriptor, &directoryInfo) == 0,
              (directoryInfo.st_mode & S_IFMT) == S_IFDIR,
              directoryInfo.st_uid == getuid()
        else { throw CapturePathError.invalidRoot }
        let temporaryName = ".\(name).\(UUID().uuidString.lowercased()).tmp"
        let flags = O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC
        let fileDescriptor = syscalls.openFile(at: descriptor, name: temporaryName, flags: flags, mode: mode_t(0o600))
        guard fileDescriptor >= 0 else {
            throw ArtifactPublicationError(stage: .open, errnoCode: errno, finalVisible: false, bytesComplete: false)
        }
        var renamed = false
        defer {
            close(fileDescriptor)
            if !renamed { _ = syscalls.unlink(at: descriptor, name: temporaryName) }
        }
        var fileInfo = stat()
        guard fstat(fileDescriptor, &fileInfo) == 0,
              (fileInfo.st_mode & S_IFMT) == S_IFREG,
              fileInfo.st_uid == getuid()
        else { throw CapturePathError.unsafeTarget }
        guard fchmod(fileDescriptor, mode_t(0o600)) == 0 else { throw CapturePathError.unsafeTarget }
        try data.withUnsafeBytes { rawBuffer in
            var written = 0
            while written < rawBuffer.count {
                guard let address = rawBuffer.baseAddress else { break }
                let count = syscalls.writeFile(fileDescriptor, buffer: address.advanced(by: written), count: rawBuffer.count - written)
                if count < 0 && errno == EINTR { continue }
                guard count > 0 else {
                    throw ArtifactPublicationError(stage: .write, errnoCode: count < 0 ? errno : EIO, finalVisible: false, bytesComplete: false)
                }
                written += count
            }
        }
        guard syscalls.sync(fileDescriptor) == 0 else {
            throw ArtifactPublicationError(stage: .fileSync, errnoCode: errno, finalVisible: false, bytesComplete: true)
        }
        guard syscalls.renameExclusive(at: descriptor, from: temporaryName, to: name) == 0 else {
            throw ArtifactPublicationError(stage: .publish, errnoCode: errno, finalVisible: false, bytesComplete: true)
        }
        renamed = true
        guard syscalls.sync(descriptor) == 0 else {
            throw ArtifactPublicationError(stage: .directorySync, errnoCode: errno, finalVisible: true, bytesComplete: true)
        }
        return .durable
    }

    static func inheritedDirectoryDescriptor() -> Int32? {
        guard let raw = ProcessInfo.processInfo.environment["ASTRA_COMPUTER_ARTIFACT_DIR_FD"],
              let descriptor = Int32(raw), descriptor >= 0
        else { return nil }
        return descriptor
    }

    static func isPlainName(_ value: String) -> Bool {
        !value.isEmpty && value.count <= 128 && !value.contains("/") && !value.contains("\\") && !value.contains("\0") && value != "." && value != ".."
    }
}

final class SnapshotArtifactSession {
    private let publishPNG: (Data, String) throws -> Void
    private let bundlePublisher: ArtifactBundlePublisher?
    private let lock = NSLock()
    private var closed = false

    init(
        publishPNG: @escaping (Data, String) throws -> Void,
        bundlePublisher: ArtifactBundlePublisher?
    ) {
        self.publishPNG = publishPNG
        self.bundlePublisher = bundlePublisher
    }

    static func inherited() -> SnapshotArtifactSession {
        let publisher = ArtifactDirectory.inheritedDirectoryDescriptor().map {
            ArtifactBundlePublisher(directoryFD: $0)
        }
        return SnapshotArtifactSession(
            publishPNG: { try ArtifactDirectory.writePNG($0, named: $1) },
            bundlePublisher: publisher
        )
    }

    func publish(
        png: Data,
        named imageName: String,
        textDetail: SnapshotTextDetailRequest,
        detail: AXTextDetailEnvelope?
    ) throws {
        lock.lock()
        defer { lock.unlock() }
        guard !closed else { throw ArtifactBundleError.closed }
        switch textDetail {
        case .off:
            guard detail == nil else { throw ArtifactBundleError.invalidData }
            try publishPNG(png, imageName)
        case let .on(detailName):
            guard let detail, let bundlePublisher else {
                throw ArtifactBundleError.unsafeDirectory
            }
            _ = try bundlePublisher.publish(
                image: png,
                imageName: imageName,
                detail: detail.data,
                detailName: detailName
            )
        }
    }

    func close() {
        lock.lock()
        defer { lock.unlock() }
        guard !closed else { return }
        closed = true
        bundlePublisher?.close()
    }
}

func smartSnapshotMetadata(
    _ envelope: AXTextDetailEnvelope,
    snapshotID: String
) -> JSONValue {
    let digest = SHA256.hash(data: envelope.data).map { String(format: "%02x", $0) }.joined()
    return .object([
        "schema_version": .number(1),
        "snapshot_id": .string(snapshotID),
        "coverage": .string("reported_ax_subtree"),
        "node_count": .number(Double(envelope.nodeCount)),
        "max_depth_observed": .number(Double(envelope.maxDepthObserved)),
        "byte_count": .number(Double(envelope.data.count)),
        "sha256": .string(digest),
        "truncated": .bool(envelope.truncated),
        "truncation_reasons": .array(envelope.truncationReasons.map(JSONValue.string)),
    ])
}

func publishAfterFinalSnapshotValidation<Authority>(
    validate: () throws -> Authority,
    publish: (Authority) throws -> Void,
    register: (Authority) -> Void
) throws -> Authority {
    let authority = try validate()
    try publish(authority)
    register(authority)
    return authority
}

struct SnapshotFinalCommit<Result> {
    let result: Result
    let commit: () -> Void
}

struct SnapshotCaptureTransactionResult<Result> {
    let snapshot: JSONValue
    let result: Result
}

func completeSnapshotCaptureTransaction<PreparedAX, Authority, Result>(
    capturePNG: () throws -> Data,
    serializeAX: () throws -> PreparedAX,
    buildSnapshot: (Data, PreparedAX) -> JSONValue,
    prepareCommit: (JSONValue) throws -> SnapshotFinalCommit<Result>,
    validateFinalIdentity: () throws -> Authority,
    publish: (Data, PreparedAX) throws -> Void,
    register: (Authority, PreparedAX) -> Void
) throws -> SnapshotCaptureTransactionResult<Result> {
    let png = try capturePNG()
    let prepared = try serializeAX()
    let snapshot = buildSnapshot(png, prepared)
    let finalCommit = try prepareCommit(snapshot)
    _ = try publishAfterFinalSnapshotValidation(
        validate: validateFinalIdentity,
        publish: { _ in try publish(png, prepared) },
        register: { authority in
            register(authority, prepared)
            finalCommit.commit()
        }
    )
    return SnapshotCaptureTransactionResult(
        snapshot: snapshot,
        result: finalCommit.result
    )
}

private struct FinalSnapshotPublicationAuthority {
    let actionGuard: ActionGuard?
}

struct CaptureIdentity: Equatable {
    let pid: pid_t
    let windowID: CGWindowID
    let bounds: CGRect
    let axIdentity: CFHashCode
}

func makeCaptureIdentity(
    frontmostPID: pid_t?,
    windowID: CGWindowID,
    bounds: CGRect,
    axIdentity: CFHashCode
) -> CaptureIdentity? {
    guard let frontmostPID else { return nil }
    return CaptureIdentity(pid: frontmostPID, windowID: windowID, bounds: bounds, axIdentity: axIdentity)
}

func verifyCaptureIdentity(target: CaptureIdentity, before: CaptureIdentity?, after: CaptureIdentity?) throws {
    guard let before, let after else { throw WindowObservationError.targetGone }
    try verifyFocusedIdentity(target: target, selected: before)
    try verifyFocusedIdentity(target: target, selected: after)
}

func verifyFocusedIdentity(target: CaptureIdentity, selected: CaptureIdentity?) throws {
    guard let selected else { throw WindowObservationError.targetGone }
    guard selected.pid == target.pid, selected.windowID == target.windowID,
          selected.axIdentity == target.axIdentity, approximatelyEqual(selected.bounds, target.bounds)
    else { throw WindowObservationError.targetNotFrontmost }
}

struct WindowCandidate {
    let pid: pid_t
    let windowID: CGWindowID
    let bounds: CGRect
    let isOnScreen: Bool
    let isDesktop: Bool
    let isRegularApplication: Bool
}

func eligibleWindowCandidates(_ candidates: [WindowCandidate], displays: [CGRect]) -> [WindowCandidate] {
    candidates.filter { candidate in
        candidate.isOnScreen && !candidate.isDesktop && candidate.isRegularApplication &&
            candidate.bounds.width > 1 && candidate.bounds.height > 1 &&
            displays.contains(where: { $0.intersects(candidate.bounds) })
    }
}

enum SnapshotInvalidationReason: CaseIterable {
    case newSnapshot
    case focus
    case appsRefresh
    case close
    case failedRefresh
}

func cursorShouldHide(for reason: SnapshotInvalidationReason) -> Bool {
    switch reason {
    case .newSnapshot: false
    case .focus, .appsRefresh, .close, .failedRefresh: true
    }
}

func cursorShouldHideAfterSnapshot(succeeded: Bool) -> Bool { !succeeded }

final class SnapshotReferenceRegistry {
    private let lock = NSLock()
    private var snapshotID: String?
    private var references: [String: SnapshotElement] = [:]
    private var indexedReferences: [Int: String] = [:]
    private var actionGuard: ActionGuard?
    private var target: WindowTarget?

    func register(
        snapshotID: String,
        references: [String: SnapshotElement],
        indexedReferences: [Int: String] = [:],
        actionGuard: ActionGuard? = nil,
        target: WindowTarget? = nil
    ) {
        lock.lock()
        defer { lock.unlock() }
        self.snapshotID = snapshotID
        self.references = references
        self.indexedReferences = indexedReferences
        self.actionGuard = actionGuard
        self.target = target
    }

    func element(for reference: String, snapshotID: String) -> SnapshotElement? {
        lock.lock()
        defer { lock.unlock() }
        guard self.snapshotID == snapshotID else { return nil }
        return references[reference]
    }

    func takeForActions(snapshotID: String) -> SnapshotActionContext? {
        lock.lock()
        defer { lock.unlock() }
        guard self.snapshotID == snapshotID, let actionGuard, actionGuard.snapshotID == snapshotID else { return nil }
        let context = SnapshotActionContext(
            guardValue: actionGuard,
            references: references,
            indexedReferences: indexedReferences,
            target: target
        )
        self.snapshotID = nil
        references = [:]
        indexedReferences = [:]
        self.actionGuard = nil
        target = nil
        return context
    }

    func contextForPlanning(snapshotID: String) -> SnapshotActionContext? {
        lock.lock()
        defer { lock.unlock() }
        guard self.snapshotID == snapshotID, let actionGuard, actionGuard.snapshotID == snapshotID else { return nil }
        return SnapshotActionContext(
            guardValue: actionGuard,
            references: references,
            indexedReferences: indexedReferences,
            target: target
        )
    }

    var registeredSnapshotID: String? {
        lock.lock()
        defer { lock.unlock() }
        return snapshotID
    }

    func invalidate(_: SnapshotInvalidationReason) {
        lock.lock()
        defer { lock.unlock() }
        snapshotID = nil
        references.removeAll(keepingCapacity: false)
        indexedReferences.removeAll(keepingCapacity: false)
        actionGuard = nil
        target = nil
    }
}

struct SnapshotActionContext {
    let guardValue: ActionGuard
    let references: [String: SnapshotElement]
    let indexedReferences: [Int: String]
    let target: WindowTarget?

    init(
        guardValue: ActionGuard,
        references: [String: SnapshotElement],
        indexedReferences: [Int: String] = [:],
        target: WindowTarget? = nil
    ) {
        self.guardValue = guardValue
        self.references = references
        self.indexedReferences = indexedReferences
        self.target = target
    }

    func elementReference(index: Int) -> String? {
        indexedReferences[index]
    }
}

enum NativeCooperativeError: Equatable {
    case cooperative(CooperativeErrorCode)
    case action(ActionExecutionError)
}

struct CooperativePlanResult {
    let summary: DispatchPlanSummary?
    let error: NativeCooperativeError?
    let fragmentBinding: FragmentStageAuthority?

    init(
        summary: DispatchPlanSummary?,
        error: NativeCooperativeError?,
        fragmentBinding: FragmentStageAuthority? = nil
    ) {
        self.summary = summary
        self.error = error
        self.fragmentBinding = fragmentBinding
    }
}

struct CooperativeActionResult {
    let batch: ActionBatchResult
    let error: NativeCooperativeError?
    let fragmentBinding: FragmentStageAuthority?

    init(
        batch: ActionBatchResult,
        error: NativeCooperativeError?,
        fragmentBinding: FragmentStageAuthority? = nil
    ) {
        self.batch = batch
        self.error = error
        self.fragmentBinding = fragmentBinding
    }
}

enum SnapshotTextDetailRequest: Equatable {
    case off
    case on(artifactName: String)
}

struct AppStateObservation {
    let target: WindowTarget
    let catalogGeneration: Int
    let snapshot: JSONValue
}

protocol WindowObserving {
    func apps() throws -> JSONValue
    func select(appRef: String, windowRef: String) throws -> WindowTarget
    func getAppState(
        appRef: String,
        windowRef: String,
        catalogGeneration: Int,
        scope: String,
        artifactName: String,
        textDetail: SnapshotTextDetailRequest
    ) throws -> AppStateObservation
    func snapshot(
        appRef: String,
        windowRef: String,
        scope: String,
        artifactName: String?,
        textDetail: SnapshotTextDetailRequest
    ) throws -> JSONValue
    func snapshotSubtree(appRef: String, windowRef: String, snapshotID: String, elementRef: String,
                         scope: String, artifactName: String?, textDetail: SnapshotTextDetailRequest) throws -> JSONValue
    func planActions(snapshotID: String, interactionMode: InteractionMode, actions: [NativeAction]) -> CooperativePlanResult
    func planActions(snapshotID: String, interactionMode: InteractionMode, actions: [NativeAction], fragment: ForegroundFragmentPlanRequest?) -> CooperativePlanResult
    func takeoverBegin(snapshotID: String, planRef: String) throws -> String
    func takeoverBegin(snapshotID: String, planRef: String, declaration: ForegroundFragmentDeclaration) throws -> String
    func cooperativeAct(snapshotID: String, interactionMode: InteractionMode, planRef: String, takeoverRef: String?, actions: [NativeAction]) -> CooperativeActionResult
    func cooperativeAct(snapshotID: String, interactionMode: InteractionMode, planRef: String, takeoverRef: String?, actions: [NativeAction], fragmentStage: FragmentStageAuthority?) -> CooperativeActionResult
    func fragmentStageCommit(takeoverRef: String, commit: FragmentStageCommit) throws -> FragmentStageCommitOutcome
    func takeoverEnd(takeoverRef: String) throws -> TakeoverOutcome
    func takeoverEnd(takeoverRef: String, restorePreviousFocus: Bool) throws -> TakeoverOutcome
    func invalidateSnapshots()
}

extension WindowObserving {
    func snapshotSubtree(appRef: String, windowRef: String, snapshotID: String, elementRef: String,
                         scope: String, artifactName: String?, textDetail: SnapshotTextDetailRequest) throws -> JSONValue {
        throw WindowObservationError.staleTarget
    }

    func snapshot(
        appRef: String,
        windowRef: String,
        scope: String,
        artifactName: String?
    ) throws -> JSONValue {
        try snapshot(
            appRef: appRef,
            windowRef: windowRef,
            scope: scope,
            artifactName: artifactName,
            textDetail: .off
        )
    }
}

extension WindowObserving {
    func getAppState(
        appRef _: String,
        windowRef _: String,
        catalogGeneration _: Int,
        scope _: String,
        artifactName _: String,
        textDetail _: SnapshotTextDetailRequest
    ) throws -> AppStateObservation {
        throw WindowObservationError.captureFailed
    }
    func select(appRef _: String, windowRef _: String) throws -> WindowTarget {
        throw WindowObservationError.targetGone
    }
    func planActions(snapshotID _: String, interactionMode _: InteractionMode, actions _: [NativeAction]) -> CooperativePlanResult {
        CooperativePlanResult(summary: nil, error: .cooperative(.sidecarFailed))
    }
    func planActions(
        snapshotID: String,
        interactionMode: InteractionMode,
        actions: [NativeAction],
        fragment: ForegroundFragmentPlanRequest?
    ) -> CooperativePlanResult {
        guard fragment == nil else {
            return CooperativePlanResult(summary: nil, error: .action(.staleSnapshot))
        }
        return planActions(snapshotID: snapshotID, interactionMode: interactionMode, actions: actions)
    }
    func takeoverBegin(snapshotID _: String, planRef _: String) throws -> String {
        throw TakeoverError.stalePlan
    }
    func takeoverBegin(
        snapshotID _: String,
        planRef _: String,
        declaration _: ForegroundFragmentDeclaration
    ) throws -> String { throw TakeoverError.stalePlan }
    func cooperativeAct(snapshotID _: String, interactionMode _: InteractionMode, planRef _: String, takeoverRef _: String?, actions _: [NativeAction]) -> CooperativeActionResult {
        CooperativeActionResult(
            batch: ActionBatchResult(outcomes: [], lastAcknowledgedAction: -1, error: nil),
            error: .cooperative(.sidecarFailed)
        )
    }
    func cooperativeAct(
        snapshotID: String,
        interactionMode: InteractionMode,
        planRef: String,
        takeoverRef: String?,
        actions: [NativeAction],
        fragmentStage: FragmentStageAuthority?
    ) -> CooperativeActionResult {
        guard fragmentStage == nil else {
            return CooperativeActionResult(
                batch: ActionBatchResult(outcomes: [], lastAcknowledgedAction: -1, error: nil),
                error: .action(.staleSnapshot)
            )
        }
        return cooperativeAct(
            snapshotID: snapshotID,
            interactionMode: interactionMode,
            planRef: planRef,
            takeoverRef: takeoverRef,
            actions: actions
        )
    }
    func fragmentStageCommit(takeoverRef _: String, commit _: FragmentStageCommit) throws -> FragmentStageCommitOutcome {
        throw TakeoverError.stalePlan
    }
    func takeoverEnd(takeoverRef: String, restorePreviousFocus: Bool) throws -> TakeoverOutcome {
        guard restorePreviousFocus else { throw TakeoverError.authorityMismatch }
        return try takeoverEnd(takeoverRef: takeoverRef)
    }
    func takeoverEnd(takeoverRef _: String) throws -> TakeoverOutcome {
        throw TakeoverError.alreadyConsumed
    }
    func invalidateSnapshots() {}
}

final class SystemWindowObserver: WindowObserving {
    private let permissions: any PermissionStatusProviding
    private let observationReadiness = AXObservationReadiness()
    private let activation: any ApplicationActivationControlling
    private let actionLock = NSLock()
    private var catalogGeneration = 0
    private var targets: [String: WindowTarget] = [:]
    private var catalogWindowIdentities: [CatalogWindowIdentityKey: [CatalogWindowIdentityRecord]] = [:]
    private var catalogAbsenceTracker = CatalogWindowAbsenceTracker()
    private let recentSuggestionPopups = RecentSuggestionPopups()
    private let windowServerInventory: () -> Set<CGWindowID>?
    private let catalogBuilder: (() throws -> WindowCatalogObservation)?
    private let catalogWindows: (() throws -> [CatalogSCWindow])?
    private let catalogAXWindowMatcher: ((AXUIElement, WindowTarget) -> AXUIElement?)?
    private let catalogAXWindows: ((pid_t) -> [CatalogAXWindowRecord])?
    private let catalogAXIdentityHash: (AXUIElement) -> CFHashCode
    private let snapshotReferences = SnapshotReferenceRegistry()
    private var cooperativePlans: [String: StoredCooperativePlan] = [:]
    private let cooperativePlanClock: () -> TimeInterval
    private var nextCooperativePlanSequence: UInt64 = 0
    private let virtualCursor: any VirtualCursorPresenting
    private let userActivity: any UserActivityMonitoring
    private let pidCompatibility: PIDInputCompatibilityRegistry
    private let pidPointerCapability: PIDPointerDeliveryCapability
    private let foregroundKeyboardCapability: ForegroundKeyboardDeliveryCapability
    private let cooperativeStateInvalidationObserver: ((SnapshotInvalidationReason) -> Void)?
    private let artifactSession: SnapshotArtifactSession
    private lazy var takeoverCoordinator = ForegroundTakeoverCoordinator(
        activity: userActivity,
        activation: activation,
        cursor: virtualCursor,
        frontmostPID: { liveFrontmostPID() },
        applicationExists: { pid in
            guard let application = NSRunningApplication(processIdentifier: pid) else { return false }
            return !application.isTerminated
        },
        revalidate: { [weak self] target in
            guard let self else { throw WindowObservationError.targetGone }
            _ = try self.backgroundTargetController().snapshotTargetState(target)
            return target
        },
        takePlan: { [weak self] planRef in
            guard let self else { return nil }
            self.pruneCooperativeFragmentDraftsLocked(now: self.cooperativePlanClock())
            guard
                  let stored = self.cooperativePlans.removeValue(forKey: planRef),
                  let target = stored.snapshotContext.target,
                  let application = self.pidTargetApplication(pid: target.pid)
            else { return nil }
            return ForegroundTakeoverPlanAuthority(
                target: target,
                guardValue: stored.context.guardValue,
                plan: stored.plan,
                actions: stored.actions,
                application: application,
                dispatcher: stored.dispatcher,
                fragmentRequest: stored.fragmentRequest,
                snapshotGuard: stored.snapshotContext.guardValue
            )
        }
    )

    init(
        permissions: any PermissionStatusProviding = SystemPermissionStatus(),
        activation: any ApplicationActivationControlling = LaunchServicesApplicationActivationController(),
        virtualCursor: any VirtualCursorPresenting = UnavailableVirtualCursorPresenter(),
        userActivity: any UserActivityMonitoring = UserActivityArbiter(),
        pidCompatibility: PIDInputCompatibilityRegistry? = nil,
        pidPointerCapability: PIDPointerDeliveryCapability = .unavailable,
        foregroundKeyboardCapability: ForegroundKeyboardDeliveryCapability = .unavailable,
        cooperativePlanClock: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
        cooperativeStateInvalidationObserver: ((SnapshotInvalidationReason) -> Void)? = nil,
        artifactSession: SnapshotArtifactSession = .inherited(),
        catalogBuilder: (() throws -> WindowCatalogObservation)? = nil,
        catalogWindows: (() throws -> [CatalogSCWindow])? = nil,
        catalogAXWindowMatcher: ((AXUIElement, WindowTarget) -> AXUIElement?)? = nil,
        catalogAXIdentityHash: @escaping (AXUIElement) -> CFHashCode = { CFHash($0) },
        catalogAXWindows: ((pid_t) -> [CatalogAXWindowRecord])? = nil,
        windowServerInventory: (() -> Set<CGWindowID>?)? = nil
    ) {
        self.permissions = permissions
        self.activation = activation
        self.virtualCursor = virtualCursor
        self.userActivity = userActivity
        self.pidCompatibility = pidCompatibility ?? .bundled()
        self.pidPointerCapability = pidPointerCapability
        self.foregroundKeyboardCapability = foregroundKeyboardCapability
        self.cooperativePlanClock = cooperativePlanClock
        self.cooperativeStateInvalidationObserver = cooperativeStateInvalidationObserver
        self.artifactSession = artifactSession
        self.catalogBuilder = catalogBuilder
        self.catalogWindows = catalogWindows
        self.catalogAXWindowMatcher = catalogAXWindowMatcher
        self.catalogAXIdentityHash = catalogAXIdentityHash
        self.catalogAXWindows = catalogAXWindows
        self.windowServerInventory = windowServerInventory ?? (
            catalogBuilder == nil && catalogWindows == nil ? fullWindowServerInventoryForAbsence : { nil }
        )
    }

    func apps() throws -> JSONValue {
        invalidateCooperativeState(.appsRefresh)
        guard permissions.screenRecordingAllowed else {
            throw WindowObservationError.permissionDenied("Screen Recording permission is required to list eligible windows")
        }
        let observation: WindowCatalogObservation
        if let catalogBuilder {
            observation = try catalogBuilder()
        } else {
            observation = try buildCatalog()
        }
        let nextGeneration = try nextCatalogGeneration(after: catalogGeneration)
        targets = observation.targets
        catalogGeneration = nextGeneration
        // Track only exposed identities, retaining history across refreshes.
        for case let .object(app) in observation.apps {
            guard case let .string(appRef)? = app["app_ref"],
                  case let .array(windows)? = app["windows"] else { continue }
            for case let .object(window) in windows {
                guard case .bool(true)? = window["bindable"],
                      case let .string(reference)? = window["window_identity_ref"],
                      case let .string(windowRef)? = window["window_ref"],
                      let target = observation.targets[windowRef], target.appRef == appRef
                else { continue }
                catalogAbsenceTracker.record(reference: reference, windowID: target.windowID)
            }
        }
        var result: [String: JSONValue] = [
            "catalog_generation": .number(Double(nextGeneration)),
            "apps": .array(observation.apps),
        ]
        let absent = catalogAbsenceTracker.confirmedAbsent(windowIDs: windowServerInventory())
        if !absent.isEmpty {
            result["confirmed_absent_window_identity_refs"] = .array(absent.map(JSONValue.string))
        }
        return .object(result)
    }

    private func buildCatalog() throws -> WindowCatalogObservation {
        let budget = AXObservationBudget(duration: 8)
        let restoreBudget = budget.install()
        defer { restoreBudget() }
        let inventories = ObservationPIDInventory<[CatalogAXWindowRecord]>()
        let windows: [CatalogSCWindow]
        if let catalogWindows {
            windows = try catalogWindows()
        } else {
            windows = try systemCatalogWindows()
        }
        var localTargets: [String: WindowTarget] = [:]
        var localWindowIdentities: [CatalogWindowIdentityKey: [CatalogWindowIdentityRecord]] = [:]
        var usedWindowIdentityReferences: Set<String> = []
        var grouped: [pid_t: (
            appRef: String,
            name: String,
            bundleID: String?,
            appVersion: String?,
            windows: [JSONValue]
        )] = [:]
        let displayBounds = NSScreen.screens.map(\.frame)
        for window in windows where localTargets.count < maximumObservedWindows && window.isOnScreen && window.bounds.width > 1 && window.bounds.height > 1 {
            guard !window.isTerminated,
                  window.isRegularApplication,
                  grouped[window.pid] != nil || grouped.count < maximumObservedApplications
            else { continue }
            let candidate = WindowCandidate(
                pid: window.pid,
                windowID: window.windowID,
                bounds: window.bounds,
                isOnScreen: window.isOnScreen,
                isDesktop: false,
                isRegularApplication: window.isRegularApplication
            )
            guard !eligibleWindowCandidates([candidate], displays: displayBounds).isEmpty else { continue }
            let appRef = grouped[window.pid]?.appRef ?? opaqueReference(prefix: "app")
            let windowRef = opaqueReference(prefix: "win")
            let target = WindowTarget(
                appRef: appRef,
                windowRef: windowRef,
                pid: window.pid,
                windowID: window.windowID,
                bounds: window.bounds,
                title: window.title
            )
            var values = grouped[window.pid] ?? (
                appRef,
                window.applicationName,
                window.bundleID,
                window.appVersion,
                []
            )
            let accessibilityTrusted = permissions.accessibilityTrusted
            try budget.check()
            let axWindow: AXUIElement?
            if accessibilityTrusted {
                if let catalogAXWindowMatcher {
                    axWindow = catalogAXWindowMatcher(AXUIElementCreateApplication(window.pid), target)
                } else {
                    let inventory = inventories.value(for: window.pid) {
                        catalogAXWindows?(window.pid) ?? catalogAXWindowRecords(pid: window.pid)
                    }
                    axWindow = matchingCatalogAXWindow(target: target, records: inventory)
                }
            } else { axWindow = nil }
            let documentPath: String?
            if let axWindow,
               let rawDocument = AXNodeReader.stringAttribute(
                   axWindow,
                   kAXDocumentAttribute
               ).value {
                documentPath = normalizedDocumentPath(rawDocument)
            } else {
                documentPath = nil
            }
            let windowIdentityRef: String?
            if let axWindow {
                let key = CatalogWindowIdentityKey(
                    pid: window.pid,
                    windowID: window.windowID,
                    axIdentity: catalogAXIdentityHash(axWindow)
                )
                let exactPrior = (catalogWindowIdentities[key] ?? []).filter {
                    CFEqual($0.element, axWindow)
                }
                let reusable = exactPrior.count == 1
                    && !usedWindowIdentityReferences.contains(exactPrior[0].reference)
                    ? exactPrior[0].reference
                    : nil
                let token = reusable ?? opaqueReference(prefix: "identity")
                usedWindowIdentityReferences.insert(token)
                localWindowIdentities[key, default: []].append(
                    CatalogWindowIdentityRecord(element: axWindow, reference: token)
                )
                windowIdentityRef = token
            } else {
                windowIdentityRef = nil
            }
            let windowValues = catalogWindowJSON(
                windowRef: windowRef,
                title: window.title,
                bounds: window.bounds,
                documentPath: documentPath,
                accessibilityTrusted: accessibilityTrusted,
                exactAXWindowFound: axWindow != nil,
                windowIdentityRef: windowIdentityRef
            )
            var catalogTarget = target
            if let axWindow {
                catalogTarget = WindowTarget(
                    appRef: appRef,
                    windowRef: windowRef,
                    pid: window.pid,
                    windowID: window.windowID,
                    bounds: window.bounds,
                    title: window.title,
                    axIdentity: catalogAXIdentityHash(axWindow),
                    axElement: axWindow
                )
            }
            localTargets[windowRef] = catalogTarget
            values.windows.append(.object(windowValues))
            grouped[window.pid] = values
        }
        let result = grouped.values.map { value in
            JSONValue.object([
                "app_ref": .string(value.appRef),
                "name": .string(bounded(value.name)),
                "bundle_id": value.bundleID.map { .string(bounded($0)) } ?? .null,
                "app_version": value.appVersion.map { .string(bounded($0)) } ?? .null,
                "windows": .array(value.windows),
            ])
        }.sorted { lhs, rhs in
            appName(lhs) < appName(rhs)
        }
        try budget.check()
        catalogWindowIdentities = localWindowIdentities
        return WindowCatalogObservation(
            targets: localTargets,
            apps: Array(result.prefix(maximumObservedApplications))
        )
    }

    func select(appRef: String, windowRef: String) throws -> WindowTarget {
        invalidateCooperativeState(.focus)
        guard permissions.accessibilityTrusted else {
            throw WindowObservationError.permissionDenied("Accessibility permission is required to select a window")
        }
        let target = try backgroundTargetController().select(appRef: appRef, windowRef: windowRef)
        targets[windowRef] = target
        return target
    }

    func snapshot(
        appRef: String,
        windowRef: String,
        scope: String,
        artifactName: String?,
        textDetail: SnapshotTextDetailRequest
    ) throws -> JSONValue {
        try captureWindowSnapshot(appRef: appRef, windowRef: windowRef, scope: scope,
                                  artifactName: artifactName, textDetail: textDetail)
    }

    func snapshotSubtree(appRef: String, windowRef: String, snapshotID: String, elementRef: String,
                         scope: String, artifactName: String?, textDetail: SnapshotTextDetailRequest) throws -> JSONValue {
        guard scope == "target_window" else { throw WindowObservationError.invalidScope }
        guard let context = snapshotReferences.contextForPlanning(snapshotID: snapshotID),
              context.target?.appRef == appRef, context.target?.windowRef == windowRef,
              let element = context.references[elementRef]?.element else {
            throw WindowObservationError.staleTarget
        }
        return try captureWindowSnapshot(appRef: appRef, windowRef: windowRef, scope: scope,
            artifactName: artifactName, textDetail: textDetail, subtree: element)
    }

    private func captureWindowSnapshot(appRef: String, windowRef: String, scope: String,
        artifactName: String?, textDetail: SnapshotTextDetailRequest, subtree: AXUIElement? = nil) throws -> JSONValue {
        let operationBudget = AXObservationBudget(duration: 12)
        let restoreBudget = operationBudget.install()
        defer { restoreBudget() }
        do {
        var snapshotSucceeded = false
        defer {
            if cursorShouldHideAfterSnapshot(succeeded: snapshotSucceeded) { virtualCursor.hide() }
        }
        guard scope == "target_window" || scope == "display" else {
            throw WindowObservationError.invalidScope
        }
        invalidateCooperativeState(.newSnapshot)
        guard permissions.screenRecordingAllowed else {
            throw WindowObservationError.permissionDenied("Screen Recording permission is required to capture a window")
        }
        guard permissions.accessibilityTrusted else {
            throw WindowObservationError.permissionDenied("Accessibility permission is required to inspect a window")
        }
        let selectedTarget = try resolve(appRef: appRef, windowRef: windowRef)
        let target = takeoverCoordinator.fragmentObservationTarget(for: selectedTarget) ?? selectedTarget
        guard let artifactName else {
            throw WindowObservationError.invalidCapturePath
        }
        let captured: SnapshotCaptureTransactionResult<Void> = try captureSnapshot(
            target: target,
            scope: scope,
            artifactName: artifactName,
            textDetail: textDetail,
            subtree: subtree,
            prepareCommit: { _ in SnapshotFinalCommit(result: (), commit: {}) }
        )
        snapshotSucceeded = true
        return captured.snapshot
        } catch {
            if !operationBudget.available { throw WindowObservationError.observationTimedOut }
            throw error
        }
    }

    func getAppState(
        appRef: String,
        windowRef: String,
        catalogGeneration requestedGeneration: Int,
        scope: String,
        artifactName: String,
        textDetail: SnapshotTextDetailRequest
    ) throws -> AppStateObservation {
        let operationBudget = AXObservationBudget(duration: 12)
        let restoreBudget = operationBudget.install()
        defer { restoreBudget() }
        do {
        var snapshotSucceeded = false
        defer {
            if cursorShouldHideAfterSnapshot(succeeded: snapshotSucceeded) { virtualCursor.hide() }
        }
        guard scope == "target_window" || scope == "display" else {
            throw WindowObservationError.invalidScope
        }
        invalidateCooperativeState(.newSnapshot)
        guard permissions.screenRecordingAllowed else {
            throw WindowObservationError.permissionDenied("Screen Recording permission is required to capture a window")
        }
        guard permissions.accessibilityTrusted else {
            throw WindowObservationError.permissionDenied("Accessibility permission is required to inspect a window")
        }
        _ = try resolveCatalogTarget(
            catalogGeneration: requestedGeneration,
            appRef: appRef,
            windowRef: windowRef,
            current: { $0 }
        )
        let controller = backgroundTargetController()
        let candidate = try controller.select(appRef: appRef, windowRef: windowRef)
        _ = try resolveCatalogTarget(
            catalogGeneration: requestedGeneration,
            appRef: appRef,
            windowRef: windowRef,
            current: { _ in candidate }
        )
        guard candidate.interactionMode == .background else {
            throw WindowObservationError.staleTarget
        }
        let request = GetAppStateRequest(
            appRef: appRef,
            windowRef: windowRef,
            catalogGeneration: requestedGeneration,
            scope: scope,
            artifactName: artifactName,
            textDetail: textDetail
        )
        let captured: SnapshotCaptureTransactionResult<AppStateObservation>
        do {
            captured = try captureSnapshot(
                target: candidate,
                scope: scope,
                artifactName: artifactName,
                textDetail: textDetail,
                prepareCommit: { snapshot in
                    let observation = AppStateObservation(
                        target: candidate,
                        catalogGeneration: requestedGeneration,
                        snapshot: snapshot
                    )
                    guard validGetAppStateResponse(
                        result: observation.helperResultJSON,
                        snapshot: snapshot,
                        request: request
                    ) else { throw WindowObservationError.captureFailed }
                    return SnapshotFinalCommit(
                        result: observation,
                        commit: { self.targets[windowRef] = candidate }
                    )
                }
            )
        } catch WindowObservationError.targetGone {
            let current = try controller.select(appRef: appRef, windowRef: windowRef)
            _ = try resolveCatalogTarget(
                catalogGeneration: requestedGeneration,
                appRef: appRef,
                windowRef: windowRef,
                current: { _ in current }
            )
            throw WindowObservationError.targetGone
        }
        snapshotSucceeded = true
        return captured.result
        } catch {
            if !operationBudget.available { throw WindowObservationError.observationTimedOut }
            throw error
        }
    }

    private func captureSnapshot<Result>(
        target: WindowTarget,
        scope: String,
        artifactName: String,
        textDetail: SnapshotTextDetailRequest,
        subtree: AXUIElement? = nil,
        prepareCommit: (JSONValue) throws -> SnapshotFinalCommit<Result>
    ) throws -> SnapshotCaptureTransactionResult<Result> {
        let budget = AXObservationBudget.current ?? AXObservationBudget(duration: 12)
        let restoreBudget = budget.install()
        let metrics = ObservationStageMetrics()
        let attributeCache = AXObservationAttributeCache()
        defer { metrics.emit(budget: budget, cache: attributeCache); restoreBudget() }
        try budget.check()
        let backgroundFrontmostPID: pid_t?
        if target.interactionMode == .background {
            guard let frontmostPID = liveFrontmostPID() else {
                throw WindowObservationError.targetNotFrontmost
            }
            backgroundFrontmostPID = frontmostPID
        } else {
            backgroundFrontmostPID = nil
        }
        guard target.interactionMode == .background ||
                liveFrontmostPID() == target.pid
        else {
            throw WindowObservationError.targetNotFrontmost
        }
        let shareableBefore = try metrics.measure(.inventory) { try waitForShareableContent() }
        guard let window = matchingCurrentWindow(for: target, in: shareableBefore.windows) else {
            logActionRejected("observation_validation=initial_window_missing")
            throw WindowObservationError.targetGone
        }
        let selectedDisplay: (native: SCDisplay, descriptor: DisplayCaptureCandidate)?
        if scope == "display" {
            let displays = shareableBefore.displays.map { display in
                DisplayCaptureCandidate(
                    displayID: display.displayID,
                    bounds: display.frame,
                    pixelSize: CGSize(width: display.width, height: display.height)
                )
            }
            let descriptor = try selectDisplayCapture(
                targetBounds: window.frame,
                candidates: displays
            )
            guard let native = shareableBefore.displays.first(where: {
                $0.displayID == descriptor.displayID
            }) else { throw WindowObservationError.displayUnavailable }
            selectedDisplay = (native, descriptor)
        } else {
            selectedDisplay = nil
        }
        let appElement = AXUIElementCreateApplication(target.pid)
        let appOwnedOverlayActive = target.interactionMode == .foregroundTakeover &&
            containedAppOwnedOverlayActive(
                targetPID: target.pid,
                targetWindowID: target.windowID,
                targetBounds: window.frame,
                records: interactionWindowRecords(for: target)
            )
        let focusedRootPreference: FocusedRootPreference = appOwnedOverlayActive
            ? .containedOverlay
            : .selectedWindow
        guard let expectedAXWindow = matchingAXWindow(in: appElement, target: target) else {
            logActionRejected("observation_validation=initial_ax_window_mismatch")
            throw WindowObservationError.targetGone
        }
        if let launchedAt = NSRunningApplication(processIdentifier: target.pid)?.launchDate?.timeIntervalSince1970 {
            let readiness = try observationReadiness.prepare(application: appElement, pid: target.pid,
                launchedAt: launchedAt, budget: budget)
            if readiness == .enabled {
                logActionRejected("observation_validation=accessibility_readiness_enabled")
            }
        }
        let focusedBefore: AXUIElement
        if target.interactionMode == .background {
            let state = try backgroundTargetController().snapshotTargetState(target)
            guard state.axIdentity == CFHash(expectedAXWindow) else {
                logActionRejected("observation_validation=initial_ax_identity_mismatch")
                throw WindowObservationError.targetGone
            }
            focusedBefore = expectedAXWindow
        } else {
            guard let focused = trustedFocusedAXRoot(
                in: appElement,
                expected: expectedAXWindow,
                targetBounds: window.frame,
                preference: focusedRootPreference
            ) else { throw WindowObservationError.targetNotFrontmost }
            focusedBefore = focused
        }
        let keyboardFocusBefore = target.interactionMode == .foregroundTakeover
            ? currentKeyboardFocusObservation(
                in: appElement,
                expectedPID: target.pid,
                containerBounds: AXNodeReader.frameAttribute(focusedBefore) ?? .zero
            ).authority
            : nil
        let backgroundSession = target.interactionMode == .background
            ? try BackgroundSnapshotSession(
                target: target,
                exactAXElement: expectedAXWindow,
                frontmostPID: backgroundFrontmostPID
            )
            : nil
        let targetIdentity = CaptureIdentity(pid: target.pid, windowID: target.windowID, bounds: target.bounds, axIdentity: CFHash(expectedAXWindow))
        let beforeIdentity = target.interactionMode == .background
            ? CaptureIdentity(
                pid: target.pid,
                windowID: window.windowID,
                bounds: window.frame,
                axIdentity: CFHash(expectedAXWindow)
            )
            : makeCaptureIdentity(
                frontmostPID: liveFrontmostPID(),
                windowID: window.windowID,
                bounds: window.frame,
                axIdentity: CFHash(expectedAXWindow)
            )
        try verifyCaptureIdentity(target: targetIdentity, before: beforeIdentity, after: beforeIdentity)
        let captureBounds: CGRect
        var sheetImageRoot: SCWindow?
        let pixelSize: CGSize
        let backingScale: CGFloat
        let observationBounds: CGRect
        let image: CGImage
        var backgroundSingletonCaptured = false
        let imageStart = ProcessInfo.processInfo.systemUptime
        if let selectedDisplay {
            let excludedWindowID = cursorOverlayExclusionWindowID(
                scope: scope,
                presenter: virtualCursor,
                availableWindows: shareableBefore.windows.compactMap { candidate in
                    guard let ownerPID = candidate.owningApplication?.processID else { return nil }
                    return VirtualCursorWindowRecord(windowID: candidate.windowID, ownerPID: ownerPID)
                }
            )
            captureBounds = CGRect(origin: .zero, size: selectedDisplay.descriptor.bounds.size)
            pixelSize = selectedDisplay.descriptor.pixelSize
            backingScale = selectedDisplay.descriptor.pixelSize.width / selectedDisplay.descriptor.bounds.width
            observationBounds = selectedDisplay.descriptor.bounds
            image = try waitForDisplayImage(
                display: selectedDisplay.native,
                descriptor: selectedDisplay.descriptor,
                excludingWindows: shareableBefore.windows.filter { $0.windowID == excludedWindowID }
            )
        } else {
            let geometry = WindowGeometry(
                bounds: window.frame,
                backingScale: NSScreen.screens.first(where: {
                    $0.frame.intersects(window.frame)
                })?.backingScaleFactor ?? 1
            )
            captureBounds = CGRect(origin: .zero, size: window.frame.size)
            observationBounds = window.frame
            let role = AXNodeReader.stringAttribute(expectedAXWindow, kAXRoleAttribute)
            let subrole = AXNodeReader.stringAttribute(expectedAXWindow, kAXSubroleAttribute)
            if role.status == .complete && role.value == kAXSheetRole {
                guard let root = sheetCaptureWindow(element: expectedAXWindow, pid: target.pid,
                    windows: shareableBefore.windows) else { throw WindowObservationError.axWindowUnmatched }
                sheetImageRoot = root
            }
            let imageWindow = sheetImageRoot ?? window
            let imageGeometry = WindowGeometry(bounds: imageWindow.frame, backingScale: geometry.backingScale)
            let sameApplicationWindowFrames = shareableBefore.windows.compactMap { candidate -> CGRect? in
                guard candidate.windowID != imageWindow.windowID,
                      candidate.owningApplication?.processID == target.pid
                else { return nil }
                return candidate.frame
            }
            do {
                let exactCaptured: CGImage
                if let backgroundSession {
                    exactCaptured = try backgroundSession.capture { selectedWindowID in
                        guard selectedWindowID == window.windowID else {
                            throw WindowObservationError.targetGone
                        }
                        return try waitForImage(
                            window: imageWindow,
                            geometry: imageGeometry,
                            focusedTransientActive: false,
                            sameApplicationWindowFrames: sameApplicationWindowFrames
                        )
                    }
                } else {
                    exactCaptured = try waitForImage(
                        window: imageWindow,
                        geometry: imageGeometry,
                        focusedTransientActive: sheetImageRoot == nil && (CFHash(focusedBefore) != CFHash(expectedAXWindow)
                            || appOwnedOverlayActive),
                        sameApplicationWindowFrames: sameApplicationWindowFrames
                    )
                }
                let exactGeometryRejected = (try? resolvedWindowImageGeometry(
                    requested: geometry,
                    imageWidth: exactCaptured.width,
                    imageHeight: exactCaptured.height
                )) == nil
                // Background reads may recover only an exact, frontmost popup-like AX window.
                // Other wrong-sized windows retain the ordinary capture refusal.
                let backgroundPopupCandidate = sheetImageRoot == nil && exactGeometryRejected &&
                    target.interactionMode == .background
                let backgroundFrontmostNow = backgroundPopupCandidate ? liveFrontmostPID() : nil
                let backgroundPopupAllowed = backgroundPopupCandidate &&
                    scope == "target_window" &&
                    popupSingletonAXRoleAllowed(role: role, subrole: subrole) &&
                    backgroundFrontmostNow == target.pid
                if backgroundPopupCandidate && !backgroundPopupAllowed {
                    let roleKind = role.value == kAXWindowRole as String ? "window" :
                        (role.value == kAXSheetRole as String ? "sheet" : "other")
                    let subroleKind = subrole.value == "AXDialog" ? "dialog" :
                        (subrole.value == "AXStandardWindow" ? "standard" : "other")
                    let subroleIdentifier: String = {
                        guard let value = subrole.value else { return "nil" }
                        guard !value.isEmpty else { return "empty" }
                        let asciiIdentifier = value.utf8.allSatisfy { byte in
                            (65...90).contains(byte) || (97...122).contains(byte) ||
                                (48...57).contains(byte)
                        }
                        return value.hasPrefix("AX") && value.utf8.count <= 48 && asciiIdentifier
                            ? value : "unrecognized"
                    }()
                    logActionRejected(
                        "CAPTURE-SOURCE singleton_filtered=background_gate_rejected"
                            + " windowID=\(window.windowID) scopeTargetWindow=\(scope == "target_window")"
                            + " roleStatus=\(role.status) roleKind=\(roleKind)"
                            + " subroleStatus=\(subrole.status) subroleKind=\(subroleKind)"
                            + " subroleIdentifier=\(subroleIdentifier)"
                            + " frontmostMatch=\(backgroundFrontmostNow == target.pid)"
                    )
                }
                let captured: CGImage
                if sheetImageRoot == nil, exactGeometryRejected,
                   (target.interactionMode == .foregroundTakeover || backgroundPopupAllowed) {
                    if backgroundPopupAllowed {
                        guard let backgroundSession else { throw WindowObservationError.targetGone }
                        try backgroundSession.verifyFrontmost(liveFrontmostPID())
                    }
                    do {
                        captured = try captureVerifiedPopupWithSingletonDisplayFilter(
                            window: window,
                            expected: PopupFilteredWindowIdentity(
                                windowID: window.windowID, pid: target.pid, frame: window.frame
                            ),
                            availableWindows: shareableBefore.windows,
                            displays: shareableBefore.displays,
                            rejectedExactWindowImage: exactCaptured,
                            requireCompositorProof: true
                        )
                    } catch {
                        // An unavailable or inconclusive B.5 proof is an overlay
                        // refusal, including singleton capture and budget errors.
                        if backgroundPopupAllowed { throw WindowObservationError.overlayBlocked }
                        throw error
                    }
                    if backgroundPopupAllowed {
                        guard let backgroundSession else { throw WindowObservationError.targetGone }
                        try backgroundSession.verifyFrontmost(liveFrontmostPID())
                        guard exactAXWindowHasPopupCaptureRole(expectedAXWindow) else {
                            logActionRejected(
                                "CAPTURE-SOURCE singleton_filtered=post_capture_ax_role_rejected"
                                    + " windowID=\(window.windowID)"
                            )
                            throw WindowObservationError.targetGone
                        }
                        backgroundSingletonCaptured = true
                    }
                    logActionRejected(
                        "CAPTURE-SOURCE singleton_filtered=accepted windowID=\(window.windowID)"
                            + " width=\(captured.width) height=\(captured.height)"
                    )
                } else { captured = exactCaptured }
                if sheetImageRoot != nil {
                    // SCK may render the whole parent composite for a sheet ID,
                    // scaled into the sheet's requested dimensions. Capture the
                    // proven owned root at its own size, then publish ONLY the sheet.
                    let crop = try sheetImageCropRect(source: imageGeometry, target: window.frame,
                        imageWidth: captured.width, imageHeight: captured.height)
                    guard let cropped = captured.cropping(to: crop) else { throw WindowObservationError.axSerializationFailed }
                    image = cropped
                } else { image = captured }
                let capturedGeometry = try resolvedWindowImageGeometry(
                    requested: geometry,
                    imageWidth: image.width,
                    imageHeight: image.height
                )
                try validateWindowImageContent(image)
                pixelSize = capturedGeometry.pixelSize
                backingScale = capturedGeometry.backingScale
            } catch {
                if sheetImageRoot == nil, case WindowObservationError.captureFailed = error {
                    PopupCaptureOwnerDiagnostics.recordIfNeeded(
                        root: expectedAXWindow, targetPID: target.pid,
                        targetWindow: window, windows: shareableBefore.windows
                    )
                }
                throw error
            }
        }
        metrics.record(.image, startedAt: imageStart)
        let shareableAfter = try metrics.measure(.inventory) { try waitForShareableContent() }
        if let root = sheetImageRoot {
            guard let current = sheetCaptureWindow(element: expectedAXWindow, pid: target.pid,
                windows: shareableAfter.windows), current.windowID == root.windowID,
                current.frame == root.frame else { throw WindowObservationError.targetGone }
        }
        guard let windowAfter = matchingCurrentWindow(for: target, in: shareableAfter.windows) else {
            throw WindowObservationError.targetGone
        }
        if backgroundSingletonCaptured {
            guard let backgroundSession else { throw WindowObservationError.targetGone }
            try backgroundSession.verifyFrontmost(liveFrontmostPID())
        }
        if let selectedDisplay {
            let postSelection = try selectDisplayCapture(
                targetBounds: windowAfter.frame,
                candidates: shareableAfter.displays.map { display in
                    DisplayCaptureCandidate(
                        displayID: display.displayID,
                        bounds: display.frame,
                        pixelSize: CGSize(width: display.width, height: display.height)
                    )
                }
            )
            guard postSelection == selectedDisplay.descriptor else {
                throw WindowObservationError.displayUnavailable
            }
        }
        guard let expectedAfter = matchingAXWindow(in: appElement, target: target),
              CFHash(expectedAfter) == CFHash(expectedAXWindow),
              let focusedBeforeBounds = AXNodeReader.frameAttribute(focusedBefore)
        else {
            logActionRejected("observation_validation=post_capture_ax_mismatch")
            throw WindowObservationError.targetGone
        }
        let focusedAfter: AXUIElement
        let focusedAfterBounds: CGRect
        if target.interactionMode == .background {
            let state = try backgroundTargetController().snapshotTargetState(target)
            guard state.axIdentity == CFHash(expectedAfter),
                  approximatelyEqual(state.bounds, windowAfter.frame),
                  let bounds = AXNodeReader.frameAttribute(expectedAfter)
            else { throw WindowObservationError.targetGone }
            focusedAfter = expectedAfter
            focusedAfterBounds = bounds
        } else {
            guard let focused = trustedFocusedAXRoot(
                in: appElement,
                expected: expectedAfter,
                targetBounds: windowAfter.frame,
                preference: focusedRootPreference
            ),
            let bounds = AXNodeReader.frameAttribute(focused),
            trustedFocusedTransition(
                expectedIdentity: CFHash(expectedAfter),
                targetBounds: windowAfter.frame,
                beforeIdentity: CFHash(focusedBefore),
                beforeBounds: focusedBeforeBounds,
                afterIdentity: CFHash(focused),
                afterBounds: bounds
            )
            else { throw WindowObservationError.targetNotFrontmost }
            focusedAfter = focused
            focusedAfterBounds = bounds
        }
        if backgroundSingletonCaptured {
            guard let backgroundSession else { throw WindowObservationError.targetGone }
            try backgroundSession.verifyFrontmost(liveFrontmostPID())
            guard exactAXWindowHasPopupCaptureRole(expectedAXWindow) else {
                logActionRejected(
                    "CAPTURE-SOURCE singleton_filtered=pre_identity_ax_role_rejected"
                        + " windowID=\(windowAfter.windowID)"
                )
                throw WindowObservationError.targetGone
            }
        }
        let afterIdentity = target.interactionMode == .background
            ? CaptureIdentity(
                pid: target.pid,
                windowID: windowAfter.windowID,
                bounds: windowAfter.frame,
                axIdentity: CFHash(expectedAfter)
            )
            : makeCaptureIdentity(
                frontmostPID: liveFrontmostPID(),
                windowID: windowAfter.windowID,
                bounds: windowAfter.frame,
                axIdentity: CFHash(expectedAfter)
            )
        try verifyCaptureIdentity(target: targetIdentity, before: beforeIdentity, after: afterIdentity)
        let snapshotID = opaqueReference(prefix: "snapshot")
        let capturePNG = {
            try metrics.measure(.png) {
                try budget.check()
                guard let png = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:]) else {
                    throw WindowObservationError.captureFailed
                }
                try budget.check()
                return png
            }
        }
        let serializeAX = {
            let restoreCache = attributeCache.install()
            defer { restoreCache() }
            do {
                try budget.check()
                let serializationRoot: AXUIElement
                if let backgroundSession {
                    serializationRoot = try backgroundSession.serialize { exactAXElement in
                        guard CFEqual(exactAXElement, focusedAfter) else {
                            throw WindowObservationError.targetGone
                        }
                        return exactAXElement
                    }
                } else {
                    serializationRoot = focusedAfter
                }
                if let subtree, !axSubtreeBelongsToWindow(subtree, window: expectedAfter, pid: target.pid) {
                    throw WindowObservationError.staleTarget
                }
                let contentBudget = budget.contentProjectionBudget()
                let windowTree = metrics.measure(.ax) { AXNodeReader.read(
                    root: subtree ?? serializationRoot,
                    windowBounds: observationBounds,
                    maximumDepth: subtree != nil ? maximumAXSubtreeDepth : focusedObservationMaximumDepth(
                        preference: focusedRootPreference,
                        rootSubrole: AXNodeReader.stringAttribute(
                            serializationRoot,
                            kAXSubroleAttribute
                        ).value,
                        rootIsExpectedWindow: CFEqual(serializationRoot, expectedAfter)
                    ),
                    recoverFocusedBranch: subtree == nil && CFEqual(serializationRoot, expectedAfter),
                    budget: contentBudget
                ) }
                try budget.check()
                let menuBarTree: AXNode? = {
                    let restoreContent = contentBudget.install()
                    defer { restoreContent() }
                    return (subtree == nil ? self.applicationMenuBar(
                        in: appElement,
                        expectedPID: target.pid
                    ) : nil).map {
                        AXNodeReader.read(
                            root: $0,
                            windowBounds: observationBounds,
                            maximumDepth: applicationMenuBarMaximumDepth,
                            budget: contentBudget
                        )
                    }
                }()
                let observationTree = subtree != nil ? windowTree : targetApplicationObservationTree(
                    window: windowTree,
                    menuBar: menuBarTree
                )
                try backgroundSession?.verifyFrontmost(
                    liveFrontmostPID()
                )
                let tree = AXSerializer.serialize(
                    observationTree,
                    snapshotID: snapshotID
                )
                let defaultButton = trustedDefaultButtonDisclosure(
                    window: expectedAfter,
                    tree: tree
                )
                let detail: AXTextDetailEnvelope?
                switch textDetail {
                case .off:
                    detail = nil
                case .on:
                    detail = try metrics.measure(.detail) {
                        let restoreContent = contentBudget.install()
                        defer { restoreContent() }
                        return try AXTextDetailSerializer.serialize(
                            root: subtree ?? serializationRoot,
                            snapshotID: snapshotID
                        )
                    }
                }
                metrics.recordContentBudget(contentBudget)
                try budget.check()
                if let subtree, !axSubtreeBelongsToWindow(subtree, window: expectedAfter, pid: target.pid) {
                    throw WindowObservationError.staleTarget
                }
                return (tree: tree, detail: detail, defaultButton: defaultButton)
            } catch let error as WindowObservationError {
                if !budget.available { throw WindowObservationError.observationTimedOut }
                throw error
            } catch {
                if !budget.available { throw WindowObservationError.observationTimedOut }
                throw WindowObservationError.axSerializationFailed
            }
        }

        let completed = try completeSnapshotCaptureTransaction(
            capturePNG: capturePNG,
            serializeAX: serializeAX,
            buildSnapshot: { _, prepared in
                var payload: [String: JSONValue] = [
                    "image_artifact": .string(artifactName),
                    "logical_size": .object([
                        "width": .number(Double(captureBounds.width)),
                        "height": .number(Double(captureBounds.height)),
                    ]),
                    "pixel_size": .object([
                        "width": .number(Double(pixelSize.width)),
                        "height": .number(Double(pixelSize.height)),
                    ]),
                    "backing_scale": .number(Double(backingScale)),
                    "capture_bounds": CGRectJSON.encode(captureBounds),
                    "ax_tree": snapshotAXTreeJSON(prepared.tree, subtree: subtree != nil),
                    "has_default_button": .bool(prepared.defaultButton.hasDefaultButton),
                ]
                if let elementRef = prepared.defaultButton.elementRef {
                    payload["default_button_element_ref"] = .string(elementRef)
                }
                // A waived suggestion list is its own window, so this capture does not show it.
                let suggestionPopups = liveSuggestionPopupRegions(
                    pid: target.pid, windowID: target.windowID, root: expectedAXWindow
                ).prefix(maximumReportedSuggestionPopups)
                if !suggestionPopups.isEmpty {
                    payload["suggestion_popups"] = .array(suggestionPopups.map {
                        CGRectJSON.encode(CGRect(x: $0.minX - window.frame.minX, y: $0.minY - window.frame.minY,
                                                 width: $0.width, height: $0.height))
                    })
                }
                if case let .on(detailName) = textDetail,
                   let detail = prepared.detail
                {
                    payload["text_detail_artifact"] = .string(detailName)
                    payload["text_detail_metadata"] = smartSnapshotMetadata(
                        detail,
                        snapshotID: snapshotID
                    )
                }
                if let selectedDisplay {
                    payload["display_id"] = .number(
                        Double(selectedDisplay.descriptor.displayID)
                    )
                    payload["target_window_bounds"] = CGRectJSON.encode(CGRect(
                        x: window.frame.minX - selectedDisplay.descriptor.bounds.minX,
                        y: window.frame.minY - selectedDisplay.descriptor.bounds.minY,
                        width: window.frame.width,
                        height: window.frame.height
                    ))
                }
                payload.merge(
                    virtualCursorPresentationJSON(virtualCursor.presentation)
                ) { _, cursorValue in cursorValue }
                return .object([
                    "snapshot_id": .string(snapshotID),
                    "payload": .object(payload),
                ])
            },
            prepareCommit: prepareCommit,
            validateFinalIdentity: {
                let finalStart = ProcessInfo.processInfo.systemUptime
                defer { metrics.record(.finalValidation, startedAt: finalStart) }
                try budget.check()
                let finalShareable = try metrics.measure(.inventory) { try waitForShareableContent() }
                guard let finalWindow = matchingCurrentWindow(
                    for: target,
                    in: finalShareable.windows
                ) else {
                    throw WindowObservationError.targetGone
                }
                if let selectedDisplay {
                    let finalDisplay = try selectDisplayCapture(
                        targetBounds: finalWindow.frame,
                        candidates: finalShareable.displays.map { display in
                            DisplayCaptureCandidate(
                                displayID: display.displayID,
                                bounds: display.frame,
                                pixelSize: CGSize(width: display.width, height: display.height)
                            )
                        }
                    )
                    guard finalDisplay == selectedDisplay.descriptor else {
                        throw WindowObservationError.displayUnavailable
                    }
                }
                guard let expectedFinal = matchingAXWindow(in: appElement, target: target),
                      CFHash(expectedFinal) == CFHash(expectedAfter)
                else { throw WindowObservationError.targetGone }
                let focusedFinal: AXUIElement
                let focusedFinalBounds: CGRect
                if target.interactionMode == .background {
                    let state = try backgroundTargetController().snapshotTargetState(target)
                    guard state.axIdentity == CFHash(expectedFinal),
                          approximatelyEqual(state.bounds, finalWindow.frame),
                          let bounds = AXNodeReader.frameAttribute(expectedFinal)
                    else { throw WindowObservationError.targetGone }
                    focusedFinal = expectedFinal
                    focusedFinalBounds = bounds
                    try backgroundSession?.verifyFrontmost(
                        liveFrontmostPID()
                    )
                } else {
                    guard let focused = trustedFocusedAXRoot(
                        in: appElement,
                        expected: expectedFinal,
                        targetBounds: finalWindow.frame,
                        preference: focusedRootPreference
                    ),
                    let bounds = AXNodeReader.frameAttribute(focused),
                    trustedFocusedTransition(
                        expectedIdentity: CFHash(expectedFinal),
                        targetBounds: finalWindow.frame,
                        beforeIdentity: CFHash(focusedAfter),
                        beforeBounds: focusedAfterBounds,
                        afterIdentity: CFHash(focused),
                        afterBounds: bounds
                    ) else { throw WindowObservationError.targetNotFrontmost }
                    focusedFinal = focused
                    focusedFinalBounds = bounds
                }
                let finalIdentity = target.interactionMode == .background
                    ? CaptureIdentity(
                        pid: target.pid,
                        windowID: finalWindow.windowID,
                        bounds: finalWindow.frame,
                        axIdentity: CFHash(expectedFinal)
                    )
                    : makeCaptureIdentity(
                        frontmostPID: liveFrontmostPID(),
                        windowID: finalWindow.windowID,
                        bounds: finalWindow.frame,
                        axIdentity: CFHash(expectedFinal)
                    )
                try verifyCaptureIdentity(
                    target: targetIdentity,
                    before: afterIdentity,
                    after: finalIdentity
                )
                let keyboardFocus = target.interactionMode == .foregroundTakeover
                    ? currentKeyboardFocusObservation(
                        in: appElement,
                        expectedPID: target.pid,
                        containerBounds: focusedFinalBounds
                    ).authority
                    : nil
                guard keyboardFocus == keyboardFocusBefore else {
                    throw WindowObservationError.targetNotFrontmost
                }
                let actionGuard = scope == "target_window" ? ActionGuard(
                    pid: target.pid,
                    windowID: target.windowID,
                    bounds: finalWindow.frame,
                    axIdentity: CFHash(expectedFinal),
                    focusedAXIdentity: CFHash(focusedFinal),
                    focusedAXBounds: focusedFinalBounds,
                    focusedRootPreference: focusedRootPreference,
                    keyboardFocus: keyboardFocus,
                    snapshotID: snapshotID,
                    interactionMode: target.interactionMode
                ) : nil
                if backgroundSingletonCaptured {
                    guard CFEqual(expectedFinal, expectedAXWindow),
                          exactAXWindowHasPopupCaptureRole(expectedAXWindow) else {
                        logActionRejected(
                            "CAPTURE-SOURCE singleton_filtered=final_ax_role_rejected"
                                + " windowID=\(finalWindow.windowID)"
                        )
                        throw WindowObservationError.targetGone
                    }
                    guard let backgroundSession else { throw WindowObservationError.targetGone }
                    try backgroundSession.verifyFrontmost(liveFrontmostPID())
                }
                return FinalSnapshotPublicationAuthority(actionGuard: actionGuard)
            },
            publish: { png, prepared in
                try budget.check()
                do {
                    try artifactSession.publish(
                        png: png,
                        named: artifactName,
                        textDetail: textDetail,
                        detail: prepared.detail
                    )
                } catch ArtifactBundleError.quotaExceeded {
                    throw WindowObservationError.artifactQuotaExceeded
                } catch ArtifactBundleError.poisoned, ArtifactBundleError.closed {
                    throw WindowObservationError.artifactPublisherFailed
                } catch let error as ArtifactPublicationError where error.finalVisible || error.publisherPoisoned || error.stage == .directorySync {
                    throw WindowObservationError.capturePublicationUncertain
                } catch is ArtifactPublicationError {
                    throw WindowObservationError.invalidCapturePath
                } catch is ArtifactBundleError {
                    throw WindowObservationError.invalidCapturePath
                } catch is CapturePathError {
                    throw WindowObservationError.invalidCapturePath
                }
            },
            register: { authority, prepared in
                snapshotReferences.register(
                    snapshotID: snapshotID,
                    references: prepared.tree.references(),
                    indexedReferences: prepared.tree.indexReferences(),
                    actionGuard: authority.actionGuard,
                    target: target
                )
            }
        )
        return completed
    }

    func resolveCatalogTarget(
        catalogGeneration requestedGeneration: Int,
        appRef: String,
        windowRef: String,
        current: (WindowTarget) throws -> WindowTarget?
    ) throws -> WindowTarget {
        guard requestedGeneration == catalogGeneration,
              let target = targets[windowRef],
              target.appRef == appRef
        else { throw WindowObservationError.staleTarget }
        guard let currentTarget = try current(target) else {
            throw WindowObservationError.targetGone
        }
        guard catalogTargetsMatch(target, currentTarget) else {
            logActionRejected("observation_validation=catalog_target_mismatch")
            throw WindowObservationError.staleTarget
        }
        return currentTarget
    }

    private func resolve(appRef: String, windowRef: String) throws -> WindowTarget {
        guard let target = targets[windowRef], target.appRef == appRef else { throw WindowObservationError.targetGone }
        return target
    }

    // Routes plan/act guard reasons into the shared diagnostics channel.
    private func planDiag(_ line: String) {
        logActionRejected(line)
    }

    private func backgroundTargetController() -> BackgroundTargetController {
        BackgroundTargetController(
            catalog: ClosureTargetCatalog(
                recordProvider: { [weak self] appRef, windowRef in
                    guard let self else { return nil }
                    let target = try self.resolve(appRef: appRef, windowRef: windowRef)
                    return try self.currentCatalogRecord(for: target)
                },
                currentProvider: { [weak self] target in
                    guard let self else { return nil }
                    return try self.currentCatalogRecord(for: target)
                }
            ),
            activation: activation
        )
    }

    private func currentCatalogRecord(for target: WindowTarget) throws -> TargetCatalogRecord? {
        let content = try waitForShareableContent()
        guard let current = matchingCurrentWindow(for: target, in: content.windows) else {
            return nil
        }
        guard let visibleWindows = systemVisibleWindowInventory() else {
            logActionRejected("observation_validation=visual_inventory_unreadable")
            throw WindowObservationError.axSerializationFailed
        }
        let app = AXUIElementCreateApplication(target.pid)
        guard let elements = completeObservedAXWindows(app) else {
            logActionRejected("observation_validation=ax_inventory_unreadable")
            throw WindowObservationError.axSerializationFailed
        }
        let screenWindows = content.windows.map {
            PIDScreenCaptureWindowObservation(pid: $0.owningApplication?.processID,
                windowID: $0.windowID, bounds: $0.frame, title: $0.title, isOnScreen: $0.isOnScreen)
        }
        let mapped: [TargetAXWindowRecord]? = mapCompleteAXElements(elements) { element in
            guard observedAXPID(element) == target.pid,
                  let bounds = AXNodeReader.frameAttribute(element) else { return nil }
            let title = accessibilityWindowName(element)
            let role = AXNodeReader.stringAttribute(element, kAXRoleAttribute)
            let subrole = AXNodeReader.stringAttribute(element, kAXSubroleAttribute)
            let windowID = matchingScreenWindow(pid: target.pid, bounds: bounds, title: title,
                windowID: observedAXWindowID(element), windows: screenWindows)?.windowID
            if windowID == nil {
                let sameBounds = content.windows.filter {
                    $0.owningApplication?.processID == target.pid &&
                    screenCaptureBoundsMatchAXBounds(screenCapture: $0.frame, accessibility: bounds)
                }
                logActionRejected("ax_sibling_mapping bounds_match=\(!sameBounds.isEmpty) visible_bounds_match=\(sameBounds.contains { $0.isOnScreen })")
            }
            let visualMatches = visibleWindows.filter {
                $0.pid == target.pid && $0.windowID == windowID
            }
            let visual = visualMatches.count == 1 ? visualMatches[0] : nil
            return TargetAXWindowRecord(
                windowID: windowID,
                bounds: bounds,
                identity: CFHash(element),
                element: element,
                role: role,
                subrole: subrole,
                zOrder: visual?.zOrder,
                layer: visual?.layer,
                alpha: visual?.alpha,
                isModal: AXNodeReader.boolAttribute(element, kAXModalAttribute),
                isSharingIndicator: windowSharingIndicator(element, bounds: bounds)
            )
        }
        guard let axWindows = mapped else {
            logActionRejected("observation_validation=ax_window_mapping_incomplete")
            throw WindowObservationError.axSerializationFailed
        }
        let selectedAXWindows = axWindows.filter {
            $0.windowID == current.windowID && screenCaptureBoundsMatchAXBounds(
                screenCapture: current.frame,
                accessibility: $0.bounds
            )
        }
        let axOverlay: ContainedAXOverlayScan = selectedAXWindows.count == 1
            ? selectedAXWindows[0].element.map { containedAXOverlay(in: $0, targetBounds: current.frame) } ?? .uncertain
            : .clear
        let containsAXOverlay = axOverlay != .clear
        let indicatorIDs = Set(axWindows.compactMap { candidate in
            candidate.isSharingIndicator && sharingIndicatorWithinTitlebar(candidate.bounds, targetBounds: current.frame)
                ? candidate.windowID : nil
        })
        let helpTagIDs = Set(axWindows.compactMap { candidate in
            axHelpTagRole(candidate.role) ? candidate.windowID : nil
        })
        let helpTags = visibleWindows.filter { $0.pid == target.pid && helpTagIDs.contains($0.windowID) }
        let withoutIndicators = visibleWindows.filter {
            !indicatorIDs.contains($0.windowID) && !($0.pid == target.pid && helpTagIDs.contains($0.windowID))
        }
        let focusedField = selectedAXWindows.count == 1 &&
            mayHaveSuggestionPopup(withoutIndicators, targetPID: target.pid, targetWindowID: current.windowID)
            ? selectedAXWindows[0].element.flatMap {
                focusedTextFieldFrame(app: app, root: $0, pid: target.pid, targetBounds: current.frame)
            } : nil
        let overlayCandidates = overlayCandidateRecords(withoutIndicators, targetPID: target.pid,
            targetWindowID: current.windowID, targetBounds: current.frame, focusedField: focusedField) ?? withoutIndicators
        let suggestionPopups = attachedSuggestionPopupRecords(withoutIndicators, targetPID: target.pid,
            targetWindowID: current.windowID, targetBounds: current.frame, focusedField: focusedField)
        let observedAt = ProcessInfo.processInfo.systemUptime
        recentSuggestionPopups.record(suggestionPopups, at: observedAt)
        func placement(_ records: [VisibleWindowRecord]) -> String {
            records.map {
                "[dx=\(Int($0.bounds.minX - current.frame.minX)) dy=\(Int($0.bounds.minY - current.frame.minY)) " +
                "w=\(Int($0.bounds.width)) h=\(Int($0.bounds.height))]"
            }.joined(separator: " ")
        }
        let strips = appStatusStripRecords(withoutIndicators, targetPID: target.pid,
            targetWindowID: current.windowID, targetBounds: current.frame)
        if !strips.isEmpty { logActionRejected("OVERLAY-PASSIVE status_strip \(placement(strips))") }
        if !helpTags.isEmpty { logActionRejected("OVERLAY-PASSIVE help_tag \(placement(helpTags))") }
        if !suggestionPopups.isEmpty {
            logActionRejected("OVERLAY-PASSIVE suggestion_popup \(placement(suggestionPopups))")
        }
        let containsVisibleOverlay = containedVisibleOverlayOrUncertain(
            targetPID: target.pid,
            targetWindowID: current.windowID,
            targetBounds: current.frame,
            records: overlayCandidates
        )
        let visibleOffenders = containsVisibleOverlay
            ? backgroundVisibleWindowAmbiguities(targetPID: target.pid, targetWindowID: current.windowID,
                targetBounds: current.frame, records: overlayCandidates)
            : []
        let overlayMayBeTransient = overlaysMayBeTransient(visibleOffenders, axOverlay: axOverlay) {
            self.recentSuggestionPopups.contains($0, at: observedAt)
        }
        if containsAXOverlay || containsVisibleOverlay {
            // Geometry and ordering only, never titles or content: enough to tell a tooltip or
            // hover card from a sheet, menu or dialog in live evidence.
            let selected = overlayCandidates.first { $0.pid == target.pid && $0.windowID == current.windowID }
            let offenders = visibleOffenders.map {
                "[layer=\($0.layer) z=\($0.zOrder) alpha=\($0.alpha) dx=\(Int($0.bounds.minX - current.frame.minX)) " +
                "dy=\(Int($0.bounds.minY - current.frame.minY)) w=\(Int($0.bounds.width)) h=\(Int($0.bounds.height))]"
            }
            logActionRejected("OVERLAY-DETAIL ax=\(axOverlay.rawValue) visible=\(containsVisibleOverlay) " +
                "transient=\(overlayMayBeTransient) " +
                "selectedLayer=\(selected.map { String($0.layer) } ?? "?") selectedZ=\(selected.map { String($0.zOrder) } ?? "?") " +
                offenders.joined(separator: " "))
        }
        let siblingOrdering = backgroundSiblingOrderingProof(
            targetPID: target.pid, targetWindowID: current.windowID, targetBounds: current.frame,
            axWindows: axWindows,
            screenWindows: screenWindows,
            visibleWindows: visibleWindows
        )
        if siblingOrdering != nil {
            logActionRejected("observation_validation=background_sibling_ordering_proven")
        }
        return TargetCatalogRecord(
            appRef: target.appRef,
            windowRef: target.windowRef,
            pid: target.pid,
            windowID: current.windowID,
            bounds: current.frame,
            title: current.title ?? target.title,
            axWindows: axWindows,
            containsUnselectedOverlay: containsAXOverlay || containsVisibleOverlay,
            overlayMayBeTransient: overlayMayBeTransient,
            siblingOrdering: siblingOrdering,
            suggestionPopupWindowIDs: Set(suggestionPopups.map(\.windowID))
        )
    }

    // Keep raw window inventories for pointer hit testing. Only modal/overlay
    // classification excludes the independently identified macOS titlebar badge.
    private func interactionWindowRecords(for target: WindowTarget) -> [VisibleWindowRecord] {
        let records = systemVisibleWindowRecords()
        let app = AXUIElementCreateApplication(target.pid)
        let windows = completeObservedAXWindows(app) ?? []
        let indicatorBounds = windows.compactMap { element -> CGRect? in
            guard let bounds = AXNodeReader.frameAttribute(element),
                  sharingIndicatorWithinTitlebar(bounds, targetBounds: target.bounds),
                  windowSharingIndicator(element, bounds: bounds) else { return nil }
            return bounds
        }
        let helpTagBounds = axHelpTagFrames(windows)
        let withoutIndicators = records.filter { record in
            record.pid != target.pid || record.windowID == target.windowID ||
                (!indicatorBounds.contains(record.bounds) && !matchesAnyFrame(record.bounds, helpTagBounds))
        }
        let focusedField = mayHaveSuggestionPopup(withoutIndicators, targetPID: target.pid,
                                                  targetWindowID: target.windowID) ? target.axElement.flatMap {
            focusedTextFieldFrame(app: app, root: $0, pid: target.pid, targetBounds: target.bounds)
        } : nil
        return overlayCandidateRecords(withoutIndicators, targetPID: target.pid, targetWindowID: target.windowID,
                                       targetBounds: target.bounds, focusedField: focusedField) ?? withoutIndicators
    }

    func planActions(
        snapshotID: String,
        interactionMode: InteractionMode,
        actions: [NativeAction]
    ) -> CooperativePlanResult {
        planActions(
            snapshotID: snapshotID,
            interactionMode: interactionMode,
            actions: actions,
            fragment: nil
        )
    }

    func planActions(
        snapshotID: String,
        interactionMode: InteractionMode,
        actions: [NativeAction],
        fragment: ForegroundFragmentPlanRequest?
    ) -> CooperativePlanResult {
        actionLock.lock()
        defer { actionLock.unlock() }
        let planTime = cooperativePlanClock()
        pruneCooperativeFragmentDraftsLocked(now: planTime)
        guard fragment == nil || interactionMode == .foregroundTakeover,
              fragment.map({ $0.authority.inputSnapshotID == snapshotID }) ?? true
        else {
            return CooperativePlanResult(summary: nil, error: .action(.staleSnapshot))
        }
        guard let snapshotContext = snapshotReferences.contextForPlanning(snapshotID: snapshotID) else {
            planDiag("PLAN-GUARD no-context snapshotID=\(snapshotID) mode=\(interactionMode.rawValue)")
            return CooperativePlanResult(summary: nil, error: .action(.staleSnapshot))
        }
        guard let target = snapshotContext.target else {
            planDiag("PLAN-GUARD no-target snapshotID=\(snapshotID)")
            return CooperativePlanResult(summary: nil, error: .action(.staleSnapshot))
        }
        guard (snapshotContext.guardValue.interactionMode == interactionMode ||
                  (snapshotContext.guardValue.interactionMode == .background && interactionMode == .foregroundTakeover)),
              (target.interactionMode == interactionMode ||
                  (target.interactionMode == .background && interactionMode == .foregroundTakeover))
        else {
            planDiag("PLAN-GUARD mode-mismatch guard=\(snapshotContext.guardValue.interactionMode.rawValue) target=\(target.interactionMode.rawValue) request=\(interactionMode.rawValue)")
            return CooperativePlanResult(summary: nil, error: .action(.staleSnapshot))
        }
        guard permissions.accessibilityTrusted else {
            return CooperativePlanResult(summary: nil, error: .action(.permissionDenied))
        }
        let performer = makeActionPerformer(target: target, snapshotContext: snapshotContext)
        let dispatcher = InputDispatcher(
            performer: performer,
            application: pidTargetApplication(pid: target.pid),
            syntheticPolicy: SyntheticInputPlanningPolicy(
                pointerCapability: pidPointerCapability,
                keyboardCapability: foregroundKeyboardCapability,
                registry: pidCompatibility,
                backgroundDeliveryEnabled: pidCompatibility.hasPointerClaims,
                genericForegroundEnabled: interactionMode == .foregroundTakeover
            ),
            fragmentDraftClock: cooperativePlanClock
        )
        do {
            let sourceGuard = snapshotContext.guardValue
            let modeGuard = ActionGuard(
                pid: sourceGuard.pid,
                windowID: sourceGuard.windowID,
                bounds: sourceGuard.bounds,
                axIdentity: sourceGuard.axIdentity,
                focusedAXIdentity: sourceGuard.focusedAXIdentity,
                focusedAXBounds: sourceGuard.focusedAXBounds,
                focusedRootPreference: sourceGuard.focusedRootPreference,
                keyboardFocus: planKeyboardFocusAuthority(
                    source: sourceGuard.keyboardFocus,
                    interactionMode: interactionMode,
                    liveFocus: { [weak self] in
                        guard let self else { return .secureOrIndeterminate }
                        return self.currentKeyboardFocusObservation(
                            in: AXUIElementCreateApplication(target.pid),
                            expectedPID: target.pid,
                            containerBounds: sourceGuard.bounds
                        )
                    }
                ),
                snapshotID: sourceGuard.snapshotID,
                interactionMode: interactionMode
            )
            let context = DispatchContext(guardValue: modeGuard)
            let resolvedActions: [NativeAction]
            do {
                resolvedActions = try resolveElementIndexes(actions, context: snapshotContext)
            } catch {
                planDiag("PLAN-THROW resolveElementIndexes: \(error)")
                return CooperativePlanResult(summary: nil, error: .action(.staleSnapshot))
            }
            let plan: DispatchPlan
            do {
                plan = try fragment == nil
                    ? dispatcher.plan(actions: resolvedActions, context: context)
                    : dispatcher.planFragmentDraft(actions: resolvedActions, context: context)
            } catch {
                planDiag("PLAN-THROW dispatcher.plan: \(error)")
                // Preserve typed refusal (for example a clipped pointer region)
                // instead of recommending a pointless stale-snapshot refresh.
                throw error
            }
            if plan.cooperativeError == nil {
                if fragment != nil {
                    reserveCooperativeFragmentDraftCapacityLocked()
                }
                let sequence = nextCooperativePlanSequence
                nextCooperativePlanSequence &+= 1
                cooperativePlans[plan.planRef] = StoredCooperativePlan(
                    dispatcher: dispatcher,
                    plan: plan,
                    context: context,
                    actions: actions,
                    snapshotContext: snapshotContext,
                    fragmentRequest: fragment,
                    fragmentDraftExpiresAt: fragment == nil
                        ? nil
                        : planTime + InputDispatcher.fragmentDraftLifetime,
                    sequence: sequence
                )
                if case let .continuing(takeoverRef, authority)? = fragment {
                    do {
                        try takeoverCoordinator.bindFragmentStage(
                            takeoverRef,
                            snapshotID: snapshotID,
                            planRef: plan.planRef,
                            stage: authority
                        )
                    } catch {
                        dispatcher.invalidatePlans()
                        return CooperativePlanResult(summary: nil, error: .action(.staleSnapshot))
                    }
                }
            }
            return CooperativePlanResult(
                summary: plan.summary,
                error: plan.cooperativeError.map(NativeCooperativeError.cooperative),
                fragmentBinding: fragment?.authority
            )
        } catch {
            return CooperativePlanResult(summary: nil, error: .action(windowActionError(error)))
        }
    }

    func takeoverBegin(snapshotID: String, planRef: String) throws -> String {
        actionLock.lock()
        defer { actionLock.unlock() }
        pruneCooperativeFragmentDraftsLocked(now: cooperativePlanClock())
        guard let stored = cooperativePlans[planRef],
              let target = stored.snapshotContext.target
        else {
            logActionRejected(
                "TAKEOVER-BEGIN stalePlan planRef=\(planRef) stored=\(cooperativePlans[planRef] != nil) storedKeys=\(cooperativePlans.keys.count) snapshotID=\(snapshotID)"
            )
            throw TakeoverError.stalePlan
        }
        let token = try takeoverCoordinator.begin(
            target: target,
            snapshotID: snapshotID,
            planRef: planRef,
            stageDigest: stored.plan.stageDigest
        )
        return token.ref
    }

    func takeoverBegin(
        snapshotID: String,
        planRef: String,
        declaration: ForegroundFragmentDeclaration
    ) throws -> String {
        actionLock.lock()
        defer { actionLock.unlock() }
        pruneCooperativeFragmentDraftsLocked(now: cooperativePlanClock())
        guard let stored = cooperativePlans[planRef],
              let target = stored.snapshotContext.target,
              case .initial? = stored.fragmentRequest
        else { throw TakeoverError.stalePlan }
        return try takeoverCoordinator.beginFragment(
            target: target,
            snapshotID: snapshotID,
            planRef: planRef,
            declaration: declaration
        ).ref
    }

    func cooperativeAct(
        snapshotID: String,
        interactionMode: InteractionMode,
        planRef: String,
        takeoverRef: String?,
        actions: [NativeAction]
    ) -> CooperativeActionResult {
        cooperativeAct(
            snapshotID: snapshotID,
            interactionMode: interactionMode,
            planRef: planRef,
            takeoverRef: takeoverRef,
            actions: actions,
            fragmentStage: nil
        )
    }

    func cooperativeAct(
        snapshotID: String,
        interactionMode: InteractionMode,
        planRef: String,
        takeoverRef: String?,
        actions: [NativeAction],
        fragmentStage: FragmentStageAuthority?
    ) -> CooperativeActionResult {
        actionLock.lock()
        defer { actionLock.unlock() }
        pruneCooperativeFragmentDraftsLocked(now: cooperativePlanClock())
        func staleAt(_ why: String) -> CooperativeActionResult {
            logActionRejected("ACT-GUARD \(why)")
            return CooperativeActionResult(
                batch: ActionBatchResult(outcomes: [], lastAcknowledgedAction: -1, error: nil),
                error: .action(.staleSnapshot)
            )
        }
        if interactionMode == .foregroundTakeover {
            return foregroundCooperativeAct(
                snapshotID: snapshotID,
                planRef: planRef,
                takeoverRef: takeoverRef,
                actions: actions,
                fragmentStage: fragmentStage
            )
        }
        guard takeoverRef == nil, fragmentStage == nil else {
            return staleAt("h takeover-ref-in-background-act")
        }
        guard let planningContext = snapshotReferences.contextForPlanning(snapshotID: snapshotID) else {
            return staleAt("i planning-context-gone snapshotID=\(snapshotID)")
        }
        let resolvedActions: [NativeAction]
        do {
            resolvedActions = try resolveElementIndexes(actions, context: planningContext)
        } catch {
            logActionRejected("ACT-GUARD j element-reindex-failed error=\(error)")
            return staleAt("j element-reindex-failed")
        }
        guard let stored = cooperativePlans.removeValue(forKey: planRef) else {
            return staleAt("a no-plan-record planRef=\(planRef)")
        }
        guard stored.plan.interactionMode == interactionMode else {
            return staleAt("b mode-mismatch")
        }
        guard stored.plan.matches(actions: resolvedActions) else {
            return staleAt("c plan-actions-mismatch")
        }
        guard stored.actions == actions else {
            return staleAt("d raw-actions-mismatch")
        }
        guard stored.context.guardValue.snapshotID == snapshotID else {
            return staleAt("e snapshot-id-mismatch")
        }
        guard let consumedSnapshot = snapshotReferences.takeForActions(snapshotID: snapshotID) else {
            return staleAt("f snapshot-not-retained")
        }
        guard consumedSnapshot.guardValue == stored.context.guardValue else {
            return staleAt("g guard-value-drift")
        }
        do {
            if stored.plan.requiresTakeover == false,
               stored.plan.backends.contains(.pidPointer) || stored.plan.backends.contains(.pidKeyboard) {
                let batch = try executeBackgroundDelivery(
                    stored: stored,
                    resolvedActions: resolvedActions
                )
                return CooperativeActionResult(
                    batch: batch,
                    error: batch.error.map(NativeCooperativeError.action)
                )
            }
            let batch = try stored.dispatcher.execute(stored.plan, context: stored.context)
            return CooperativeActionResult(
                batch: batch,
                error: batch.error.map(NativeCooperativeError.action)
            )
        } catch InputDispatchError.backgroundActionUnsupported {
            return CooperativeActionResult(
                batch: ActionBatchResult(outcomes: [], lastAcknowledgedAction: -1, error: nil),
                error: .cooperative(.backgroundActionUnsupported)
            )
        } catch {
            logActionRejected("BG-DELIVERY-CATCH-all mapped=\(windowActionError(error)) raw=\(error)")
            return CooperativeActionResult(
                batch: ActionBatchResult(outcomes: [], lastAcknowledgedAction: -1, error: nil),
                error: .action(windowActionError(error))
            )
        }
    }

    private func resolveElementIndexes(
        _ actions: [NativeAction],
        context: SnapshotActionContext
    ) throws -> [NativeAction] {
        try actions.map { action in
            guard let index = action.elementIndex else { return action }
            guard let reference = context.elementReference(index: index) else {
                throw ActionExecutionError.staleSnapshot
            }
            return action.resolvingElementReference(reference)
        }
    }

    private func executeBackgroundDelivery(
        stored: StoredCooperativePlan,
        resolvedActions: [NativeAction]
    ) throws -> ActionBatchResult {
        guard let target = stored.snapshotContext.target else {
            logActionRejected("BG-DELIVERY target-nil")
            throw ActionExecutionError.staleSnapshot
        }
        let performer = makeActionPerformer(target: target, snapshotContext: stored.snapshotContext)
        let validator = BackgroundPIDActionGuardValidator(state: { [weak self] in
            guard let self else { throw ActionExecutionError.targetGone }
            let state = try self.backgroundTargetController().snapshotTargetState(target)
            return PIDActionTargetState(
                target: state,
                snapshotID: stored.context.guardValue.snapshotID,
                isFrontmost: false,
                isKeyWindow: false
            )
        }, pointerExclusions: {
            livePassiveOverlayRegions(pid: target.pid, windowID: target.windowID, root: target.axElement)
        })
        let executor = PIDTargetedActionExecutor(
            poster: CGPIDTargetedInputPoster(),
            compatibility: pidCompatibility,
            activity: userActivity,
            validator: validator,
            element: { [weak self] reference, requestedSnapshotID in
                guard requestedSnapshotID == stored.context.guardValue.snapshotID,
                      let snapshotElement = stored.snapshotContext.references[reference]
                else { return nil }
                return self!.currentActionElement(
                    snapshotElement: snapshotElement,
                    target: target,
                    windowBounds: stored.context.guardValue.bounds
                )
            },
            evidence: { _, expected in
                (try? validator.revalidate(expected: expected, point: nil)) != nil
            }
        )
        let application = pidTargetApplication(pid: target.pid)
        guard let application else {
            throw ActionExecutionError.targetGone
        }
        let marker = UInt64(bitPattern: Int64(Date().timeIntervalSinceReferenceDate * 1_000_000)) &+ 1
        let lease = try userActivity.arm(marker: marker, notification: UserActivityPauseSignal())
        defer {
            (try? userActivity.disarm(lease: lease))
        }
        let entries = try stored.dispatcher.consumeBackgroundPlan(
            stored.plan,
            resolvedActions: resolvedActions
        )
        let prepared = try executor.preflight(
            expected: stored.context.guardValue,
            application: application,
            marker: lease.marker,
            entries: entries,
            allowBackgroundDelivery: true
        )
        var outcomes: [ActionOutcome] = []
        var acknowledged = -1
        for sourceIndex in prepared.sourceIndexes {
            let result = executor.executePrepared(
                sourceIndex: sourceIndex,
                from: prepared,
                expected: stored.context.guardValue,
                lease: lease
            )
            outcomes.append(contentsOf: result.outcomes)
            acknowledged = result.lastAcknowledgedAction
            if let error = result.error {
                return ActionBatchResult(
                    outcomes: outcomes,
                    lastAcknowledgedAction: acknowledged,
                    error: error
                )
            }
        }
        return ActionBatchResult(
            outcomes: outcomes,
            lastAcknowledgedAction: acknowledged,
            error: nil
        )
    }

    func fragmentStageCommit(takeoverRef: String, commit: FragmentStageCommit) throws -> FragmentStageCommitOutcome {
        actionLock.lock()
        defer { actionLock.unlock() }
        guard let observation = snapshotReferences.contextForPlanning(snapshotID: commit.freshSnapshotID) else {
            takeoverCoordinator.cancelFragment(takeoverRef)
            throw TakeoverError.authorityMismatch
        }
        return try takeoverCoordinator.commitFragmentStage(
            takeoverRef,
            commit: commit,
            observedTarget: observation.target,
            observedGuard: observation.guardValue
        )
    }

    func takeoverEnd(takeoverRef: String) throws -> TakeoverOutcome {
        try takeoverEnd(takeoverRef: takeoverRef, restorePreviousFocus: true)
    }

    func takeoverEnd(takeoverRef: String, restorePreviousFocus: Bool) throws -> TakeoverOutcome {
        actionLock.lock()
        defer { actionLock.unlock() }
        return try takeoverCoordinator.end(takeoverRef, allowRestore: restorePreviousFocus)
    }

    func invalidateSnapshots() {
        invalidateCooperativeState(.close)
    }

    private func pruneCooperativeFragmentDraftsLocked(now: TimeInterval) {
        let expired = cooperativePlans.compactMap { reference, stored in
            stored.fragmentDraftExpiresAt.map { $0 < now ? reference : nil } ?? nil
        }
        expired.forEach { reference in
            cooperativePlans.removeValue(forKey: reference)?.dispatcher.invalidatePlans()
        }
    }

    private func reserveCooperativeFragmentDraftCapacityLocked() {
        while cooperativePlans.values.lazy.filter({ $0.fragmentDraftExpiresAt != nil }).count >=
            InputDispatcher.maximumFragmentDrafts,
            let oldest = cooperativePlans
                .filter({ $0.value.fragmentDraftExpiresAt != nil })
                .min(by: { $0.value.sequence < $1.value.sequence })?.key
        {
            cooperativePlans.removeValue(forKey: oldest)?.dispatcher.invalidatePlans()
        }
    }

    private func makeActionPerformer(
        target: WindowTarget,
        snapshotContext: SnapshotActionContext
    ) -> SystemActionPerformer {
        SystemActionPerformer(
            state: { [weak self] in
                guard let self else { throw ActionExecutionError.helperFailed }
                // 选控制器必须看**本次动作**的模式，而不是快照记录的模式：快照永远在
                // background 下捕获，guardValue.interactionMode 恒为 .background，用它判断
                // 会把 foreground takeover 也送进后台专用的 snapshotTargetState —— 那里的
                // `target.interactionMode == .background` guard 随即失败并抛出 targetGone，
                // 再被下面洗成 stale_snapshot，使整条 takeover 坐标点击路径不可用。
                if target.interactionMode == .background {
                    do { return try self.backgroundTargetController().snapshotTargetState(target) }
                    catch let error as WindowObservationError {
                        logActionRejected("TARGET-STATE-OBS snapshotID=\(snapshotContext.guardValue.snapshotID) obsError=\(error)")
                        throw ActionExecutionError.staleSnapshot
                    }
                    catch { throw ActionExecutionError.helperFailed }
                }
                return try self.currentActionState(
                    for: target,
                    expectedFocusedRootPreference: snapshotContext.guardValue.focusedRootPreference
                )
            },
            lookup: { [weak self] reference, requestedSnapshotID in
                guard requestedSnapshotID == snapshotContext.guardValue.snapshotID,
                      let snapshotElement = snapshotContext.references[reference]
                else { return nil }
                return self?.currentActionElement(
                    snapshotElement: snapshotElement,
                    target: target,
                    windowBounds: snapshotContext.guardValue.bounds
                )
            },
            pointerLookup: { [weak self] reference, requestedSnapshotID in
                guard requestedSnapshotID == snapshotContext.guardValue.snapshotID,
                      let snapshotElement = snapshotContext.references[reference]
                else { return nil }
                return self?.currentActionElement(
                    snapshotElement: snapshotElement,
                    target: target,
                    windowBounds: snapshotContext.guardValue.bounds,
                    requiresExactBounds: true
                )
            },
            scrollPressLookup: { [weak self] reference, requestedSnapshotID, direction in
                guard let self,
                      requestedSnapshotID == snapshotContext.guardValue.snapshotID,
                      let selected = selectAXScrollPressReferenceTarget(
                          rootReference: reference,
                          direction: direction,
                          references: snapshotContext.references
                      ),
                      let ownerSnapshot = snapshotContext.references[selected.ownerReference],
                      let buttonSnapshot = snapshotContext.references[selected.buttonReference],
                      let owner = self.currentActionElement(
                          snapshotElement: ownerSnapshot,
                          target: target,
                          windowBounds: snapshotContext.guardValue.bounds
                      ),
                      let button = self.currentActionElement(
                          snapshotElement: buttonSnapshot,
                          target: target,
                          windowBounds: snapshotContext.guardValue.bounds
                      ),
                      let ownerElement = owner.element,
                      let buttonElement = button.element
                else { return nil }
                let (parentError, parentValue) = observationAXAttribute(buttonElement, kAXParentAttribute)
                guard parentError == .success,
                      let parent = decodeAXElement(parentValue),
                      CFEqual(parent, ownerElement)
                else { return nil }
                return AXScrollPressTarget(owner: owner, button: button)
            },
            selectedTextWriter: SystemAXSelectedTextWriter(effectProbe: .live),
            replacementTargetValidation: { [weak self] expected in
                guard let self, target.interactionMode == .foregroundTakeover,
                      let element = expected.element else { throw ActionExecutionError.staleSnapshot }
                let guardValue = snapshotContext.guardValue
                let live = try self.currentActionState(for: target,
                    expectedFocusedRootPreference: guardValue.focusedRootPreference)
                guard live.pid == guardValue.pid, live.windowID == guardValue.windowID,
                      live.axIdentity == guardValue.axIdentity, live.bounds == guardValue.bounds,
                      live.focusedAXIdentity == guardValue.focusedAXIdentity,
                      live.focusedAXBounds == guardValue.focusedAXBounds,
                      live.focusedRootPreference == guardValue.focusedRootPreference,
                      let root = self.catalogExactAXWindow(in: AXUIElementCreateApplication(target.pid), target: target),
                      CFHash(root) == guardValue.axIdentity,
                      let retained = snapshotContext.references.values.first(where: {
                          $0.element.map { CFEqual($0, element) } ?? false
                      }) else { throw ActionExecutionError.staleSnapshot }
                let current = self.currentActionElement(snapshotElement: retained, target: target,
                    windowBounds: guardValue.bounds, requiresExactBounds: true)
                try validateReplacementField(expected: expected, current: current,
                    belongs: { focusedElementBelongsToExactAXRoot(element: $0, root: root, pid: target.pid) })
            },
            // 键盘焦点获取能力在此**显式装配**（默认 nil ＝ 不获取）。有了它，`type` 才可能
            // 在目标未被点击聚焦时把焦点带过去，走纯 AX 写入而不是合成键盘事件。
            focusAcquisition: acquireKeyboardFocusViaAX,
            // 文本布局安全检测也在此**显式装配**：每次调用实时读 TIS、不缓存（输入法随时会切）。
            // 它守的是"输入法激活时绝不做 AX 文本写"这条不变量 —— 实测 unicode 形态免疫输入法，
            // 而真实按键码会被候选窗截走。see docs/macos-computer-use.md#input-delivery-contracts
            textInputSafety: { SystemBackgroundTextInputSafetyDetector().detect() },
            // Live Edge proved an accepted AXSelectedText write can leave the page unchanged:
            // web fields go to keyboard delivery, other writes are read back, and processes
            // that ignored one are remembered for this helper lifetime.
            webContentProbe: { axElementIsInsideWebArea($0) },
            axTextWriteMemory: .shared,
            keyboardOnlyTextProcess: { ChromiumProcessClassifier.shared.usesChromiumRenderer(pid: $0) }
        )
    }

    private func invalidateCooperativeState(_ reason: SnapshotInvalidationReason) {
        cooperativeStateInvalidationObserver?(reason)
        if cursorShouldHide(for: reason) { virtualCursor.hide() }
        actionLock.lock()
        defer { actionLock.unlock() }
        snapshotReferences.invalidate(reason)
        cooperativePlans.values.forEach { $0.dispatcher.invalidatePlans() }
        cooperativePlans.removeAll(keepingCapacity: false)
        if reason != .newSnapshot {
            takeoverCoordinator.invalidate(allowRestore: reason == .close)
        }
    }

    private func foregroundCooperativeAct(
        snapshotID: String,
        planRef: String,
        takeoverRef: String?,
        actions: [NativeAction],
        fragmentStage: FragmentStageAuthority?
    ) -> CooperativeActionResult {
        guard let takeoverRef else {
            planDiag("ACT-FAIL takeoverRef=nil snapshotID=\(snapshotID) planRef=\(planRef)")
            return CooperativeActionResult(
                batch: ActionBatchResult(outcomes: [], lastAcknowledgedAction: -1, error: nil),
                error: .action(.staleSnapshot)
            )
        }
        let execution: ForegroundTakeoverExecutionAuthority
        // 接管路径原来**一个字节都不落**就拒了：后台路径每道门都有 `ACT-GUARD a/b/c…`，
        // 而这里几个 throw 全被静默 catch 掉 —— 实机排查"计划合法(entries=1)却打不进字"时，
        // 只能靠猜（猜错两次）。用一个随阶段前移的变量给拒绝标上发生在哪道门。
        var takeoverStage = "consume"
        do {
            if let fragmentStage {
                takeoverStage = "consume-fragment"
                _ = try takeoverCoordinator.consumeFragment(
                    takeoverRef,
                    actions: actions,
                    stage: fragmentStage,
                    planRef: planRef
                )
            } else {
                _ = try takeoverCoordinator.consume(takeoverRef, actions: actions)
            }
            takeoverStage = "authority"
            let consumed = snapshotReferences.takeForActions(snapshotID: snapshotID)
            execution = try takeoverCoordinator.executionAuthority(
                for: takeoverRef,
                snapshotID: snapshotID,
                planRef: planRef,
                consumedGuard: consumed?.guardValue
            )
            takeoverStage = "consume-plan"
            guard let consumed,
                  let dispatcher = execution.planAuthority.dispatcher
            else { throw TakeoverError.authorityMismatch }
            let authority = execution.planAuthority
            let popupPoint = execution.popupPointerPoint
            if popupPoint != nil { virtualCursor.hide() }
            let popupRuntime = SystemApplicationActivationRuntime()
            let entries = try dispatcher.consumeForegroundPlan(
                authority.plan,
                authority: ForegroundPlanConsumptionAuthority(
                    planRef: planRef,
                    snapshotID: snapshotID,
                    interactionMode: .foregroundTakeover,
                    actions: actions,
                    backends: authority.plan.backends,
                    guardValue: authority.guardValue
                ),
                validateFocusMutation: {
                    try self.userActivity.assertNotPaused(lease: execution.lease)
                },
                popupPointerOnlyState: popupPoint.map { point in
                    { () throws -> ActionTargetState in
                        guard popupRuntime.popupPointerWindowMatches(authority.target, at: point) else {
                            throw ActionExecutionError.targetNotFrontmost
                        }
                        return popupPointerSealedState(authority.guardValue)
                    }
                }
            )
            let result = executeForegroundActions(
                authority: authority,
                plannedPopupPoint: popupPoint,
                lease: execution.lease,
                snapshotID: snapshotID,
                consumed: consumed,
                entries: entries,
                managesActivityFragment: fragmentStage == nil
            )
            if let fragmentStage {
                takeoverCoordinator.finishFragmentStage(
                    takeoverRef,
                    stage: fragmentStage,
                    planRef: planRef,
                    succeeded: result.error == nil && result.batch.error == nil
                )
                return CooperativeActionResult(
                    batch: result.batch,
                    error: result.error,
                    fragmentBinding: fragmentStage
                )
            }
            return result
        } catch is UserActivityMonitoringError {
            if fragmentStage != nil { takeoverCoordinator.cancelFragment(takeoverRef) }
            return CooperativeActionResult(
                batch: ActionBatchResult(outcomes: [], lastAcknowledgedAction: -1, error: nil),
                error: .cooperative(.userActivityPaused),
                fragmentBinding: fragmentStage
            )
        } catch InputDispatchError.backgroundActionUnsupported {
            planDiag(
                "ACT-FAIL foreground stage=\(takeoverStage) error=backgroundActionUnsupported"
                    + " planRef=\(planRef) snapshotID=\(snapshotID)"
            )
            if fragmentStage != nil { takeoverCoordinator.cancelFragment(takeoverRef) }
            return CooperativeActionResult(
                batch: ActionBatchResult(outcomes: [], lastAcknowledgedAction: -1, error: nil),
                error: .cooperative(.backgroundActionUnsupported),
                fragmentBinding: fragmentStage
            )
        } catch let error as ActionExecutionError {
            planDiag(
                "ACT-FAIL foreground stage=\(takeoverStage) error=\(String(describing: error))"
                    + " planRef=\(planRef) snapshotID=\(snapshotID)"
            )
            if fragmentStage != nil { takeoverCoordinator.cancelFragment(takeoverRef) }
            return CooperativeActionResult(
                batch: ActionBatchResult(outcomes: [], lastAcknowledgedAction: -1, error: nil),
                error: .action(error),
                fragmentBinding: fragmentStage
            )
        } catch {
            planDiag(
                "ACT-FAIL foreground stage=\(takeoverStage) unknown-error=\(error)"
                    + " planRef=\(planRef) snapshotID=\(snapshotID)"
            )
            if fragmentStage != nil { takeoverCoordinator.cancelFragment(takeoverRef) }
            return CooperativeActionResult(
                batch: ActionBatchResult(outcomes: [], lastAcknowledgedAction: -1, error: nil),
                error: .action(.staleSnapshot),
                fragmentBinding: fragmentStage
            )
        }
    }

    private func executeForegroundActions(
        authority: ForegroundTakeoverPlanAuthority,
        plannedPopupPoint: CGPoint?,
        lease: UserActivitySessionLease,
        snapshotID: String,
        consumed: SnapshotActionContext,
        entries: [PlannedDispatchEntry],
        managesActivityFragment: Bool = true
    ) -> CooperativeActionResult {
        let target = authority.target
        let performer = makeActionPerformer(target: target, snapshotContext: consumed)
        let strictValidator = ExactPIDActionGuardValidator(stateForDrag: { [weak self] displacement in
            guard let self else { throw ActionExecutionError.targetGone }
            if displacement != nil {
                return try self.currentCapturedDragTargetState(for: target, snapshotID: snapshotID)
            }
            return try self.currentForegroundPIDActionTargetState(
                for: target,
                snapshotID: snapshotID,
                expectedFocusedRootPreference: authority.guardValue.focusedRootPreference
            )
        })
        let popupPoint: CGPoint? = {
            guard let plannedPopupPoint,
                  popupPointerClickEntriesMatch(
                      plan: authority.plan, actions: authority.actions,
                      expected: authority.guardValue, entries: entries
                  )
            else { return nil }
            return plannedPopupPoint
        }()
        let popupRuntime = SystemApplicationActivationRuntime()
        let validator: any PIDActionGuardValidating
        if let popupPoint {
            validator = PopupPointerClickGuardValidator(
                sealed: authority.guardValue,
                authorizedPoint: popupPoint,
                popupProof: { popupRuntime.popupPointerWindowMatches(target, at: popupPoint) }
            )
        } else {
            validator = strictValidator
        }
        virtualCursor.hide()
        let executor = PIDTargetedActionExecutor(
            poster: CGForegroundInputPoster(pointIsInTargetWindow: { point, pid in
                foregroundPointBelongsToWindow(point, pid: pid, window: target.axElement) &&
                    !livePassiveOverlayRegions(pid: pid, windowID: target.windowID, root: target.axElement)
                        .contains { $0.contains(point) }
            }),
            compatibility: pidCompatibility,
            genericForegroundEnabled: true,
            activity: userActivity,
            validator: validator,
            element: { reference, requestedSnapshotID in
                guard requestedSnapshotID == snapshotID,
                      let snapshotElement = consumed.references[reference]
                else { return nil }
                return self.currentActionElement(
                    snapshotElement: snapshotElement,
                    target: target,
                    windowBounds: authority.guardValue.bounds,
                    requiresExactBounds: true
                )
            },
            evidence: { _, expected in
                // A popup can close as soon as the balanced click completes.
                // Posting is acknowledged, while its application effect needs
                // a fresh observation instead of a second pixel proof or replay.
                if popupPoint != nil { return false }
                return (try? validator.revalidate(expected: expected, point: nil)) != nil
            }
        )
        let keyboardExecutor = ForegroundKeyboardExecutor(
            poster: CGForegroundInputPoster(pointIsInTargetWindow: { point, pid in
                foregroundPointBelongsToWindow(point, pid: pid, window: target.axElement) &&
                    !livePassiveOverlayRegions(pid: pid, windowID: target.windowID, root: target.axElement)
                        .contains { $0.contains(point) }
            }),
            compatibility: pidCompatibility,
            genericForegroundEnabled: true,
            activity: userActivity,
            validator: strictValidator,
            focus: { [weak self] in
                guard let self else { throw ActionExecutionError.staleSnapshot }
                return self.currentKeyboardFocusObservation(
                    in: AXUIElementCreateApplication(target.pid),
                    expectedPID: target.pid,
                    containerBounds: authority.guardValue.focusedAXBounds
                )
            },
            continuingTextFocus: { [weak self] expected, wanted in
                guard let self else { throw ActionExecutionError.targetGone }
                let observation = try self.currentForegroundActionObservation(
                    for: target, expectedFocusedRootPreference: expected.focusedRootPreference,
                    continuingTextFocus: wanted
                )
                let state = observation.stateFactory.make(
                    target: observation.target, snapshotID: snapshotID,
                    frontmostPID: liveFrontmostPID(), observedKeyboardFocus: observation.keyboardFocus
                )
                // The suggestion surface is not substituted for the authorized
                // window: key-window, PID, identities and geometry stay exact.
                let focus = try ExactPIDActionGuardValidator(state: { state })
                    .revalidateAndObserveFocus(expected: expected, point: nil)
                return KeyboardTextContinuationObservation(focus: focus ?? .stale,
                    observationRequired: observation.observationRequired)
            },
            keyTransitionFocus: { [weak self] expected in
                guard let self else { throw ActionExecutionError.targetGone }
                let observation = try self.currentForegroundActionObservation(
                    for: target, expectedFocusedRootPreference: expected.focusedRootPreference,
                    toleratesContainedOverlay: true
                )
                let state = observation.stateFactory.make(
                    target: observation.target, snapshotID: snapshotID,
                    frontmostPID: liveFrontmostPID(), observedKeyboardFocus: observation.keyboardFocus
                )
                let focus = try ExactPIDActionGuardValidator(state: { state })
                    .revalidateAndObserveFocus(expected: expected, point: nil)
                return KeyboardTextContinuationObservation(focus: focus ?? .stale,
                    observationRequired: observation.observationRequired)
            }
        )
        let result = ForegroundPlanExecutor(
            activity: userActivity,
            performer: performer,
            pidExecutor: executor,
            keyboardExecutor: keyboardExecutor,
            pointerOnlyPreflightState: popupPoint.map { point in
                { () throws -> ActionTargetState in
                    guard popupRuntime.popupPointerWindowMatches(target, at: point) else {
                        throw ActionExecutionError.targetNotFrontmost
                    }
                    return popupPointerSealedState(authority.guardValue)
                }
            }
        ).run(
            expected: popupPoint == nil ? deliveryKeyboardFocusGuard(
                base: authority.guardValue,
                liveFocus: { [weak self] in
                    guard let self else { return .secureOrIndeterminate }
                    return self.currentKeyboardFocusObservation(
                        in: AXUIElementCreateApplication(target.pid),
                        expectedPID: target.pid,
                        containerBounds: authority.guardValue.focusedAXBounds
                    )
                }
            ) : authority.guardValue,
            application: authority.application,
            lease: lease,
            entries: entries,
            managesActivityFragment: managesActivityFragment
        )
        let batch = ActionBatchResult(
            outcomes: result.outcomes,
            lastAcknowledgedAction: result.lastAcknowledgedAction,
            error: result.error
        )
        if let cooperative = result.cooperativeError {
            return CooperativeActionResult(batch: batch, error: .cooperative(cooperative))
        }
        return CooperativeActionResult(batch: batch, error: result.error.map(NativeCooperativeError.action))
    }

    private func pidTargetApplication(pid: pid_t) -> PIDTargetApplication? {
        guard let application = NSRunningApplication(processIdentifier: pid),
              !application.isTerminated,
              let identifier = application.bundleIdentifier,
              !identifier.isEmpty,
              let version = exactApplicationVersion(bundleURL: application.bundleURL)
        else { return nil }
        return PIDTargetApplication(bundleIdentifier: identifier, version: version)
    }

    private func currentWindow(for target: WindowTarget) throws -> SCWindow {
        let content = try waitForShareableContent()
        guard let current = matchingCurrentWindow(for: target, in: content.windows) else {
            throw WindowObservationError.targetGone
        }
        return current
    }

    private func matchingCurrentWindow(for target: WindowTarget, in windows: [SCWindow]) -> SCWindow? {
        windows.first(where: {
            $0.windowID == target.windowID
                && $0.owningApplication?.processID == target.pid
                && $0.isOnScreen
        })
    }

    private func currentActionState(
        for target: WindowTarget,
        expectedFocusedRootPreference: FocusedRootPreference
    ) throws -> ActionTargetState {
        try currentForegroundActionObservation(
            for: target,
            expectedFocusedRootPreference: expectedFocusedRootPreference
        ).target
    }

    /// Only the captured generic drag asks for this state. CG and AX frame
    /// publication can differ throughout an AppKit tracking loop. Exact object
    /// continuity proves the key window; the executor independently bounds both
    /// frames against the already-posted path before every following event.
    private func currentCapturedDragTargetState(for target: WindowTarget, snapshotID: String) throws -> PIDActionTargetState {
        guard liveFrontmostPID() == target.pid,
              let retained = target.axElement, let identity = target.axIdentity,
              CFHash(retained) == identity else { throw ActionExecutionError.targetNotFrontmost }
        let content = try waitForShareableContent()
        guard let window = matchingCurrentWindow(for: target, in: content.windows),
              !containedAppOwnedOverlayActive(targetPID: target.pid, targetWindowID: target.windowID,
                  targetBounds: window.frame, records: interactionWindowRecords(for: target))
        else { throw ActionExecutionError.staleSnapshot }
        let app = AXUIElementCreateApplication(target.pid)
        let matches = (completeObservedAXWindows(app) ?? []).filter {
            CFHash($0) == identity && CFEqual($0, retained)
        }
        guard matches.count == 1, let focused = focusedAXWindow(in: app),
              CFEqual(focused, retained), let focusedBounds = AXNodeReader.frameAttribute(focused)
        else { throw ActionExecutionError.targetNotFrontmost }
        return PIDActionTargetState(
            target: ActionTargetState(pid: target.pid, windowID: window.windowID,
                bounds: window.frame, axIdentity: identity, focusedAXIdentity: CFHash(focused),
                focusedAXBounds: focusedBounds, focusedRootPreference: .selectedWindow),
            snapshotID: snapshotID, isFrontmost: liveFrontmostPID() == target.pid,
            isKeyWindow: true)
    }

    private func currentForegroundPIDActionTargetState(
        for target: WindowTarget,
        snapshotID: String,
        expectedFocusedRootPreference: FocusedRootPreference
    ) throws -> PIDActionTargetState {
        let observation = try currentForegroundActionObservation(
            for: target,
            expectedFocusedRootPreference: expectedFocusedRootPreference
        )
        return observation.stateFactory.make(
            target: observation.target,
            snapshotID: snapshotID,
            frontmostPID: liveFrontmostPID(),
            observedKeyboardFocus: observation.keyboardFocus
        )
    }

    /// `toleratesContainedOverlay` is only for the check after a delivered key: an app-owned
    /// contained popup that key opened is reported for observation instead of failing.
    private func currentForegroundActionObservation(
        for target: WindowTarget,
        expectedFocusedRootPreference: FocusedRootPreference,
        continuingTextFocus: KeyboardFocusAuthority? = nil,
        toleratesContainedOverlay: Bool = false
    ) throws -> (target: ActionTargetState, stateFactory: ForegroundPIDActionStateFactory,
                 keyboardFocus: KeyboardFocusObservation, observationRequired: Bool) {
        guard liveFrontmostPID() == target.pid else {
            throw ActionExecutionError.targetNotFrontmost
        }
        let content: SCShareableContent
        do { content = try waitForShareableContent() }
        catch { throw ActionExecutionError.targetGone }
        guard let window = matchingCurrentWindow(for: target, in: content.windows) else {
            throw ActionExecutionError.targetGone
        }
        var focusedRootPreference: FocusedRootPreference = containedAppOwnedOverlayActive(
            targetPID: target.pid,
            targetWindowID: target.windowID,
            targetBounds: window.frame,
            records: interactionWindowRecords(for: target)
        ) ? .containedOverlay : .selectedWindow
        let observedPreference = focusedRootPreference
        let observationRequired = focusedRootPreference != expectedFocusedRootPreference
        let app = AXUIElementCreateApplication(target.pid)
        // Read mutable frame/title from the same CG window. The action validator
        // separately enforces its snapshot geometry (or a bounded active drag).
        let observedTarget = WindowTarget(
            appRef: target.appRef, windowRef: target.windowRef, pid: target.pid,
            windowID: target.windowID, bounds: window.frame, title: window.title ?? "",
            axIdentity: target.axIdentity, axElement: target.axElement,
            interactionMode: target.interactionMode
        )
        guard let expected = matchingAXWindow(in: app, target: observedTarget) else {
            throw ActionExecutionError.targetNotFrontmost
        }
        if observationRequired {
            guard continuingTextFocus != nil || toleratesContainedOverlay,
                  expectedFocusedRootPreference == .selectedWindow,
                  focusedRootPreference == .containedOverlay else {
                logActionRejected("FOCUSED-ROOT-PREFERENCE-FLIP pid=\(target.pid) windowID=\(target.windowID)")
                throw ActionExecutionError.staleSnapshot
            }
            focusedRootPreference = expectedFocusedRootPreference
        }
        guard let focused = trustedFocusedAXRoot(
                  in: app,
                  expected: expected,
                  targetBounds: window.frame,
                  preference: focusedRootPreference
              ),
              let focusedBounds = AXNodeReader.frameAttribute(focused)
        else {
            logActionRejected("PID-GUARD root-match cgBounds=\(window.frame) retainedAXBounds=\(String(describing: target.axElement.flatMap { AXNodeReader.frameAttribute($0) }))")
            throw ActionExecutionError.targetNotFrontmost
        }
        let keyboardFocus: KeyboardFocusObservation
        if observationRequired, continuingTextFocus != nil {
            guard let proof = provenContinuingTextFocus(
                expectedPreference: expectedFocusedRootPreference, observedPreference: observedPreference,
                wanted: continuingTextFocus, windowBounds: focusedBounds,
                readFocused: { self.currentFocusedAXElement(in: app) },
                observe: { self.currentKeyboardFocusObservation(of: $0, expectedPID: target.pid,
                    containerBounds: focusedBounds) },
                belongs: { focusedElementBelongsToExactAXRoot(element: $0, root: expected, pid: target.pid) },
                same: { CFEqual($0, $1) },
                reject: { logActionRejected("TEXT-CONTINUITY-REJECT pid=\(target.pid) reason=\($0)") }
            ) else { throw ActionExecutionError.staleSnapshot }
            keyboardFocus = proof
        } else {
            keyboardFocus = currentKeyboardFocusObservation(
                in: app, expectedPID: target.pid, containerBounds: focusedBounds
            )
        }
        let state = ActionTargetState(
            pid: target.pid,
            windowID: window.windowID,
            bounds: window.frame,
            axIdentity: CFHash(expected),
            focusedAXIdentity: CFHash(focused),
            focusedAXBounds: focusedBounds,
            focusedRootPreference: focusedRootPreference,
            keyboardFocus: keyboardFocus.authority
        )
        let screenCaptureWindows = content.windows.map {
            PIDScreenCaptureWindowObservation(
                pid: $0.owningApplication?.processID,
                windowID: $0.windowID,
                bounds: $0.frame,
                title: $0.title,
                isOnScreen: $0.isOnScreen
            )
        }
        return (
            state,
            ForegroundPIDActionStateFactory(
                focusedAXWindow: {
                    self.focusedPIDAXWindowObservation(in: app)
                },
                screenCaptureWindows: { screenCaptureWindows }
            ),
            keyboardFocus,
            observationRequired
        )
    }

    private func currentActionElement(
        snapshotElement: SnapshotElement,
        target: WindowTarget,
        windowBounds: CGRect,
        requiresExactBounds: Bool = false
    ) -> ActionElement? {
        guard let element = snapshotElement.element,
              let globalBounds = AXNodeReader.frameAttribute(element)
        else { return nil }
        let localBounds = CGRect(
            x: globalBounds.minX - windowBounds.minX,
            y: globalBounds.minY - windowBounds.minY,
            width: globalBounds.width,
            height: globalBounds.height
        )
        guard requiresExactBounds
            ? pointerSafeRegionBoundsMatch(localBounds, snapshotElement.bounds)
            : approximatelyEqual(localBounds, snapshotElement.bounds)
        else { return nil }
        let role = AXNodeReader.stringAttribute(element, kAXRoleAttribute)
        let subrole = AXNodeReader.stringAttribute(element, kAXSubroleAttribute)
        return ActionElement(
            element: element,
            identityToken: "ax:\(CFHash(element))",
            bounds: localBounds,
            roleResult: role,
            subroleResult: subrole,
            enabled: AXNodeReader.boolAttribute(element, kAXEnabledAttribute),
            actionNames: AXNodeReader.actionNameResults(element)
        )
    }

    private func systemCatalogWindows() throws -> [CatalogSCWindow] {
        let versions = ObservationPIDInventory<String?>()
        return try waitForShareableWindows().compactMap { window in
            guard let owner = window.owningApplication,
                  let application = NSRunningApplication(processIdentifier: owner.processID)
            else { return nil }
            return CatalogSCWindow(
                pid: owner.processID,
                windowID: window.windowID,
                bounds: window.frame,
                isOnScreen: window.isOnScreen,
                title: window.title ?? "",
                applicationName: owner.applicationName,
                bundleID: owner.bundleIdentifier,
                appVersion: versions.value(for: owner.processID) { exactApplicationVersion(bundleURL: application.bundleURL) },
                isTerminated: application.isTerminated,
                isRegularApplication: application.activationPolicy == .regular
            )
        }
    }

    private func catalogExactAXWindow(
        in app: AXUIElement,
        target: WindowTarget
    ) -> AXUIElement? {
        if let catalogAXWindowMatcher {
            return catalogAXWindowMatcher(app, target)
        }
        return matchingAXWindow(in: app, target: target)
    }

    private func waitForShareableWindows() throws -> [SCWindow] {
        try waitForShareableContent().windows
    }

    private func waitForShareableContent() throws -> SCShareableContent {
        do {
            return try waitForAsync(timeout: AXObservationBudget.current?.phaseTimeout(maximum: 3) ?? 3) {
                try await SCShareableContent.excludingDesktopWindows(
                    true,
                    onScreenWindowsOnly: true
                )
            }
        } catch AsyncWaitError.timedOut {
            throw WindowObservationError.targetGone
        }
    }

    private func waitForDisplayImage(
        display: SCDisplay,
        descriptor: DisplayCaptureCandidate,
        excludingWindows: [SCWindow]
    ) throws -> CGImage {
        let configuration = SCStreamConfiguration()
        configuration.width = Int(descriptor.pixelSize.width)
        configuration.height = Int(descriptor.pixelSize.height)
        configuration.showsCursor = false
        let filter = SCContentFilter(display: display, excludingWindows: excludingWindows)
        do {
            return try waitForAsync(timeout: AXObservationBudget.current?.phaseTimeout(maximum: 5) ?? 5) {
                try await SCScreenshotManager.captureImage(
                    contentFilter: filter,
                    configuration: configuration
                )
            }
        } catch {
            throw WindowObservationError.captureFailed
        }
    }

    private func waitForImage(
        window: SCWindow,
        geometry: WindowGeometry,
        focusedTransientActive: Bool,
        sameApplicationWindowFrames: [CGRect]
    ) throws -> CGImage {
        let configuration = SCStreamConfiguration()
        configuration.width = Int(geometry.pixelSize.width)
        configuration.height = Int(geometry.pixelSize.height)
        configuration.showsCursor = false
        configuration.ignoreShadowsSingleWindow = true
        let filter = SCContentFilter(desktopIndependentWindow: window)
        let provider = SystemExactWindowImageProvider(
            primary: {
                if focusedTransientActive || !primaryWindowContentHasExactSize(
                    contentRect: filter.contentRect, expectedBounds: geometry.bounds
                ) {
                    throw PrimaryWindowCaptureError.contentMismatch
                }
                do {
                    return try waitForAsync(timeout: AXObservationBudget.current?.phaseTimeout(maximum: 5) ?? 5) {
                        try await SCScreenshotManager.captureImage(
                            contentFilter: filter,
                            configuration: configuration
                        )
                    }
                } catch AsyncWaitError.timedOut {
                    throw PrimaryWindowCaptureError.timedOut
                }
            },
            fallback: { windowID in
                if #available(macOS 15.0, *) {
                    return ArtifactDirectory.captureExactWindow(windowID: windowID)
                }
                return CGWindowListCreateImage(
                    .null,
                    .optionIncludingWindow,
                    windowID,
                    [.boundsIgnoreFraming, .bestResolution]
                )
            }
        )
        return try captureExactWindowImage(
            windowID: CGWindowID(window.windowID), provider: provider,
            expectedBounds: geometry.bounds,
            sameApplicationWindowFrames: sameApplicationWindowFrames
        )
    }

    private func matchingAXWindow(in app: AXUIElement, target: WindowTarget) -> AXUIElement? {
        let matches = (completeObservedAXWindows(app) ?? []).filter { element in
            guard observedAXPID(element) == target.pid else { return false }
            guard let frame = AXNodeReader.frameAttribute(element) else { return false }
            guard screenCaptureBoundsMatchAXBounds(
                screenCapture: target.bounds,
                accessibility: frame
            ) else { return false }
            let windowID = observedAXWindowID(element)
            if let windowID, windowID != target.windowID { return false }
            if let expectedIdentity = target.axIdentity, CFHash(element) != expectedIdentity { return false }
            if let expectedElement = target.axElement {
                // An exact retained AX object identifies this window across title
                // changes caused by navigation. Titles remain a matching guard
                // when no retained object is available.
                return CFEqual(element, expectedElement)
            }
            if windowID != nil { return true }
            let titleResult = accessibilityWindowName(element)
            guard titleResult.status == .complete else { return false }
            let title = titleResult.value ?? ""
            return windowTitlesMatch(screenCaptureTitle: target.title, accessibilityTitle: title)
        }
        return matches.count == 1 ? matches[0] : nil
    }

    private func containedAXOverlay(in root: AXUIElement, targetBounds: CGRect) -> ContainedAXOverlayScan {
        containedAXOverlayScan(
            root: root,
            targetBounds: targetBounds,
            children: completeAXChildren,
            attributes: { candidate in
                AXOverlayAttributes(
                    role: AXNodeReader.stringAttribute(candidate, kAXRoleAttribute),
                    subrole: AXNodeReader.stringAttribute(candidate, kAXSubroleAttribute),
                    bounds: AXNodeReader.frameAttribute(candidate)
                )
            }
        )
    }

    private func completeAXChildren(_ element: AXUIElement, maximum: Int) -> [AXUIElement]? {
        let role = AXNodeReader.stringAttribute(element, kAXRoleAttribute)
        let subrole = AXNodeReader.stringAttribute(element, kAXSubroleAttribute)
        return completeAXElementArray(
            element,
            attribute: kAXChildrenAttribute,
            maximum: maximum,
            unsupportedMeansEmpty: knownAXLeafRole(role: role, subrole: subrole)
        )
    }

    private func focusedAXWindow(in app: AXUIElement) -> AXUIElement? {
        var value: CFTypeRef?
        guard observationAXCall(element: app, fallback: AXError.cannotComplete, {
            AXUIElementCopyAttributeValue(app, kAXFocusedWindowAttribute as CFString, &value)
        }) == .success else { return nil }
        return decodeAXElement(value)
    }

    private func currentKeyboardFocusObservation(
        in app: AXUIElement,
        expectedPID: pid_t,
        containerBounds: CGRect
    ) -> KeyboardFocusObservation {
        guard let focused = currentFocusedAXElement(in: app) else { return .secureOrIndeterminate }
        return currentKeyboardFocusObservation(of: focused, expectedPID: expectedPID, containerBounds: containerBounds)
    }

    private func currentFocusedAXElement(in app: AXUIElement) -> AXUIElement? {
        var value: CFTypeRef?
        guard observationAXCall(element: app, fallback: AXError.cannotComplete, {
            AXUIElementCopyAttributeValue(app, kAXFocusedUIElementAttribute as CFString, &value)
            }) == .success else { return nil }
        return decodeAXElement(value)
    }

    private func currentKeyboardFocusObservation(
        of focused: AXUIElement, expectedPID: pid_t, containerBounds: CGRect
    ) -> KeyboardFocusObservation {
        var pid = pid_t()
        guard AXUIElementGetPid(focused, &pid) == .success else { return .secureOrIndeterminate }
        guard pid == expectedPID else { return .stale }
        let role = AXNodeReader.stringAttribute(focused, kAXRoleAttribute)
        let subrole = AXNodeReader.stringAttribute(focused, kAXSubroleAttribute)
        if keyboardFocusIsExplicitlySecure(role: role.value, subrole: subrole.value) {
            return .secure
        }
        guard let bounds = AXNodeReader.frameAttribute(focused) else { return .secureOrIndeterminate }
        return makeKeyboardFocusObservation(
            expectedPID: expectedPID,
            actualPID: pid,
            identityToken: "ax:\(CFHash(focused))",
            bounds: bounds,
            role: role,
            subrole: subrole,
            enabled: AXNodeReader.boolAttribute(focused, kAXEnabledAttribute),
            containerBounds: containerBounds
        )
    }

    private func focusedPIDAXWindowObservation(
        in app: AXUIElement
    ) -> PIDAXFocusedWindowObservation? {
        guard let focused = focusedAXWindow(in: app),
              let focusedBounds = AXNodeReader.frameAttribute(focused)
        else { return nil }
        var focusedPID = pid_t()
        guard AXUIElementGetPid(focused, &focusedPID) == .success else { return nil }
        let title = accessibilityWindowName(focused)
        let role = AXNodeReader.stringAttribute(focused, kAXRoleAttribute)
        let provenSheet = role.status == .complete && role.value == kAXSheetRole &&
            (completeObservedAXWindows(app) ?? []).filter { CFEqual($0, focused) }.count == 1
        return PIDAXFocusedWindowObservation(
            pid: focusedPID,
            bounds: focusedBounds,
            axIdentity: CFHash(focused),
            title: title.status == .complete ? (title.value ?? "") : nil,
            isProvenSheet: provenSheet,
            windowID: observedAXWindowID(focused)
        )
    }

    private func applicationMenuBar(
        in app: AXUIElement,
        expectedPID: pid_t
    ) -> AXUIElement? {
        var applicationPID = pid_t()
        guard AXUIElementGetPid(app, &applicationPID) == .success else { return nil }
        var value: CFTypeRef?
        guard observationAXCall(element: app, fallback: AXError.cannotComplete, {
            AXUIElementCopyAttributeValue(app, kAXMenuBarAttribute as CFString, &value)
            }) == .success,
        let menuBar = decodeAXElement(value)
        else { return nil }
        var menuBarPID = pid_t()
        guard AXUIElementGetPid(menuBar, &menuBarPID) == .success,
              trustedApplicationMenuBarOwner(
                  targetPID: expectedPID,
                  applicationPID: applicationPID,
                  menuBarPID: menuBarPID
              )
        else { return nil }
        return menuBar
    }

    private func trustedFocusedAXRoot(
        in app: AXUIElement,
        expected: AXUIElement,
        targetBounds: CGRect,
        preference: FocusedRootPreference = .selectedWindow
    ) -> AXUIElement? {
        if AXNodeReader.stringAttribute(expected, kAXRoleAttribute).value == kAXSheetRole {
            return focusBelongsToExactAXRoot(app: app, root: expected) ? expected : nil
        }
        if preference == .containedOverlay,
           let contained = focusedContainedDialogRoot(in: app, targetBounds: targetBounds)
        {
            return contained
        }
        if let focused = focusedAXWindow(in: app) {
            guard let focusedBounds = AXNodeReader.frameAttribute(focused),
                  trustedFocusedWindow(
                      expectedIdentity: CFHash(expected),
                      targetBounds: targetBounds,
                      focusedIdentity: CFHash(focused),
                      focusedBounds: focusedBounds
                  )
            else { return nil }
            return focused
        }
        var focusedValue: CFTypeRef?
        guard observationAXCall(element: app, fallback: AXError.cannotComplete, {
            AXUIElementCopyAttributeValue(app, kAXFocusedUIElementAttribute as CFString, &focusedValue)
            }) == .success,
        let focusedElement = decodeAXElement(focusedValue),
        let focusedBounds = AXNodeReader.frameAttribute(focusedElement),
        trustedFocusedElement(
            role: AXNodeReader.stringAttribute(focusedElement, kAXRoleAttribute),
            subrole: AXNodeReader.stringAttribute(focusedElement, kAXSubroleAttribute),
            enabled: AXNodeReader.boolAttribute(focusedElement, kAXEnabledAttribute),
            targetBounds: targetBounds,
            focusedBounds: focusedBounds
        )
        else { return nil }
        return focusedElement
    }

    private func focusedContainedDialogRoot(
        in app: AXUIElement,
        targetBounds: CGRect
    ) -> AXUIElement? {
        var focusedValue: CFTypeRef?
        guard observationAXCall(element: app, fallback: AXError.cannotComplete, {
            AXUIElementCopyAttributeValue(app, kAXFocusedUIElementAttribute as CFString, &focusedValue)
            }) == .success,
        let focusedValue,
        let focusedElement = decodeAXElement(focusedValue)
        else { return nil }
        var candidate = focusedElement
        var preferredBody: AXUIElement?
        for _ in 0..<maximumAXDepth {
            let role = AXNodeReader.stringAttribute(candidate, kAXRoleAttribute)
            if let bounds = AXNodeReader.frameAttribute(candidate) {
                if preferredBody == nil,
                   trustedContainedDialogBody(
                       role: role,
                       targetBounds: targetBounds,
                       candidateBounds: bounds
                   )
                {
                    preferredBody = candidate
                }
                if trustedContainedDialogRoot(
                   role: role,
                   subrole: AXNodeReader.stringAttribute(candidate, kAXSubroleAttribute),
                   targetBounds: targetBounds,
                   candidateBounds: bounds
                ) {
                    return preferredBody ?? candidate
                }
            }
            var parentValue: CFTypeRef?
            guard observationAXCall(element: candidate, fallback: AXError.cannotComplete, {
                AXUIElementCopyAttributeValue(candidate, kAXParentAttribute as CFString, &parentValue)
                }) == .success,
            let parentValue,
            let parent = decodeAXElement(parentValue)
            else { return nil }
            candidate = parent
        }
        return nil
    }

    private func opaqueReference(prefix: String) -> String { "\(prefix)_\(UUID().uuidString.lowercased())" }

    private func bounded(_ value: String) -> String { String(value.prefix(maximumAXStringCharacters)) }

    private func appName(_ value: JSONValue) -> String {
        guard case let .object(object) = value, case let .string(name)? = object["name"] else { return "" }
        return name
    }
}

private struct StoredCooperativePlan {
    let dispatcher: InputDispatcher
    let plan: DispatchPlan
    let context: DispatchContext
    let actions: [NativeAction]
    let snapshotContext: SnapshotActionContext
    let fragmentRequest: ForegroundFragmentPlanRequest?
    let fragmentDraftExpiresAt: TimeInterval?
    let sequence: UInt64
}

private func windowActionError(_ error: Error) -> ActionExecutionError {
    error as? ActionExecutionError ?? .helperFailed
}

func windowTitlesMatch(screenCaptureTitle: String, accessibilityTitle: String) -> Bool {
    if screenCaptureTitle.isEmpty || screenCaptureTitle == accessibilityTitle { return true }
    if screenCaptureTitle.hasPrefix("/") {
        return URL(fileURLWithPath: screenCaptureTitle).lastPathComponent == accessibilityTitle
    }
    // Chromium/Electron windows commonly expose one side as "<title> - <app>"
    // while the other side is the bare page title; accept that exact suffix
    // form in either direction. Window ID, bounds, and AX-identity checks
    // still run after this match, so a suffix collision cannot select a
    // wrong window by itself.
    return titleSuffixMatch(base: screenCaptureTitle, extended: accessibilityTitle) ||
        titleSuffixMatch(base: accessibilityTitle, extended: screenCaptureTitle)
}

private func titleSuffixMatch(base: String, extended: String) -> Bool {
    guard base.count >= 2, extended.count > base.count, extended.hasPrefix(base) else { return false }
    let remainder = extended.dropFirst(base.count)
    return remainder.hasPrefix(" - ")
}

struct PIDAXFocusedWindowObservation: Equatable {
    let pid: pid_t
    let bounds: CGRect
    let axIdentity: CFHashCode
    let title: String?
    var isProvenSheet: Bool = false
    var windowID: CGWindowID? = nil
}

struct PIDScreenCaptureWindowObservation: Equatable {
    let pid: pid_t?
    let windowID: CGWindowID
    let bounds: CGRect
    let title: String?
    let isOnScreen: Bool
}

struct PIDFocusedWindowObservation: Equatable {
    let pid: pid_t
    let windowID: CGWindowID
    let axBounds: CGRect
    let screenCaptureBounds: CGRect
    let axIdentity: CFHashCode
}

struct ForegroundPIDActionStateFactory {
    typealias FocusedAXWindow = () -> PIDAXFocusedWindowObservation?
    typealias ScreenCaptureWindows = () -> [PIDScreenCaptureWindowObservation]

    private let focusedAXWindow: FocusedAXWindow
    private let screenCaptureWindows: ScreenCaptureWindows

    init(
        focusedAXWindow: @escaping FocusedAXWindow,
        screenCaptureWindows: @escaping ScreenCaptureWindows
    ) {
        self.focusedAXWindow = focusedAXWindow
        self.screenCaptureWindows = screenCaptureWindows
    }

    func make(
        target: ActionTargetState,
        snapshotID: String,
        frontmostPID: pid_t?,
        observedKeyboardFocus: KeyboardFocusObservation? = nil
    ) -> PIDActionTargetState {
        let focused = mappedFocusedWindow(targetPID: target.pid)
        let exactKeyWindow = focused.map {
            $0.pid == target.pid &&
                $0.windowID == target.windowID &&
                $0.screenCaptureBounds == target.bounds &&
                $0.axIdentity == target.axIdentity
        } ?? false
        return PIDActionTargetState(
            target: target,
            snapshotID: snapshotID,
            isFrontmost: frontmostPID == target.pid,
            isKeyWindow: exactKeyWindow,
            observedKeyboardFocus: observedKeyboardFocus
        )
    }

    private func mappedFocusedWindow(targetPID: pid_t) -> PIDFocusedWindowObservation? {
        guard let focused = focusedAXWindow(),
              focused.pid == targetPID
        else { return nil }
        let windows = screenCaptureWindows()
        if let windowID = focused.windowID {
            guard let window = matchingScreenWindow(pid: focused.pid, bounds: focused.bounds,
                title: BoundedAXStringResult(value: focused.title, status: .complete),
                windowID: windowID, windows: windows) else { return nil }
            return PIDFocusedWindowObservation(pid: focused.pid, windowID: window.windowID,
                axBounds: focused.bounds, screenCaptureBounds: window.bounds, axIdentity: focused.axIdentity)
        }
        guard let focusedTitle = focused.title else { return nil }
        let candidates = windows.filter { candidate in
            candidate.pid == focused.pid &&
                candidate.isOnScreen &&
                approximatelyEqual(candidate.bounds, focused.bounds)
        }
        // An unnamed nested sheet still has a retained AX identity and an owned
        // parent chain. It may map only to a sole, also explicitly unnamed CG
        // candidate. make() additionally checks the exact window ID and identity.
        let unnamedSheet = focused.isProvenSheet && focused.axIdentity != 0 &&
            focusedTitle.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        // Stacked browser windows often have identical bounds. Require complete
        // title evidence before disambiguating them; an unnamed sibling may be
        // the focused window. make() still checks native ID, bounds and AX identity.
        guard candidates.allSatisfy({ candidate in
            guard let title = candidate.title else { return false }
            if unnamedSheet {
                return candidates.count == 1 && title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            }
            return !focusedTitle.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty &&
                !title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }) else { return nil }
        let matchingTitles = candidates.filter { candidate in
            guard let title = candidate.title else { return false }
            return windowTitlesMatch(screenCaptureTitle: title, accessibilityTitle: focusedTitle)
        }
        guard matchingTitles.count == 1,
              let window = matchingTitles.first
        else { return nil }
        return PIDFocusedWindowObservation(
            pid: focused.pid,
            windowID: window.windowID,
            axBounds: focused.bounds,
            screenCaptureBounds: window.bounds,
            axIdentity: focused.axIdentity
        )
    }
}

struct VisibleWindowRecord {
    let pid: pid_t
    let windowID: CGWindowID
    let bounds: CGRect
    let layer: Int
    let alpha: Double
    let zOrder: Int

    init(
        pid: pid_t,
        windowID: CGWindowID,
        bounds: CGRect,
        layer: Int,
        alpha: Double,
        zOrder: Int = .max
    ) {
        self.pid = pid
        self.windowID = windowID
        self.bounds = bounds
        self.layer = layer
        self.alpha = alpha
        self.zOrder = zOrder
    }
}

struct AXOverlayAttributes {
    let role: BoundedAXStringResult
    let subrole: BoundedAXStringResult
    let bounds: CGRect?
}

func completeAXElementCount(
    error: AXError,
    available: CFIndex,
    maximum: Int,
    unsupportedMeansEmpty: Bool = false
) -> Int? {
    if unsupportedMeansEmpty && (error == .attributeUnsupported || error == .noValue) {
        return 0
    }
    guard error == .success,
          maximum >= 0,
          available >= 0,
          available <= CFIndex(maximum)
    else { return nil }
    return Int(available)
}

func completeAXElementArray(
    _ element: AXUIElement,
    attribute: String,
    maximum: Int,
    unsupportedMeansEmpty: Bool = false
) -> [AXUIElement]? {
    var available: CFIndex = 0
    let countError = observationAXCall(element: element, fallback: AXError.cannotComplete) {
        AXUIElementGetAttributeValueCount(element, attribute as CFString, &available)
    }
    guard let count = completeAXElementCount(
        error: countError,
        available: available,
        maximum: maximum,
        unsupportedMeansEmpty: unsupportedMeansEmpty
    ) else { return nil }
    if count == 0 { return [] }

    var values: CFArray?
    guard observationAXCall(element: element, fallback: AXError.cannotComplete, {
        AXUIElementCopyAttributeValues(element, attribute as CFString, 0, available, &values)
    }) == .success,
    let values,
    CFArrayGetCount(values) == available
    else { return nil }

    var result: [AXUIElement] = []
    result.reserveCapacity(count)
    for index in 0..<count {
        let raw = CFArrayGetValueAtIndex(values, index)
        let value = unsafeBitCast(raw, to: CFTypeRef.self)
        guard let child = decodeAXElement(value) else { return nil }
        result.append(child)
    }
    return result
}

func knownAXLeafRole(
    role: BoundedAXStringResult,
    subrole: BoundedAXStringResult
) -> Bool {
    guard role.status == .complete,
          subrole.status == .complete,
          let role = role.value
    else { return false }
    switch role {
    case "AXButton",
         "AXCheckBox",
         "AXColorWell",
         "AXImage",
         "AXLink",
         "AXProgressIndicator",
         "AXRadioButton",
         "AXSlider",
         "AXTextArea",
         "AXTextField",
         "AXValueIndicator":
        return true
    default:
        return false
    }
}

func mapCompleteAXElements<T>(
    _ elements: [AXUIElement],
    transform: (AXUIElement) -> T?
) -> [T]? {
    var result: [T] = []
    result.reserveCapacity(elements.count)
    for element in elements {
        guard let mapped = transform(element) else { return nil }
        result.append(mapped)
    }
    return result
}

let maximumContainedOverlayDepth = 8
let maximumShallowOverlayDepth = 2

/// What the shallow AX scan of a window found. `uncertain` is a read that failed, such as a
/// Chromium page tree rebuilt during navigation; `overlay` is a sheet, dialog or window inside it.
enum ContainedAXOverlayScan: String, Equatable {
    case clear
    case uncertain
    case overlay
}

func containedAXOverlayOrUncertain(
    root: AXUIElement,
    targetBounds: CGRect,
    children: (AXUIElement, Int) -> [AXUIElement]?,
    attributes: (AXUIElement) -> AXOverlayAttributes?
) -> Bool {
    containedAXOverlayScan(root: root, targetBounds: targetBounds, children: children, attributes: attributes) != .clear
}

func containedAXOverlayScan(
    root: AXUIElement,
    targetBounds: CGRect,
    children: (AXUIElement, Int) -> [AXUIElement]?,
    attributes: (AXUIElement) -> AXOverlayAttributes?
) -> ContainedAXOverlayScan {
    var queue: [(element: AXUIElement, depth: Int)] = [(root, 0)]
    var visited: [AXUIElement] = []
    var remaining = 128
    let maximumDepth = maximumContainedOverlayDepth

    while !queue.isEmpty {
        let (candidate, depth) = queue.removeFirst()
        if visited.contains(where: { CFEqual($0, candidate) }) {
            // Shared references across parents are legitimate in Office-style
            // AX trees. The visited set exists to skip re-expansion, not to
            // flag occlusion; the node budget already bounds the traversal.
            overlayDiagnostic("shared AX reference revisited — skipped")
            continue
        }
        visited.append(candidate)

        let isRoot = CFEqual(candidate, root)
        if !isRoot {
            guard remaining > 0 else {
                if depth <= maximumShallowOverlayDepth {
                    overlayDiagnostic("shallow scan budget exhausted (depth=\(depth)) — window stays uncertain")
                    return .uncertain
                }
                // Breadth-first order guarantees the shallow layer — where
                // sheets and dialogs live — was fully visited before the
                // budget ran out at a deeper level. Proceeding without a
                // positive overlay finding is therefore sound here.
                overlayDiagnostic("scan budget exhausted at depth=\(depth) — shallow layer fully checked, no overlay")
                return .clear
            }
            remaining -= 1
        }

        if !isRoot {
            guard let values = attributes(candidate),
                  values.role.status == .complete,
                  values.role.value != nil
            else {
                overlayDiagnostic("role read failed during overlay scan (depth=\(depth))")
                return .uncertain
            }
            // Chromium/Electron accessibility trees commonly fail to expose
            // subrole for ordinary descendants. A failed subrole read is not
            // occlusion evidence and must not make the whole window uncertain;
            // role-level window evidence (AXSheet/AXWindow) is what detects
            // actual overlays, and AXDialog is trusted only when subrole is
            // complete.
            let isOverlay = values.role.value == "AXSheet" ||
                values.role.value == kAXWindowRole as String ||
                (values.subrole.status == .complete && values.subrole.value == "AXDialog")
            if isOverlay {
                guard let bounds = values.bounds else {
                    overlayDiagnostic("overlay role \(values.role.value ?? "?") has unreadable bounds")
                    return .overlay
                }
                if trustedFocusedWindow(
                    expectedIdentity: 1,
                    targetBounds: targetBounds,
                    focusedIdentity: 2,
                    focusedBounds: bounds
                ) || approximatelyEqual(bounds, targetBounds) {
                    return .overlay
                }
            }
        }

        // Window-level overlays (sheets/dialogs) are shallow AX descendants;
        // deep content trees (editors, panels) cannot contain them. Once the
        // depth limit is reached, subtree enumeration stops without making the
        // window uncertain. This keeps Chromium-sized trees (thousands of
        // nodes) verifiable while the shallow window layer is fully checked.
        guard depth < maximumDepth else { continue }
        guard let descendants = children(candidate, remaining) else {
            if depth <= maximumShallowOverlayDepth {
                overlayDiagnostic("shallow children read failed (depth=\(depth)) — window stays uncertain")
                return .uncertain
            }
            // Sheets and dialogs hang directly off the window as shallow AX
            // descendants. A failed children read deep inside a content tree
            // (Finder columns, editors) cannot hide one: skipping the branch
            // keeps live windows bindable without weakening shallow checks.
            overlayDiagnostic("deep children read failed (depth=\(depth), remaining=\(remaining)) — branch skipped")
            continue
        }
        queue.append(contentsOf: descendants.map { ($0, depth + 1) })
    }
    return .clear
}

private func overlayDiagnostic(_ reason: String) {
    let line = "[overlay_uncertain] \(reason)\n"
    FileHandle.standardError.write(Data(line.utf8))
    let diagnosticPath = "/tmp/astra-target-gone-diagnostics.log"
    if let handle = FileHandle(forWritingAtPath: diagnosticPath) {
        defer { try? handle.close() }
        handle.seekToEndOfFile()
        handle.write(Data(line.utf8))
    } else {
        try? line.write(toFile: diagnosticPath, atomically: true, encoding: .utf8)
    }
}

func containedVisibleOverlayOrUncertain(
    targetPID: pid_t,
    targetWindowID: CGWindowID,
    targetBounds: CGRect,
    records: [VisibleWindowRecord]?
) -> Bool {
    guard let records else { return true }
    return backgroundVisibleWindowAmbiguityActive(
        targetPID: targetPID,
        targetWindowID: targetWindowID,
        targetBounds: targetBounds,
        records: records
    )
}

func backgroundVisibleWindowAmbiguityActive(
    targetPID: pid_t,
    targetWindowID: CGWindowID,
    targetBounds: CGRect,
    records: [VisibleWindowRecord]
) -> Bool {
    !backgroundVisibleWindowAmbiguities(targetPID: targetPID, targetWindowID: targetWindowID,
        targetBounds: targetBounds, records: records).isEmpty
}

func backgroundVisibleWindowAmbiguities(
    targetPID: pid_t,
    targetWindowID: CGWindowID,
    targetBounds: CGRect,
    records: [VisibleWindowRecord]
) -> [VisibleWindowRecord] {
    let selected = records.filter { $0.pid == targetPID && $0.windowID == targetWindowID }
    return records.filter { record in
        let behind = selected.count == 1 && approximatelyEqual(selected[0].bounds, targetBounds) && windowIsProvablyBehind(
            candidateLayer: record.layer, candidateOrder: record.zOrder,
            selectedLayer: selected[0].layer, selectedOrder: selected[0].zOrder
        )
        return !behind && record.pid == targetPID &&
            record.windowID != targetWindowID &&
            record.alpha > 0 &&
            (trustedFocusedWindow(
                expectedIdentity: 1,
                targetBounds: targetBounds,
                focusedIdentity: 2,
                focusedBounds: record.bounds
            ) || approximatelyEqual(record.bounds, targetBounds))
    }
}

/// Browsers show a hovered link's address in a thin strip on the window's bottom edge (live Edge:
/// 24 pt tall, 437 to 1283 pt wide, alpha animating). It takes no focus and hides no control of the
/// window's own capture, so it does not block binding; pointer input inside it is still refused.
func appStatusStripOverlay(_ record: VisibleWindowRecord, targetBounds: CGRect, targetLayer: Int) -> Bool {
    let bounds = record.bounds
    guard [bounds.minX, bounds.minY, bounds.width, bounds.height].allSatisfy(\.isFinite),
          record.layer == targetLayer,
          bounds.width > 0, bounds.height > 0, bounds.height <= 32,
          bounds.minX >= targetBounds.minX, bounds.maxX <= targetBounds.maxX,
          bounds.maxY <= targetBounds.maxY, targetBounds.maxY - bounds.maxY <= 8
    else { return false }
    return true
}

/// This app's bottom-edge status strips over the target, judged against the target's own live
/// record. Without that record nothing is waived.
func appStatusStripRecords(
    _ records: [VisibleWindowRecord], targetPID: pid_t, targetWindowID: CGWindowID, targetBounds: CGRect
) -> [VisibleWindowRecord] {
    let selected = records.filter { $0.pid == targetPID && $0.windowID == targetWindowID }
    guard selected.count == 1, let target = selected.first else { return [] }
    return records.filter {
        $0.pid == targetPID && $0.windowID != targetWindowID &&
            appStatusStripOverlay($0, targetBounds: targetBounds, targetLayer: target.layer)
    }
}

func appStatusStripRegions(
    _ records: [VisibleWindowRecord], targetPID: pid_t, targetWindowID: CGWindowID, targetBounds: CGRect
) -> [CGRect] {
    appStatusStripRecords(records, targetPID: targetPID, targetWindowID: targetWindowID,
                          targetBounds: targetBounds).map(\.bounds)
}

/// Snapshots name at most this many open suggestion lists.
let maximumReportedSuggestionPopups = 4

/// Overlays that may be waited out instead of blocking at once: small floating windows of the app
/// (caret indicators), suggestion lists proven moments ago, and a window AX tree that failed a read
/// (live Edge: the page tree is rebuilt while Return loads the page). Menus, sheets, dialogs and
/// AX-contained overlays block immediately.
func overlaysMayBeTransient(
    _ offenders: [VisibleWindowRecord], axOverlay: ContainedAXOverlayScan,
    recentlyProvenSuggestionPopup: (VisibleWindowRecord) -> Bool
) -> Bool {
    let waitable = offenders.allSatisfy {
        ($0.layer > 0 && $0.bounds.width <= 200 && $0.bounds.height <= 200) || recentlyProvenSuggestionPopup($0)
    }
    switch axOverlay {
    case .overlay: return false
    case .uncertain: return waitable
    case .clear: return !offenders.isEmpty && waitable
    }
}

/// Suggestion lists proven attached to a focused field, remembered for a few seconds. Return moves
/// focus away while the list is still drawn (live Edge: 1002x199 as the page began loading), so the
/// list can no longer be proven; it is then waited out like a tooltip rather than blocking at once.
final class RecentSuggestionPopups {
    static let memorySeconds: TimeInterval = 3
    private let lock = NSLock()
    private var provenAt: [CGWindowID: (pid: pid_t, time: TimeInterval)] = [:]

    func record(_ popups: [VisibleWindowRecord], at now: TimeInterval) {
        lock.lock()
        defer { lock.unlock() }
        provenAt = provenAt.filter { now - $0.value.time <= Self.memorySeconds }
        for popup in popups { provenAt[popup.windowID] = (popup.pid, now) }
    }

    func contains(_ record: VisibleWindowRecord, at now: TimeInterval) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard let entry = provenAt[record.windowID] else { return false }
        return entry.pid == record.pid && now - entry.time <= Self.memorySeconds
    }
}

/// A focused text field's own suggestion list (live Edge: a separate window on the page's layer that
/// encloses the address field and drops 199 to 471 pt below it; Finder drops its search list just
/// under the field). Keyboard focus stays in the field, so the list does not block binding; pointer
/// input inside it is refused. Menus and modal panels use other layers and still block.
func attachedSuggestionPopup(
    _ record: VisibleWindowRecord, focusedField field: CGRect, targetBounds: CGRect, targetLayer: Int
) -> Bool {
    let bounds = record.bounds
    let reach: CGFloat = 16
    guard [bounds.minX, bounds.minY, bounds.width, bounds.height,
           field.minX, field.minY, field.width, field.height].allSatisfy(\.isFinite),
          record.layer == targetLayer || record.layer == Int(CGWindowLevelForKey(.floatingWindow)),
          bounds.width > 0, bounds.height > 0, field.width > 0, field.height > 0,
          targetBounds.insetBy(dx: -1, dy: -1).contains(field),
          // A list never covers most of its window; another document window would.
          bounds.width <= targetBounds.width + 1, bounds.height <= targetBounds.height * 0.75
    else { return false }
    let overlap = min(bounds.maxX, field.maxX) - max(bounds.minX, field.minX)
    guard overlap >= min(bounds.width, field.width) / 2 else { return false }
    let hangsBelow = bounds.minY >= field.minY - reach && bounds.minY <= field.maxY + reach &&
        bounds.maxY > field.maxY
    let opensAbove = bounds.maxY >= field.minY - reach && bounds.maxY <= field.maxY + reach &&
        bounds.minY < field.minY
    return hangsBelow || opensAbove
}

/// This app's suggestion lists hanging from the focused field, judged against the target's own live
/// record. Without that record or a focused field nothing is waived.
func attachedSuggestionPopupRecords(
    _ records: [VisibleWindowRecord], targetPID: pid_t, targetWindowID: CGWindowID, targetBounds: CGRect,
    focusedField: CGRect?
) -> [VisibleWindowRecord] {
    guard let focusedField else { return [] }
    let selected = records.filter { $0.pid == targetPID && $0.windowID == targetWindowID }
    guard selected.count == 1, let target = selected.first else { return [] }
    return records.filter {
        $0.pid == targetPID && $0.windowID != targetWindowID &&
            attachedSuggestionPopup($0, focusedField: focusedField, targetBounds: targetBounds,
                                    targetLayer: target.layer)
    }
}

/// Windows that may block exact binding: everything except this app's bottom-edge status strips and
/// the suggestion lists of `focusedField`, the target's focused text field.
func overlayCandidateRecords(
    _ records: [VisibleWindowRecord]?, targetPID: pid_t, targetWindowID: CGWindowID, targetBounds: CGRect,
    focusedField: CGRect? = nil
) -> [VisibleWindowRecord]? {
    guard let records else { return nil }
    let passive = appStatusStripRecords(records, targetPID: targetPID, targetWindowID: targetWindowID,
                                        targetBounds: targetBounds) +
        attachedSuggestionPopupRecords(records, targetPID: targetPID, targetWindowID: targetWindowID,
                                       targetBounds: targetBounds, focusedField: focusedField)
    let passiveIDs = Set(passive.map(\.windowID))
    return records.filter { !passiveIDs.contains($0.windowID) }
}

/// Only another on-screen window of the target app can be a suggestion list, so the focused field
/// is read only then.
func mayHaveSuggestionPopup(_ records: [VisibleWindowRecord], targetPID: pid_t, targetWindowID: CGWindowID) -> Bool {
    records.contains { $0.pid == targetPID && $0.windowID != targetWindowID }
}

/// The frame of the app's focused text field when it is not secure and lies in `root`, the exact
/// target window. An unreadable focus is no field.
func focusedTextFieldFrame(app: AXUIElement, root: AXUIElement, pid: pid_t, targetBounds: CGRect) -> CGRect? {
    let (error, value) = observationAXAttribute(app, kAXFocusedUIElementAttribute)
    guard error == .success, let focused = decodeAXElement(value) else { return nil }
    let role = AXNodeReader.stringAttribute(focused, kAXRoleAttribute)
    let subrole = AXNodeReader.stringAttribute(focused, kAXSubroleAttribute)
    guard role.status == .complete, subrole.status == .complete,
          let roleValue = role.value,
          [kAXTextFieldRole as String, kAXTextAreaRole as String, kAXComboBoxRole as String].contains(roleValue),
          !keyboardFocusIsExplicitlySecure(role: roleValue, subrole: subrole.value),
          let frame = AXNodeReader.frameAttribute(focused),
          targetBounds.insetBy(dx: -1, dy: -1).contains(frame),
          focusedElementBelongsToExactAXRoot(element: focused, root: root, pid: pid)
    else { return nil }
    return frame
}

/// Live rectangles over one window that bind but must not receive pointer input: this app's status
/// strips and tooltips and, while `root` holds the focused text field, that field's suggestion lists.
func livePassiveOverlayRegions(pid: pid_t, windowID: CGWindowID, root: AXUIElement?) -> [CGRect] {
    let records = systemVisibleWindowRecords()
    guard let target = records.first(where: { $0.pid == pid && $0.windowID == windowID }) else { return [] }
    let others = mayHaveSuggestionPopup(records, targetPID: pid, targetWindowID: windowID)
    let app = AXUIElementCreateApplication(pid)
    let field = others ? root.flatMap {
        focusedTextFieldFrame(app: app, root: $0, pid: pid, targetBounds: target.bounds)
    } : nil
    let helpTags = others ? axHelpTagFrames(completeObservedAXWindows(app) ?? []) : []
    return (appStatusStripRecords(records, targetPID: pid, targetWindowID: windowID, targetBounds: target.bounds) +
        attachedSuggestionPopupRecords(records, targetPID: pid, targetWindowID: windowID,
                                       targetBounds: target.bounds, focusedField: field)).map(\.bounds) + helpTags
}

/// A tooltip is an AX window of its app with the help-tag role (live Edge and Outlook: 18 pt tall on
/// CG layer 103, shown while the pointer rests on a control). It never takes focus or input, so it
/// does not block binding; pointer input inside it is refused.
func axHelpTagRole(_ role: BoundedAXStringResult) -> Bool {
    role.status == .complete && role.value == kAXHelpTagRole as String
}

func axHelpTagFrames(_ windows: [AXUIElement]) -> [CGRect] {
    windows.compactMap { window in
        axHelpTagRole(AXNodeReader.stringAttribute(window, kAXRoleAttribute)) ? AXNodeReader.frameAttribute(window) : nil
    }
}

func matchesAnyFrame(_ bounds: CGRect, _ frames: [CGRect]) -> Bool {
    frames.contains { screenCaptureBoundsMatchAXBounds(screenCapture: bounds, accessibility: $0) }
}

/// Live suggestion lists of the focused field in `root`, for telling the observer they are open.
func liveSuggestionPopupRegions(pid: pid_t, windowID: CGWindowID, root: AXUIElement) -> [CGRect] {
    let records = systemVisibleWindowRecords()
    guard let target = records.first(where: { $0.pid == pid && $0.windowID == windowID }),
          mayHaveSuggestionPopup(records, targetPID: pid, targetWindowID: windowID),
          let field = focusedTextFieldFrame(app: AXUIElementCreateApplication(pid), root: root, pid: pid,
                                            targetBounds: target.bounds)
    else { return [] }
    return attachedSuggestionPopupRecords(records, targetPID: pid, targetWindowID: windowID,
                                          targetBounds: target.bounds, focusedField: field).map(\.bounds)
}

func windowIsProvablyBehind(
    candidateLayer: Int?, candidateOrder: Int?, selectedLayer: Int?, selectedOrder: Int?
) -> Bool {
    guard let candidateLayer, let selectedLayer,
          let candidateOrder, let selectedOrder,
          candidateOrder >= 0, selectedOrder >= 0,
          candidateOrder != .max, selectedOrder != .max
    else { return false }
    if candidateLayer != selectedLayer { return candidateLayer < selectedLayer }
    return candidateOrder > selectedOrder
}

enum PopupSingletonVisibilityProof: String, Equatable {
    case proven
    case inventoryUnavailable
    case invalidTarget
    case targetNotUnique
    case targetIdentityMismatch
    case targetNotVisible
    case targetOrderingUnknown
    case candidateBoundsUnknown
    case intersectingWindowNotBehind
}

private func popupSingletonBoundsAreFinite(_ bounds: CGRect) -> Bool {
    [bounds.minX, bounds.minY, bounds.maxX, bounds.maxY,
     bounds.width, bounds.height].allSatisfy(\.isFinite) &&
        bounds.width >= 0 && bounds.height >= 0
}

/// Metadata-only ordering diagnostic. The observation path now requires the
/// target-rectangle compositor pixel proof; this result grants no authority.
func popupSingletonVisibilityProof(
    targetPID: pid_t,
    targetWindowID: CGWindowID,
    targetBounds: CGRect,
    records: [VisibleWindowRecord]?
) -> PopupSingletonVisibilityProof {
    guard targetPID > 0, targetWindowID > 0,
          popupSingletonBoundsAreFinite(targetBounds),
          targetBounds.width > 0, targetBounds.height > 0
    else { return .invalidTarget }
    guard let records else { return .inventoryUnavailable }
    let selectedMatches = records.filter { $0.windowID == targetWindowID }
    guard selectedMatches.count == 1, let selected = selectedMatches.first else {
        return .targetNotUnique
    }
    guard selected.pid == targetPID, selected.bounds == targetBounds else {
        return .targetIdentityMismatch
    }
    guard selected.alpha.isFinite, selected.alpha > 0 else { return .targetNotVisible }
    guard selected.zOrder >= 0, selected.zOrder != .max else {
        return .targetOrderingUnknown
    }
    for candidate in records where candidate.windowID != targetWindowID {
        guard popupSingletonBoundsAreFinite(candidate.bounds) else {
            return .candidateBoundsUnknown
        }
        let overlap = candidate.bounds.intersection(targetBounds)
        guard !overlap.isNull, overlap.width > 0, overlap.height > 0 else { continue }
        guard windowIsProvablyBehind(
            candidateLayer: candidate.layer, candidateOrder: candidate.zOrder,
            selectedLayer: selected.layer, selectedOrder: selected.zOrder
        ) else { return .intersectingWindowNotBehind }
    }
    return .proven
}

func popupSingletonAXRoleAllowed(
    role: BoundedAXStringResult,
    subrole: BoundedAXStringResult
) -> Bool {
    role.status == .complete && role.value == kAXWindowRole as String &&
        subrole.status == .complete &&
        (subrole.value == "AXDialog" || subrole.value == "AXUnknown")
}

private func exactAXWindowHasPopupCaptureRole(_ element: AXUIElement) -> Bool {
    let role = AXNodeReader.stringAttribute(element, kAXRoleAttribute)
    let subrole = AXNodeReader.stringAttribute(element, kAXSubroleAttribute)
    return popupSingletonAXRoleAllowed(role: role, subrole: subrole)
}

/// A visual proof for the pointer gate's covering-window case. It only answers
/// whether the popup's exact pixels are visible now; the caller must still
/// require a fresh system-wide AX hit before dispatching a mouse event.
func popupPointerCompositorRegionMatches(target: WindowTarget) -> Bool {
    guard target.interactionMode == .foregroundTakeover,
          target.pid > 0, target.windowID > 0,
          let element = target.axElement,
          let identity = target.axIdentity,
          CFHash(element) == identity,
          let axBounds = AXNodeReader.frameAttribute(element),
          screenCaptureBoundsMatchAXBounds(
              screenCapture: target.bounds, accessibility: axBounds
          ),
          liveFrontmostPID() == target.pid else { return false }
    var axPID: pid_t = 0
    guard AXUIElementGetPid(element, &axPID) == .success,
          axPID == target.pid else { return false }
    if let axWindowID = observedAXWindowID(element),
       axWindowID != target.windowID { return false }
    do {
        let timeout = try AXObservationBudget.current?.phaseTimeout(maximum: 3) ?? 3
        let content = try waitForAsync(timeout: timeout) {
            try await SCShareableContent.excludingDesktopWindows(
                true, onScreenWindowsOnly: true
            )
        }
        let matches = content.windows.filter { $0.windowID == target.windowID }
        guard matches.count == 1,
              matches[0].owningApplication?.processID == target.pid,
              screenCaptureBoundsMatchAXBounds(
                  screenCapture: matches[0].frame, accessibility: target.bounds
              ) else { return false }
        let expected = PopupFilteredWindowIdentity(
            windowID: target.windowID, pid: target.pid, frame: matches[0].frame
        )
        _ = try captureVerifiedPopupWithSingletonDisplayFilter(
            window: matches[0], expected: expected,
            availableWindows: content.windows,
            displays: content.displays,
            requireCompositorProof: true
        )
        guard CFHash(element) == identity,
              let finalBounds = AXNodeReader.frameAttribute(element),
              screenCaptureBoundsMatchAXBounds(
                  screenCapture: expected.frame, accessibility: finalBounds
              ),
              liveFrontmostPID() == target.pid else { return false }
        if let axWindowID = observedAXWindowID(element),
           axWindowID != target.windowID { return false }
        return true
    } catch {
        return false
    }
}

func containedAppOwnedOverlayActive(
    targetPID: pid_t,
    targetWindowID: CGWindowID,
    targetBounds: CGRect,
    records: [VisibleWindowRecord]
) -> Bool {
    let selected = records.filter { $0.pid == targetPID && $0.windowID == targetWindowID }
    return records.contains { record in
        let behind = selected.count == 1 && approximatelyEqual(selected[0].bounds, targetBounds) &&
            windowIsProvablyBehind(candidateLayer: record.layer, candidateOrder: record.zOrder,
                selectedLayer: selected[0].layer, selectedOrder: selected[0].zOrder)
        return !behind && record.pid == targetPID &&
            record.windowID != targetWindowID &&
            record.alpha > 0 &&
            trustedFocusedWindow(
                expectedIdentity: 1,
                targetBounds: targetBounds,
                focusedIdentity: 2,
                focusedBounds: record.bounds
            )
    }
}

func trustedContainedDialogRoot(
    role: BoundedAXStringResult,
    subrole: BoundedAXStringResult,
    targetBounds: CGRect,
    candidateBounds: CGRect
) -> Bool {
    guard role.status == .complete,
          role.value == kAXWindowRole as String,
          subrole.status == .complete
    else { return false }
    return trustedFocusedWindow(
        expectedIdentity: 1,
        targetBounds: targetBounds,
        focusedIdentity: 2,
        focusedBounds: candidateBounds
    )
}

func trustedContainedDialogBody(
    role: BoundedAXStringResult,
    targetBounds: CGRect,
    candidateBounds: CGRect
) -> Bool {
    guard role.status == .complete,
          let value = role.value,
          value == "AXSheet" || value == "AXSplitGroup"
    else { return false }
    return trustedFocusedWindow(
        expectedIdentity: 1,
        targetBounds: targetBounds,
        focusedIdentity: 2,
        focusedBounds: candidateBounds
    )
}

func appendingApplicationMenuBar(window: AXNode, menuBar: AXNode) -> AXNode {
    AXNode(
        role: window.role,
        subrole: window.subrole,
        label: window.label,
        title: window.title,
        help: window.help,
        value: window.value,
        enabled: window.enabled,
        focused: window.focused,
        actions: window.actions,
        bounds: window.bounds,
        children: window.children + [menuBar],
        sourceElement: window.sourceElement
    )
}

func targetApplicationObservationTree(window: AXNode, menuBar: AXNode?) -> AXNode {
    guard let menuBar else { return window }
    return appendingApplicationMenuBar(window: window, menuBar: menuBar)
}

func trustedApplicationMenuBarOwner(
    targetPID: pid_t,
    applicationPID: pid_t,
    menuBarPID: pid_t
) -> Bool {
    targetPID > 0 && targetPID == applicationPID && targetPID == menuBarPID
}

func systemVisibleWindowInventory() -> [VisibleWindowRecord]? {
    guard let values = CGWindowListCopyWindowInfo(
        [.optionOnScreenOnly, .excludeDesktopElements],
        kCGNullWindowID
    ) as? [[String: Any]] else { return nil }
    var records: [VisibleWindowRecord] = []
    records.reserveCapacity(values.count)
    for (zOrder, value) in values.enumerated() {
        guard let pid = (value[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value,
              let windowID = (value[kCGWindowNumber as String] as? NSNumber)?.uint32Value,
              let layer = (value[kCGWindowLayer as String] as? NSNumber)?.intValue,
              let alpha = (value[kCGWindowAlpha as String] as? NSNumber)?.doubleValue,
              let rawBounds = value[kCGWindowBounds as String] as? NSDictionary,
              let bounds = CGRect(dictionaryRepresentation: rawBounds)
        else { return nil }
        records.append(VisibleWindowRecord(
            pid: pid,
            windowID: windowID,
            bounds: bounds,
            layer: layer,
            alpha: alpha,
            zOrder: zOrder
        ))
    }
    return records
}

func systemVisibleWindowRecords() -> [VisibleWindowRecord] {
    systemVisibleWindowInventory() ?? []
}

func trustedFocusedWindow(
    expectedIdentity: CFHashCode,
    targetBounds: CGRect,
    focusedIdentity: CFHashCode,
    focusedBounds: CGRect
) -> Bool {
    guard targetBounds.origin.x.isFinite,
          targetBounds.origin.y.isFinite,
          targetBounds.width.isFinite,
          targetBounds.height.isFinite,
          targetBounds.width > 0,
          targetBounds.height > 0,
          focusedBounds.origin.x.isFinite,
          focusedBounds.origin.y.isFinite,
          focusedBounds.width.isFinite,
          focusedBounds.height.isFinite,
          focusedBounds.width > 0,
          focusedBounds.height > 0
    else { return false }
    if expectedIdentity == focusedIdentity {
        return screenCaptureBoundsMatchAXBounds(
            screenCapture: targetBounds,
            accessibility: focusedBounds
        )
    }
    let strictlySmaller = focusedBounds.width < targetBounds.width - 1 ||
        focusedBounds.height < targetBounds.height - 1
    return strictlySmaller && targetBounds.insetBy(dx: -1, dy: -1).contains(focusedBounds)
}

func reusableFrontmostTarget(
    frontmostPID: pid_t,
    targetPID: pid_t,
    expectedIdentity: CFHashCode,
    targetBounds: CGRect,
    focusedIdentity: CFHashCode,
    focusedBounds: CGRect
) -> Bool {
    frontmostPID == targetPID && trustedFocusedWindow(
        expectedIdentity: expectedIdentity,
        targetBounds: targetBounds,
        focusedIdentity: focusedIdentity,
        focusedBounds: focusedBounds
    )
}

func trustedFocusedTransition(
    expectedIdentity: CFHashCode,
    targetBounds: CGRect,
    beforeIdentity: CFHashCode,
    beforeBounds: CGRect,
    afterIdentity: CFHashCode,
    afterBounds: CGRect
) -> Bool {
    beforeIdentity == afterIdentity &&
    approximatelyEqual(beforeBounds, afterBounds) &&
    trustedFocusedWindow(
        expectedIdentity: expectedIdentity,
        targetBounds: targetBounds,
        focusedIdentity: beforeIdentity,
        focusedBounds: beforeBounds
    ) && trustedFocusedWindow(
        expectedIdentity: expectedIdentity,
        targetBounds: targetBounds,
        focusedIdentity: afterIdentity,
        focusedBounds: afterBounds
    )
}

func focusedObservationMaximumDepth(
    preference: FocusedRootPreference,
    rootSubrole: String? = nil,
    rootIsExpectedWindow: Bool = false
) -> Int {
    if preference == .containedOverlay && !rootIsExpectedWindow { return 2 }
    if preference == .containedOverlay && rootIsExpectedWindow { return 5 }
    return rootSubrole == "AXDialog" ? 5 : maximumAXDepth
}

func trustedFocusedElement(
    role: BoundedAXStringResult,
    subrole: BoundedAXStringResult,
    enabled: Bool?,
    targetBounds: CGRect,
    focusedBounds: CGRect
) -> Bool {
    guard role.status == .complete,
          let roleValue = role.value,
          roleValue == kAXTextFieldRole as String || roleValue == kAXTextAreaRole as String,
          subrole.status == .complete,
          !secureActionIdentity(role: role, subrole: subrole),
          enabled != false,
          targetBounds.origin.x.isFinite,
          targetBounds.origin.y.isFinite,
          targetBounds.width.isFinite,
          targetBounds.height.isFinite,
          targetBounds.width > 0,
          targetBounds.height > 0,
          focusedBounds.origin.x.isFinite,
          focusedBounds.origin.y.isFinite,
          focusedBounds.width.isFinite,
          focusedBounds.height.isFinite,
          focusedBounds.width > 0,
          focusedBounds.height > 0
    else { return false }
    let strictlySmaller = focusedBounds.width < targetBounds.width - 1 ||
        focusedBounds.height < targetBounds.height - 1
    return strictlySmaller && targetBounds.insetBy(dx: -1, dy: -1).contains(focusedBounds)
}

func normalizedDocumentPath(_ value: String) -> String? {
    if value.hasPrefix("/") {
        return URL(fileURLWithPath: value).standardizedFileURL.path
    }
    guard let url = URL(string: value), url.isFileURL else { return nil }
    return url.standardizedFileURL.path
}

func prepareSnapshotForPublication<T>(
    buildAX: () throws -> T,
    publishArtifact: () throws -> Void
) throws -> T {
    let tree = try buildAX()
    try publishArtifact()
    return tree
}

func decodeAXElement(_ value: CFTypeRef?) -> AXUIElement? {
    guard let value, CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
    return unsafeBitCast(value, to: AXUIElement.self)
}

private func approximatelyEqual(_ lhs: CGRect, _ rhs: CGRect) -> Bool {
    abs(lhs.origin.x - rhs.origin.x) <= 1 && abs(lhs.origin.y - rhs.origin.y) <= 1 && abs(lhs.width - rhs.width) <= 1 && abs(lhs.height - rhs.height) <= 1
}

func pointerSafeRegionBoundsMatch(_ current: CGRect, _ snapshot: CGRect) -> Bool {
    current == snapshot
}

private enum AsyncWaitError: Error { case timedOut }

private struct SystemExactWindowImageProvider: ExactWindowImageProviding {
    let primary: () throws -> CGImage
    let fallback: (CGWindowID) -> CGImage?

    func primaryImage() throws -> CGImage { try primary() }
    func fallbackImage(for windowID: CGWindowID) -> CGImage? { fallback(windowID) }
}

private func waitForAsync<T>(
    timeout: TimeInterval? = nil,
    _ operation: @escaping () async throws -> T
) throws -> T {
    let semaphore = DispatchSemaphore(value: 0)
    var result: Result<T, Error>!
    let task = Task {
        do { result = .success(try await operation()) }
        catch { result = .failure(error) }
        semaphore.signal()
    }
    if let timeout,
       semaphore.wait(timeout: .now() + timeout) == .timedOut
    {
        task.cancel()
        throw AsyncWaitError.timedOut
    }
    if timeout == nil { semaphore.wait() }
    return try result.get()
}
