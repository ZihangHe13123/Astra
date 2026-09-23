import CoreGraphics
import Darwin
import Foundation
import ScreenCaptureKit

/// The caller's already bound target and the fresh SCK inventory use the same
/// identity. No screenshot returned here grants action authority by itself.
struct PopupFilteredWindowIdentity: Equatable {
    let windowID: CGWindowID
    let pid: pid_t
    let frame: CGRect
}

struct PopupFilteredDisplayIdentity: Equatable {
    let displayID: CGDirectDisplayID
    let frame: CGRect
}

struct PopupFilteredCapturePlan {
    let target: PopupFilteredWindowIdentity
    let display: PopupFilteredDisplayIdentity
    let scale: Int
    let displayPixelWidth: Int
    let displayPixelHeight: Int
    let targetPixelWidth: Int
    let targetPixelHeight: Int
    /// Display-local logical points. Both filters must request this rectangle;
    /// neither may allocate a whole-display screenshot for the proof.
    let sourceRect: CGRect
}

/// One numeric, content-free record for the guard that refused this candidate.
/// Successful captures are logged by the caller after its identity checks.
private func popupFilteredFailure(
    _ stage: String, windowID: CGWindowID, numericDetail: String = ""
) -> WindowObservationError {
    let suffix = numericDetail.isEmpty ? "" : " \(numericDetail)"
    logActionRejected(
        "CAPTURE-SOURCE singleton_filtered=rejected stage=\(stage)"
            + " windowID=\(windowID)\(suffix)"
    )
    return .captureFailed
}

private func popupRectIsFiniteAndPositive(_ rect: CGRect) -> Bool {
    [rect.minX, rect.minY, rect.width, rect.height].allSatisfy(\.isFinite) &&
        rect.width > 0 && rect.height > 0
}

private func popupPixelDimension(_ logical: CGFloat, scale: Int) -> Int? {
    let value = logical * CGFloat(scale)
    guard value.isFinite, value > 0, value <= 16_384,
          abs(value - value.rounded()) < 0.01 else { return nil }
    return Int(value.rounded())
}

private func popupPixelOrigin(_ logical: CGFloat, scale: Int) -> Int? {
    let value = logical * CGFloat(scale)
    guard value.isFinite, value >= 0, value <= 16_384,
          abs(value - value.rounded()) < 0.01 else { return nil }
    return Int(value.rounded())
}

/// The legacy exact-window source has already failed exact 1x/2x geometry.
/// This selection never broadens to an unrelated display or application.
func selectPopupFilteredDisplay(
    target: PopupFilteredWindowIdentity,
    availableWindows: [PopupFilteredWindowIdentity],
    displays: [PopupFilteredDisplayIdentity],
    rejectedExactWindowImage: CGImage? = nil
) throws -> PopupFilteredDisplayIdentity {
    guard target.windowID != 0, target.pid > 0 else {
        throw popupFilteredFailure("select.target_identity", windowID: target.windowID)
    }
    guard popupRectIsFiniteAndPositive(target.frame) else {
        throw popupFilteredFailure("select.target_frame", windowID: target.windowID)
    }
    if let rejectedExactWindowImage {
        guard rejectedExactWindowImage.width > 0, rejectedExactWindowImage.height > 0 else {
            throw popupFilteredFailure("select.rejected_image_size", windowID: target.windowID)
        }
        guard let logicalWidth = popupPixelDimension(target.frame.width, scale: 1),
              let logicalHeight = popupPixelDimension(target.frame.height, scale: 1),
              let retinaWidth = popupPixelDimension(target.frame.width, scale: 2),
              let retinaHeight = popupPixelDimension(target.frame.height, scale: 2)
        else { throw popupFilteredFailure("select.target_pixel_dimensions", windowID: target.windowID) }
        let logical = (logicalWidth, logicalHeight)
        let retina = (retinaWidth, retinaHeight)
        guard (rejectedExactWindowImage.width, rejectedExactWindowImage.height) != logical,
              (rejectedExactWindowImage.width, rejectedExactWindowImage.height) != retina
        else {
            throw popupFilteredFailure("select.rejected_image_exact_size", windowID: target.windowID,
                numericDetail: "width=\(rejectedExactWindowImage.width) height=\(rejectedExactWindowImage.height)")
        }
    }
    guard availableWindows.filter({ $0.windowID == target.windowID }).count == 1 else {
        throw popupFilteredFailure("select.target_inventory_unique", windowID: target.windowID)
    }
    guard availableWindows.contains(target) else {
        throw popupFilteredFailure("select.target_inventory_identity", windowID: target.windowID)
    }
    guard availableWindows.contains(where: {
        $0.windowID != target.windowID && $0.pid == target.pid &&
            popupRectIsFiniteAndPositive($0.frame) &&
            $0.frame.width > target.frame.width &&
            $0.frame.height > target.frame.height * 1.5 &&
            $0.frame.contains(target.frame)
    }) else {
        throw popupFilteredFailure("select.same_pid_parent", windowID: target.windowID)
    }
    let containing = displays.filter {
        popupRectIsFiniteAndPositive($0.frame) && $0.frame.contains(target.frame)
    }
    guard containing.count == 1, let display = containing.first else {
        throw popupFilteredFailure("select.containing_display_count", windowID: target.windowID,
            numericDetail: "count=\(containing.count)")
    }
    guard displays.filter({ $0.displayID == display.displayID }).count == 1 else {
        throw popupFilteredFailure("select.display_id_unique", windowID: target.windowID,
            numericDetail: "displayID=\(display.displayID)")
    }
    return display
}

