@preconcurrency import ApplicationServices
import AppKit

enum WPSPanelField: Hashable {
    case filename, find, replacement

    static func label(_ text: String?) -> Self? {
        let name = (text ?? "").lowercased().replacingOccurrences(of: "[（(][a-z][)）][:：]?$", with: "",
            options: .regularExpression).trimmingCharacters(in: CharacterSet(charactersIn: " :："))
        switch name {
        case "查找内容", "find what": return .find
        case "替换为", "replace with": return .replacement
        default: return nil
        }
    }
}

private func wpsValue(_ element: AXUIElement) -> String? {
    let (status, raw) = observationAXAttribute(element, kAXValueAttribute)
    guard status == .success, let raw, CFGetTypeID(raw) == CFStringGetTypeID() else { return nil }
    let value = unsafeBitCast(raw, to: CFString.self)
    guard CFStringGetLength(value) <= maximumNativeTextCharacters * 2, let string = raw as? String,
          string.unicodeScalars.count <= maximumNativeTextCharacters else { return nil }
    return string
}

private func wpsChildren(_ element: AXUIElement) -> [AXUIElement]? {
    let (code, value) = observationAXAttribute(element, kAXChildrenAttribute)
    guard code == .success, let children = value as? [AXUIElement], children.count <= 1024 else { return nil }
    return children
}

private func wpsString(_ element: AXUIElement, _ attribute: String) -> String? {
    let result = AXNodeReader.stringAttribute(element, attribute)
    return result.status == .complete ? result.value : nil
}

struct WPSPanelPath {
    struct Edge {
        let parent: AXUIElement
        let index: Int
        let child: AXUIElement
    }
    let edges: [Edge]

    func matches(children: (AXUIElement) -> [AXUIElement]? = wpsChildren) -> Bool {
        edges.allSatisfy { edge in
            guard let live = children(edge.parent), live.indices.contains(edge.index),
                  CFEqual(live[edge.index], edge.child) else { return false }
            return live.filter { CFEqual($0, edge.child) }.count == 1
        }
    }
}

private struct WPSPanelControl {
    let element: AXUIElement
    let path: WPSPanelPath
    let field: WPSPanelField?
    let oldValue: String?
    let replaceButton: Bool
    let labelElement: AXUIElement?
    let labelIndex: Int?

    func validate(in panel: WPSPanelSnapshot, compareValue: Bool) throws {
        guard path.matches(), observedAXPID(element) == panel.pid,
              AXNodeReader.boolAttribute(element, kAXEnabledAttribute) == true,
              !secureActionIdentity(role: AXNodeReader.stringAttribute(element, kAXRoleAttribute),
                                    subrole: AXNodeReader.stringAttribute(element, kAXSubroleAttribute))
        else { throw ActionExecutionError.staleSnapshot }
        if let field {
            guard panel.uniqueFields.contains(field), let oldValue else { throw ActionExecutionError.observationRequired }
            if field == .filename {
                guard wpsString(element, kAXIdentifierAttribute) == "saveAsNameTextField" else { throw ActionExecutionError.staleSnapshot }
            } else {
                guard let parent = path.edges.last?.parent, let editors = wpsChildren(parent)?.filter({
                    wpsString($0, kAXRoleAttribute) == "AXTextField"
                }), editors.count == 1, CFEqual(editors[0], element) else { throw ActionExecutionError.staleSnapshot }
                if let labelElement, let index = labelIndex {
                    guard let group = path.edges.dropLast().last?.parent, let children = wpsChildren(group),
                          children.indices.contains(index), CFEqual(children[index], labelElement),
                          WPSPanelField.label(wpsValue(labelElement)) == field else { throw ActionExecutionError.staleSnapshot }
                } else if compareValue {
                    let labels = Set([wpsString(parent, kAXTitleAttribute), wpsString(parent, kAXDescriptionAttribute)].compactMap(WPSPanelField.label))
                    guard labels == [field] else { throw ActionExecutionError.staleSnapshot }
                }
            }
            if compareValue {
                guard let actual = wpsValue(element), actual.utf8.elementsEqual(oldValue.utf8) else { throw ActionExecutionError.staleSnapshot }
            }
        } else {
            guard AXNodeReader.actionNameResults(element).completeNames?.contains(kAXPressAction as String) == true else {
                throw ActionExecutionError.staleSnapshot
            }
            if replaceButton {
                guard panel.uniqueFields.isSuperset(of: [.find, .replacement]) else { throw ActionExecutionError.observationRequired }
                for control in panel.controls.values where control.field == .find || control.field == .replacement {
                    try control.validate(in: panel, compareValue: true)
                }
            }
        }
    }
}

/// Internal snapshot evidence. The public screenshot, a title and a remembered field value
/// cannot substitute for the retained element at the same position in the same window.
struct WPSPanelSnapshot {
    let pid: pid_t
    let originalFront: pid_t
    let window: AXUIElement
    fileprivate var controls: [String: WPSPanelControl] = [:]
    fileprivate var uniqueFields = Set<WPSPanelField>()

