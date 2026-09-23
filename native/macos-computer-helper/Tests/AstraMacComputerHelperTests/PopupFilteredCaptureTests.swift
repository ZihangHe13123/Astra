@testable import AstraMacComputerHelperCore
import CoreGraphics
import Foundation
import Testing

private let popupTarget = PopupFilteredWindowIdentity(
    windowID: 73, pid: 42, frame: CGRect(x: 40, y: 30, width: 100, height: 80)
)
private let popupParent = PopupFilteredWindowIdentity(
    windowID: 72, pid: 42, frame: CGRect(x: 0, y: 0, width: 200, height: 160)
)
private let popupDisplay = PopupFilteredDisplayIdentity(
    displayID: 1, frame: CGRect(x: 0, y: 0, width: 300, height: 220)
)

private func popupTestImage(
    width: Int, height: Int, opaqueRects: [CGRect]
) throws -> CGImage {
    let context = try #require(CGContext(
        data: nil, width: width, height: height,
        bitsPerComponent: 8, bytesPerRow: width * 4,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ))
    context.clear(CGRect(x: 0, y: 0, width: width, height: height))
    context.setFillColor(CGColor(red: 0, green: 0, blue: 0, alpha: 1))
    // Bitmap CGContext uses bottom-left drawing coordinates. Test inputs use
    // the top-left pixel coordinates reported by SCK and scanned below.
    for rect in opaqueRects {
        context.fill(CGRect(
            x: rect.minX, y: CGFloat(height) - rect.maxY,
            width: rect.width, height: rect.height
        ))
    }
    return try #require(context.makeImage())
}

private func wrongSizedExactWindowImage() throws -> CGImage {
    try popupTestImage(width: 218, height: 178, opaqueRects: [
        CGRect(x: 0, y: 0, width: 218, height: 178)
    ])
}

private func popupRegionImage(
    width: Int, height: Int,
    baseColor: CGColor = CGColor(red: 0, green: 0, blue: 0, alpha: 1),
    patch: CGRect? = nil,
    patchColor: CGColor = CGColor(red: 1, green: 1, blue: 1, alpha: 1)
) throws -> CGImage {
    let context = try #require(CGContext(
        data: nil, width: width, height: height,
        bitsPerComponent: 8, bytesPerRow: width * 4,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ))
    context.setFillColor(baseColor)
    context.fill(CGRect(x: 0, y: 0, width: width, height: height))
    if let patch {
        context.setBlendMode(.copy)
        context.setFillColor(patchColor)
        context.fill(CGRect(
            x: patch.minX, y: CGFloat(height) - patch.maxY,
            width: patch.width, height: patch.height
        ))
    }
    return try #require(context.makeImage())
}

@Test func filteredPopupSelectionRequiresSamePIDParentAndOneContainingDisplay() throws {
    let rejected = try wrongSizedExactWindowImage()
    let chosen = try selectPopupFilteredDisplay(
        target: popupTarget, availableWindows: [popupTarget, popupParent],
        displays: [popupDisplay], rejectedExactWindowImage: rejected
    )
    #expect(chosen == popupDisplay)

    let unrelatedParent = PopupFilteredWindowIdentity(
        windowID: 72, pid: 43, frame: popupParent.frame
    )
    let ambiguousDisplay = PopupFilteredDisplayIdentity(
        displayID: 2, frame: popupDisplay.frame
    )
    let duplicateDisplayID = PopupFilteredDisplayIdentity(
        displayID: popupDisplay.displayID,
        frame: CGRect(x: 1000, y: 1000, width: 300, height: 220)
    )
    let crossingDisplay = PopupFilteredDisplayIdentity(
        displayID: 3, frame: CGRect(x: 0, y: 0, width: 120, height: 220)
    )
    for (windows, displays) in [
        ([popupTarget, unrelatedParent], [popupDisplay]),
        ([popupTarget, popupParent, popupTarget], [popupDisplay]),
        ([popupTarget, popupParent], [popupDisplay, ambiguousDisplay]),
        ([popupTarget, popupParent], [popupDisplay, duplicateDisplayID]),
        ([popupTarget, popupParent], [crossingDisplay]),
    ] {
        #expect(throws: WindowObservationError.self) {
            try selectPopupFilteredDisplay(
                target: popupTarget, availableWindows: windows,
                displays: displays, rejectedExactWindowImage: rejected
            )
        }
    }
    for (width, height) in [(100, 80), (200, 160)] {
        let exact = try popupTestImage(
            width: width, height: height,
            opaqueRects: [CGRect(x: 0, y: 0, width: width, height: height)]
        )
        #expect(throws: WindowObservationError.self) {
            try selectPopupFilteredDisplay(
                target: popupTarget, availableWindows: [popupTarget, popupParent],
                displays: [popupDisplay], rejectedExactWindowImage: exact
            )
        }
    }
    let impossibleTarget = PopupFilteredWindowIdentity(
        windowID: 73, pid: 42,
        frame: CGRect(x: 0, y: 0, width: CGFloat.greatestFiniteMagnitude, height: 80)
    )
    #expect(throws: WindowObservationError.self) {
        try selectPopupFilteredDisplay(
            target: impossibleTarget, availableWindows: [impossibleTarget, popupParent],
            displays: [popupDisplay], rejectedExactWindowImage: rejected
        )
    }
}

