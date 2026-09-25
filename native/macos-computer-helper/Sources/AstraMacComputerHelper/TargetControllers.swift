@preconcurrency import ApplicationServices
import AppKit
import CoreGraphics
import Darwin
import Foundation

private func observationFailure(_ reason: String) -> WindowObservationError {
    // Bounded stderr diagnostics for target_gone: the enum carries no
    // payload, and the agent drains the helper's stderr in memory without
    // a readback surface. Append to a fixed file so a failing live
    // reproduction can be inspected directly. One line per throw site.
    let line = "[target_gone] \(reason)\n"
    FileHandle.standardError.write(Data(line.utf8))
    let diagnosticPath = "/tmp/astra-target-gone-diagnostics.log"
    if let handle = FileHandle(forWritingAtPath: diagnosticPath) {
        defer { try? handle.close() }
        handle.seekToEndOfFile()
        handle.write(Data(line.utf8))
    } else {
        try? line.write(toFile: diagnosticPath, atomically: true, encoding: .utf8)
    }
    return .targetGone
}

protocol TargetSelecting {
    func select(appRef: String, windowRef: String) throws -> WindowTarget
    func snapshotTargetState(_ target: WindowTarget) throws -> ActionTargetState
}

protocol ApplicationActivationControlling {
    func activate(_ target: WindowTarget) throws
    func activatePopupPointerOnly(_ target: WindowTarget, at point: CGPoint) throws
    func restore(pid: pid_t) throws
}

extension ApplicationActivationControlling {
    // Existing injected activators keep the strict path unless they explicitly
    // implement the narrower popup pointer authority.
    func activatePopupPointerOnly(_ target: WindowTarget, at _: CGPoint) throws {
        try activate(target)
    }
}

struct TargetAXWindowRecord {
    let windowID: CGWindowID?
    let bounds: CGRect
    let identity: CFHashCode
    let element: AXUIElement?
    let role: BoundedAXStringResult
    let subrole: BoundedAXStringResult
    let zOrder: Int?
    let layer: Int?
    let alpha: Double?
    let isModal: Bool?
    let isSharingIndicator: Bool

    init(
        windowID: CGWindowID?,
        bounds: CGRect,
        identity: CFHashCode,
        element: AXUIElement? = nil,
        role: BoundedAXStringResult = BoundedAXStringResult(value: nil, status: .failed),
        subrole: BoundedAXStringResult = BoundedAXStringResult(value: nil, status: .failed),
        zOrder: Int? = nil,
        layer: Int? = nil,
        alpha: Double? = nil,
        isModal: Bool? = nil,
        isSharingIndicator: Bool = false
    ) {
        self.windowID = windowID
        self.bounds = bounds
        self.identity = identity
        self.element = element
        self.role = role
        self.subrole = subrole
        self.zOrder = zOrder
        self.layer = layer
        self.alpha = alpha
        self.isModal = isModal
        self.isSharingIndicator = isSharingIndicator
    }
}

struct TargetCatalogRecord {
    let appRef: String
    let windowRef: String
    let pid: pid_t
    let windowID: CGWindowID?
    let bounds: CGRect
    let title: String
    let axWindows: [TargetAXWindowRecord]
    let containsUnselectedOverlay: Bool
    /// Every overlay is a small floating window of the target app, the shape of a caret
    /// indicator or tooltip, or a suggestion list proven moments ago, so it may vanish on its
    /// own. It still blocks until it does.
    let overlayMayBeTransient: Bool
    let siblingOrdering: BackgroundSiblingOrderingProof?
    /// Windows proven to be the focused text field's own suggestion lists; they bind like the target.
    let suggestionPopupWindowIDs: Set<CGWindowID>
    /// Their live CG frames, for an AX window that has not mapped to a window number yet.
    let suggestionPopupFrames: [CGRect]

    init(
        appRef: String,
        windowRef: String,
        pid: pid_t,
        windowID: CGWindowID?,
        bounds: CGRect,
        title: String,
        axWindows: [TargetAXWindowRecord],
        containsUnselectedOverlay: Bool = false,
        overlayMayBeTransient: Bool = false,
        siblingOrdering: BackgroundSiblingOrderingProof? = nil,
        suggestionPopupWindowIDs: Set<CGWindowID> = [],
        suggestionPopupFrames: [CGRect] = []
    ) {
        self.overlayMayBeTransient = overlayMayBeTransient
        self.suggestionPopupWindowIDs = suggestionPopupWindowIDs
        self.suggestionPopupFrames = suggestionPopupFrames
        self.appRef = appRef
        self.windowRef = windowRef
        self.pid = pid
        self.windowID = windowID
        self.bounds = bounds
        self.title = title
        self.axWindows = axWindows
        self.containsUnselectedOverlay = containsUnselectedOverlay
        self.siblingOrdering = siblingOrdering
    }
}

protocol TargetCataloging {
    func record(appRef: String, windowRef: String) throws -> TargetCatalogRecord?
    func currentRecord(for target: WindowTarget) throws -> TargetCatalogRecord?
}

