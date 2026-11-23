import ApplicationServices
import Testing
@testable import AstraMacComputerHelperCore

@Test func appMenuRequiresTheLiveMenuBarAndTheSameMainAndFocusedDocument() {
    let nodes = (101...108).map { AXUIElementCreateApplication(pid_t($0)) }
    let document = nodes[0], other = nodes[1], bar = nodes[2], file = nodes[3], menu = nodes[4], command = nodes[5]
    func valid(main: AXUIElement? = nil, focused: AXUIElement? = nil, liveBar: AXUIElement? = nil,
               foreignMenu: Bool = false, wrongBranch: Bool = false) -> Bool {
        applicationMenuDocumentMatch(command, window: document, pid: 501,
            appAttribute: { _, key in
                switch key {
                case kAXMainWindowAttribute: main ?? document
                case kAXFocusedWindowAttribute: focused ?? document
                case kAXMenuBarAttribute: liveBar ?? bar
                default: nil
                }
            }, role: {
                if CFEqual($0, bar) || CFEqual($0, other) { return kAXMenuBarRole }
                if CFEqual($0, file) { return kAXMenuBarItemRole }
                if CFEqual($0, menu) { return wrongBranch ? kAXGroupRole : kAXMenuRole }
                return kAXMenuItemRole
            }, owner: { foreignMenu && CFEqual($0, menu) ? 777 : 501 }, parent: {
                if CFEqual($0, command) { return menu }
                if CFEqual($0, menu) { return file }
                if CFEqual($0, file) { return bar }
                return nil
            }) == true
    }
    #expect(valid())
    #expect(!valid(main: other))
    #expect(!valid(focused: other))
    #expect(!valid(liveBar: other))
    #expect(!valid(foreignMenu: true))
    #expect(!valid(wrongBranch: true))
}

@Test func aMenuCommandWhoseDocumentChangedIsRejectedBeforeAXPress() {
    let ax = AXUIElementCreateApplication(11)
    let element = ActionElement(element: ax, bounds: .zero, role: kAXMenuItemRole as String,
                                subrole: nil, actions: [kAXPressAction as String])
    var writes = 0
    let performer = SystemActionPerformer(state: {
        ActionTargetState(pid: 11, windowID: 22, bounds: CGRect(x: 0, y: 0, width: 200, height: 200), axIdentity: 33)
    }, lookup: { _, _ in element }, validateMenuPress: { _ in throw ActionExecutionError.staleSnapshot },
       performAXAction: { _, _ in writes += 1; return .success })
    let action = ResolvedAction(source: .click(elementRef: "save-as"), method: .accessibilityPress,
                                screenPoint: nil, endScreenPoint: nil, element: ax, verifiedElement: element)
    do {
        _ = try performer.perform(action)
        Issue.record("a menu command followed the wrong document")
    } catch let failure as ActionPerformFailure {
        #expect(failure.error == .staleSnapshot && !failure.inputStarted)
    } catch { Issue.record("unexpected menu rejection: \(error)") }
    #expect(writes == 0)
}
