import ApplicationServices
@testable import AstraMacComputerHelperCore
import Testing

@Test func focusedSheetRequiresExactOwnedParentWindowAndSheetRole() {
    func candidate(windows: [Int] = [1], foreign: Int = -1, sheet: Bool = true, contained: Bool = true) -> Int? {
        focusedSheetCandidate(windows: windows, focused: 3, same: ==,
            isOwned: { $0 != foreign }, parent: { [3: 2, 2: 1][$0] },
            isSheet: { $0 == 2 && sheet }, contained: { _, _ in contained })
    }
    #expect(candidate() == 2)
    #expect(candidate(windows: [9]) == nil)
    #expect(candidate(windows: [1, 1]) == nil)
    #expect(candidate(foreign: 2) == nil)
    #expect(candidate(sheet: false) == nil)
    #expect(candidate(contained: false) == nil)
}

@Test func focusedSheetRejectsDetachedCyclicAndNonImmediateSheets() {
    let detached = focusedSheetCandidate(windows: [1], focused: 3, same: ==,
        isOwned: { _ in true }, parent: { _ in nil },
        isSheet: { _ in true }, contained: { _, _ in true })
    #expect(detached == nil)
    let cyclic = focusedSheetCandidate(windows: [1], focused: 3, same: ==,
        isOwned: { _ in true }, parent: { $0 == 3 ? 2 : 3 },
        isSheet: { _ in true }, contained: { _, _ in true })
    #expect(cyclic == nil)
    let nested = focusedSheetCandidate(windows: [1], focused: 4, same: ==,
        isOwned: { _ in true }, parent: { [4: 3, 3: 2, 2: 1][$0] },
        isSheet: { $0 == 3 }, contained: { _, _ in true })
    #expect(nested == nil)
}

@Test func nestedSheetsRequireEveryOwnedDirectLinkAndContainment() {
    func chain(windows: [Int] = [1], foreign: Int = -1, missingRole: Int = -1,
               outside: Int = -1, maximum: Int = 8) -> [Int] {
        focusedSheetChain(windows: windows, focused: 4, maximumSheets: maximum,
            same: ==, isOwned: { $0 != foreign }, parent: { [4: 3, 3: 2, 2: 1][$0] },
            isSheet: { [2, 3].contains($0) && $0 != missingRole },
            contained: { _, child in child != outside })
    }
    #expect(chain() == [2, 3])
    #expect(chain(windows: [1, 1]).isEmpty)
    #expect(chain(foreign: 3).isEmpty)
    #expect(chain(missingRole: 2).isEmpty)
    #expect(chain(missingRole: 3) == [2])
    #expect(chain(outside: 3) == [2])
    #expect(chain(maximum: 1) == [2])
    #expect(chain(maximum: 0).isEmpty)
}

@Test func sheetButtonsPrecedeLargeColumnTreesWithoutDroppingOrDuplicatingChildren() {
    let children = ["sidebar", "columns", "toolbar", "Cancel", "Open", "footer"]
    var visited: [String] = []
    let ordered = prioritizeSheetChildren(children, isButton: {
        visited.append($0)
        return ["Cancel", "Open"].contains($0)
    }, isFocusedBranch: { _ in false })
    #expect(visited == children)
    #expect(ordered == ["Cancel", "Open", "sidebar", "columns", "toolbar", "footer"])
    #expect(prioritizeSheetChildren(children, isButton: { _ in false },
        isFocusedBranch: { _ in false }) == children)
}

@Test func currentColumnPrecedesOffscreenAncestorsWithoutAddingAuthority() {
    let children = ["old-column", "current-column", "other-column", "Open", "Cancel"]
    let ordered = prioritizeSheetChildren(children,
        isButton: { ["Open", "Cancel"].contains($0) },
        isFocusedBranch: { $0 == "current-column" })
    #expect(ordered == ["Open", "Cancel", "current-column", "old-column", "other-column"])
    #expect(prioritizeSheetChildren(children, isButton: { _ in false },
        isFocusedBranch: { $0 == "missing-child" }) == children)
}

@Test func focusedSheetPathRequiresCompleteBoundedOwnedAncestry() {
    let parents = [4: 3, 3: 2, 2: 1]
    #expect(focusedAncestorPath(root: 1, focused: 4, same: ==,
        isOwned: { _ in true }, parent: { parents[$0] }) == [4, 3, 2])
    #expect(focusedAncestorPath(root: 1, focused: 4, same: ==,
        isOwned: { $0 != 3 }, parent: { parents[$0] }).isEmpty)
    #expect(focusedAncestorPath(root: 1, focused: 4, same: ==,
        isOwned: { _ in true }, parent: { $0 == 4 ? 3 : 4 }).isEmpty)
    #expect(focusedAncestorPath(root: 1, focused: 4, same: ==,
        isOwned: { _ in true }, parent: { _ in nil }).isEmpty)
    #expect(focusedAncestorPath(root: 1, focused: 100, same: ==,
        isOwned: { _ in true }, parent: { $0 - 1 }).isEmpty)
}

@Test func anEdgeEditorCannotBorrowTheOwnerOfItsContainingSheet() {
    let app = (90...93).map { AXUIElementCreateApplication(pid_t($0)) }
    let window = app[0], field = app[1], container = app[2]
    func belongs(root: AXUIElement, intermediateRole: String?) -> Bool {
        fieldBelongsToNearestAXWindow(field, root: root, pid: 10, owner: { _ in 10 }, parent: {
            if CFEqual($0, field) { return container }
            return CFEqual($0, container) ? window : nil
        }, role: {
            if CFEqual($0, field) { return kAXTextAreaRole }
            if CFEqual($0, container) { return intermediateRole }
            return kAXWindowRole
        })
    }
    #expect(belongs(root: window, intermediateRole: kAXScrollAreaRole))
    #expect(!belongs(root: window, intermediateRole: nil))
    #expect(!belongs(root: window, intermediateRole: kAXSheetRole))
    #expect(!belongs(root: window, intermediateRole: kAXWindowRole))
    #expect(belongs(root: container, intermediateRole: kAXSheetRole))
}