struct ClosureTargetCatalog: TargetCataloging {
    let recordProvider: (String, String) throws -> TargetCatalogRecord?
    let currentProvider: (WindowTarget) throws -> TargetCatalogRecord?

    func record(appRef: String, windowRef: String) throws -> TargetCatalogRecord? {
        try recordProvider(appRef, windowRef)
    }

    func currentRecord(for target: WindowTarget) throws -> TargetCatalogRecord? {
        try currentProvider(target)
    }
}

final class BackgroundTargetController: TargetSelecting {
    private let catalog: any TargetCataloging
    // Deliberately retained but never called. Injection gives tests a sentinel
    // proving background selection and observation cannot cross this boundary.
    private let activation: (any ApplicationActivationControlling)?
    private let transientOverlaySettleMilliseconds: Int
    private let transientOverlayPollMilliseconds: Int
    private let sleepMilliseconds: (Int) -> Void

    init(
        catalog: any TargetCataloging,
        activation: (any ApplicationActivationControlling)? = nil,
        transientOverlaySettleMilliseconds: Int = 2_000,
        transientOverlayPollMilliseconds: Int = 100,
        sleepMilliseconds: @escaping (Int) -> Void = { usleep(useconds_t(max(0, $0)) * 1_000) }
    ) {
        self.catalog = catalog
        self.activation = activation
        self.transientOverlaySettleMilliseconds = transientOverlaySettleMilliseconds
        self.transientOverlayPollMilliseconds = transientOverlayPollMilliseconds
        self.sleepMilliseconds = sleepMilliseconds
    }

    /// Waits out a small floating overlay of the target app (live: the input-source indicator
    /// macOS draws at the caret for ~1.5 s after a switch). Anything larger, AX-contained or
    /// still present after the settle window blocks exactly as before.
    private func recordAfterTransientOverlay(_ fetch: () throws -> TargetCatalogRecord?) throws -> TargetCatalogRecord? {
        var record = try fetch()
        var waited = 0
        while let current = record, current.containsUnselectedOverlay, current.overlayMayBeTransient,
              waited < transientOverlaySettleMilliseconds {
            sleepMilliseconds(transientOverlayPollMilliseconds)
            waited += transientOverlayPollMilliseconds
            record = try fetch()
        }
        if waited > 0 {
            logActionRejected("OVERLAY-TRANSIENT waited_ms=\(waited) cleared=\(record?.containsUnselectedOverlay == false)")
        }
        return record
    }

    func select(appRef: String, windowRef: String) throws -> WindowTarget {
        guard let record = try recordAfterTransientOverlay({ try catalog.record(appRef: appRef, windowRef: windowRef) }) else {
            throw observationFailure("no catalog record for app_ref=\(appRef) window_ref=\(windowRef)")
        }
        let selected = try exactSelectedAXWindow(record)
        guard let windowID = selected.windowID, let selectedElement = selected.element else {
            throw observationFailure("record resolved but no exact AX window: windowID=\(record.windowID.map(String.init) ?? "nil") axWindows=\(record.axWindows.count)")
        }
        return WindowTarget(
            appRef: record.appRef,
            windowRef: record.windowRef,
            pid: record.pid,
            windowID: windowID,
            bounds: record.bounds,
            title: record.title,
            axIdentity: selected.identity,
            axElement: selectedElement,
            interactionMode: .background
        )
    }

    func snapshotTargetState(_ target: WindowTarget) throws -> ActionTargetState {
        guard target.interactionMode == .background,
              let expectedIdentity = target.axIdentity,
              let expectedElement = target.axElement,
              let record = try recordAfterTransientOverlay({ try catalog.currentRecord(for: target) }),
              record.appRef == target.appRef,
              record.windowRef == target.windowRef,
              record.pid == target.pid,
              record.windowID == target.windowID,
              targetBoundsEqual(record.bounds, target.bounds)
        else {
            logActionRejected("observation_validation=background_target_mismatch")
            throw WindowObservationError.targetGone
        }
        let selected = try exactSelectedAXWindow(record)
        guard selected.identity == expectedIdentity,
              let selectedElement = selected.element,
              CFEqual(selectedElement, expectedElement)
        else {
            logActionRejected("observation_validation=background_ax_identity_mismatch")
            throw WindowObservationError.targetGone
        }
        return ActionTargetState(
            pid: record.pid,
            windowID: target.windowID,
            bounds: record.bounds,
            axIdentity: selected.identity,
            focusedAXIdentity: selected.identity,
            focusedAXBounds: selected.bounds,
            focusedRootPreference: .selectedWindow
        )
    }

