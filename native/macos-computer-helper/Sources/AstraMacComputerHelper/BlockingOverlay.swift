import ApplicationServices
import Foundation

/// Descriptive evidence only. Window numbers and dimensions never confer action authority.
struct BlockingOverlayWindow {
    let windowID: CGWindowID?
    let bounds: CGRect?
    let layer: Int?
    let title: String?
    let isModal: Bool?
    let source: String

    var json: JSONValue {
        var object: [String: JSONValue] = ["source": .string(source)]
        if let windowID { object["window_id"] = .number(Double(windowID)) }
        if let bounds, bounds.width.isFinite, bounds.height.isFinite,
           (0.5...32768).contains(bounds.width), (0.5...32768).contains(bounds.height) {
            object["width_pt"] = .number(bounds.width.rounded())
            object["height_pt"] = .number(bounds.height.rounded())
        }
        if let layer { object["layer"] = .number(Double(layer)) }
        if let title, !title.isEmpty { object["title"] = .string(String(title.prefix(160))) }
        if let isModal { object["modal"] = .bool(isModal) }
        return .object(object)
    }
}

struct ContainedBlockingElement {
    let element: AXUIElement
    let window: BlockingOverlayWindow
}

struct BlockingOverlay: Error {
    let appRef: String
    let windowRef: String
    let pid: pid_t
    let parentID: CGWindowID
    let application: String?
    let ax: ContainedAXOverlayScan
    let windows: [BlockingOverlayWindow]
    let visibleCount: Int
    let contained: ContainedBlockingElement?
    var png: Data?
    var imageStatus: String

    var result: JSONValue {
        var overlay: [String: JSONValue] = [
            "app_ref": .string(appRef), "window_ref": .string(windowRef),
            "pid": .number(Double(pid)), "ax_scan": .string(ax.rawValue),
            "visible_count": .number(Double(visibleCount)),
            "windows": .array(windows.map(\.json)), "image_status": .string(imageStatus)
        ]
        if let application, !application.isEmpty { overlay["app_name"] = .string(String(application.prefix(128))) }
        var result: [String: JSONValue] = ["blocking_overlay": .object(overlay)]
        if let png { result["overlay_png"] = .string(png.base64EncodedString()) }
        return .object(result)
    }
}

func blockingOverlayEvidence(
    for target: WindowTarget, application: String?, ax: ContainedAXOverlayScan,
    contained: ContainedBlockingElement?, offenders: [VisibleWindowRecord],
    screens: [PIDScreenCaptureWindowObservation], axWindows: [TargetAXWindowRecord]
) -> BlockingOverlay {
    let visible = offenders.filter { $0.pid == target.pid && $0.windowID != target.windowID && $0.alpha > 0 }
    var windows: [BlockingOverlayWindow] = visible.prefix(8).map { record in
        let matches = screens.filter { $0.pid == target.pid && $0.windowID == record.windowID &&
            $0.isOnScreen && screenCaptureBoundsMatchAXBounds(screenCapture: $0.bounds, accessibility: record.bounds) }
        let axMatches = axWindows.filter { $0.windowID == record.windowID &&
            screenCaptureBoundsMatchAXBounds(screenCapture: record.bounds, accessibility: $0.bounds) }
        let child = contained?.window
        let childMatches = child?.windowID == record.windowID && child?.bounds == record.bounds
        return BlockingOverlayWindow(windowID: record.windowID, bounds: record.bounds, layer: record.layer,
            title: matches.count == 1 ? matches[0].title : nil,
            isModal: childMatches ? child?.isModal : axMatches.count == 1 ? axMatches[0].isModal : nil,
            source: childMatches || axMatches.count == 1 ? "window_server_and_ax" : "window_server")
    }
    if let child = contained?.window, !windows.contains(where: { $0.windowID != nil && $0.windowID == child.windowID && $0.bounds == child.bounds }) {
        windows.append(child)
    }
    return BlockingOverlay(appRef: target.appRef, windowRef: target.windowRef, pid: target.pid,
        parentID: target.windowID, application: application, ax: ax, windows: windows,
        visibleCount: visible.count, contained: contained,
        imageStatus: visible.isEmpty ? "no_visible_blocker" : "window_binding_unverified")
}
