import ApplicationServices
import Foundation
import Testing
@testable import AstraMacComputerHelperCore

@Test func cuContentBudgetReservesTimeForFinalIdentityValidation() throws {
    var now = 0.0
    let hard = AXObservationBudget(duration: 5, clock: { now }, setTimeout: { _, _ in .success })
    let content = hard.contentProjectionBudget(maximumDuration: 3, reservingDuration: 1)
    #expect(try content.phaseTimeout(maximum: 10) == 3)
    now = 3
    #expect(content.expired)
    #expect(hard.available)
    #expect(try hard.phaseTimeout(maximum: 10) == 2)
    try hard.check()
}

@Test func cuContentBudgetAccountsForAlreadySpentObservationTime() throws {
    var now = 0.0
    let hard = AXObservationBudget(duration: 5, clock: { now }, setTimeout: { _, _ in .success })
    now = 3.5
    let content = hard.contentProjectionBudget(maximumDuration: 3, reservingDuration: 1)
    #expect(try content.phaseTimeout(maximum: 10) == 0.5)
    now = 4
    #expect(content.expired)
    #expect(hard.available)
    #expect(try hard.phaseTimeout(maximum: 10) == 1)
}

@Test func cuContentBudgetCannotOutliveParentCancellationOrFailure() {
    var remaining = 4.0
    let hard = AXObservationBudget(remainingBudget: { remaining }, setTimeout: { _, _ in .success })
    let content = hard.contentProjectionBudget(maximumDuration: 3, reservingDuration: 1)
    remaining = 0
    #expect(!content.available)
    #expect(!hard.available)

    let broken = AXObservationBudget(duration: 5, setTimeout: { _, _ in .failure })
    let child = broken.contentProjectionBudget(maximumDuration: 3, reservingDuration: 1)
    let value: Bool? = broken.call(element: AXUIElementCreateApplication(Int32.max)) { true }
    #expect(value == nil)
    #expect(!child.available)
}

@Test func cuContentBudgetRefusesToSpendFinalValidationReserve() {
    let hard = AXObservationBudget(duration: 0.5, clock: { 0 }, setTimeout: { _, _ in .success })
    let content = hard.contentProjectionBudget(maximumDuration: 3, reservingDuration: 1)
    #expect(!content.available)
    #expect(hard.available)
    #expect(!hard.contentProjectionBudget(maximumDuration: .infinity, reservingDuration: 1).available)
    #expect(!hard.contentProjectionBudget(maximumDuration: 3, reservingDuration: -1).available)
}

@Test func cuContentBudgetReturnsPartialContentAndRestoresHardBudget() throws {
    var now = 0.0
    let hard = AXObservationBudget(duration: 5, clock: { now }, setTimeout: { _, _ in .success })
    let restore = hard.install()
    defer { restore() }
    let content = hard.contentProjectionBudget(maximumDuration: 1, reservingDuration: 1)
    let provider = CUExpensiveContentProvider { now += 0.6 }
    let node = AXNodeReader.read(provider: provider, windowBounds: .zero, budget: content)
    #expect(node.childrenTruncated)
    #expect(node.sourceElement == nil)
    #expect(provider.valueReads == 0)
    #expect(content.expired)
    #expect(hard.available)
    #expect(AXObservationBudget.current === hard)
    try hard.check()
}

@Test(arguments: ["children", "last_child", "help", "focused", "source"])
func cuContentBudgetMarksLateExhaustionInPublishedTree(stage: String) throws {
    var now = 0.0
    let hard = AXObservationBudget(duration: 5, clock: { now }, setTimeout: { _, _ in .success })
    let content = hard.contentProjectionBudget(maximumDuration: 1, reservingDuration: 1)
    let provider = CULateExpiringProvider(stage: stage, expire: { now = 1.1 })
    let node = AXNodeReader.read(provider: provider, windowBounds: .zero, budget: content)
    #expect(node.childrenTruncated)
    #expect(node.sourceElement == nil)
    let published = AXSerializer.serialize(node).root.asJSON()
    guard case let .object(fields) = published else { Issue.record("expected public AX object"); return }
    #expect(fields["children_truncated"] == .bool(true))
    #expect(content.expired)
    try hard.check()
    now = 5
    #expect(throws: WindowObservationError.self) { try hard.check() }
}

private final class CULateExpiringProvider: AXNodeAttributeProvider {
    let stage: String
    let expire: () -> Void
    init(stage: String, expire: @escaping () -> Void) { self.stage = stage; self.expire = expire }
    func stringValue(for attribute: String) -> BoundedAXStringResult {
        if stage == "help", attribute == kAXHelpAttribute { expire() }
        return BoundedAXStringResult(value: attribute == kAXRoleAttribute ? "AXTextField" : nil, status: .complete)
    }
    func boolValue(for attribute: String) -> Bool? {
        if stage == "focused", attribute == kAXFocusedAttribute { expire() }
        return true
    }
    func bounds() -> CGRect { CGRect(x: 0, y: 0, width: 100, height: 20) }
    func actions() -> [BoundedAXStringResult] { [] }
    func children(remaining: Int) -> [any AXNodeAttributeProvider] {
        if stage == "children" { expire() }
        return stage == "last_child" ? [CULateExpiringProvider(stage: "source", expire: expire)] : []
    }
    func sourceElement() -> AXUIElement? {
        if stage == "source" { expire() }
        return AXUIElementCreateApplication(Int32.max)
    }
}

private final class CUExpensiveContentProvider: AXNodeAttributeProvider {
    let advance: () -> Void
    var valueReads = 0
    init(advance: @escaping () -> Void) { self.advance = advance }
    func stringValue(for attribute: String) -> BoundedAXStringResult {
        if attribute == kAXRoleAttribute || attribute == kAXSubroleAttribute {
            advance()
            return BoundedAXStringResult(value: attribute == kAXRoleAttribute ? "AXTextField" : "AXSecureTextField", status: .complete)
        }
        if attribute == kAXValueAttribute { valueReads += 1 }
        return BoundedAXStringResult(value: nil, status: .complete)
    }
    func boolValue(for attribute: String) -> Bool? { true }
    func bounds() -> CGRect { .zero }
    func actions() -> [BoundedAXStringResult] { [] }
    func children(remaining: Int) -> [any AXNodeAttributeProvider] { [] }
    func sourceElement() -> AXUIElement? { AXUIElementCreateApplication(Int32.max) }
}