    private func exactSelectedAXWindow(_ record: TargetCatalogRecord) throws -> TargetAXWindowRecord {
        guard record.appRef.unicodeScalars.count > 0,
              record.windowRef.unicodeScalars.count > 0,
              record.pid > 0,
              let windowID = record.windowID,
              validTargetBounds(record.bounds)
        else {
            throw observationFailure(
                "invalid record: pid=\(record.pid) windowID=\(record.windowID.map(String.init) ?? "nil") bounds=\(record.bounds) overlay=\(record.containsUnselectedOverlay) axWindows=\(record.axWindows.count)"
            )
        }

        guard !record.containsUnselectedOverlay else {
            logActionRejected("observation_validation=overlay_blocked")
            throw WindowObservationError.overlayBlocked
        }

        let exact = record.axWindows.filter {
            $0.windowID == windowID && screenCaptureBoundsMatchAXBounds(
                screenCapture: record.bounds,
                accessibility: $0.bounds
            )
        }
        guard exact.count == 1, let selected = exact.first, selected.identity != 0 else {
            logActionRejected("observation_validation=ax_window_unmatched candidates=\(exact.count)")
            throw WindowObservationError.axWindowUnmatched
        }

        // Only the exact selected window ID plus retained AX element is exempt.
        // Any other contained/equal candidate must prove it cannot obscure the
        // target; missing role or ordering evidence is itself ambiguous.
        let ambiguousContainedOverlay = record.axWindows.contains { candidate in
            guard candidate.identity != 0,
                  containedOrEqual(candidate.bounds, in: record.bounds)
            else { return false }
            if candidate.windowID == windowID,
               let candidateElement = candidate.element,
               let selectedElement = selected.element,
               CFEqual(candidateElement, selectedElement)
            {
                return false
            }
            if candidate.windowID != nil, candidate.isSharingIndicator,
               sharingIndicatorWithinTitlebar(candidate.bounds, targetBounds: record.bounds) {
                return false
            }
            // A tooltip never takes focus or input.
            if axHelpTagRole(candidate.role) { return false }
            // The focused field's suggestion list is an AX window too; the catalog proved it from
            // live focus and geometry.
            if let candidateWindowID = candidate.windowID,
               record.suggestionPopupWindowIDs.contains(candidateWindowID) {
                return false
            }
            // A hovered link's bottom-edge status strip is an AX window too (live Edge). Judge it
            // by its mapped live layer; an unmapped or unordered strip keeps blocking.
            if let candidateWindowID = candidate.windowID, let layer = candidate.layer,
               let selectedLayer = selected.layer,
               appStatusStripOverlay(
                   VisibleWindowRecord(pid: record.pid, windowID: candidateWindowID, bounds: candidate.bounds,
                                       layer: layer, alpha: candidate.alpha ?? 0, zOrder: candidate.zOrder ?? 0),
                   targetBounds: record.bounds, targetLayer: selectedLayer) {
                return false
            }
            guard candidate.windowID != nil else {
                if record.siblingOrdering?.covers(
                    targetPID: record.pid, targetWindowID: windowID, targetBounds: record.bounds,
                    selected: selected, sibling: candidate
                ) == true { return false }
                if record.suggestionPopupFrames.contains(where: { openingListFrame(candidate.bounds, matches: $0) }) {
                    return false
                }
                logActionRejected("observation_validation=background_unmapped_sibling")
                return true
            }
            // A separately mapped window behind the selected one cannot obscure
            // it. Dialogs additionally require an explicit nonmodal read; unknown
            // modality never inherits an ordinary window's exemption.
            let provenWindowRole = candidate.subrole.value == "AXStandardWindow" ||
                (candidate.subrole.value == "AXDialog" && candidate.isModal == false)
            if candidate.role.status == .complete, candidate.role.value == "AXWindow",
               candidate.subrole.status == .complete, provenWindowRole,
               windowIsProvablyBehind(candidateLayer: candidate.layer, candidateOrder: candidate.zOrder,
                   selectedLayer: selected.layer, selectedOrder: selected.zOrder) {
                return false
            }
            let roleUncertain = candidate.role.status != .complete ||
                candidate.role.value == nil ||
                candidate.subrole.status != .complete
            let overlayRole = candidate.role.value == "AXSheet" ||
                candidate.role.value == kAXWindowRole as String ||
                candidate.subrole.value == "AXDialog"
            let ambiguous = roleUncertain || overlayRole || mayOcclude(candidate, selected: selected)
            if ambiguous {
                logActionRejected("background_sibling ordinary=\(candidate.role.value == "AXWindow" && candidate.subrole.value == "AXStandardWindow") role_complete=\(!roleUncertain) ordering_complete=\(candidate.layer != nil && candidate.zOrder != nil && selected.layer != nil && selected.zOrder != nil)")
            }
            return ambiguous
        }
        guard !ambiguousContainedOverlay else {
            logActionRejected("observation_validation=background_ambiguous_overlay")
            throw WindowObservationError.overlayBlocked
        }
        return selected
    }
}

struct BackgroundSnapshotSession {
    private let targetWindowID: CGWindowID
    private let exactAXElement: AXUIElement
    private let sentinelPID: pid_t