/// Both the singleton and live-compositor display filters use the exact target
/// rectangle as their sourceRect. SCK's contentRect may use either the
/// display's global origin or local zero, so sourceRect is display-local.
func popupFilteredCapturePlan(
    target: PopupFilteredWindowIdentity,
    display: PopupFilteredDisplayIdentity,
    includedWindowIDs: [CGWindowID],
    filterContentRect: CGRect,
    pointPixelScale: CGFloat
) throws -> PopupFilteredCapturePlan {
    guard includedWindowIDs == [target.windowID] else {
        throw popupFilteredFailure("plan.singleton_filter", windowID: target.windowID,
            numericDetail: "includedCount=\(includedWindowIDs.count)")
    }
    guard popupRectIsFiniteAndPositive(display.frame) else {
        throw popupFilteredFailure("plan.display_frame", windowID: target.windowID)
    }
    guard display.frame.contains(target.frame) else {
        throw popupFilteredFailure("plan.display_contains_target", windowID: target.windowID)
    }
    guard popupRectIsFiniteAndPositive(filterContentRect) else {
        throw popupFilteredFailure("plan.content_rect", windowID: target.windowID)
    }
    guard filterContentRect.size == display.frame.size else {
        throw popupFilteredFailure("plan.content_rect_size", windowID: target.windowID,
            numericDetail: "filterWidth=\(filterContentRect.width) filterHeight=\(filterContentRect.height)"
                + " displayWidth=\(display.frame.width) displayHeight=\(display.frame.height)")
    }
    guard filterContentRect.origin == display.frame.origin || filterContentRect.origin == .zero else {
        throw popupFilteredFailure("plan.content_rect_origin", windowID: target.windowID,
            numericDetail: "filterX=\(filterContentRect.minX) filterY=\(filterContentRect.minY)"
                + " displayX=\(display.frame.minX) displayY=\(display.frame.minY)")
    }
    guard pointPixelScale.isFinite, pointPixelScale == 1 || pointPixelScale == 2 else {
        throw popupFilteredFailure("plan.point_pixel_scale", windowID: target.windowID,
            numericDetail: "scale=\(pointPixelScale)")
    }
    let scale = Int(pointPixelScale)
    guard let displayWidth = popupPixelDimension(display.frame.width, scale: scale),
          let displayHeight = popupPixelDimension(display.frame.height, scale: scale),
          let targetWidth = popupPixelDimension(target.frame.width, scale: scale),
          let targetHeight = popupPixelDimension(target.frame.height, scale: scale)
    else { throw popupFilteredFailure("plan.pixel_dimensions", windowID: target.windowID) }
    guard targetWidth <= displayWidth, targetHeight <= displayHeight else {
        throw popupFilteredFailure("plan.target_fits_display_pixels", windowID: target.windowID)
    }
    guard targetWidth * targetHeight <= 4_194_304 else {
        throw popupFilteredFailure("plan.target_pixel_area_budget", windowID: target.windowID,
            numericDetail: "width=\(targetWidth) height=\(targetHeight)")
    }
    let sourceRect = target.frame.offsetBy(
        dx: -display.frame.minX, dy: -display.frame.minY
    )
    guard popupRectIsFiniteAndPositive(sourceRect),
          sourceRect.minX >= 0, sourceRect.minY >= 0,
          sourceRect.maxX <= display.frame.width,
          sourceRect.maxY <= display.frame.height,
          popupPixelOrigin(sourceRect.minX, scale: scale) != nil,
          popupPixelOrigin(sourceRect.minY, scale: scale) != nil else {
        throw popupFilteredFailure("plan.source_rect", windowID: target.windowID)
    }
    return PopupFilteredCapturePlan(
        target: target, display: display, scale: scale,
        displayPixelWidth: displayWidth, displayPixelHeight: displayHeight,
        targetPixelWidth: targetWidth, targetPixelHeight: targetHeight,
        sourceRect: sourceRect
    )
}