@Test func filteredPopupPlanHandlesOneAndTwoPixelScalesWithShiftedOrigins() throws {
    for (dx, dy) in [(-400.0, -300.0), (100.0, 100.0)] {
        for scale in [1, 2] {
        let shiftedTarget = PopupFilteredWindowIdentity(
            windowID: 73, pid: 42,
            frame: CGRect(x: dx + 50, y: dy + 50, width: 100, height: 80)
        )
        let shiftedParent = PopupFilteredWindowIdentity(
            windowID: 72, pid: 42,
            frame: CGRect(x: dx + 10, y: dy + 10, width: 200, height: 160)
        )
        let shiftedDisplay = PopupFilteredDisplayIdentity(
            displayID: 4, frame: CGRect(x: dx, y: dy, width: 300, height: 220)
        )
        let selected = try selectPopupFilteredDisplay(
            target: shiftedTarget,
            availableWindows: [shiftedTarget, shiftedParent],
            displays: [shiftedDisplay],
            rejectedExactWindowImage: wrongSizedExactWindowImage()
        )
        #expect(selected == shiftedDisplay)
        for origin in [shiftedDisplay.frame.origin, CGPoint.zero] {
            let plan = try popupFilteredCapturePlan(
                target: shiftedTarget,
                display: shiftedDisplay,
                includedWindowIDs: [shiftedTarget.windowID],
                filterContentRect: CGRect(origin: origin, size: shiftedDisplay.frame.size),
                pointPixelScale: CGFloat(scale)
            )
            #expect(plan.displayPixelWidth == 300 * scale)
            #expect(plan.displayPixelHeight == 220 * scale)
            #expect(plan.targetPixelWidth == 100 * scale)
            #expect(plan.targetPixelHeight == 80 * scale)
            #expect(plan.sourceRect == CGRect(x: 50, y: 50, width: 100, height: 80))
            #expect(try popupCompositorRegionSourceRect(
                plan: plan,
                filterContentRect: CGRect(origin: origin, size: shiftedDisplay.frame.size),
                pointPixelScale: CGFloat(scale)
            ) == plan.sourceRect)
            let exact = try cropVerifiedPopupFilteredImage(
                popupTestImage(
                    width: plan.targetPixelWidth, height: plan.targetPixelHeight,
                    opaqueRects: [CGRect(
                        x: 2 * scale, y: 0,
                        width: 96 * scale, height: 72 * scale
                    )]
                ),
                plan: plan,
                sameApplicationWindowFrames: [shiftedParent.frame]
            )
            #expect(exact.width == 100 * scale)
            #expect(exact.height == 80 * scale)
        }
        }
    }
}