    init(
        target: WindowTarget,
        exactAXElement: AXUIElement?,
        frontmostPID: pid_t?
    ) throws {
        guard let frontmostPID else {
            throw WindowObservationError.targetNotFrontmost
        }
        guard target.interactionMode == .background,
              let retained = target.axElement,
              let exactAXElement,
              CFEqual(retained, exactAXElement)
        else { throw WindowObservationError.targetGone }
        targetWindowID = target.windowID
        self.exactAXElement = exactAXElement
        sentinelPID = frontmostPID
    }

    func capture<T>(_ operation: (CGWindowID) throws -> T) rethrows -> T {
        try operation(targetWindowID)
    }

    func serialize<T>(_ operation: (AXUIElement) throws -> T) rethrows -> T {
        try operation(exactAXElement)
    }

    func verifyFrontmost(_ currentPID: pid_t?) throws {
        guard let currentPID, currentPID == sentinelPID else {
            logActionRejected("observation_validation=background_frontmost_changed sentinel=\(sentinelPID)"
                + " current=\(currentPID.map(String.init) ?? "unknown")")
            throw WindowObservationError.targetNotFrontmost
        }
    }
}

struct ApplicationLaunchIdentity {
    let bundleURL: URL?
    let localizedName: String?
}

struct ProcessLaunchInvocation: Equatable {
    let executable: String
    let arguments: [String]
}

protocol ApplicationProcessLaunching {
    func run(
        executable: String,
        arguments: [String],
        timeout: TimeInterval
    ) throws -> ApplicationProcessLaunchResult
}

enum ApplicationProcessLaunchResult: Equatable {
    case exited(Int32)
    case timedOut
}

protocol ApplicationManagedProcess: AnyObject {
    var isRunning: Bool { get }
    var terminationStatus: Int32 { get }
    func run(executable: URL, arguments: [String]) throws
    func waitUntilExit()
    func terminate()
    func forceTerminate()
}

private final class FoundationApplicationManagedProcess: ApplicationManagedProcess {
    private let process = Process()

    var isRunning: Bool { process.isRunning }
    var terminationStatus: Int32 { process.terminationStatus }

    func run(executable: URL, arguments: [String]) throws {
        process.executableURL = executable
        process.arguments = arguments
        try process.run()
    }

    func waitUntilExit() {
        process.waitUntilExit()
    }

    func terminate() {
        if process.isRunning { process.terminate() }
    }

    func forceTerminate() {
        if process.isRunning {
            _ = Darwin.kill(process.processIdentifier, SIGKILL)
        }
    }
}

protocol ApplicationActivationRuntime {
    func applicationIdentity(pid: pid_t) -> ApplicationLaunchIdentity?
    func unhide(pid: pid_t)
    func setFocusedWindow(_ target: WindowTarget) -> Bool
    func raiseWindow(_ target: WindowTarget) -> Bool
    func frontmostPID() -> pid_t?
    func focusedWindowMatches(_ target: WindowTarget) -> Bool
    func popupPointerWindowMatches(_ target: WindowTarget, at point: CGPoint) -> Bool
}

extension ApplicationActivationRuntime {
    func popupPointerWindowMatches(_: WindowTarget, at _: CGPoint) -> Bool { false }
}

/// The focused AX window may remain the parent while a popup handles pointer
/// input. This proof is intentionally restricted to a visible, raised dialog
/// and one point on that exact window; it never authorizes keyboard input.
enum PopupPointerProofFailureStage: String, CaseIterable {
    case invalidTarget = "invalid_target"
    case notFrontmost = "not_frontmost"
    case axRole = "ax_role"
    case selectedWindowCount = "selected_window_count"
    case selectedWindowIdentity = "selected_window_identity"
    case selectedWindowVisibility = "selected_window_visibility"
    case coveringWindow = "covering_window"
    case exactAXWindow = "exact_ax_window"
    case windowInventory = "window_inventory"
    case axRoleRead = "ax_role_read"
    case hitInvalidInput = "hit_invalid_input"
    case hitSystemTimeout = "hit_system_timeout"
    case hitRead = "hit_read"
    case hitPID = "hit_pid"
    case hitAncestorTimeout = "hit_ancestor_timeout"
    case hitWindowMismatch = "hit_window_mismatch"
    case hitParentRead = "hit_parent_read"
    case hitDepth = "hit_depth"
}

/// Bounded numeric geometry only. No CG window names or AX content is read.
func popupPointerCoveringWindowDiagnostic(
    selected: VisibleWindowRecord, candidate: VisibleWindowRecord
) -> String {
    func scalar(_ value: Double, limit: Double) -> String {
        guard value.isFinite, abs(value) <= limit else { return "unavailable" }
        return String(format: "%.2f", value)
    }
    return "selected_layer=\(selected.layer) selected_z=\(selected.zOrder)"
        + " selected_alpha=\(scalar(selected.alpha, limit: 10))"
        + " candidate_pid=\(candidate.pid) candidate_windowID=\(candidate.windowID)"
        + " candidate_layer=\(candidate.layer) candidate_z=\(candidate.zOrder)"
        + " candidate_alpha=\(scalar(candidate.alpha, limit: 10))"
        + " dx=\(scalar(Double(candidate.bounds.minX - selected.bounds.minX), limit: 100_000))"
        + " dy=\(scalar(Double(candidate.bounds.minY - selected.bounds.minY), limit: 100_000))"
        + " w=\(scalar(Double(candidate.bounds.width), limit: 100_000))"
        + " h=\(scalar(Double(candidate.bounds.height), limit: 100_000))"
}