/// SCK must return exactly the requested target-sized sourceRect. Its alpha
/// footprint must fill the popup's expected edges; there is no secondary crop.
func cropVerifiedPopupFilteredImage(
    _ image: CGImage,
    plan: PopupFilteredCapturePlan,
    sameApplicationWindowFrames: [CGRect]
) throws -> CGImage {
    guard image.width == plan.targetPixelWidth,
          image.height == plan.targetPixelHeight else {
        throw popupFilteredFailure("crop.target_image_size", windowID: plan.target.windowID,
            numericDetail: "width=\(image.width) height=\(image.height)"
                + " expectedWidth=\(plan.targetPixelWidth) expectedHeight=\(plan.targetPixelHeight)")
    }
    guard image.alphaInfo != .none,
          image.alphaInfo != .noneSkipFirst,
          image.alphaInfo != .noneSkipLast else {
        throw popupFilteredFailure("crop.alpha_channel", windowID: plan.target.windowID)
    }
    guard let context = CGContext(
        data: nil, width: image.width, height: image.height,
        bitsPerComponent: 8, bytesPerRow: image.width * 4,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ), let data = context.data else {
        throw popupFilteredFailure("crop.bitmap_allocation", windowID: plan.target.windowID)
    }
    let fullRect = CGRect(x: 0, y: 0, width: image.width, height: image.height)
    context.interpolationQuality = .none
    context.clear(fullRect)
    context.draw(image, in: fullRect)
    let bytes = data.assumingMemoryBound(to: UInt8.self)
    var minX = image.width, minY = image.height, maxX = -1, maxY = -1
    for y in 0..<image.height {
        let row = y * context.bytesPerRow
        for x in 0..<image.width where bytes[row + x * 4 + 3] != 0 {
            minX = min(minX, x); minY = min(minY, y)
            maxX = max(maxX, x); maxY = max(maxY, y)
        }
    }
    guard maxX >= 0 else {
        throw popupFilteredFailure("crop.all_transparent", windowID: plan.target.windowID)
    }
    guard minX <= max(8, plan.targetPixelWidth / 50) else {
        throw popupFilteredFailure("crop.left_edge", windowID: plan.target.windowID)
    }
    guard minY <= max(8, plan.targetPixelHeight / 30) else {
        throw popupFilteredFailure("crop.top_edge", windowID: plan.target.windowID)
    }
    guard maxX >= plan.targetPixelWidth - 1 - max(8, plan.targetPixelWidth / 50) else {
        throw popupFilteredFailure("crop.right_edge", windowID: plan.target.windowID)
    }
    guard maxY >= plan.targetPixelHeight - 1 - max(20, plan.targetPixelHeight / 10) else {
        throw popupFilteredFailure("crop.bottom_edge", windowID: plan.target.windowID,
            numericDetail: "lastOpaqueY=\(maxY) height=\(plan.targetPixelHeight)")
    }
    guard !windowImageHasScaledParentPadding(
        image,
        expectedBounds: plan.target.frame,
        sameApplicationWindowFrames: sameApplicationWindowFrames
    ) else {
        throw popupFilteredFailure("crop.scaled_parent_padding", windowID: plan.target.windowID)
    }
    do { try validateWindowImageContent(image) }
    catch {
        _ = popupFilteredFailure("crop.content_validation", windowID: plan.target.windowID)
        throw error
    }
    do {
        _ = try resolvedWindowImageGeometry(
            requested: WindowGeometry(bounds: plan.target.frame, backingScale: CGFloat(plan.scale)),
            imageWidth: image.width, imageHeight: image.height
        )
    } catch {
        _ = popupFilteredFailure("crop.geometry", windowID: plan.target.windowID)
        throw error
    }
    return image
}