@Test func popupRegionProofAcceptsExactStableTargetPixels() throws {
    let plan = try popupFilteredCapturePlan(
        target: popupTarget, display: popupDisplay,
        includedWindowIDs: [73], filterContentRect: popupDisplay.frame,
        pointPixelScale: 1
    )
    let image = try popupRegionImage(width: 100, height: 80)
    try verifyPopupCompositorRegionAgreement(
        singleton: image, before: image, after: image, plan: plan
    )
    #expect(popupRegionInventoryMatches(
        expected: popupTarget, display: popupDisplay,
        windows: [popupTarget], rawWindowMatchCount: 1,
        displays: [popupDisplay]
    ))
}

@Test func popupRegionProofRemovesUnprovedTranslucentPixelsBeforePublication() throws {
    let plan = try popupFilteredCapturePlan(
        target: popupTarget, display: popupDisplay,
        includedWindowIDs: [73], filterContentRect: popupDisplay.frame,
        pointPixelScale: 1
    )
    let patch = CGRect(x: 20, y: 20, width: 10, height: 10)
    let singleton = try popupRegionImage(
        width: 100, height: 80,
        patch: patch,
        patchColor: CGColor(red: 1, green: 0, blue: 0, alpha: 249.0 / 255.0)
    )
    let compositor = try popupRegionImage(
        width: 100, height: 80,
        patch: patch,
        patchColor: CGColor(red: 0, green: 1, blue: 0, alpha: 1)
    )
    let referenceBytes = try popupImagePixelBytes(singleton, plan: plan, stage: "test.reference")
    let compositorBytes = try popupImagePixelBytes(compositor, plan: plan, stage: "test.compositor")
    #expect(popupRegionPixelProof(
        reference: referenceBytes, before: compositorBytes, after: compositorBytes,
        width: 100, height: 80
    ) == .proven)
    try verifyPopupCompositorRegionAgreement(
        singleton: singleton, before: compositor, after: compositor, plan: plan
    )
    let publishable = try verifyPopupPublishedImage(
        popupImageRemovingUnprovedPixels(singleton, plan: plan),
        original: singleton, plan: plan
    )
    let bytes = try popupImagePixelBytes(publishable, plan: plan, stage: "test")
    let lowAlpha = (25 * 100 + 25) * 4
    #expect(Array(bytes[lowAlpha..<(lowAlpha + 4)]) == [0, 0, 0, 0])
    let proven = (5 * 100 + 5) * 4
    #expect(bytes[proven + 3] == 255)
    #expect(throws: WindowObservationError.self) {
        try verifyPopupPublishedImage(singleton, original: singleton, plan: plan)
    }
    let threshold = try popupRegionImage(
        width: 100, height: 80,
        patch: patch,
        patchColor: CGColor(red: 0, green: 0, blue: 0, alpha: 250.0 / 255.0)
    )
    let thresholdBytes = try popupImagePixelBytes(
        verifyPopupPublishedImage(
            popupImageRemovingUnprovedPixels(threshold, plan: plan),
            original: threshold, plan: plan
        ),
        plan: plan, stage: "test.threshold"
    )
    #expect(thresholdBytes[lowAlpha + 3] >= 250)
}