func popupPointerWindowTopmostFailure(
    targetPID: pid_t,
    frontmostPID: pid_t?,
    windowID: CGWindowID,
    bounds: CGRect,
    point: CGPoint,
    role: String?,
    subrole: String?,
    visibleWindows: [VisibleWindowRecord],
    onCoveringWindow: ((VisibleWindowRecord, VisibleWindowRecord) -> Void)? = nil
) -> PopupPointerProofFailureStage? {
    guard targetPID > 0, windowID > 0,
          validTargetBounds(bounds), point.x.isFinite, point.y.isFinite,
          point.x > bounds.minX, point.x < bounds.maxX,
          point.y > bounds.minY, point.y < bounds.maxY
    else { return .invalidTarget }
    guard frontmostPID == targetPID else { return .notFrontmost }
    guard role == "AXWindow", subrole == "AXDialog" else { return .axRole }
    let matching = visibleWindows.filter { $0.windowID == windowID }
    guard matching.count == 1, let selected = matching.first else { return .selectedWindowCount }
    guard selected.pid == targetPID, selected.bounds == bounds else { return .selectedWindowIdentity }
    guard
          selected.layer > 0, selected.alpha >= 0.99,
          selected.zOrder >= 0, selected.zOrder != .max
    else { return .selectedWindowVisibility }
    if let covering = visibleWindows.first(where: { candidate in
        candidate.windowID != windowID && candidate.bounds.contains(point) &&
            !windowIsProvablyBehind(
                candidateLayer: candidate.layer, candidateOrder: candidate.zOrder,
                selectedLayer: selected.layer, selectedOrder: selected.zOrder
            )
    }) {
        onCoveringWindow?(selected, covering)
        return .coveringWindow
    }
    return nil
}

func popupPointerWindowIsTopmost(
    targetPID: pid_t,
    frontmostPID: pid_t?,
    windowID: CGWindowID,
    bounds: CGRect,
    point: CGPoint,
    role: String?,
    subrole: String?,
    visibleWindows: [VisibleWindowRecord]
) -> Bool {
    popupPointerWindowTopmostFailure(
        targetPID: targetPID, frontmostPID: frontmostPID, windowID: windowID,
        bounds: bounds, point: point, role: role, subrole: subrole,
        visibleWindows: visibleWindows
    ) == nil
}

/// A covering CG window may be a click-through compositor surface. Its
/// presence can be resolved only by fresh visual agreement for the exact
/// popup rectangle and an independent system-wide AX hit on that popup.
/// Every other geometry/authority failure remains final.
func popupPointerProofAllowsDispatch(
    geometryFailure: PopupPointerProofFailureStage?,
    visualAgreement: () -> Bool,
    exactAXHit: () -> Bool,
    onGeometryFailure: (PopupPointerProofFailureStage) -> Void
) -> Bool {
    if let geometryFailure {
        guard geometryFailure == .coveringWindow,
              visualAgreement() else {
            onGeometryFailure(geometryFailure)
            return false
        }
    }
    return exactAXHit()
}

struct SystemApplicationProcessLauncher: ApplicationProcessLaunching {
    private let processFactory: () -> any ApplicationManagedProcess
    private let now: () -> Date
    private let sleep: (TimeInterval) -> Void
    private let pollInterval: TimeInterval

    init(
        processFactory: @escaping () -> any ApplicationManagedProcess = {
            FoundationApplicationManagedProcess()
        },
        now: @escaping () -> Date = Date.init,
        sleep: @escaping (TimeInterval) -> Void = Thread.sleep,
        pollInterval: TimeInterval = 0.01
    ) {
        self.processFactory = processFactory
        self.now = now
        self.sleep = sleep
        self.pollInterval = pollInterval
    }

    func run(
        executable: String,
        arguments: [String],
        timeout: TimeInterval
    ) throws -> ApplicationProcessLaunchResult {
        guard timeout > 0 else { return .timedOut }
        let process = processFactory()
        try process.run(executable: URL(fileURLWithPath: executable), arguments: arguments)
        let deadline = now().addingTimeInterval(timeout)
        while process.isRunning {
            let remaining = deadline.timeIntervalSince(now())
            guard remaining > 0 else {
                process.terminate()
                if process.isRunning { process.forceTerminate() }
                return .timedOut
            }
            sleep(min(pollInterval, remaining))
        }
        // macOS 26: isRunning can report false while the child is still in its
        // final teardown state; waitUntilExit is required so the child's side
        // effects (e.g. LaunchServices activation) are fully completed.
        process.waitUntilExit()
        return .exited(process.terminationStatus)
    }
}

struct SystemApplicationActivationRuntime: ApplicationActivationRuntime {
    private let diagnostic: (String) -> Void
    private let popupCompositorRegionMatches: (WindowTarget) -> Bool

