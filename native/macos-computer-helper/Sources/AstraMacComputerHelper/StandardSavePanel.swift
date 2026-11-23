import ApplicationServices
import Foundation
import Darwin

/// Evidence from the exact, selected NSSavePanel. File-list entries, sidebar
/// selections, typed Go To text and a document's source path cannot establish its directory.
struct StandardSavePanelField {
    let identifier: String
    let role: String
    let reference: String
    let value: String?
    let directoryPath: String?
    var directorySource: String = "chooser_file_url"
}

func savePanelLeafName(_ value: String?) -> String? {
    guard let value, !value.isEmpty, value != ".", value != "..", value.utf8.count <= 255,
          !value.contains("/"), !value.contains("\\"), !value.contains(":"),
          !value.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains)
    else { return nil }
    return value
}

func savePanelLocalDirectory(_ raw: CFTypeRef?) -> String? {
    guard let raw else { return nil }
    let url: URL?
    if CFGetTypeID(raw) == CFURLGetTypeID() { url = raw as? URL }
    else if let s = raw as? String, s.utf8.count <= 4096, s.hasPrefix("file://") { url = URL(string: s) }
    else { return nil }
    guard let url, url.isFileURL, url.host == nil || url.host == "" || url.host == "localhost",
          url.query == nil, url.fragment == nil, url.path.hasPrefix("/"), url.path.utf8.count <= 4096,
          !url.path.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains)
    else { return nil }
    let path = url.standardizedFileURL
    guard let properties = try? path.resourceValues(forKeys: [.isDirectoryKey, .volumeIsLocalKey]),
          properties.isDirectory == true, properties.volumeIsLocal == true else { return nil }
    return path.path
}

func standardSavePanelFields(_ fields: [StandardSavePanelField], complete: Bool) -> JSONValue {
    var result: [String: JSONValue] = ["filename_status": .string("unverified"),
                                       "directory_status": .string("unverified")]
    guard complete else { return .object(result) }
    let hide = fields.filter { $0.role == kAXCheckBoxRole as String && $0.identifier == "hideExtension" }
    if hide.count == 1, let value = hide[0].value {
        result["filename_extension_visibility"] = .string(value)
        if !hide[0].reference.isEmpty { result["hide_extension_element_ref"] = .string(hide[0].reference) }
    }
    let commits = fields.filter { $0.identifier == "OKButton" && $0.role == kAXButtonRole as String }
    if commits.count == 1 && !commits[0].reference.isEmpty { result["commit_element_ref"] = .string(commits[0].reference) }
    let names = fields.filter { $0.identifier == "saveAsNameTextField" && $0.role == kAXTextFieldRole as String }
    if names.count == 1, let name = savePanelLeafName(names[0].value) {
        result["filename"] = .string(name)
        result["filename_status"] = .string("observed")
        if !names[0].reference.isEmpty { result["filename_element_ref"] = .string(names[0].reference) }
    }
    let selectors = fields.filter { $0.identifier == "where popup" && $0.role == kAXPopUpButtonRole as String }
    if selectors.count == 1 {
        let field = selectors[0]
        if !field.reference.isEmpty { result["directory_chooser_ref"] = .string(field.reference) }
        if let path = field.directoryPath {
            result["directory"] = .string(path)
            result["directory_status"] = .string(field.directorySource)
        } else if let label = savePanelLeafName(field.value) {
            result["directory_display_name"] = .string(label)
            result["directory_status"] = .string("display_name_only")
        }
    }
    return .object(result)
}

private func panelElements(_ e: AXUIElement, _ key: String, limit: Int) -> [AXUIElement]? {
    let (status, raw) = observationAXAttribute(e, key)
    guard status == .success, let values = raw as? NSArray, values.count <= limit else { return nil }
    let result = values.compactMap { decodeAXElement($0 as CFTypeRef) }
    return result.count == values.count ? result : nil
}

enum SaveBrowserSelection: Equatable {
    case empty
    case selectedVirtualItem
    case directory(URL)
    case file(URL)
}