@Test func popupPublicationAllowsShadowPixelsRemovedAfterTheOriginalEdgeProof() throws {
    let tallTarget = PopupFilteredWindowIdentity(
        windowID: 73, pid: 42, frame: CGRect(x: 40, y: 10, width: 100, height: 200)
    )
    let plan = try popupFilteredCapturePlan(
        target: tallTarget, display: popupDisplay,
        includedWindowIDs: [73], filterContentRect: popupDisplay.frame,
        pointPixelScale: 1
    )
    let original = try cropVerifiedPopupFilteredImage(
        popupRegionImage(
            width: 100, height: 200,
            baseColor: CGColor(red: 0, green: 0, blue: 0, alpha: 0.5),
            patch: CGRect(x: 0, y: 0, width: 100, height: 150),
            patchColor: CGColor(red: 0, green: 0, blue: 0, alpha: 1)
        ),
        plan: plan, sameApplicationWindowFrames: []
    )
    let masked = try popupImageRemovingUnprovedPixels(original, plan: plan)
    try verifyPopupCompositorRegionAgreement(
        singleton: original, before: original, after: original, plan: plan
    )
    // The source reaches the bottom edge through its translucent shadow. Once
    // that unproved shadow is intentionally removed, the same edge check must
    // no longer be used as a publication check.
    #expect(throws: WindowObservationError.self) {
        try cropVerifiedPopupFilteredImage(
            masked, plan: plan,
            sameApplicationWindowFrames: []
        )
    }
    let publishable = try verifyPopupPublishedImage(masked, original: original, plan: plan)
    let pixels = try popupImagePixelBytes(publishable, plan: plan, stage: "test.publishable")
    #expect(pixels[(149 * 100 + 50) * 4 + 3] == 255)
    #expect(pixels[(150 * 100 + 50) * 4 + 3] == 0)
    #expect(pixels[(199 * 100 + 50) * 4 + 3] == 0)
}

@Test func popupPublicationRejectsChangesToProvenOpaquePixels() throws {
    let plan = try popupFilteredCapturePlan(
        target: popupTarget, display: popupDisplay,
        includedWindowIDs: [73], filterContentRect: popupDisplay.frame,
        pointPixelScale: 1
    )
    let original = try popupRegionImage(width: 100, height: 80)
    let changed = try popupRegionImage(
        width: 100, height: 80,
        patch: CGRect(x: 20, y: 20, width: 10, height: 10),
        patchColor: CGColor(red: 1, green: 0, blue: 0, alpha: 1)
    )
    #expect(throws: WindowObservationError.self) {
        try verifyPopupPublishedImage(changed, original: original, plan: plan)
    }
    #expect(throws: WindowObservationError.self) {
        try verifyPopupPublishedImage(
            wrongSizedExactWindowImage(), original: original, plan: plan
        )
    }
    var source = try popupImagePixelBytes(original, plan: plan, stage: "test.source")
    let published = source
    #expect(popupPublishedPixelProof(
        reference: source, published: published, width: 100, height: 80
    ) == .proven)
    source[(20 * 100 + 20) * 4] = 1
    #expect(popupPublishedPixelProof(
        reference: source, published: published, width: 100, height: 80
    ) == .provenPixelChanged)
}

@Test func popupExclusionNegativeControlRejectsSameSourceAndLocalizedChanges() throws {
    let plan = try popupFilteredCapturePlan(
        target: popupTarget, display: popupDisplay,
        includedWindowIDs: [73], filterContentRect: popupDisplay.frame,
        pointPixelScale: 1
    )
    let popup = try popupRegionImage(width: 100, height: 80)
    let different = try popupRegionImage(
        width: 100, height: 80,
        baseColor: CGColor(red: 1, green: 1, blue: 1, alpha: 1)
    )
    let localized = try popupRegionImage(
        width: 100, height: 80,
        patch: CGRect(x: 0, y: 0, width: 30, height: 30)
    )
    try verifyPopupExclusionRegionDifference(
        singleton: popup, excluded: different, plan: plan
    )
    for excluded in [popup, localized] {
        #expect(throws: WindowObservationError.self) {
            try verifyPopupExclusionRegionDifference(
                singleton: popup, excluded: excluded, plan: plan
            )
        }
    }
}

@Test func popupRegionProofRejectsVisibleOverlayAndTemporalChange() throws {
    let plan = try popupFilteredCapturePlan(
        target: popupTarget, display: popupDisplay,
        includedWindowIDs: [73], filterContentRect: popupDisplay.frame,
        pointPixelScale: 1
    )
    let source = try popupRegionImage(width: 100, height: 80)
    let covered = try popupRegionImage(
        width: 100, height: 80,
        patch: CGRect(x: 20, y: 20, width: 5, height: 5)
    )
    for (before, after) in [(covered, covered), (source, covered), (covered, source)] {
        #expect(throws: WindowObservationError.self) {
            try verifyPopupCompositorRegionAgreement(
                singleton: source, before: before, after: after, plan: plan
            )
        }
    }
}

