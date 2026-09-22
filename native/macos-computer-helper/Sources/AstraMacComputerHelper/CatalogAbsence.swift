import ApplicationServices
import Foundation

/// Prove absence against the full WindowServer inventory, not the bounded UI
/// catalog. Only exposed opaque identities from this helper lifetime qualify.
struct CatalogWindowAbsenceTracker {
    private let capacity: Int
    private var order: [String] = []
    private var windows: [String: CGWindowID] = [:]
    private var reliable = true

    init(capacity: Int = 400) { self.capacity = max(1, min(capacity, 400)) }

    mutating func record(reference: String, windowID: CGWindowID) {
        guard !reference.isEmpty, reference.utf8.count <= 256, windowID > 0 else {
            reliable = false
            return
        }
        if let previous = windows[reference] {
            if previous != windowID { reliable = false }
            return
        }
        windows[reference] = windowID
        order.append(reference)
        while order.count > capacity { windows.removeValue(forKey: order.removeFirst()) }
    }

    func confirmedAbsent(windowIDs: Set<CGWindowID>?) -> [String] {
        guard reliable, let windowIDs, !windowIDs.isEmpty else { return [] }
        return Array(order.filter { reference in
            guard let windowID = windows[reference] else { return false }
            return !windowIDs.contains(windowID)
        }.suffix(200))
    }

    static func validatedInventory(_ rows: [[String: Any]]?) -> Set<CGWindowID>? {
        // A wholly empty WindowServer reply is not reliable evidence of a
        // vanished target (session/permission transitions can look empty).
        guard let rows, !rows.isEmpty, rows.count <= 16_384 else { return nil }
        var result = Set<CGWindowID>()
        for row in rows {
            guard let number = row[kCGWindowNumber as String] as? NSNumber,
                  CFGetTypeID(number) != CFBooleanGetTypeID() else { return nil }
            let value = number.doubleValue
            guard value.isFinite, value > 0, value <= Double(CGWindowID.max),
                  value.rounded(.towardZero) == value,
                  result.insert(CGWindowID(value)).inserted else { return nil }
        }
        return result
    }
}

func fullWindowServerInventoryForAbsence() -> Set<CGWindowID>? {
    guard CGPreflightScreenCaptureAccess() else { return nil }
    // optionAll includes minimized/offscreen windows. Failed or malformed
    // enumeration is unknown, not the empty inventory.
    let rows = CGWindowListCopyWindowInfo(.optionAll, kCGNullWindowID) as? [[String: Any]]
    return CatalogWindowAbsenceTracker.validatedInventory(rows)
}
