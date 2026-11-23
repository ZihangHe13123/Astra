@testable import AstraMacComputerHelperCore
import Foundation
import ApplicationServices
import Testing

@Test func standardSavePanelNamesExactFieldsAndTreatsFolderLabelsAsUnverified() {
    let fname = StandardSavePanelField(identifier: "saveAsNameTextField", role: "AXTextField", reference: "filename",
        value: "local-note.docx", directoryPath: nil)
    let location = StandardSavePanelField(identifier: "where popup", role: "AXPopUpButton", reference: "where",
        value: "reports", directoryPath: nil)
    let unrelated = StandardSavePanelField(identifier: "other", role: "AXTextField", reference: "other",
        value: "/other/reports", directoryPath: nil)
    guard case let .object(result) = standardSavePanelFields([fname,location,unrelated], complete: true) else {
        Issue.record("missing evidence"); return
    }
    #expect(result["filename"] == .string("local-note.docx"))
    #expect(result["filename_element_ref"] == .string("filename"))
    #expect(result["directory_status"] == .string("display_name_only"))
    #expect(result["directory_display_name"] == .string("reports"))
    #expect(result["directory"] == nil)
}

@Test func standardSavePanelRequiresUniqueUntruncatedControlsAndPlainNames() {
    let fname = StandardSavePanelField(identifier: "saveAsNameTextField", role: "AXTextField", reference: "field",
        value: "new.docx", directoryPath: nil)
    let fake = StandardSavePanelField(identifier: "saveAsNameTextField", role: "AXTextArea", reference: "wrong",
        value: "other.docx", directoryPath: nil)
    for (fields,complete) in [([fname,fname],true),([fake],true),([fname],false)] {
        guard case let .object(result) = standardSavePanelFields(fields, complete: complete) else {
            Issue.record("missing evidence"); return
        }
        #expect(result["filename_status"] == .string("unverified") && result["filename"] == nil)
    }
    #expect(savePanelLeafName("../another.docx") == nil)
    #expect(savePanelLeafName("unsafe\u{0000}file") == nil)
}

@Test func standardSavePanelFolderEvidenceRequiresExplicitFileURLForLocalDirectory() throws {
    let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: false)
    defer { try? FileManager.default.removeItem(at: dir) }
    let file = dir.appendingPathComponent("data.txt")
    try Data().write(to: file)
    let resolved = try #require(realpath(dir.path, nil))
    defer { free(resolved) }
    let actual = String(cString: resolved)
    #expect(savePanelLocalDirectory(dir as NSURL) == actual)
    #expect(savePanelLocalDirectory(dir.absoluteString as NSString) == actual)
    #expect(savePanelLocalDirectory(file as NSURL) == nil)
    #expect(savePanelLocalDirectory(dir.lastPathComponent as NSString) == nil)
    #expect(savePanelLocalDirectory("file://other-host/Directory" as NSString) == nil)
}

@Test func savePanelBrowserSelectionUsesTheActualSelectedFolderAndItsContentsColumn() {
    let base = URL(fileURLWithPath: "/fixture"), parent = base.appendingPathComponent("A"), folder = parent.appendingPathComponent("reports")
    let selected = [SaveBrowserSelection.selectedVirtualItem, .directory(parent), .directory(folder)]
    #expect(provenSaveBrowserDirectory(selected + [.empty], popupLabel: "reports") == folder.path)
    #expect(provenSaveBrowserDirectory(selected + [.file(folder.appendingPathComponent("existing.docx"))], popupLabel: "reports") == folder.path)
    #expect(provenSaveBrowserDirectory([.directory(base.appendingPathComponent("B")),.directory(folder),.empty], popupLabel: "reports") == nil)
    #expect(provenSaveBrowserDirectory(selected + [.empty], popupLabel: "other") == nil)
    #expect(provenSaveBrowserDirectory([.file(folder.appendingPathComponent("existing.docx")),.empty], popupLabel: "reports") == nil)
    #expect(provenSaveBrowserDirectory(selected + [.selectedVirtualItem], popupLabel: "reports") == nil)
}

@Test func newSavePanelCopyChecksTheRealDirectoryNameAndFilesystemImmediatelyBeforeSave() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    let a = root.appendingPathComponent("A/reports"), b = root.appendingPathComponent("B/reports")
    try FileManager.default.createDirectory(at: a, withIntermediateDirectories: true)
    try FileManager.default.createDirectory(at: b, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: root) }
    let copy = b.appendingPathComponent("Handover-B-verified.docx"), name = copy.lastPathComponent
    try proveNewSavePanelDestination(requested: copy.path, filename: name, directory: b.path)
    #expect(throws: ActionExecutionError.saveDestinationMismatch) { try proveNewSavePanelDestination(requested: copy.path, filename: name, directory: a.path) }
    #expect(throws: ActionExecutionError.saveDestinationMismatch) { try proveNewSavePanelDestination(requested: copy.path, filename: "source.docx", directory: b.path) }
    #expect(throws: ActionExecutionError.saveDestinationUnverified) { try proveNewSavePanelDestination(requested: copy.path, filename: name, directory: nil) }
    try Data("concurrent file".utf8).write(to: copy)
    #expect(throws: ActionExecutionError.saveDestinationExists) { try proveNewSavePanelDestination(requested: copy.path, filename: name, directory: b.path) }
    #expect(try Data(contentsOf: copy) == Data("concurrent file".utf8))
    try FileManager.default.removeItem(at: copy)
    try FileManager.default.createSymbolicLink(at: copy, withDestinationURL: root.appendingPathComponent("absent"))
    #expect(throws: ActionExecutionError.saveDestinationExists) { try proveNewSavePanelDestination(requested: copy.path, filename: name, directory: b.path) }
}