@Test func popupRegionProofAllowsEightChannelLevelsButRejectsNineAtOnePixel() throws {
    let width = 100, height = 80
    var source = [UInt8](repeating: 0, count: width * height * 4)
    for pixel in 0..<(width * height) { source[pixel * 4 + 3] = 255 }
    var withinTolerance = source
    for pixel in 0..<(width * height) {
        for channel in 0..<3 { withinTolerance[pixel * 4 + channel] = 8 }
    }
    #expect(popupRegionPixelProof(
        reference: source, before: withinTolerance,
        after: withinTolerance, width: width, height: height
    ) == .proven)
    var onePixelMismatch = withinTolerance
    onePixelMismatch[(5 * width + 5) * 4] = 9
    #expect(popupRegionPixelProof(
        reference: source, before: withinTolerance,
        after: onePixelMismatch, width: width, height: height
    ) == .colorOrTemporalMismatch)
    var alphaMismatch = withinTolerance
    alphaMismatch[(5 * width + 5) * 4 + 3] = 0
    #expect(popupRegionPixelProof(
        reference: source, before: withinTolerance,
        after: alphaMismatch, width: width, height: height
    ) == .compositorAlpha)
}

@Test func popupRegionProofRejectsAlphaGapsScaleAndPoorSpatialCoverage() throws {
    let plan = try popupFilteredCapturePlan(
        target: popupTarget, display: popupDisplay,
        includedWindowIDs: [73], filterContentRect: popupDisplay.frame,
        pointPixelScale: 1
    )
    let source = try popupRegionImage(width: 100, height: 80)
    let alphaGap = try popupTestImage(width: 100, height: 80, opaqueRects: [
        CGRect(x: 0, y: 0, width: 100, height: 30),
        CGRect(x: 0, y: 40, width: 100, height: 40),
        CGRect(x: 0, y: 30, width: 40, height: 10),
        CGRect(x: 50, y: 30, width: 50, height: 10),
    ])
    let lowCoverage = try popupTestImage(width: 100, height: 80, opaqueRects: [
        CGRect(x: 0, y: 0, width: 50, height: 80)
    ])
    let unbalancedCoverage = try popupTestImage(width: 100, height: 80, opaqueRects: [
        CGRect(x: 0, y: 0, width: 70, height: 80)
    ])
    let wrongScale = try popupRegionImage(width: 200, height: 160)
    for (singleton, before, after) in [
        (source, alphaGap, source),
        (lowCoverage, source, source),
        (unbalancedCoverage, source, source),
        (source, wrongScale, source),
    ] {
        #expect(throws: WindowObservationError.self) {
            try verifyPopupCompositorRegionAgreement(
                singleton: singleton, before: before, after: after, plan: plan
            )
        }
    }
}

@Test func popupRegionProofRejectsChangedIdentityAndFilterGeometry() throws {
    let plan = try popupFilteredCapturePlan(
        target: popupTarget, display: popupDisplay,
        includedWindowIDs: [73], filterContentRect: popupDisplay.frame,
        pointPixelScale: 2
    )
    let changedWindow = PopupFilteredWindowIdentity(
        windowID: 73, pid: 42,
        frame: CGRect(x: 41, y: 30, width: 100, height: 80)
    )
    let changedDisplay = PopupFilteredDisplayIdentity(
        displayID: 1, frame: CGRect(x: 0, y: 0, width: 301, height: 220)
    )
    let overlappingDisplay = PopupFilteredDisplayIdentity(
        displayID: 2, frame: popupDisplay.frame
    )
    for (windows, count, displays) in [
        ([changedWindow], 1, [popupDisplay]),
        ([popupTarget], 2, [popupDisplay]),
        ([popupTarget], 1, [changedDisplay]),
        ([popupTarget], 1, [popupDisplay, overlappingDisplay]),
    ] {
        #expect(!popupRegionInventoryMatches(
            expected: popupTarget, display: popupDisplay,
            windows: windows, rawWindowMatchCount: count,
            displays: displays
        ))
    }
    for (rect, scale) in [
        (CGRect(x: 1, y: 0, width: 300, height: 220), CGFloat(2)),
        (popupDisplay.frame, CGFloat(1)),
        (CGRect(x: 0, y: 0, width: 299, height: 220), CGFloat(2)),
    ] {
        #expect(throws: WindowObservationError.self) {
            try popupCompositorRegionSourceRect(
                plan: plan, filterContentRect: rect,
                pointPixelScale: scale
            )
        }
    }
}