    init(
        diagnostic: @escaping (String) -> Void = logActionRejected,
        popupCompositorRegionMatches: @escaping (WindowTarget) -> Bool = {
            popupPointerCompositorRegionMatches(target: $0)
        }
    ) {
        self.diagnostic = diagnostic
        self.popupCompositorRegionMatches = popupCompositorRegionMatches
    }

    func applicationIdentity(pid: pid_t) -> ApplicationLaunchIdentity? {
        guard let application = NSRunningApplication(processIdentifier: pid), !application.isTerminated else {
            return nil
        }
        return ApplicationLaunchIdentity(
            bundleURL: application.bundleURL,
            localizedName: application.localizedName
        )
    }

    func unhide(pid: pid_t) {
        NSRunningApplication(processIdentifier: pid)?.unhide()
    }

    func setFocusedWindow(_ target: WindowTarget) -> Bool {
        guard let window = exactWindow(target) else { return false }
        return AXUIElementSetAttributeValue(
            AXUIElementCreateApplication(target.pid),
            kAXFocusedWindowAttribute as CFString,
            window
        ) == .success
    }

    func raiseWindow(_ target: WindowTarget) -> Bool {
        guard let window = exactWindow(target) else { return false }
        if AXNodeReader.stringAttribute(window, kAXRoleAttribute).value == kAXSheetRole {
            // Activation has already brought the application forward. A sheet
            // has no independent AXRaise authority; never raise a fresh parent.
            return frontmostPID() == target.pid && focusBelongsToExactAXRoot(
                app: AXUIElementCreateApplication(target.pid), root: window
            )
        }
        return AXUIElementPerformAction(window, kAXRaiseAction as CFString) == .success
    }

    func frontmostPID() -> pid_t? {
        liveFrontmostPID()
    }

    func focusedWindowMatches(_ target: WindowTarget) -> Bool {
        let app = AXUIElementCreateApplication(target.pid)
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(
            app,
            kAXFocusedWindowAttribute as CFString,
            &value
        ) == .success,
        let focused = decodeAXElement(value),
        let expected = exactWindow(target)
        else { return false }
        if AXNodeReader.stringAttribute(expected, kAXRoleAttribute).value == kAXSheetRole {
            return focusBelongsToExactAXRoot(app: app, root: expected)
        }
        return CFEqual(focused, expected)
    }

    func popupPointerWindowMatches(_ target: WindowTarget, at point: CGPoint) -> Bool {
        func reject(_ stage: PopupPointerProofFailureStage, numbers: String? = nil) -> Bool {
            diagnostic("POPUP-POINTER-PROOF-FAIL stage=\(stage.rawValue) pid=\(target.pid) windowID=\(target.windowID)"
                + (numbers.map { " \($0)" } ?? ""))
            return false
        }
        guard let expected = exactWindow(target) else { return reject(.exactAXWindow) }
        guard let windows = systemVisibleWindowInventory() else { return reject(.windowInventory) }
        let role = AXNodeReader.stringAttribute(expected, kAXRoleAttribute)
        let subrole = AXNodeReader.stringAttribute(expected, kAXSubroleAttribute)
        guard role.status == .complete, subrole.status == .complete else { return reject(.axRoleRead) }
        var coveringNumbers: String?
        let geometryFailure = popupPointerWindowTopmostFailure(
            targetPID: target.pid, frontmostPID: frontmostPID(),
            windowID: target.windowID, bounds: target.bounds, point: point,
            role: role.value, subrole: subrole.value, visibleWindows: windows,
            onCoveringWindow: { selected, candidate in
                coveringNumbers = popupPointerCoveringWindowDiagnostic(selected: selected, candidate: candidate)
            }
        )
        return popupPointerProofAllowsDispatch(
            geometryFailure: geometryFailure,
            visualAgreement: { popupCompositorRegionMatches(target) },
            exactAXHit: {
                guard frontmostPID() == target.pid else { return reject(.notFrontmost) }
                guard let freshExpected = exactWindow(target) else { return reject(.exactAXWindow) }
                let freshRole = AXNodeReader.stringAttribute(freshExpected, kAXRoleAttribute)
                let freshSubrole = AXNodeReader.stringAttribute(freshExpected, kAXSubroleAttribute)
                guard freshRole.status == .complete, freshSubrole.status == .complete else {
                    return reject(.axRoleRead)
                }
                guard freshRole.value == "AXWindow", freshSubrole.value == "AXDialog" else {
                    return reject(.axRole)
                }
                return foregroundPointBelongsToWindow(point, pid: target.pid, window: freshExpected,
                    onFailure: { _ = reject($0) })
            },
            onGeometryFailure: { _ = reject($0, numbers: coveringNumbers) }
        )
    }

