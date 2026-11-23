import CoreGraphics
import Testing
@testable import AstraMacComputerHelperCore

@Test func sheetCaptureRequiresBoundedOwnedDirectAncestry() {
    func root(foreign: Int = -1, missingRole: Int = -1, outside: Int = -1) -> Int? {
        ownedSheetRoot(sheet: 3, same: ==, isOwned: { $0 != foreign },
            parent: { [3: 2, 2: 1][$0] },
            role: { $0 == missingRole ? nil : ($0 == 1 ? "AXWindow" : "AXSheet") },
            contained: { _, child in child != outside })
    }
    #expect(root() == 1)
    #expect(root(foreign: 2) == nil)
    #expect(root(missingRole: 2) == nil)
    #expect(root(outside: 3) == nil)
    #expect(ownedSheetRoot(sheet: 3, same: ==, isOwned: { _ in true },
        parent: { $0 == 3 ? 2 : 3 }, role: { _ in "AXSheet" }, contained: { _, _ in true }) == nil)
}

@Test func distinctSheetSurfaceMustDifferFromEveryAttachedAncestor() {
    func root(parentSurface: CGWindowID?, rootSurface: CGWindowID?) -> Int? {
        ownedSheetRoot(sheet: 3, same: ==, isOwned: { _ in true },
            parent: { [3: 2, 2: 1][$0] }, role: { $0 == 1 ? "AXWindow" : "AXSheet" },
            contained: { _, _ in true },
            distinctSurface: (33, { $0 == 1 ? rootSurface : parentSurface }))
    }
    #expect(root(parentSurface: 22, rootSurface: 11) == 1)
    #expect(root(parentSurface: 33, rootSurface: 11) == nil)
    #expect(root(parentSurface: 22, rootSurface: 33) == nil)
    #expect(root(parentSurface: 22, rootSurface: nil) == nil)
}

@Test func sheetCropUsesActualPixelsAndGlobalParentOffset() throws {
    let source = WindowGeometry(bounds: CGRect(x: -100, y: 30, width: 1352, height: 768), backingScale: 2)
    let sheet = CGRect(x: 136, y: 190, width: 880, height: 448)
    #expect(try sheetImageCropRect(source: source, target: sheet, imageWidth: 2704, imageHeight: 1536)
        == CGRect(x: 472, y: 320, width: 1760, height: 896))
    #expect(try sheetImageCropRect(source: source, target: sheet, imageWidth: 1352, imageHeight: 768)
        == CGRect(x: 236, y: 160, width: 880, height: 448))
    #expect(throws: (any Error).self) {
        try sheetImageCropRect(source: source, target: sheet, imageWidth: 1760, imageHeight: 896)
    }
    #expect(throws: (any Error).self) {
        try sheetImageCropRect(source: source, target: CGRect(x: -101, y: 30, width: 100, height: 100),
            imageWidth: 2704, imageHeight: 1536)
    }
    #expect(throws: (any Error).self) {
        try sheetImageCropRect(source: source, target: CGRect(x: 136.2, y: 190, width: 880, height: 448),
            imageWidth: 2704, imageHeight: 1536)
    }
}