private enum PopupFilteredWaitError: Error { case timedOut }

private func waitForPopupFilteredCapture<T>(
    timeout: TimeInterval, _ operation: @escaping () async throws -> T
) throws -> T {
    let semaphore = DispatchSemaphore(value: 0)
    var result: Result<T, Error>!
    let task = Task {
        do { result = .success(try await operation()) }
        catch { result = .failure(error) }
        semaphore.signal()
    }
    if semaphore.wait(timeout: .now() + timeout) == .timedOut {
        task.cancel()
        throw PopupFilteredWaitError.timedOut
    }
    return try result.get()
}

/// A display filter may report either global or local contentRect coordinates.
/// The requested source rectangle remains display-local in both cases.
func popupCompositorRegionSourceRect(
    plan: PopupFilteredCapturePlan,
    filterContentRect: CGRect,
    pointPixelScale: CGFloat
) throws -> CGRect {
    guard popupRectIsFiniteAndPositive(filterContentRect),
          filterContentRect.size == plan.display.frame.size,
          filterContentRect.origin == .zero ||
            filterContentRect.origin == plan.display.frame.origin,
          pointPixelScale.isFinite,
          pointPixelScale == CGFloat(plan.scale) else {
        throw popupCompositorFailure("plan.filter_geometry", windowID: plan.target.windowID)
    }
    return plan.sourceRect
}

private func popupCompositorFailure(
    _ stage: String, windowID: CGWindowID, numericDetail: String = ""
) -> WindowObservationError {
    let suffix = numericDetail.isEmpty ? "" : " \(numericDetail)"
    logActionRejected(
        "CAPTURE-SOURCE region_proof=rejected stage=\(stage)"
            + " windowID=\(windowID)\(suffix)"
    )
    return .overlayBlocked
}

func popupImagePixelBytes(
    _ image: CGImage, plan: PopupFilteredCapturePlan, stage: String
) throws -> [UInt8] {
    guard image.width == plan.targetPixelWidth,
          image.height == plan.targetPixelHeight else {
        throw popupCompositorFailure("\(stage).size", windowID: plan.target.windowID,
            numericDetail: "width=\(image.width) height=\(image.height)")
    }
    let bytesPerRow = plan.targetPixelWidth * 4
    guard let context = CGContext(
        data: nil, width: plan.targetPixelWidth, height: plan.targetPixelHeight,
        bitsPerComponent: 8, bytesPerRow: bytesPerRow,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGBitmapInfo.byteOrder32Big.rawValue |
            CGImageAlphaInfo.premultipliedLast.rawValue
    ), let data = context.data else {
        throw popupCompositorFailure("\(stage).bitmap", windowID: plan.target.windowID)
    }
    context.interpolationQuality = .none
    let bounds = CGRect(x: 0, y: 0, width: image.width, height: image.height)
    context.clear(bounds)
    context.draw(image, in: bounds)
    return Array(UnsafeBufferPointer(
        start: data.assumingMemoryBound(to: UInt8.self),
        count: bytesPerRow * plan.targetPixelHeight
    ))
}

enum PopupRegionPixelProof: String, Equatable {
    case proven
    case invalidBuffer
    case compositorAlpha
    case colorOrTemporalMismatch
    case insufficientOpaqueCoverage
    case insufficientSpatialCoverage
}

enum PopupRegionExclusionProof: String, Equatable {
    case proven
    case invalidBuffer
    case insufficientChange
    case insufficientSpatialChange
}

/// A negative control: removing the selected window must materially change
/// the same display rectangle. This rules out a singleton filter that merely
/// returned the live compositor crop regardless of its included window.
func popupRegionExclusionProof(
    reference: [UInt8], excluded: [UInt8], width: Int, height: Int
) -> PopupRegionExclusionProof {
    guard width > 0, height > 0, width <= 16_384, height <= 16_384,
          width * height <= 4_194_304,
          reference.count == width * height * 4,
          excluded.count == reference.count else { return .invalidBuffer }
    let area = width * height
    var changed = 0
    var quadrants = [0, 0, 0, 0]
    for y in 0..<height {
        for x in 0..<width {
            let offset = (y * width + x) * 4
            guard reference[offset + 3] >= 250 else { continue }
            let differs = excluded[offset + 3] < 250 || (0..<3).contains { channel in
                abs(Int(reference[offset + channel]) - Int(excluded[offset + channel])) >= 16
            }
            guard differs else { continue }
            changed += 1
            quadrants[(y >= height / 2 ? 2 : 0) + (x >= width / 2 ? 1 : 0)] += 1
        }
    }
    guard changed * 20 >= area else { return .insufficientChange }
    guard quadrants.allSatisfy({ $0 * 100 >= area / 4 }) else {
        return .insufficientSpatialChange
    }
    return .proven
}