func provenSaveBrowserDirectory(_ columns: [SaveBrowserSelection], popupLabel: String?,
                                displayName: (URL) -> String = { FileManager.default.displayName(atPath: $0.path) }) -> String? {
    guard let popupLabel, !popupLabel.isEmpty, columns.count >= 2 else { return nil }
    // The trailing column is the current location's contents. A selected file can
    // corroborate its parent, but cannot establish a location on its own.
    let branch = Array(columns.dropLast())
    guard case let .directory(folder)? = branch.last else { return nil }
    switch columns.last {
    case .empty?: break
    case let .file(file)? where file.deletingLastPathComponent().standardizedFileURL.path == folder.path: break
    default: return nil
    }
    // Finder can show a virtual volume or alias in earlier columns. Anchor
    // the selected folder's own absolute URL to its actual selected parent and
    // to the popup label, without inferring a path from those earlier names.
    guard !branch.contains(.empty) else { return nil }
    if branch.count >= 2 {
        guard case let .directory(parent) = branch[branch.count - 2],
              parent.path != folder.path, folder.deletingLastPathComponent().standardizedFileURL.path == parent.path
        else { return nil }
    } else if folder.path != "/" { return nil }
    guard popupLabel == folder.lastPathComponent || popupLabel == displayName(folder) else { return nil }
    return folder.path
}

private func panelRole(_ e: AXUIElement) -> String? {
    let read = AXNodeReader.stringAttribute(e, kAXRoleAttribute)
    return read.status == .complete ? read.value : nil
}

private func localItemURL(_ e: AXUIElement) -> URL? {
    let (status, raw) = observationAXAttribute(e, kAXURLAttribute)
    guard status == .success, let url = raw as? URL, url.isFileURL,
          url.host == nil || url.host == "" || url.host == "localhost",
          url.path.hasPrefix("/"), url.path.utf8.count <= 4096, url.query == nil, url.fragment == nil
    else { return nil }
    return url.standardizedFileURL.resolvingSymlinksInPath()
}

private func columnSelection(_ column: AXUIElement) -> SaveBrowserSelection? {
    let list: AXUIElement
    if panelRole(column) == kAXListRole as String { list = column }
    else {
        guard let children = panelElements(column, kAXChildrenAttribute, limit: 12) else { return nil }
        let lists = children.filter { panelRole($0) == kAXListRole as String }
        guard lists.count == 1 else { return nil }
        list = lists[0]
    }
    guard let selected = panelElements(list, kAXSelectedChildrenAttribute, limit: 2) else { return nil }
    if selected.isEmpty { return .empty }
    guard selected.count == 1 else { return nil }
    // In NSSavePanel column view the selected row may be a group with the file URL
    // on its text child. Do not examine siblings, sidebar pins or generic file-list items.
    var candidates = [selected[0]]
    if panelRole(selected[0]) == kAXGroupRole as String {
        guard let children = panelElements(selected[0], kAXChildrenAttribute, limit: 12) else { return nil }
        candidates += children.filter { panelRole($0) == kAXTextFieldRole as String }
    }
    let urls = Set(candidates.compactMap(localItemURL))
    if urls.isEmpty { return .selectedVirtualItem }
    guard urls.count == 1, let url = urls.first else { return nil }
    guard let props = try? url.resourceValues(forKeys: [.isDirectoryKey, .volumeIsLocalKey]),
          props.volumeIsLocal == true, let directory = props.isDirectory else { return .selectedVirtualItem }
    return directory ? .directory(url) : .file(url)
}