    private func exactWindow(_ target: WindowTarget) -> AXUIElement? {
        guard let expectedIdentity = target.axIdentity,
              let expectedElement = target.axElement
        else { return nil }
        let app = AXUIElementCreateApplication(target.pid)
        let matches = (completeObservedAXWindows(app) ?? []).filter { element in
            guard CFHash(element) == expectedIdentity,
                  CFEqual(element, expectedElement),
                  let bounds = AXNodeReader.frameAttribute(element),
                  screenCaptureBoundsMatchAXBounds(
                      screenCapture: target.bounds,
                      accessibility: bounds
                  )
            else { return false }
            // Title is mutable navigation metadata. The retained AX object,
            // exact identity and current bounds above bind this window even
            // when the previous click changed its title.
            return true
        }
        return matches.count == 1 ? matches[0] : nil
    }
}

final class LaunchServicesApplicationActivationController: ApplicationActivationControlling {
    private let runtime: any ApplicationActivationRuntime
    private let launcher: any ApplicationProcessLaunching
    private let now: () -> Date
    private let sleep: (TimeInterval) -> Void
    private let diagnostic: (String) -> Void
    private let timeout: TimeInterval
    private let retryInterval: TimeInterval
    private let activationSettleInterval: TimeInterval

    init(
        runtime: any ApplicationActivationRuntime = SystemApplicationActivationRuntime(),
        launcher: any ApplicationProcessLaunching = SystemApplicationProcessLauncher(),
        now: @escaping () -> Date = Date.init,
        sleep: @escaping (TimeInterval) -> Void = Thread.sleep,
        diagnostic: @escaping (String) -> Void = logActionRejected,
        timeout: TimeInterval = 2,
        retryInterval: TimeInterval = 0.05,
        activationSettleInterval: TimeInterval = 0.3
    ) {
        self.runtime = runtime
        self.launcher = launcher
        self.now = now
        self.sleep = sleep
        self.diagnostic = diagnostic
        self.timeout = timeout
        self.retryInterval = retryInterval
        self.activationSettleInterval = activationSettleInterval
    }

    func activate(_ target: WindowTarget) throws {
        guard target.interactionMode == .foregroundTakeover,
              target.axIdentity != nil,
              let identity = runtime.applicationIdentity(pid: target.pid),
              let arguments = launchArguments(identity)
        else { throw WindowObservationError.targetGone }
        runtime.unhide(pid: target.pid)
        let deadline = now().addingTimeInterval(timeout)
        var lastFrontmost: Bool?
        // Which application the last check saw in front: "unknown" when it could not tell.
        var lastFrontmostPID: pid_t??
        var lastSetFocused: Bool?
        var lastRaised: Bool?
        var lastFocusedMatch: Bool?
        var attempts = 0
        var launches = 0
        var launchFailures = 0
        func targetIsFrontmost() -> Bool {
            let pid = runtime.frontmostPID()
            lastFrontmostPID = .some(pid)
            return pid == target.pid
        }
        while now() < deadline {
            let frontmost = targetIsFrontmost()
            lastFrontmost = frontmost
            if !frontmost {
                launches += 1
                if launchOnce(arguments: arguments, deadline: deadline) {
                    guard waitForFrontmost(deadline: deadline, frontmost: targetIsFrontmost) else { continue }
                    lastFrontmost = true
                } else {
                    launchFailures += 1
                    waitUntilNextLaunch(deadline: deadline)
                    continue
                }
            }
            while now() < deadline {
                let currentlyFrontmost = targetIsFrontmost()
                lastFrontmost = currentlyFrontmost
                guard currentlyFrontmost else { break }
                attempts += 1
                let selected = runtime.setFocusedWindow(target)
                lastSetFocused = selected
                guard now() < deadline else { break }
                let raised = runtime.raiseWindow(target)
                lastRaised = raised
                guard now() < deadline else { break }
                // Preserve short-circuit verification: a failed set/raise must
                // not produce extra AX reads merely for diagnostics.
                var focusedMatch: Bool?
                if selected && raised {
                    let stillFrontmost = targetIsFrontmost()
                    lastFrontmost = stillFrontmost
                    if stillFrontmost { focusedMatch = runtime.focusedWindowMatches(target) }
                }
                lastFocusedMatch = focusedMatch
                if focusedMatch == true {
                    return
                }
                let retryDelay = min(retryInterval, deadline.timeIntervalSince(now()))
                if retryDelay > 0 { sleep(retryDelay) }
            }
        }
        func value(_ flag: Bool?) -> String {
            flag.map { $0 ? "true" : "false" } ?? "not_checked"
        }
        let seenFrontmost = lastFrontmostPID.map { $0.map(String.init) ?? "unknown" } ?? "not_checked"
        diagnostic(
            "ACTIVATE-FAIL pid=\(target.pid) windowID=\(target.windowID)"
                + " attempts=\(attempts) frontmost=\(value(lastFrontmost))"
                + " setFocusedWindow=\(value(lastSetFocused))"
                + " raiseWindow=\(value(lastRaised))"
                + " focusedWindowMatches=\(value(lastFocusedMatch))"
                + " lastFrontmostPID=\(seenFrontmost) launches=\(launches) launchFailures=\(launchFailures)"
        )
        throw WindowObservationError.targetNotFrontmost
    }