@Test func standardSavePanelProvesDarwinTemporaryDirectoryAliasesWithoutFollowingTheLeaf() throws {
    let folder = "/private/tmp/" + UUID().uuidString
    let alias = String(folder.dropFirst("/private".count))
    let folderURL = URL(fileURLWithPath: folder, isDirectory: true)
    try FileManager.default.createDirectory(at: folderURL, withIntermediateDirectories: false)
    defer { try? FileManager.default.removeItem(at: folderURL) }
    let name = "copy.docx"
    #expect(savePanelLocalDirectory(URL(fileURLWithPath: alias) as NSURL) == folder)
    try proveNewSavePanelDestination(requested: alias + "/" + name, filename: name, directory: folder)
    try proveNewSavePanelDestination(requested: folder + "/" + name, filename: name, directory: alias)
    try FileManager.default.createSymbolicLink(at: folderURL.appendingPathComponent(name),
        withDestinationURL: folderURL.appendingPathComponent("missing.docx"))
    #expect(throws: ActionExecutionError.saveDestinationExists) {
        try proveNewSavePanelDestination(requested: alias + "/" + name, filename: name, directory: folder)
    }
}

@Test func newSavePanelCopyWireAndPlanDigestBindTheRequestedDestination() throws {
    func click(_ path: String) throws -> NativeAction {
        try NativeAction.parse(.object(["type": .string("click"), "element_ref": .string("ok"), "save_new_copy_to": .string(path)]))
    }
    let first = try click("/test/A.docx"), second = try click("/test/B.docx")
    #expect(first.saveNewCopyTo == "/test/A.docx" && InputDispatcher.digest([first]) != InputDispatcher.digest([second]))
    #expect(InputDispatcher.digest([first]) != InputDispatcher.digest([.click(elementRef:"ok")]))
    #expect(throws: ActionExecutionError.invalidAction) { try click("relative.docx") }
    #expect(throws: ActionExecutionError.invalidAction) { try click("/test/../replace.docx") }
    #expect(throws: ActionExecutionError.invalidAction) {
        try NativeAction.parse(.object(["type":.string("keypress"),"key":.string("a"),"save_new_copy_to":.string("/test/a.docx")]))
    }
    #expect(throws: ActionExecutionError.invalidAction) {
        try NativeAction.parse(.object(["type":.string("click"),"x":.number(40),"y":.number(1),"save_new_copy_to":.string("/test/a.docx")]))
    }
    let keyboard = try NativeAction.parse(.object(["type":.string("keypress"),"key":.string("return"),"save_new_copy_to":.string("/test/a.docx")]))
    #expect(keyboard.saveNewCopyTo == "/test/a.docx")
}

@Test func savePanelGuardRefusesStaleCommitIdentityOrAChildSheetBeforeAnyMutation() throws {
    let yes = AXUIElementCreateApplication(10), name = AXUIElementCreateApplication(11), other = AXUIElementCreateApplication(12)
    let parent = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: false)
    defer { try? FileManager.default.removeItem(at: parent) }
    let fields = [
        StandardSavePanelField(identifier: "saveAsNameTextField", role: "AXTextField", reference: "name", value: "new.docx", directoryPath: nil),
        StandardSavePanelField(identifier: "where popup", role: "AXPopUpButton", reference: "where", value: nil, directoryPath: parent.path),
        StandardSavePanelField(identifier: "OKButton", role: "AXButton", reference: "ok", value: nil, directoryPath: nil),
    ]
    let panel = LiveSavePanel(fields: fields, commitButton: yes, defaultButton: yes, filenameField: name)
    let path = parent.appendingPathComponent("new.docx").path
    let click = try NativeAction.parse(.object(["type":.string("click"),"element_ref":.string("ok"),"save_new_copy_to":.string(path)]))
    let enter = try NativeAction.parse(.object(["type":.string("keypress"),"key":.string("return"),"save_new_copy_to":.string(path)]))
    try checkStandardSaveCommit(panel, action: click, clicked: yes, exactFocusedPanel: true, focusedControl: nil, enabled: { _ in true }, readFilename: { _ in "new.docx" })
    try checkStandardSaveCommit(panel, action: enter, clicked: nil, exactFocusedPanel: true, focusedControl: name, enabled: { _ in true }, readFilename: { _ in "new.docx" })
    #expect(throws: ActionExecutionError.saveDestinationMismatch) {
        try checkStandardSaveCommit(panel, action: click, clicked: yes, exactFocusedPanel: true, focusedControl: nil,
                                    enabled: { _ in true }, readFilename: { _ in "changed.docx" })
    }
    for (action,clicked,focused,panelFocused) in [(click,other,nil as AXUIElement?,true),(click,yes,nil,false),(enter,nil,other,true)] {
        #expect(throws: ActionExecutionError.saveDestinationUnverified) {
            try checkStandardSaveCommit(panel, action: action, clicked: clicked, exactFocusedPanel: panelFocused,
                                        focusedControl: focused, enabled: { _ in true })
        }
    }
    #expect(!FileManager.default.fileExists(atPath:path))
}