private func folderFromBrowser(_ browser: AXUIElement, popupLabel: String?, budget: AXObservationBudget) -> String? {
    func prove(_ columns: [AXUIElement]) -> String? {
        guard (2...48).contains(columns.count) else { return nil }
        var selections: [SaveBrowserSelection] = []
        for column in columns {
            guard budget.available, let selection = columnSelection(column) else { return nil }
            selections.append(selection)
        }
        return provenSaveBrowserDirectory(selections, popupLabel: popupLabel)
    }
    if let cols = panelElements(browser, kAXColumnsAttribute, limit: 48), let path = prove(cols) { return path }
    // Some versions expose columns as parallel scroll areas under the browser's
    // sole scroll container. Their window geometry must give one unambiguous order.
    guard let children = panelElements(browser, kAXChildrenAttribute, limit: 12) else { return nil }
    let viewports = children.filter { panelRole($0) == kAXScrollAreaRole as String }
    guard viewports.count == 1, let contents = panelElements(viewports[0], kAXChildrenAttribute, limit: 64) else { return nil }
    let scrolls = contents.filter { panelRole($0) == kAXScrollAreaRole as String }
    let measured = scrolls.compactMap { col in AXNodeReader.frameAttribute(col).map { (col, $0) } }.sorted { $0.1.minX < $1.1.minX }
    guard measured.count == scrolls.count,
          zip(measured, measured.dropFirst()).allSatisfy({ l,r in
              l.1.width > 0 && r.1.width > 0 && abs(l.1.minY-r.1.minY) < 2 && l.1.maxX <= r.1.minX+2
          }) else { return nil }
    return prove(measured.map { $0.0 })
}

struct LiveSavePanel {
    let fields: [StandardSavePanelField]
    let commitButton: AXUIElement?
    let defaultButton: AXUIElement?
    let filenameField: AXUIElement?
    var evidence: JSONValue { standardSavePanelFields(fields, complete: true) }
}

func readStandardSavePanel(_ root: AXUIElement, budget: AXObservationBudget,
                          reference: (AXUIElement) -> String? = { _ in nil }) -> LiveSavePanel? {
    guard budget.available else { return nil }
    let restore = budget.install()
    defer { restore() }
    let role = panelRole(root)
    guard role == kAXSheetRole as String || (role == kAXWindowRole as String &&
              AXNodeReader.stringAttribute(root, kAXSubroleAttribute).value == kAXDialogSubrole as String),
          AXNodeReader.stringAttribute(root, kAXIdentifierAttribute).value == "save-panel" else { return nil }
    let containers: Set<String> = ["AXGroup", "AXSplitGroup", "AXScrollArea", "AXToolbar", "AXBox"]
    var next = [root], fields: [StandardSavePanelField] = [], browsers: [AXUIElement] = [], commits: [AXUIElement] = []
    var nameFields: [AXUIElement] = []
    var visited = Set<AXUIElement>()
    while let element = next.popLast() {
        guard budget.available, visited.count < 192 else { return nil }
        guard visited.insert(element).inserted else { continue }
        guard let role = panelRole(element) else { return nil }
        if CFEqual(element, root) || containers.contains(role) {
            if let children = panelElements(element, kAXChildrenAttribute, limit: 128) { next += children }
            else { return nil }
        } else if role == kAXBrowserRole as String { browsers.append(element) }
        else if role == kAXCheckBoxRole as String {
            let id = AXNodeReader.stringAttribute(element, kAXIdentifierAttribute).value?.lowercased() ?? ""
            let title = AXNodeReader.stringAttribute(element, kAXTitleAttribute).value?.lowercased() ?? ""
            guard ["hideextension", "hideextensionbutton", "hide extension"].contains(id)
                    || ["hide extension", "隐藏扩展名", "隱藏副檔名", "隱藏延伸檔名"].contains(title),
                  let checked = AXNodeReader.boolAttribute(element, kAXValueAttribute)
            else { continue }
            fields.append(StandardSavePanelField(identifier: "hideExtension", role: role,
                reference: reference(element) ?? "", value: checked ? "hidden" : "shown", directoryPath: nil))
        }
        else if ["AXTextField", "AXPopUpButton", "AXButton"].contains(role) {
            let id = AXNodeReader.stringAttribute(element, kAXIdentifierAttribute)
            guard id.status == .complete, let identifier = id.value,
                  ["saveAsNameTextField","where popup","OKButton"].contains(identifier) else { continue }
            let subrole = AXNodeReader.stringAttribute(element, kAXSubroleAttribute)
            guard subrole.status == .complete, !keyboardFocusIsExplicitlySecure(role: role,subrole: subrole.value) else { continue }
            let value = AXNodeReader.stringAttribute(element, kAXValueAttribute)
            var path: String?
            if identifier == "where popup", role == kAXPopUpButtonRole as String {
                let (valueStatus, rawValue) = observationAXAttribute(element, kAXValueAttribute)
                let valueURL = valueStatus == .success ? savePanelLocalDirectory(rawValue) : nil
                let (urlStatus, rawURL) = observationAXAttribute(element, kAXURLAttribute)
                let pathURL = urlStatus == .success ? savePanelLocalDirectory(rawURL) : nil
                if let valueURL { path = pathURL == nil || pathURL == valueURL ? valueURL : nil }
                else if let pathURL, let label = value.value, value.status == .complete {
                    let url = URL(fileURLWithPath: pathURL)
                    if label == url.lastPathComponent || label == FileManager.default.displayName(atPath: pathURL) {
                        path = pathURL
                    }
                }
            }
            if identifier == "OKButton", role == kAXButtonRole as String { commits.append(element) }
            if identifier == "saveAsNameTextField", role == kAXTextFieldRole as String { nameFields.append(element) }
            fields.append(StandardSavePanelField(identifier: identifier, role: role, reference: reference(element) ?? "",
                value: value.status == .complete ? value.value : nil, directoryPath: path))
        }
    }
    let positions = fields.indices.filter { fields[$0].identifier == "where popup" && fields[$0].role == "AXPopUpButton" }
    if positions.count == 1, browsers.count == 1 {
        let i = positions[0]
        if let browserPath = folderFromBrowser(browsers[0], popupLabel: fields[i].value, budget: budget) {
            let previous = fields[i]
            let agrees = previous.directoryPath == nil || previous.directoryPath == browserPath
            fields[i] = StandardSavePanelField(identifier: previous.identifier, role: previous.role, reference: previous.reference,
                value: previous.value, directoryPath: agrees ? browserPath : nil, directorySource: "selected_browser_path")
        }
    }
    guard budget.available else { return nil }
    let defaultButton = decodeAXElement(observationAXAttribute(root,kAXDefaultButtonAttribute).1)
    return LiveSavePanel(fields: fields, commitButton: commits.count == 1 ? commits[0] : nil, defaultButton: defaultButton,
                         filenameField: nameFields.count == 1 ? nameFields[0] : nil)
}

