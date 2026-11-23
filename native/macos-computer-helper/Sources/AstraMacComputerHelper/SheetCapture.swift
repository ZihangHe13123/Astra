import ApplicationServices
import Foundation
import ScreenCaptureKit

func ownedSheetRoot<Element>(
    sheet: Element, same: (Element, Element) -> Bool,
    isOwned: (Element) -> Bool, parent: (Element) -> Element?,
    role: (Element) -> String?, contained: (Element, Element) -> Bool,
    distinctSurface: (id: CGWindowID, backingID: (Element) -> CGWindowID?)? = nil
) -> Element? {
    guard isOwned(sheet), role(sheet) == kAXSheetRole else { return nil }
    var current = sheet
    var visited = [sheet]
    for _ in 0..<8 {
        guard let next = parent(current), isOwned(next),
              !visited.contains(where: { same($0, next) }), contained(next, current)
        else { return nil }
        if let distinctSurface {
            guard let ancestorID = distinctSurface.backingID(next), ancestorID != distinctSurface.id else { return nil }
        }
        if role(next) == kAXWindowRole { return next }
        guard role(next) == kAXSheetRole else { return nil }
        visited.append(next)
        current = next
    }
    return nil
}

func attachedSheetRoot(_ element: AXUIElement, pid: pid_t,
                       excludingBackingID: CGWindowID? = nil) -> AXUIElement? {
    ownedSheetRoot(sheet: element, same: { CFEqual($0, $1) },
        isOwned: { observedAXPID($0) == pid }, parent: observedAXParent,
        role: {
            let role = AXNodeReader.stringAttribute($0, kAXRoleAttribute)
            return role.status == .complete ? role.value : nil
        }, contained: { outer, inner in
            guard let outer = AXNodeReader.frameAttribute(outer),
                  let inner = AXNodeReader.frameAttribute(inner) else { return false }
            return outer.contains(inner)
        }, distinctSurface: excludingBackingID.map { ($0, observedAXBackingWindowID) })
}

func sheetCaptureWindow(element: AXUIElement, pid: pid_t, windows: [SCWindow]) -> SCWindow? {
    guard let root = attachedSheetRoot(element, pid: pid),
          let bounds = AXNodeReader.frameAttribute(root) else { return nil }
    let title = accessibilityWindowName(root)
    guard title.status == .complete else { return nil }
    let matches = windows.filter {
        $0.owningApplication?.processID == pid && $0.isOnScreen &&
            screenCaptureBoundsMatchAXBounds(screenCapture: $0.frame, accessibility: bounds) &&
            windowTitlesMatch(screenCaptureTitle: $0.title ?? "", accessibilityTitle: title.value ?? "")
    }
    return matches.count == 1 ? matches[0] : nil
}

func sheetImageCropRect(source: WindowGeometry, target: CGRect, imageWidth: Int, imageHeight: Int) throws -> CGRect {
    let actual = try resolvedWindowImageGeometry(requested: source, imageWidth: imageWidth, imageHeight: imageHeight)
    guard target.width > 0, target.height > 0, source.bounds.contains(target) else {
        throw WindowObservationError.axSerializationFailed
    }
    let scale = actual.backingScale
    let raw = CGRect(x: (target.minX - source.bounds.minX) * scale,
        y: (target.minY - source.bounds.minY) * scale,
        width: target.width * scale, height: target.height * scale)
    let rounded = CGRect(x: raw.minX.rounded(), y: raw.minY.rounded(),
        width: raw.width.rounded(), height: raw.height.rounded())
    guard [raw.minX, raw.minY, raw.width, raw.height].allSatisfy({ $0.isFinite }),
          abs(raw.minX - rounded.minX) < 0.01, abs(raw.minY - rounded.minY) < 0.01,
          abs(raw.width - rounded.width) < 0.01, abs(raw.height - rounded.height) < 0.01,
          CGRect(x: 0, y: 0, width: imageWidth, height: imageHeight).contains(rounded)
    else { throw WindowObservationError.axSerializationFailed }
    return rounded
}
