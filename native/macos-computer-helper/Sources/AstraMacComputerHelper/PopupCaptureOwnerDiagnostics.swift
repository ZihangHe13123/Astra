@preconcurrency import ApplicationServices
import CoreGraphics
import Foundation
import ScreenCaptureKit

/// Read-only evidence for a popup whose capture appears to contain its parent.
/// These probes never select a capture source or grant input authority.
enum PopupCaptureOwnerDiagnostics {
    struct Window: Equatable {
        let pid: pid_t
        let windowID: CGWindowID
        let frame: CGRect
    }

    enum ChainStop: String {
        case application, missingParent, foreignPID, cycle, depthLimit
    }

    enum MatchKind: String {
        case exactWindowID = "exact_window_id"
        case metadataTitleFrame = "metadata_title_frame"
    }

    struct CandidateMatch: Equatable {
        let windowID: CGWindowID
        let kind: MatchKind

        var summary: String { "\(windowID):\(kind.rawValue)" }
    }

    static func containingCandidates(target: Window, windows: [Window]) -> [Window] {
        guard validFrame(target.frame) else { return [] }
        return windows.filter { candidate in
            candidate.pid == target.pid && candidate.windowID != target.windowID &&
                validFrame(candidate.frame) && candidate.frame.contains(target.frame) &&
                (candidate.frame.width > target.frame.width + 1 ||
                    candidate.frame.height > target.frame.height + 1)
        }
    }

    static func parentChain<Element>(
        from root: Element, ownerPID: pid_t, maximumDepth: Int = 8,
        same: (Element, Element) -> Bool,
        pid: (Element) -> pid_t?,
        parent: (Element) -> Element?,
        isApplication: (Element) -> Bool
    ) -> (parents: [Element], stop: ChainStop) {
        var seen = [root]
        var current = root
        for _ in 0..<max(0, min(maximumDepth, 8)) {
            guard let next = parent(current) else { return (Array(seen.dropFirst()), .missingParent) }
            guard pid(next) == ownerPID else { return (Array(seen.dropFirst()), .foreignPID) }
            guard !seen.contains(where: { same($0, next) }) else {
                return (Array(seen.dropFirst()), .cycle)
            }
            seen.append(next)
            if isApplication(next) { return (Array(seen.dropFirst()), .application) }
            current = next
        }
        return (Array(seen.dropFirst()), .depthLimit)
    }

    static func matchedCandidate(
        pid: pid_t, frame: CGRect, title: BoundedAXStringResult,
        axWindowID: CGWindowID?, screenWindows: [PIDScreenCaptureWindowObservation],
        candidates: [Window]
    ) -> CandidateMatch? {
        guard let match = matchingScreenWindow(
            pid: pid, bounds: frame, title: title,
            windowID: axWindowID, windows: screenWindows
        ), candidates.contains(where: { $0.windowID == match.windowID })
        else { return nil }
        return CandidateMatch(
            windowID: match.windowID,
            kind: axWindowID == nil ? .metadataTitleFrame : .exactWindowID
        )
    }

    static func ownerEvidence(
        candidateCount: Int, matches: [CandidateMatch],
        chainStop: ChainStop, budgetFailed: Bool, budgetExpired: Bool
    ) -> String {
        guard candidateCount == 1, chainStop == .application,
              !budgetFailed, !budgetExpired,
              matches.count == 1
        else { return "inconclusive" }
        return "positive_ax_parent"
    }