func standardSavePanelObservation(tree: SerializedAXTree, window: AXUIElement,
                                  budget: AXObservationBudget) -> JSONValue? {
    let root = tree.root
    guard root.role == kAXSheetRole as String || (root.role == kAXWindowRole as String && root.subrole == kAXDialogSubrole as String),
          let source = root.sourceElement, CFEqual(source, window) else { return nil }
    return readStandardSavePanel(window, budget: budget, reference: tree.elementReference(matching:))?.evidence
}

func savePanelRequestedURL(_ path: String) -> URL? {
    guard path.hasPrefix("/"), !path.hasPrefix("//"), !path.hasSuffix("/"), path.utf8.count <= 4096,
          !path.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains),
          !path.split(separator: "/").contains(where: { $0 == "." || $0 == ".." }) else { return nil }
    let file = URL(fileURLWithPath: path)
    guard savePanelLeafName(file.lastPathComponent) != nil else { return nil }
    return file
}

/// A requested suffix is not proof of the format the app will append.
private func verifySavePanelExtension(_ file: URL, visibleName: String, visibility: String?) throws {
    if visibility == "hidden" || (visibility != "shown" && !file.pathExtension.isEmpty &&
        visibleName.utf8.elementsEqual(file.deletingPathExtension().lastPathComponent.utf8)) {
        throw ActionExecutionError.saveExtensionUnverified
    }
}