/// One byte-exact comparison seam. Values are DeviceRGB, premultiplied-last
/// RGBA; every highly opaque singleton pixel must agree with both compositor
/// samples within eight channel levels. No mismatch percentage is accepted.
func popupRegionPixelProof(
    reference: [UInt8], before: [UInt8], after: [UInt8],
    width: Int, height: Int
) -> PopupRegionPixelProof {
    guard width > 0, height > 0, width <= 16_384, height <= 16_384,
          width * height <= 4_194_304,
          reference.count == width * height * 4,
          before.count == reference.count,
          after.count == reference.count else { return .invalidBuffer }
    let area = width * height
    var coverage = 0
    var quadrants = [0, 0, 0, 0]
    for y in 0..<height {
        for x in 0..<width {
            let offset = (y * width + x) * 4
            guard reference[offset + 3] >= 250 else { continue }
            coverage += 1
            let quadrant = (y >= height / 2 ? 2 : 0) + (x >= width / 2 ? 1 : 0)
            quadrants[quadrant] += 1
            guard before[offset + 3] >= 250, after[offset + 3] >= 250 else {
                return .compositorAlpha
            }
            for channel in 0..<3 {
                let expected = Int(reference[offset + channel])
                guard abs(expected - Int(before[offset + channel])) <= 8,
                      abs(expected - Int(after[offset + channel])) <= 8,
                      abs(Int(before[offset + channel]) - Int(after[offset + channel])) <= 8 else {
                    return .colorOrTemporalMismatch
                }
            }
        }
    }
    guard coverage * 3 >= area * 2 else { return .insufficientOpaqueCoverage }
    for quadrant in quadrants {
        guard quadrant * 2 >= area / 4 else { return .insufficientSpatialCoverage }
    }
    return .proven
}

/// Compositor samples are private and never returned or published.
func verifyPopupCompositorRegionAgreement(
    singleton: CGImage,
    before: CGImage,
    after: CGImage,
    plan: PopupFilteredCapturePlan
) throws {
    let reference = try popupImagePixelBytes(singleton, plan: plan, stage: "singleton")
    let first = try popupImagePixelBytes(before, plan: plan, stage: "before")
    let last = try popupImagePixelBytes(after, plan: plan, stage: "after")
    let proof = popupRegionPixelProof(
        reference: reference, before: first, after: last,
        width: plan.targetPixelWidth, height: plan.targetPixelHeight
    )
    guard proof == .proven else {
        throw popupCompositorFailure("pixels.\(proof.rawValue)", windowID: plan.target.windowID)
    }
}

func verifyPopupExclusionRegionDifference(
    singleton: CGImage, excluded: CGImage, plan: PopupFilteredCapturePlan
) throws {
    let reference = try popupImagePixelBytes(singleton, plan: plan, stage: "singleton.exclusion")
    let removed = try popupImagePixelBytes(excluded, plan: plan, stage: "excluded")
    let proof = popupRegionExclusionProof(
        reference: reference, excluded: removed,
        width: plan.targetPixelWidth, height: plan.targetPixelHeight
    )
    guard proof == .proven else {
        throw popupCompositorFailure("exclusion.\(proof.rawValue)", windowID: plan.target.windowID)
    }
}