    static func capture(tree: SerializedAXTree, target: WindowTarget, front: pid_t?,
                        identifier: (AXUIElement) -> String? = { wpsString($0, kAXIdentifierAttribute) },
                        readValue: (AXUIElement) -> String? = wpsValue) -> Self? {
        guard let front, front > 0, front != target.pid, let root = tree.root.sourceElement,
              let selected = target.axElement, CFEqual(root, selected),
              tree.root.role == "AXWindow" || tree.root.role == "AXSheet" else { return nil }
        let save = identifier(root) == "save-panel"
        let title = (tree.root.title ?? tree.root.label ?? "").lowercased()
        let find = ["查找和替换", "查找与替换", "find & replace", "find and replace"].contains(title)
        guard save || find || (tree.root.subrole == "AXDialog" && AXNodeReader.boolAttribute(root, kAXModalAttribute) == true)
        else { return nil }
        var result = Self(pid: target.pid, originalFront: front, window: root)
        var occurrences: [WPSPanelField: Int] = [:]
        func visit(_ node: SerializedAXNode, ancestors: [SerializedAXNode], edges: [WPSPanelPath.Edge]) {
            guard let element = node.sourceElement else { return }
            var field: WPSPanelField?
            var labelNode: AXUIElement?
            var labelIndex: Int?
            if node.role == "AXTextField" {
                if save && identifier(element) == "saveAsNameTextField" { field = .filename }
                if find, let popup = ancestors.last, ["AXPopUpButton", "AXComboBox"].contains(popup.role),
                   popup.children.filter({ $0.role == "AXTextField" }).count == 1,
                   let group = ancestors.dropLast().last, let index = edges.dropLast().last?.index {
                    var labels = Set([popup.title, popup.label].compactMap(WPSPanelField.label))
                    if group.role == "AXGroup", index > 0 {
                        let preceding = group.children[index - 1]
                        if preceding.role == "AXStaticText", let label = WPSPanelField.label(preceding.value),
                           let source = preceding.sourceElement {
                            labels.insert(label); labelNode = source; labelIndex = index - 1
                        }
                    }
                    if labels.count == 1 { field = labels.first }
                }
            }
            if let field { occurrences[field, default: 0] += 1 }
            if field != nil || (node.role == "AXButton" && node.actions.contains(kAXPressAction as String)) {
                let liveValue = field == nil ? nil : readValue(element)
                let sameValue = liveValue.map { $0.utf8.elementsEqual((node.value ?? "").utf8) } ?? false
                let replace = find && node.role == "AXButton" && [node.title, node.label].contains {
                    ($0 ?? "").replacingOccurrences(of: "[（(][a-zA-Z][)）]$", with: "", options: .regularExpression)
                        .lowercased().trimmingCharacters(in: .whitespaces) == "replace" ||
                    ($0 ?? "").replacingOccurrences(of: "[（(][a-zA-Z][)）]$", with: "", options: .regularExpression) == "替换"
                }
                result.controls[node.elementRef.wireValue] = WPSPanelControl(element: element,
                    path: WPSPanelPath(edges: edges), field: field, oldValue: sameValue ? liveValue : nil,
                    replaceButton: replace, labelElement: labelNode, labelIndex: labelIndex)
            }
            for (index, child) in node.children.enumerated() {
                guard let source = child.sourceElement else { continue }
                visit(child, ancestors: ancestors + [node], edges: edges + [.init(parent: element, index: index, child: source)])
            }
        }
        visit(tree.root, ancestors: [], edges: [])
        result.uniqueFields = Set(occurrences.compactMap { $0.value == 1 ? $0.key : nil })
        return result
    }

    func handles(_ action: NativeAction) -> Bool {
        if action.kind == .type && action.replace == true { return true }
        guard action.kind == .click, action.x == nil, action.y == nil, let reference = action.elementRef,
              let control = controls[reference] else { return false }
        return control.field == nil
    }

    func windowIdentities() -> Set<AXUIElement>? {
        let (code, raw) = observationAXAttribute(AXUIElementCreateApplication(pid), kAXWindowsAttribute)
        guard code == .success, let windows = raw as? [AXUIElement], windows.count <= 256 else { return nil }
        return Set(windows)
    }

    func windowEffect(since prior: Set<AXUIElement>?) -> Bool {
        guard let prior, let after = windowIdentities() else { return false }
        if prior.contains(window) && !after.contains(window) { return true }
        let sheets = observationAXAttribute(window, "AXSheets").1 as? [AXUIElement] ?? []
        return after.subtracting(prior).contains { child in
            sheets.contains(child) || observedAXParent(child).map { CFEqual($0, window) } == true
        }
    }

    func validateFront(_ current: pid_t?) throws {
        guard current == originalFront && current != pid else { throw ActionExecutionError.staleSnapshot }
    }

    func boundField(reference: String) -> WPSPanelField? {
        guard let control = controls[reference], let field = control.field, control.oldValue != nil,
              uniqueFields.contains(field) else { return nil }
        return field
    }

    func validate(_ action: NativeAction, current: ActionElement, compareValue: Bool = true) throws {
        guard let reference = action.elementRef, let control = controls[reference], let ax = current.element,
              CFEqual(ax, control.element), current.enabled == true, current.keyboardTargetRejection == nil,
              (action.kind != .type || boundField(reference: reference) != nil),
              (control.field != nil) == (action.kind == .type) else { throw ActionExecutionError.observationRequired }
        try control.validate(in: self, compareValue: compareValue)
        if action.kind == .type {
            if AXTextComposition.observe(current) == .marked { throw ActionExecutionError.textCompositionActive }
            guard case .settable = SystemAXTextValueReplacer().preflight(to: ax) else { throw ActionExecutionError.observationRequired }
        }
    }

    func write(_ action: NativeAction, current: ActionElement, validate: () throws -> Void) throws -> ActionPerformance {
        guard let ref = action.elementRef, let expected = controls[ref]?.oldValue,
              let ax = current.element, let text = action.text else { throw ActionExecutionError.observationRequired }
        let result = try SystemAXTextValueReplacer().replace(text, to: ax, expectedValue: expected) {
            try validate()
            try self.validate(action, current: current, compareValue: false)
        }
        return ActionPerformance(inputStarted: result.inputStarted, effectVerification: result.effectVerification)
    }
}