    func activatePopupPointerOnly(_ target: WindowTarget, at point: CGPoint) throws {
        // The popup is already visible inside the frontmost process. Launching,
        // raising, or setting AX focus can dismiss it; this path only observes.
        guard target.interactionMode == .foregroundTakeover,
              target.pid > 0, target.windowID > 0, target.axIdentity != nil,
              runtime.frontmostPID() == target.pid,
              runtime.popupPointerWindowMatches(target, at: point)
        else {
            diagnostic("POPUP-POINTER-ACTIVATE-FAIL pid=\(target.pid) windowID=\(target.windowID)")
            throw WindowObservationError.targetNotFrontmost
        }
        diagnostic("POPUP-POINTER-ACTIVATE pid=\(target.pid) windowID=\(target.windowID)")
    }

    func restore(pid: pid_t) throws {
        guard let identity = runtime.applicationIdentity(pid: pid),
              let arguments = launchArguments(identity)
        else { throw WindowObservationError.targetGone }
        runtime.unhide(pid: pid)
        let deadline = now().addingTimeInterval(timeout)
        while now() < deadline {
            if runtime.frontmostPID() == pid { return }
            if launchOnce(arguments: arguments, deadline: deadline) {
                if waitForFrontmost(deadline: deadline, frontmost: { runtime.frontmostPID() == pid }) { return }
            } else {
                waitUntilNextLaunch(deadline: deadline)
            }
        }
        throw WindowObservationError.targetNotFrontmost
    }

    private func launchOnce(arguments: [String], deadline: Date) -> Bool {
        let remaining = deadline.timeIntervalSince(now())
        guard remaining > 0 else { return false }
        let result = try? launcher.run(
            executable: "/usr/bin/open",
            arguments: arguments,
            timeout: remaining
        )
        guard now() < deadline, case .exited(0) = result else { return false }
        return true
    }

    private func waitForFrontmost(deadline: Date, frontmost: () -> Bool) -> Bool {
        let settleDeadline = min(
            deadline,
            now().addingTimeInterval(activationSettleInterval)
        )
        while now() < settleDeadline {
            if frontmost() { return true }
            let delay = min(retryInterval, settleDeadline.timeIntervalSince(now()))
            if delay > 0 { sleep(delay) }
        }
        return false
    }

    private func waitUntilNextLaunch(deadline: Date) {
        let delay = min(activationSettleInterval, deadline.timeIntervalSince(now()))
        if delay > 0 { sleep(delay) }
    }

    private func launchArguments(_ identity: ApplicationLaunchIdentity) -> [String]? {
        if let bundleURL = identity.bundleURL {
            return [bundleURL.path]
        }
        if let localizedName = identity.localizedName, !localizedName.isEmpty {
            return ["-a", localizedName]
        }
        return nil
    }
}

private func validTargetBounds(_ bounds: CGRect) -> Bool {
    bounds.origin.x.isFinite && bounds.origin.y.isFinite &&
        bounds.width.isFinite && bounds.height.isFinite &&
        bounds.width > 1 && bounds.height > 1
}

func screenCaptureBoundsMatchAXBounds(
    screenCapture: CGRect,
    accessibility: CGRect
) -> Bool {
    guard validTargetBounds(screenCapture), validTargetBounds(accessibility) else {
        return false
    }
    return abs(screenCapture.minX - accessibility.minX) <= 1 &&
        abs(screenCapture.minY - accessibility.minY) <= 1 &&
        abs(screenCapture.maxX - accessibility.maxX) <= 1 &&
        abs(screenCapture.maxY - accessibility.maxY) <= 1
}

private func targetBoundsEqual(_ lhs: CGRect, _ rhs: CGRect) -> Bool {
    abs(lhs.origin.x - rhs.origin.x) <= 1 &&
        abs(lhs.origin.y - rhs.origin.y) <= 1 &&
        abs(lhs.width - rhs.width) <= 1 &&
        abs(lhs.height - rhs.height) <= 1
}

private func strictlyContained(_ candidate: CGRect, in target: CGRect) -> Bool {
    guard validTargetBounds(candidate), target.insetBy(dx: -1, dy: -1).contains(candidate) else {
        return false
    }
    return candidate.width < target.width - 1 || candidate.height < target.height - 1
}

private func containedOrEqual(_ candidate: CGRect, in target: CGRect) -> Bool {
    targetBoundsEqual(candidate, target) || strictlyContained(candidate, in: target)
}

private func mayOcclude(
    _ candidate: TargetAXWindowRecord,
    selected: TargetAXWindowRecord
) -> Bool {
    guard candidate.alpha != 0 else { return false }
    guard let candidateLayer = candidate.layer,
          let selectedLayer = selected.layer
    else { return true }
    if candidateLayer != selectedLayer { return candidateLayer > selectedLayer }
    guard let candidateOrder = candidate.zOrder,
          let selectedOrder = selected.zOrder
    else { return true }
    return candidateOrder < selectedOrder
}