/// The proof compares only highly opaque popup pixels because translucent
/// pixels legitimately blend with the desktop. Remove every unproved pixel
/// before the exact popup image can leave this capture path.
func popupImageRemovingUnprovedPixels(
    _ image: CGImage, plan: PopupFilteredCapturePlan
) throws -> CGImage {
    var pixels = try popupImagePixelBytes(image, plan: plan, stage: "publish")
    for offset in stride(from: 0, to: pixels.count, by: 4) where pixels[offset + 3] < 250 {
        pixels[offset] = 0
        pixels[offset + 1] = 0
        pixels[offset + 2] = 0
        pixels[offset + 3] = 0
    }
    let bytesPerRow = plan.targetPixelWidth * 4
    let byteCount = pixels.count
    guard let context = CGContext(
        data: nil, width: plan.targetPixelWidth, height: plan.targetPixelHeight,
        bitsPerComponent: 8, bytesPerRow: bytesPerRow,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGBitmapInfo.byteOrder32Big.rawValue |
            CGImageAlphaInfo.premultipliedLast.rawValue
    ), let data = context.data else {
        throw popupCompositorFailure("publish.bitmap", windowID: plan.target.windowID)
    }
    pixels.withUnsafeBytes { source in
        guard let base = source.baseAddress else { return }
        data.copyMemory(from: base, byteCount: byteCount)
    }
    guard let masked = context.makeImage() else {
        throw popupCompositorFailure("publish.image", windowID: plan.target.windowID)
    }
    return masked
}

enum PopupPublishedPixelProof: String, Equatable {
    case proven
    case invalidBuffer
    case unprovedPixelNotCleared
    case provenPixelChanged
}

/// Publication may remove translucent pixels only. The source has already
/// passed its geometry, content, and compositor checks; the published bitmap
/// must preserve every pixel that those checks proved visible.
func popupPublishedPixelProof(
    reference: [UInt8], published: [UInt8], width: Int, height: Int
) -> PopupPublishedPixelProof {
    guard width > 0, height > 0, width <= 16_384, height <= 16_384,
          width * height <= 4_194_304,
          reference.count == width * height * 4,
          published.count == reference.count else { return .invalidBuffer }
    for offset in stride(from: 0, to: reference.count, by: 4) {
        if reference[offset + 3] < 250 {
            guard published[offset] == 0, published[offset + 1] == 0,
                  published[offset + 2] == 0, published[offset + 3] == 0
            else { return .unprovedPixelNotCleared }
        } else {
            for channel in 0..<4 where published[offset + channel] != reference[offset + channel] {
                return .provenPixelChanged
            }
        }
    }
    return .proven
}

/// The source's edge shape must not be rechecked after deliberately removing
/// unproved translucent shadows. Validate the exact transformation instead.
func verifyPopupPublishedImage(
    _ published: CGImage, original: CGImage, plan: PopupFilteredCapturePlan
) throws -> CGImage {
    guard published.width == plan.targetPixelWidth,
          published.height == plan.targetPixelHeight else {
        throw popupCompositorFailure("publish.size", windowID: plan.target.windowID)
    }
    guard published.alphaInfo != .none,
          published.alphaInfo != .noneSkipFirst,
          published.alphaInfo != .noneSkipLast else {
        throw popupCompositorFailure("publish.alpha_channel", windowID: plan.target.windowID)
    }
    let reference = try popupImagePixelBytes(original, plan: plan, stage: "publish.original")
    let pixels = try popupImagePixelBytes(published, plan: plan, stage: "publish.pixels")
    let proof = popupPublishedPixelProof(
        reference: reference, published: pixels,
        width: plan.targetPixelWidth, height: plan.targetPixelHeight
    )
    guard proof == .proven else {
        throw popupCompositorFailure("publish.\(proof.rawValue)", windowID: plan.target.windowID)
    }
    return published
}

private func capturePopupRegion(
    filter: SCContentFilter,
    plan: PopupFilteredCapturePlan,
    stage: String
) throws -> CGImage {
    let configuration = SCStreamConfiguration()
    configuration.width = plan.targetPixelWidth
    configuration.height = plan.targetPixelHeight
    configuration.sourceRect = plan.sourceRect
    configuration.showsCursor = false
    let timeout: TimeInterval
    do { timeout = try AXObservationBudget.current?.phaseTimeout(maximum: 5) ?? 5 }
    catch { throw popupCompositorFailure("\(stage).budget", windowID: plan.target.windowID) }
    let image: CGImage
    do {
        image = try waitForPopupFilteredCapture(timeout: timeout) {
            try await SCScreenshotManager.captureImage(
                contentFilter: filter, configuration: configuration
            )
        }
    } catch {
        throw popupCompositorFailure("\(stage).capture", windowID: plan.target.windowID)
    }
    guard image.width == plan.targetPixelWidth,
          image.height == plan.targetPixelHeight else {
        throw popupCompositorFailure("\(stage).output_size", windowID: plan.target.windowID,
            numericDetail: "width=\(image.width) height=\(image.height)")
    }
    return image
}