    static func recordIfNeeded(
        root: AXUIElement, targetPID: pid_t, targetWindow: SCWindow,
        windows: [SCWindow]
    ) {
        let target = Window(pid: targetPID, windowID: targetWindow.windowID, frame: targetWindow.frame)
        let candidates = containingCandidates(
            target: target,
            windows: windows.compactMap { candidate in
                guard candidate.isOnScreen, let pid = candidate.owningApplication?.processID else { return nil }
                return Window(pid: pid, windowID: candidate.windowID, frame: candidate.frame)
            }
        )
        let candidateIDs = candidates.prefix(8).map { String($0.windowID) }.joined(separator: ",")
        let prefix = "POPUP-CAPTURE-OWNER-DIAG pid=\(targetPID) target=\(target.windowID)" +
            " candidates=\(candidates.count) candidate_ids=\(candidateIDs)"
        guard !candidates.isEmpty else {
            logActionRejected(prefix + " status=no_containing_candidate owner_evidence=inconclusive")
            return
        }
        // This runs only after capture or geometry validation has failed. Keep
        // the diagnostic inside the remaining hard deadline; its own AX timeout
        // cannot poison the failed observation's safety budget.
        guard let hardBudget = AXObservationBudget.current,
              let remaining = try? hardBudget.phaseTimeout(maximum: 2),
              remaining > 0.5
        else {
            logActionRejected(prefix + " status=skipped_budget owner_evidence=inconclusive")
            return
        }
        let probeBudget = AXObservationBudget(duration: 0.4)
        let restore = probeBudget.install()
        defer { restore() }

        let screenWindows = windows.map {
            PIDScreenCaptureWindowObservation(
                pid: $0.owningApplication?.processID,
                windowID: $0.windowID,
                bounds: $0.frame,
                title: $0.title,
                isOnScreen: $0.isOnScreen
            )
        }
        func roleName(_ element: AXUIElement) -> String {
            let role = AXNodeReader.stringAttribute(element, kAXRoleAttribute)
            guard role.status == .complete else { return "unread" }
            switch role.value {
            case kAXWindowRole: return "window"
            case kAXSheetRole: return "sheet"
            case kAXApplicationRole: return "application"
            default: return "other"
            }
        }
        func matchedCandidateForAX(_ element: AXUIElement?) -> CandidateMatch? {
            guard let element, observedAXPID(element) == targetPID,
                  roleName(element) == "window",
                  let frame = AXNodeReader.frameAttribute(element)
            else { return nil }
            let id = observedAXWindowID(element)
            let name = id == nil ? accessibilityWindowName(element) :
                BoundedAXStringResult(value: nil, status: .complete)
            return matchedCandidate(
                pid: targetPID, frame: frame, title: name,
                axWindowID: id, screenWindows: screenWindows, candidates: candidates
            )
        }
        var parentReadError: AXError?
        let chain = parentChain(
            from: root, ownerPID: targetPID,
            same: { CFEqual($0, $1) }, pid: observedAXPID,
            parent: { element in
                let (error, value) = observationAXAttribute(element, kAXParentAttribute)
                if error != .success { parentReadError = error }
                return error == .success ? decodeAXElement(value) : nil
            },
            isApplication: { roleName($0) == "application" }
        )
        let chainRoles = chain.parents.map(roleName).joined(separator: ",")
        let ancestorMatches = chain.parents.compactMap(matchedCandidateForAX)
        let (windowError, windowValue) = observationAXAttribute(root, kAXWindowAttribute)
        let axWindow = windowError == .success ? decodeAXElement(windowValue) : nil
        let (topError, topValue) = observationAXAttribute(root, kAXTopLevelUIElementAttribute)
        let topLevel = topError == .success ? decodeAXElement(topValue) : nil
        func relation(_ element: AXUIElement?) -> String {
            guard let element else { return "unread" }
            if CFEqual(element, root) { return "self" }
            return chain.parents.contains(where: { CFEqual($0, element) }) ? "ancestor" : "other"
        }
        let axWindowMatch = matchedCandidateForAX(axWindow)
        let topLevelMatch = matchedCandidateForAX(topLevel)
        let ancestorSummary = ancestorMatches.prefix(8).map(\.summary).joined(separator: ",")
        let partial = probeBudget.expired || probeBudget.failed ||
            chain.stop != .application || windowError != .success || topError != .success
        let evidence = ownerEvidence(
            candidateCount: candidates.count, matches: ancestorMatches,
            chainStop: chain.stop, budgetFailed: probeBudget.failed,
            budgetExpired: probeBudget.expired
        )
        logActionRejected(
            prefix + " status=\(partial ? "partial" : "complete") owner_evidence=\(evidence)" +
                " chain=\(chainRoles) stop=\(chain.stop.rawValue)" +
                " parent_error=\(parentReadError.map { String($0.rawValue) } ?? "none")" +
                " ancestor_candidates=\(ancestorSummary)" +
                " ax_window=\(windowError.rawValue):\(relation(axWindow)):\(axWindowMatch?.summary ?? "none")" +
                " top_level=\(topError.rawValue):\(relation(topLevel)):\(topLevelMatch?.summary ?? "none")" +
                " probe_expired=\(probeBudget.expired) probe_failed=\(probeBudget.failed)"
        )
    }

    private static func validFrame(_ frame: CGRect) -> Bool {
        [frame.minX, frame.minY, frame.width, frame.height].allSatisfy(\.isFinite) &&
            frame.width > 1 && frame.height > 1
    }
}