@Test func savePanelNewCopyChecksAgainImmediatelyBeforeAXPress() throws {
    let button = AXUIElementCreateApplication(11)
    let element = ActionElement(element: button, identityToken: "test-button",
        bounds: CGRect(x: 1, y: 1, width: 20, height: 10), roleResult: .init(value:"AXButton",status:.complete),
        subroleResult: .init(value:nil,status:.complete), enabled: true, actionNames: .complete(["AXPress"]))
    let source = try NativeAction.parse(.object(["type":.string("click"),"element_ref":.string("button"),
        "save_new_copy_to":.string("/local/output.docx")]))
    var checks = 0, presses = 0
    let performer = SystemActionPerformer(state: {
        ActionTargetState(pid:11, windowID:20, bounds:CGRect(x:0,y:0,width:100,height:100), axIdentity:33)
    }, lookup: { _, _ in element }, savePanelGuard: { _ in
        checks += 1
        if checks > 1 { throw ActionExecutionError.saveDestinationMismatch }
    }, performAXAction: { _,_ in presses += 1; return .success })
    try performer.validateSaveDestination(source)
    let resolved = ResolvedAction(source: source, method:.accessibilityPress, screenPoint:nil,
        endScreenPoint:nil, element: button, verifiedElement: element)
    do {
        _ = try performer.perform(resolved)
        Issue.record("the destination changed after approval")
    } catch let failure as ActionPerformFailure {
        #expect(failure.error == .saveDestinationMismatch && !failure.inputStarted)
    }
    #expect(checks == 2 && presses == 0)
}

@Test func hiddenSaveExtensionRequiresVisibleFullNameAndNeverGuessesFormat() throws {
    let parent = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: parent) }
    let path = parent.appendingPathComponent("draft.v2.docx").path
    for shown in ["draft.v2", "draft.v2.docx"] {
        #expect(throws: ActionExecutionError.saveExtensionUnverified) {
            try proveNewSavePanelDestination(requested: path, filename: shown, directory: parent.path, extensionVisibility: "hidden")
        }
    }
    #expect(throws: ActionExecutionError.saveExtensionUnverified) {
        try proveNewSavePanelDestination(requested: path, filename: "draft.v2", directory: parent.path)
    }
    try proveNewSavePanelDestination(requested: path, filename: "draft.v2.docx", directory: parent.path, extensionVisibility: "shown")
    let option = StandardSavePanelField(identifier: "hideExtension", role: "AXCheckBox", reference: "toggle",
                                        value: "hidden", directoryPath: nil)
    guard case let .object(panel) = standardSavePanelFields([option], complete: true) else {
        Issue.record("missing standard-panel evidence"); return
    }
    #expect(panel["filename_extension_visibility"] == .string("hidden"))
    #expect(panel["hide_extension_element_ref"] == .string("toggle"))
}

@Test func guardedSaveEnterIsCanonicalBeforeNativeShapeAndDigestChecks() throws {
    func key(_ name: String) throws -> NativeAction {
        try NativeAction.parse(.object(["type": .string("keypress"), "key": .string(name),
                                       "save_new_copy_to": .string("/tmp/Copy.docx")]))
    }
    let enter = try key("enter"), canonical = try key("return")
    #expect(enter.key == "return")
    #expect(InputDispatcher.digest([enter]) == InputDispatcher.digest([canonical]))
}

@Test func attachedSavePanelAcceptsOnlyItsOwnFocusedControl() {
    let panel = AXUIElementCreateApplication(10), document = AXUIElementCreateApplication(11)
    let filename = AXUIElementCreateApplication(12), other = AXUIElementCreateApplication(13)
    func focused(_ window: AXUIElement, _ field: AXUIElement?) -> Bool {
        standardSavePanelHasFocus(panel, focusedWindow: window, focusedControl: field, pid: 10,
            sheetRoot: { _,_ in document }, nearestWindowMatches: { f, p, _ in CFEqual(f, filename) && CFEqual(p, panel) })
    }
    #expect(focused(panel, filename))
    #expect(focused(document, filename))
    #expect(!focused(document, nil))
    #expect(!focused(document, other))
    #expect(!focused(other, filename))
}