private func requireFreshPopupRegionIdentity(
    target: PopupFilteredWindowIdentity,
    display: PopupFilteredDisplayIdentity,
    stage: String
) throws {
    let timeout: TimeInterval
    do { timeout = try AXObservationBudget.current?.phaseTimeout(maximum: 3) ?? 3 }
    catch { throw popupCompositorFailure("\(stage).budget", windowID: target.windowID) }
    do {
        let content = try waitForPopupFilteredCapture(timeout: timeout) {
            try await SCShareableContent.excludingDesktopWindows(
                true, onScreenWindowsOnly: true
            )
        }
        let matches = content.windows.filter { $0.windowID == target.windowID }
        let observedWindows = matches.compactMap { window -> PopupFilteredWindowIdentity? in
            guard let pid = window.owningApplication?.processID else { return nil }
            return PopupFilteredWindowIdentity(
                windowID: window.windowID, pid: pid, frame: window.frame
            )
        }
        let observedDisplays = content.displays.map {
            PopupFilteredDisplayIdentity(displayID: $0.displayID, frame: $0.frame)
        }
        guard popupRegionInventoryMatches(
            expected: target, display: display,
            windows: observedWindows,
            rawWindowMatchCount: matches.count,
            displays: observedDisplays
        ) else {
            throw popupCompositorFailure("\(stage).identity", windowID: target.windowID)
        }
    } catch {
        throw popupCompositorFailure("\(stage).inventory", windowID: target.windowID)
    }
}

func popupRegionInventoryMatches(
    expected: PopupFilteredWindowIdentity,
    display: PopupFilteredDisplayIdentity,
    windows: [PopupFilteredWindowIdentity],
    rawWindowMatchCount: Int,
    displays: [PopupFilteredDisplayIdentity]
) -> Bool {
    guard rawWindowMatchCount == 1, windows == [expected],
          popupRectIsFiniteAndPositive(display.frame),
          display.frame.contains(expected.frame) else { return false }
    let byID = displays.filter { $0.displayID == display.displayID }
    let containing = displays.filter {
        popupRectIsFiniteAndPositive($0.frame) && $0.frame.contains(expected.frame)
    }
    return byID == [display] && containing == [display]
}