@Test func filteredPopupPlanRejectsFilterScopeScaleAndPixelAmbiguity() throws {
    let base = (
        target: popupTarget,
        display: popupDisplay,
        includedWindowIDs: [popupTarget.windowID],
        filterContentRect: popupDisplay.frame,
        pointPixelScale: CGFloat(2)
    )
    for ids: [CGWindowID] in [[], [72], [73, 72]] {
        #expect(throws: WindowObservationError.self) {
            try popupFilteredCapturePlan(
                target: base.target, display: base.display,
                includedWindowIDs: ids,
                filterContentRect: base.filterContentRect,
                pointPixelScale: base.pointPixelScale
            )
        }
    }
    for rect in [
        CGRect(x: 1, y: 0, width: 300, height: 220),
        CGRect(x: 0, y: 0, width: 301, height: 220),
        CGRect(x: 0, y: 0, width: 300, height: 221),
    ] {
        #expect(throws: WindowObservationError.self) {
            try popupFilteredCapturePlan(
                target: base.target, display: base.display,
                includedWindowIDs: base.includedWindowIDs,
                filterContentRect: rect,
                pointPixelScale: base.pointPixelScale
            )
        }
    }
    for scale: CGFloat in [0, 1.5, 3, .nan] {
        #expect(throws: WindowObservationError.self) {
            try popupFilteredCapturePlan(
                target: base.target, display: base.display,
                includedWindowIDs: base.includedWindowIDs,
                filterContentRect: base.filterContentRect,
                pointPixelScale: scale
            )
        }
    }
    let fractional = PopupFilteredWindowIdentity(
        windowID: 73, pid: 42,
        frame: CGRect(x: 40, y: 30, width: 100.25, height: 80)
    )
    #expect(throws: WindowObservationError.self) {
        try popupFilteredCapturePlan(
            target: fractional, display: popupDisplay,
            includedWindowIDs: [73], filterContentRect: popupDisplay.frame,
            pointPixelScale: 2
        )
    }
    let fractionalSourceOrigin = PopupFilteredWindowIdentity(
        windowID: 73, pid: 42,
        frame: CGRect(x: 40.25, y: 30, width: 100, height: 80)
    )
    #expect(throws: WindowObservationError.self) {
        try popupFilteredCapturePlan(
            target: fractionalSourceOrigin, display: popupDisplay,
            includedWindowIDs: [73], filterContentRect: popupDisplay.frame,
            pointPixelScale: 2
        )
    }
}

@Test func filteredPopupCropRejectsExtraPixelsParentShapeAndUncertainContent() throws {
    let plan = try popupFilteredCapturePlan(
        target: popupTarget, display: popupDisplay,
        includedWindowIDs: [73], filterContentRect: popupDisplay.frame,
        pointPixelScale: 2
    )
    let legitimate = CGRect(x: 4, y: 0, width: 192, height: 144)
    let cases: [(Int, Int, [CGRect])] = [
        (200, 160, [CGRect(x: 0, y: 0, width: 65, height: 160)]),
        (200, 160, []),
        (200, 160, [CGRect(x: 50, y: 30, width: 100, height: 80)]),
        (201, 160, [legitimate]),
        (200, 161, [legitimate]),
    ]
    for (width, height, rectangles) in cases {
        let image = try popupTestImage(
            width: width, height: height, opaqueRects: rectangles
        )
        #expect(throws: WindowObservationError.self) {
            try cropVerifiedPopupFilteredImage(
                image, plan: plan,
                sameApplicationWindowFrames: [popupParent.frame]
            )
        }
    }
}