/// A guard, never a filesystem writer. Open and inspect only the actual selected
/// directory. AT_SYMLINK_NOFOLLOW also treats a dangling symlink as an existing destination.
func proveNewSavePanelDestination(requested: String, filename: String?, directory: String?, extensionVisibility: String? = nil) throws {
    guard let file = savePanelRequestedURL(requested), let name = savePanelLeafName(filename),
          let directory, !file.pathExtension.isEmpty else { throw ActionExecutionError.saveDestinationUnverified }
    try verifySavePanelExtension(file, visibleName: name, visibility: extensionVisibility)
    let actualParent = URL(fileURLWithPath: directory, isDirectory: true).standardizedFileURL.resolvingSymlinksInPath()
    let wantedParent = file.deletingLastPathComponent().standardizedFileURL.resolvingSymlinksInPath()
    guard name.utf8.elementsEqual(file.lastPathComponent.utf8), actualParent.path.utf8.elementsEqual(wantedParent.path.utf8)
    else { throw ActionExecutionError.saveDestinationMismatch }
    guard savePanelLocalDirectory(actualParent as NSURL) != nil else { throw ActionExecutionError.saveDestinationUnverified }
    let descriptor = open(actualParent.path, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW)
    guard descriptor >= 0 else { throw ActionExecutionError.saveDestinationUnverified }
    defer { close(descriptor) }
    var info = stat()
    if fstatat(descriptor, name, &info, AT_SYMLINK_NOFOLLOW) == 0 { throw ActionExecutionError.saveDestinationExists }
    guard errno == ENOENT else { throw ActionExecutionError.saveDestinationUnverified }
}

/// Some AppKit sheets keep the document as AXFocusedWindow. The focused control
/// must then belong to this exact sheet, not to the document or a further child sheet.
func standardSavePanelHasFocus(_ panel: AXUIElement, focusedWindow: AXUIElement?,
                               focusedControl: AXUIElement?, pid: pid_t,
                               sheetRoot: (AXUIElement, pid_t) -> AXUIElement? = { attachedSheetRoot($0, pid: $1) },
                               nearestWindowMatches: (AXUIElement, AXUIElement, pid_t) -> Bool = {
                                   fieldBelongsToNearestAXWindow($0, root: $1, pid: $2)
                               }) -> Bool {
    guard let focusedWindow else { return false }
    if CFEqual(focusedWindow, panel) { return true }
    guard let owner = sheetRoot(panel, pid), CFEqual(owner, focusedWindow), let focusedControl else { return false }
    return nearestWindowMatches(focusedControl, panel, pid)
}

func checkStandardSaveCommit(_ panel: LiveSavePanel, action: NativeAction, clicked: AXUIElement?,
                             exactFocusedPanel: Bool, focusedControl: AXUIElement?,
                             enabled: (AXUIElement) -> Bool? = { AXNodeReader.boolAttribute($0, kAXEnabledAttribute) },
                             readFilename: (AXUIElement) -> String? = {
                                 let value = AXNodeReader.stringAttribute($0, kAXValueAttribute)
                                 return value.status == .complete ? value.value : nil
                             }) throws {
    guard exactFocusedPanel, let requested = action.saveNewCopyTo, let commit = panel.commitButton,
          enabled(commit) == true else { throw ActionExecutionError.saveDestinationUnverified }
    switch action.kind {
    case .click:
        guard let clicked, CFEqual(clicked, commit) else { throw ActionExecutionError.saveDestinationUnverified }
    case .keypress:
        guard action.key == "return", action.modifiers.isEmpty, let button = panel.defaultButton, CFEqual(button, commit),
              let focusedControl, CFEqual(focusedControl,commit) || panel.filenameField.map({ CFEqual(focusedControl,$0) }) == true
        else { throw ActionExecutionError.saveDestinationUnverified }
    default: throw ActionExecutionError.invalidAction
    }
    guard case let .object(facts) = panel.evidence, case let .string(name)? = facts["filename"],
          case let .string(directory)? = facts["directory"] else { throw ActionExecutionError.saveDestinationUnverified }
    var visibility: String?
    if case let .string(state)? = facts["filename_extension_visibility"] { visibility = state }
    try proveNewSavePanelDestination(requested: requested, filename: name, directory: directory, extensionVisibility: visibility)
    // Directory discovery may perform several AX reads. Re-read the actual name
    // control after them, before the delivery backend does its final activity check.
    guard let field = panel.filenameField, let live = readFilename(field) else { throw ActionExecutionError.saveDestinationUnverified }
    if !live.utf8.elementsEqual(name.utf8) {
        if let file = savePanelRequestedURL(requested) { try verifySavePanelExtension(file, visibleName: live, visibility: nil) }
        throw ActionExecutionError.saveDestinationMismatch
    }
}