/// Capture candidate for the rare compositor case where exact-window capture
/// returned a wrong-sized parent image. The caller must still run its normal
/// post-capture SCK and AX identity validation before publishing anything.
func captureVerifiedPopupWithSingletonDisplayFilter(
    window: SCWindow,
    expected: PopupFilteredWindowIdentity,
    availableWindows: [SCWindow],
    displays: [SCDisplay],
    rejectedExactWindowImage: CGImage? = nil,
    requireCompositorProof: Bool = false
) throws -> CGImage {
    guard #available(macOS 15.2, *) else {
        throw popupFilteredFailure("capture.os_availability", windowID: expected.windowID)
    }
    guard let ownerPID = window.owningApplication?.processID else {
        throw popupFilteredFailure("capture.window_owner_pid", windowID: expected.windowID)
    }
    guard window.windowID == expected.windowID else {
        throw popupFilteredFailure("capture.window_id", windowID: expected.windowID)
    }
    guard ownerPID == expected.pid else {
        throw popupFilteredFailure("capture.window_pid", windowID: expected.windowID)
    }
    guard window.frame == expected.frame else {
        throw popupFilteredFailure("capture.window_frame", windowID: expected.windowID)
    }
    guard availableWindows.filter({ $0.windowID == expected.windowID }).count == 1 else {
        throw popupFilteredFailure("capture.raw_target_id_unique", windowID: expected.windowID)
    }
    let inventory = availableWindows.compactMap { candidate -> PopupFilteredWindowIdentity? in
        guard let pid = candidate.owningApplication?.processID else { return nil }
        return PopupFilteredWindowIdentity(
            windowID: candidate.windowID, pid: pid, frame: candidate.frame
        )
    }
    let displayIdentities = displays.map {
        PopupFilteredDisplayIdentity(displayID: $0.displayID, frame: $0.frame)
    }
    let selected = try selectPopupFilteredDisplay(
        target: expected, availableWindows: inventory,
        displays: displayIdentities,
        rejectedExactWindowImage: rejectedExactWindowImage
    )
    guard let nativeDisplay = displays.first(where: { $0.displayID == selected.displayID }) else {
        throw popupFilteredFailure("capture.display_lookup", windowID: expected.windowID)
    }
    let filter = SCContentFilter(display: nativeDisplay, including: [window])
    filter.includeMenuBar = false
    let plan = try popupFilteredCapturePlan(
        target: expected,
        display: selected,
        includedWindowIDs: filter.includedWindows.map { $0.windowID },
        filterContentRect: filter.contentRect,
        pointPixelScale: CGFloat(filter.pointPixelScale)
    )
    var compositorFilter: SCContentFilter?
    var exclusionFilter: SCContentFilter?
    var beforeRegion: CGImage?
    if requireCompositorProof {
        guard liveFrontmostPID() == expected.pid else {
            throw popupCompositorFailure("before.frontmost", windowID: expected.windowID)
        }
        try requireFreshPopupRegionIdentity(
            target: expected, display: selected, stage: "before"
        )
        let liveFilter = SCContentFilter(display: nativeDisplay, excludingWindows: [])
        _ = try popupCompositorRegionSourceRect(
            plan: plan, filterContentRect: liveFilter.contentRect,
            pointPixelScale: CGFloat(liveFilter.pointPixelScale)
        )
        compositorFilter = liveFilter
        let withoutSelected = SCContentFilter(display: nativeDisplay, excludingWindows: [window])
        _ = try popupCompositorRegionSourceRect(
            plan: plan, filterContentRect: withoutSelected.contentRect,
            pointPixelScale: CGFloat(withoutSelected.pointPixelScale)
        )
        exclusionFilter = withoutSelected
        beforeRegion = try capturePopupRegion(
            filter: liveFilter, plan: plan, stage: "before"
        )
    }
    let configuration = SCStreamConfiguration()
    configuration.width = plan.targetPixelWidth
    configuration.height = plan.targetPixelHeight
    configuration.sourceRect = plan.sourceRect
    configuration.showsCursor = false
    configuration.includeChildWindows = false
    configuration.ignoreShadowsDisplay = true
    let timeout: TimeInterval
    do { timeout = try AXObservationBudget.current?.phaseTimeout(maximum: 5) ?? 5 }
    catch {
        _ = popupFilteredFailure("capture.budget", windowID: expected.windowID)
        throw error
    }
    let targetImage: CGImage
    do {
        targetImage = try waitForPopupFilteredCapture(timeout: timeout) {
            try await SCScreenshotManager.captureImage(
                contentFilter: filter, configuration: configuration
            )
        }
    } catch PopupFilteredWaitError.timedOut {
        throw popupFilteredFailure("capture.screenshot_timeout", windowID: expected.windowID)
    } catch {
        throw popupFilteredFailure("capture.screenshot_error", windowID: expected.windowID)
    }
    let samePIDFrames = inventory.filter {
        $0.pid == expected.pid && $0.windowID != expected.windowID
    }.map(\.frame)
    let exact = try cropVerifiedPopupFilteredImage(
        targetImage, plan: plan, sameApplicationWindowFrames: samePIDFrames
    )
    var publishable = exact
    if let compositorFilter, let exclusionFilter, let beforeRegion {
        guard liveFrontmostPID() == expected.pid else {
            throw popupCompositorFailure("after_singleton.frontmost", windowID: expected.windowID)
        }
        let excludedRegion = try capturePopupRegion(
            filter: exclusionFilter, plan: plan, stage: "excluded"
        )
        let afterRegion = try capturePopupRegion(
            filter: compositorFilter, plan: plan, stage: "after"
        )
        try verifyPopupCompositorRegionAgreement(
            singleton: exact, before: beforeRegion, after: afterRegion, plan: plan
        )
        try verifyPopupExclusionRegionDifference(
            singleton: exact, excluded: excludedRegion, plan: plan
        )
        publishable = try verifyPopupPublishedImage(
            popupImageRemovingUnprovedPixels(exact, plan: plan),
            original: exact, plan: plan
        )
        try requireFreshPopupRegionIdentity(
            target: expected, display: selected, stage: "after"
        )
        guard liveFrontmostPID() == expected.pid else {
            throw popupCompositorFailure("after.frontmost", windowID: expected.windowID)
        }
    }
    return publishable
}
