@preconcurrency import ApplicationServices
import CoreGraphics
import Foundation

let maximumAXDepth = 20
let maximumAXSubtreeDepth = 48
let maximumAXTraversalDepth = 64
let maximumAXNodes = 4_000
let maximumAXStringCharacters = 512
let maximumAXJSONBytes = 1024 * 1024
let maximumAXReadStringBytes = 16 * 1024
let maximumAXReadTreeStringBytes = 1024 * 1024
let maximumAXTextDetailStructuralStringBytes = 4 * 1024
let maximumAXTextDetailValueBytes = 256 * 1024
let maximumAXTextDetailAggregateTextBytes = 4 * 1024 * 1024
let maximumAXTextDetailJSONBytes = 8 * 1024 * 1024
let maximumAXTextDetailDuration: TimeInterval = 5
let canonicalAXUnknownIdentity = "AXUnknown"
// The wire form is deliberately tighter than the advertised maxima. This
// fixed per-node budget makes the final JSON bound deterministic in one pass.
let maximumSerializedAXNodes = 1_000
let minimumAXNodesPerTopBranch = 150
private let maximumSerializedAXActions = 4
let maximumObservedApplications = 100
let maximumObservedWindows = 200

struct ElementReference: Equatable {
    let snapshotID: String
    let ordinal: Int
    let wireValue: String

    init(snapshotID: String, ordinal: Int, wireValue: String = "ax_\(UUID().uuidString.lowercased())") {
        self.snapshotID = snapshotID
        self.ordinal = ordinal
        self.wireValue = wireValue
    }

    func isValid(for snapshotID: String) -> Bool {
        self.snapshotID == snapshotID
    }
}

struct AXNode {
    let role: String
    let subrole: String?
    let label: String?
    let title: String?
    let help: String?
    let value: String?
    let enabled: Bool?
    let focused: Bool?
    let actions: [String]
    let bounds: CGRect
    let children: [AXNode]
    let sourceElement: AXUIElement?
    let childrenTruncated: Bool

    init(
        role: String,
        subrole: String? = nil,
        label: String? = nil,
        title: String? = nil,
        help: String? = nil,
        value: String? = nil,
        enabled: Bool? = nil,
        focused: Bool? = nil,
        actions: [String] = [],
        bounds: CGRect,
        children: [AXNode] = [],
        sourceElement: AXUIElement? = nil,
        childrenTruncated: Bool = false
    ) {
        self.role = role
        self.subrole = subrole
        self.label = label
        self.title = title
        self.help = help
        self.value = value
        self.enabled = enabled
        self.focused = focused
        self.actions = actions
        self.bounds = bounds
        self.children = children
        self.sourceElement = sourceElement
        self.childrenTruncated = childrenTruncated
    }
}

struct SerializedAXNode {
    let index: Int
    let role: String
    let subrole: String?
    let label: String?
    let title: String?
    let help: String?
    let value: String?
    let enabled: Bool?
    let focused: Bool?
    let actions: [String]
    let bounds: CGRect
    let elementRef: ElementReference
    let children: [SerializedAXNode]
    let sourceElement: AXUIElement?
    var childrenTruncated: Bool = false

    func asJSON() -> JSONValue {
        var result: [String: JSONValue] = [
            "index": .number(Double(index)),
            "role": .string(role),
            "bounds": CGRectJSON.encode(bounds),
            "element_ref": .string(elementRef.wireValue),
        ]
        if let subrole { result["subrole"] = .string(subrole) }
        if let label { result["label"] = .string(label) }
        if let title { result["title"] = .string(title) }
        if let help { result["help"] = .string(help) }
        if let value { result["value"] = .string(value) }
        if role == "AXRadioButton" || role == "AXCheckBox" {
            if let sourceElement {
                // Observation correlation only. Actions still require a fresh
                // snapshot ref; this token cannot authorize or locate input.
                result["observation_identity"] = .string("\(ProcessInfo.processInfo.processIdentifier):\(CFHash(sourceElement))")
            }
            if value == "true" || value.flatMap(Double.init) == 1 { result["checked"] = .bool(true) }
            else if value == "false" || value.flatMap(Double.init) == 0 { result["checked"] = .bool(false) }
        }
        if let enabled { result["enabled"] = .bool(enabled) }
        if let focused { result["focused"] = .bool(focused) }
        if !actions.isEmpty { result["actions"] = .array(actions.map(JSONValue.string)) }
        if !children.isEmpty { result["children"] = .array(children.map { $0.asJSON() }) }
        if childrenTruncated { result["children_truncated"] = .bool(true) }
        return .object(result)
    }
}

struct SerializedAXTree {
    let root: SerializedAXNode
    let nodeCount: Int
    let jsonByteCount: Int

    var role: String { root.role }
    var label: String? { root.label }
    var value: String? { root.value }

    func asJSON() -> JSONValue { root.asJSON() }

    func references() -> [String: SnapshotElement] {
        var values: [String: SnapshotElement] = [:]
        collect(root, into: &values)
        return values
    }

    func indexReferences() -> [Int: String] {
        var values: [Int: String] = [:]
        collectIndexes(root, into: &values)
        return values
    }

    private func collectIndexes(_ node: SerializedAXNode, into values: inout [Int: String]) {
        values[node.index] = node.elementRef.wireValue
        for child in node.children { collectIndexes(child, into: &values) }
    }

    func elementReference(matching element: AXUIElement) -> String? {
        var remaining = maximumSerializedAXNodes
        var stack = [root]
        while let node = stack.popLast(), remaining > 0 {
            remaining -= 1
            if let sourceElement = node.sourceElement, CFEqual(sourceElement, element) {
                return node.elementRef.wireValue
            }
            stack.append(contentsOf: node.children.reversed())
        }
        return nil
    }

    private func collect(_ node: SerializedAXNode, into values: inout [String: SnapshotElement]) {
        values[node.elementRef.wireValue] = SnapshotElement(
            element: node.sourceElement,
            bounds: node.bounds,
            role: node.role,
            subrole: node.subrole,
            enabled: node.enabled,
            actions: node.actions,
            childReferences: node.children.map(\.elementRef.wireValue)
        )
        for child in node.children { collect(child, into: &values) }
    }
}

struct DefaultButtonDisclosure: Equatable {
    let hasDefaultButton: Bool
    let elementRef: String?
}

func trustedDefaultButtonDisclosure(
    window: AXUIElement,
    tree: SerializedAXTree,
    copyAttributeValue: (AXUIElement, String) -> (AXError, CFTypeRef?) = copyAXAttributeValue
) -> DefaultButtonDisclosure {
    let (defaultError, defaultValue) = copyAttributeValue(window, kAXDefaultButtonAttribute)
    guard defaultError == .success,
          let defaultButton = decodeAXElement(defaultValue)
    else {
        return DefaultButtonDisclosure(hasDefaultButton: false, elementRef: nil)
    }
    let (ownerError, ownerValue) = copyAttributeValue(defaultButton, kAXWindowAttribute)
    guard ownerError == .success,
          let owner = decodeAXElement(ownerValue),
          CFEqual(owner, window)
    else {
        return DefaultButtonDisclosure(hasDefaultButton: true, elementRef: nil)
    }
    return DefaultButtonDisclosure(
        hasDefaultButton: true,
        elementRef: tree.elementReference(matching: defaultButton)
    )
}

private func copyAXAttributeValue(
    _ element: AXUIElement,
    _ attribute: String
) -> (AXError, CFTypeRef?) {
    observationAXAttribute(element, attribute)
}

struct SnapshotElement {
    let element: AXUIElement?
    let bounds: CGRect
    let role: String
    let subrole: String?
    let enabled: Bool?
    let actions: [String]
    let childReferences: [String]

    init(
        element: AXUIElement?,
        bounds: CGRect,
        role: String = "AXUnknown",
        subrole: String? = nil,
        enabled: Bool? = nil,
        actions: [String] = [],
        childReferences: [String] = []
    ) {
        self.element = element
        self.bounds = bounds
        self.role = role
        self.subrole = subrole
        self.enabled = enabled
        self.actions = actions
        self.childReferences = childReferences
    }
}

enum AXScrollDirection: Equatable {
    case increment
    case decrement

    var pageSubrole: String { self == .increment ? "AXIncrementPage" : "AXDecrementPage" }
    var arrowSubrole: String { self == .increment ? "AXIncrementArrow" : "AXDecrementArrow" }
}

struct AXScrollPressReferenceTarget: Equatable {
    let ownerReference: String
    let buttonReference: String
}

func selectAXScrollPressReferenceTarget(
    rootReference: String,
    direction: AXScrollDirection,
    references: [String: SnapshotElement]
) -> AXScrollPressReferenceTarget? {
    guard references[rootReference] != nil else { return nil }
    var stack = [rootReference]
    var visited = Set<String>()
    var scrollbars: [(String, SnapshotElement)] = []
    while let reference = stack.popLast(), visited.count < references.count {
        guard visited.insert(reference).inserted,
              let element = references[reference]
        else { continue }
        if element.role == kAXScrollBarRole as String, element.enabled != false {
            scrollbars.append((reference, element))
        }
        stack.append(contentsOf: element.childReferences.reversed())
    }

    func candidates(subrole: String) -> [AXScrollPressReferenceTarget] {
        scrollbars.flatMap { ownerReference, owner in
            owner.childReferences.compactMap { buttonReference in
                guard let button = references[buttonReference],
                      button.role == kAXButtonRole as String,
                      button.subrole == subrole,
                      button.enabled != false,
                      button.actions.contains(kAXPressAction as String)
                else { return nil }
                return AXScrollPressReferenceTarget(
                    ownerReference: ownerReference,
                    buttonReference: buttonReference
                )
            }
        }
    }

    let page = candidates(subrole: direction.pageSubrole)
    if page.count == 1 { return page[0] }
    if !page.isEmpty { return nil }
    let arrow = candidates(subrole: direction.arrowSubrole)
    return arrow.count == 1 ? arrow[0] : nil
}

enum AXSerializer {
    static func serialize(_ node: AXNode, snapshotID: String = "test-snapshot") -> SerializedAXTree {
        let sourceCount = countNodes(node, limit: maximumSerializedAXNodes)
        let nodeTextBudget = max(0, min(16_384, 400_000 / max(sourceCount, 1) - 180))
        var state = State(snapshotID: snapshotID, nodeTextBudget: nodeTextBudget,
            choiceLabelBudget: min(512, 128_000 / max(choiceNodeCount(node), 1)))
        let root = state.visit(node, depth: 0, budget: maximumSerializedAXNodes)?.node ?? SerializedAXNode(
            index: 1,
            role: "AXUnknown",
            subrole: nil,
            label: nil,
            title: nil,
            help: nil,
            value: nil,
            enabled: nil,
            focused: nil,
            actions: [],
            bounds: .zero,
            elementRef: ElementReference(snapshotID: snapshotID, ordinal: 0),
            children: [],
            sourceElement: nil
        )
        let json = root.asJSON()
        if let byteCount = try? JSONEncoder().encode(json).count, byteCount <= maximumAXJSONBytes {
            return SerializedAXTree(root: root, nodeCount: state.nodeCount, jsonByteCount: byteCount)
        }
        let fallback = SerializedAXNode(index: 1, role: "AXWindow", subrole: nil, label: nil, title: nil, help: nil, value: nil, enabled: nil, focused: nil, actions: [], bounds: .zero, elementRef: ElementReference(snapshotID: snapshotID, ordinal: 0), children: [], sourceElement: node.sourceElement)
        let fallbackByteCount: Int
        do {
            fallbackByteCount = try JSONEncoder().encode(fallback.asJSON()).count
        } catch {
            // The fallback contains only finite constants and a bounded UUID,
            // so this branch is defensive and never misreports zero bytes.
            fallbackByteCount = maximumAXJSONBytes
        }
        return SerializedAXTree(root: fallback, nodeCount: 1, jsonByteCount: fallbackByteCount)
    }

    private struct State {
        let snapshotID: String
        let nodeTextBudget: Int
        let choiceLabelBudget: Int
        var nodeCount = 0

        mutating func visit(_ node: AXNode, depth: Int, budget: Int) -> (node: SerializedAXNode, used: Int)? {
            guard depth < maximumAXTraversalDepth,
                  nodeCount < maximumSerializedAXNodes,
                  budget > 0
            else { return nil }
            let ordinal = nodeCount
            nodeCount += 1
            let secure = node.role.localizedCaseInsensitiveContains("secure") ||
                (node.subrole?.localizedCaseInsensitiveContains("secure") ?? false)
            var children: [SerializedAXNode]
            var used = 1
            if depth == 0, !node.children.isEmpty {
                // The root's first-level branches (menu bar, toolbar, content,
                // status bar) each reserve a minimum quota before the rest of
                // the budget is shared out. A single oversized branch must not
                // starve later branches: paging controls and status rows used
                // to disappear entirely whenever an early branch consumed the
                // whole node budget.
                let branches = visitTopLevelBranches(node.children, depth: depth + 1, budget: budget - 1)
                children = branches.map(\.node)
                used += branches.reduce(0) { $0 + $1.used }
            } else {
                children = []
                var remaining = budget - 1
                for child in node.children {
                    guard remaining > 0 else { break }
                    if let built = visit(child, depth: depth + 1, budget: remaining) {
                        children.append(built.node)
                        used += built.used
                        remaining -= built.used
                    }
                }
            }
            let candidateFields: [String?] = [node.role, node.subrole, node.label, node.title, node.help, secure ? nil : node.value]
                + node.actions.prefix(maximumSerializedAXActions).map(Optional.some)
            var quota = TextQuota(bytes: nodeTextBudget, fields: candidateFields.compactMap { $0 }.count)
            let role = quota.take(node.role) ?? "AX"
            let subrole = quota.take(node.subrole)
            let compactLabel = quota.take(node.label)
            let label = (node.role == "AXRadioButton" || node.role == "AXCheckBox") && !secure
                ? node.label.map { truncateUTF8Linearly($0, maximumCharacters: maximumAXStringCharacters, maximumBytes: choiceLabelBudget) }
                : compactLabel
            let compactTitle = quota.take(node.title)
            let title = (node.role == "AXRadioButton" || node.role == "AXCheckBox") && !secure && node.label == nil
                ? node.title.map { truncateUTF8Linearly($0, maximumCharacters: maximumAXStringCharacters, maximumBytes: choiceLabelBudget) }
                : compactTitle
            let help = quota.take(node.help)
            let value = secure ? "<redacted>" : quota.take(node.value)
            let actions = node.actions.prefix(maximumSerializedAXActions).compactMap { quota.take($0) }
            return (SerializedAXNode(
                index: ordinal + 1,
                role: role,
                subrole: subrole,
                label: label,
                title: title,
                help: help,
                value: value,
                enabled: node.enabled,
                focused: node.focused,
                actions: actions,
                bounds: node.bounds,
                elementRef: ElementReference(snapshotID: snapshotID, ordinal: ordinal),
                children: children,
                sourceElement: node.sourceElement,
                childrenTruncated: node.childrenTruncated || children.count < node.children.count
            ), used)
        }

        private mutating func visitTopLevelBranches(
            _ branches: [AXNode],
            depth: Int,
            budget: Int
        ) -> [(node: SerializedAXNode, used: Int)] {
            guard !branches.isEmpty, budget > 0 else { return [] }
            let sizes = branches.map { AXSerializer.countNodes($0, limit: maximumSerializedAXNodes) }
            var budgets = [Int](repeating: 0, count: branches.count)
            var allocated = 0
            for index in branches.indices {
                let grant = min(sizes[index], minimumAXNodesPerTopBranch, max(0, budget - allocated))
                budgets[index] = grant
                allocated += grant
            }
            let needs = branches.indices.map { index in max(0, sizes[index] - budgets[index]) }
            let totalNeeds = needs.reduce(0, +)
            if totalNeeds > 0 {
                let leftover = max(0, budget - allocated)
                for index in branches.indices {
                    let extra = min(needs[index], leftover * needs[index] / totalNeeds)
                    budgets[index] += extra
                    allocated += extra
                }
            }
            var results = [(node: SerializedAXNode, used: Int)]()
            for index in branches.indices where budgets[index] > 0 {
                if let built = visit(branches[index], depth: depth, budget: budgets[index]) {
                    results.append(built)
                }
            }
            return results
        }
    }

    private struct TextQuota {
        var bytes: Int
        var fields: Int
        mutating func take(_ value: String?) -> String? {
            defer { fields = max(0, fields - 1) }
            guard let value else { return nil }
            let byteLimit = max(0, bytes / max(fields, 1))
            let result = truncateUTF8Linearly(
                value,
                maximumCharacters: maximumAXStringCharacters,
                maximumBytes: byteLimit
            )
            bytes -= result.lengthOfBytes(using: .utf8)
            return result.isEmpty ? nil : result
        }
    }

    private static func countNodes(_ node: AXNode, limit: Int) -> Int {
        var count = 0
        func visit(_ current: AXNode, depth: Int) {
            guard count < limit, depth < maximumAXTraversalDepth else { return }
            count += 1
            for child in current.children { visit(child, depth: depth + 1) }
        }
        visit(node, depth: 0)
        return count
    }
}

enum AXTextDetailSerializationError: Error, Equatable {
    case invalidSnapshotID
    case invalidGeometry
    case attributeReadFailed
    case childrenReadFailed
    case encodingFailed
    case finalByteLimitExceeded
}

enum AXTextDetailChildrenStatus: Equatable {
    case complete
    case truncated
    case failed
}

enum AXTextDetailAttributeStatus: Equatable {
    case complete
    case truncated
    case missing
    case unreadable
    case hardFailure
    case invalid
}

struct AXTextDetailStringResult: Equatable {
    let value: String?
    let status: AXTextDetailAttributeStatus
}

struct AXTextDetailBoolResult: Equatable {
    let value: Bool?
    let status: AXTextDetailAttributeStatus
}

struct AXTextDetailBoundsResult: Equatable {
    let value: CGRect?
    let status: AXTextDetailAttributeStatus
}

struct AXTextDetailChildrenResult {
    let values: [any AXTextDetailAttributeProvider]
    let status: AXTextDetailChildrenStatus
}

protocol AXTextDetailAttributeProvider: AnyObject {
    func stringValue(for attribute: String, maximumBytes: Int) -> AXTextDetailStringResult
    func boolValue(for attribute: String) -> AXTextDetailBoolResult
    func bounds() -> AXTextDetailBoundsResult
    func children(maximumCount: Int) -> AXTextDetailChildrenResult
}

struct AXTextDetailEnvelope {
    let data: Data
    let nodeCount: Int
    let maxDepthObserved: Int
    let truncated: Bool
    let truncationReasons: [String]
}

enum AXTextDetailSerializer {
    static func serialize(
        root: AXUIElement,
        snapshotID: String,
        clock: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }
    ) throws -> AXTextDetailEnvelope {
        guard !snapshotID.isEmpty, snapshotID.unicodeScalars.count <= 256 else {
            throw AXTextDetailSerializationError.invalidSnapshotID
        }
        let start = clock()
        let deadline = start.isFinite ? start + maximumAXTextDetailDuration : -.infinity
        return try serialize(
            provider: SystemAXTextDetailAttributeProvider(
                element: root,
                remainingTime: {
                    let now = clock()
                    let remaining = deadline - now
                    return now.isFinite && remaining.isFinite && remaining > 0 ? remaining : nil
                }
            ),
            snapshotID: snapshotID,
            clock: clock,
            deadline: deadline
        )
    }

    static func serialize(
        provider: any AXTextDetailAttributeProvider,
        snapshotID: String,
        clock: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }
    ) throws -> AXTextDetailEnvelope {
        guard !snapshotID.isEmpty, snapshotID.unicodeScalars.count <= 256 else {
            throw AXTextDetailSerializationError.invalidSnapshotID
        }
        let start = clock()
        return try serialize(
            provider: provider,
            snapshotID: snapshotID,
            clock: clock,
            deadline: start.isFinite ? start + maximumAXTextDetailDuration : -.infinity
        )
    }

    private static func serialize(
        provider: any AXTextDetailAttributeProvider,
        snapshotID: String,
        clock: @escaping () -> TimeInterval,
        deadline: TimeInterval
    ) throws -> AXTextDetailEnvelope {
        var state = State(
            clock: clock,
            deadline: deadline
        )
        let root = try state.visit(provider, depth: 0)
        var retainedNodeCount = state.nodeCount
        var retainedMaxDepth = state.maxDepthObserved
        var data = try encode(
            snapshotID: snapshotID,
            root: root?.asJSON(retainingOrdinalsBelow: retainedNodeCount) ?? .object([:]),
            nodeCount: retainedNodeCount,
            maxDepthObserved: retainedMaxDepth,
            reasons: state.reasons
        )

        if data.count > maximumAXTextDetailJSONBytes, let root {
            state.reasons.insert("final_byte_limit")
            var low = 1
            var high = retainedNodeCount
            var bestData: Data?
            var bestNodeCount = 0
            var bestMaxDepth = 0
            while low <= high {
                let candidate = low + (high - low) / 2
                let candidateMaxDepth = state.depths.prefix(candidate).max() ?? 0
                let candidateData = try encode(
                    snapshotID: snapshotID,
                    root: root.asJSON(retainingOrdinalsBelow: candidate),
                    nodeCount: candidate,
                    maxDepthObserved: candidateMaxDepth,
                    reasons: state.reasons
                )
                if candidateData.count <= maximumAXTextDetailJSONBytes {
                    bestData = candidateData
                    bestNodeCount = candidate
                    bestMaxDepth = candidateMaxDepth
                    low = candidate + 1
                } else {
                    high = candidate - 1
                }
            }
            guard let bestData else {
                throw AXTextDetailSerializationError.finalByteLimitExceeded
            }
            data = bestData
            retainedNodeCount = bestNodeCount
            retainedMaxDepth = bestMaxDepth
        }
        guard data.count <= maximumAXTextDetailJSONBytes else {
            throw AXTextDetailSerializationError.finalByteLimitExceeded
        }

        let orderedReasons = textDetailTruncationReasonOrder.filter(state.reasons.contains)
        return AXTextDetailEnvelope(
            data: data,
            nodeCount: retainedNodeCount,
            maxDepthObserved: retainedMaxDepth,
            truncated: !orderedReasons.isEmpty,
            truncationReasons: orderedReasons
        )
    }

    private static func encode(
        snapshotID: String,
        root: JSONValue,
        nodeCount: Int,
        maxDepthObserved: Int,
        reasons: Set<String>
    ) throws -> Data {
        let orderedReasons = textDetailTruncationReasonOrder.filter(reasons.contains)
        let envelope = JSONValue.object([
            "schema_version": .number(1),
            "snapshot_id": .string(snapshotID),
            "coverage": .string("reported_ax_subtree"),
            "limits": .object([
                "maximum_depth": .number(Double(maximumAXDepth)),
                "maximum_nodes": .number(Double(maximumAXNodes)),
                "maximum_structural_string_bytes": .number(Double(maximumAXTextDetailStructuralStringBytes)),
                "maximum_value_bytes": .number(Double(maximumAXTextDetailValueBytes)),
                "maximum_aggregate_text_bytes": .number(Double(maximumAXTextDetailAggregateTextBytes)),
                "maximum_final_bytes": .number(Double(maximumAXTextDetailJSONBytes)),
                "wall_clock_ms": .number(maximumAXTextDetailDuration * 1_000),
            ]),
            "stats": .object([
                "node_count": .number(Double(nodeCount)),
                "max_depth_observed": .number(Double(maxDepthObserved)),
                "truncated": .bool(!orderedReasons.isEmpty),
                "truncation_reasons": .array(orderedReasons.map(JSONValue.string)),
            ]),
            "root": root,
        ])
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        do {
            return try encoder.encode(envelope)
        } catch {
            throw AXTextDetailSerializationError.encodingFailed
        }
    }

    private final class DetailNode {
        let ordinal: Int
        let values: [String: JSONValue]
        let children: [DetailNode]

        init(ordinal: Int, values: [String: JSONValue], children: [DetailNode]) {
            self.ordinal = ordinal
            self.values = values
            self.children = children
        }

        func asJSON(retainingOrdinalsBelow limit: Int) -> JSONValue {
            var result = values
            let retainedChildren = children
                .filter { $0.ordinal < limit }
                .map { $0.asJSON(retainingOrdinalsBelow: limit) }
            if !retainedChildren.isEmpty {
                result["children"] = .array(retainedChildren)
            }
            return .object(result)
        }
    }

    private struct TakenText {
        let value: String?
        let completeAndNonempty: Bool
    }

    private struct State {
        let clock: () -> TimeInterval
        let deadline: TimeInterval
        var nodeCount = 0
        var maxDepthObserved = 0
        var depths: [Int] = []
        var remainingAggregateTextBytes = maximumAXTextDetailAggregateTextBytes
        var reasons = Set<String>()
        var expired = false

        mutating func visit(
            _ provider: any AXTextDetailAttributeProvider,
            depth: Int
        ) throws -> DetailNode? {
            guard nodeCount < maximumAXNodes else {
                reasons.insert("node_limit")
                return nil
            }
            guard hasTimeRemaining() else { return nil }
            guard remainingAggregateTextBytes >= canonicalAXUnknownIdentity.utf8.count else {
                reasons.insert("aggregate_text_limit")
                return nil
            }

            let ordinal = nodeCount
            nodeCount += 1
            maxDepthObserved = max(maxDepthObserved, depth)
            depths.append(depth)

            let rawRole = try read(
                provider,
                attribute: kAXRoleAttribute,
                maximumBytes: maximumAXTextDetailStructuralStringBytes
            )
            let role = canonicalizeRole(take(
                rawRole,
                byteLimit: maximumAXTextDetailStructuralStringBytes,
                reason: "structural_string_limit"
            ))
            let rawSubrole = try read(
                provider,
                attribute: kAXSubroleAttribute,
                maximumBytes: maximumAXTextDetailStructuralStringBytes
            )
            let subrole = canonicalizeSubrole(take(
                rawSubrole,
                byteLimit: maximumAXTextDetailStructuralStringBytes,
                reason: "structural_string_limit"
            ))
            let secure = !role.completeAndNonempty || !subrole.completeAndNonempty ||
                role.value == canonicalAXUnknownIdentity ||
                subrole.value == canonicalAXUnknownIdentity ||
                role.value?.localizedCaseInsensitiveContains("secure") == true ||
                subrole.value?.localizedCaseInsensitiveContains("secure") == true

            var values: [String: JSONValue] = [
                "node_id": .string("node_\(ordinal)"),
                "role": .string(role.value ?? canonicalAXUnknownIdentity),
            ]
            if let value = subrole.value { values["subrole"] = .string(value) }

            if let boundsResult = try providerCall(
                { provider.bounds() },
                classifyFailure: attributeFailure
            ) {
                switch boundsResult.status {
                case .hardFailure:
                    throw AXTextDetailSerializationError.attributeReadFailed
                case .invalid:
                    throw AXTextDetailSerializationError.invalidGeometry
                case .complete:
                    guard let bounds = boundsResult.value,
                          bounds.origin.x.isFinite, bounds.origin.y.isFinite,
                          bounds.width.isFinite, bounds.height.isFinite,
                          bounds.width >= 0, bounds.height >= 0
                    else { throw AXTextDetailSerializationError.invalidGeometry }
                    values["bounds"] = CGRectJSON.encode(bounds)
                case .missing, .unreadable, .truncated:
                    break
                }
            }

            try takeStructural(provider, attribute: kAXDescriptionAttribute, key: "label", into: &values)
            try takeStructural(provider, attribute: kAXTitleAttribute, key: "title", into: &values)
            try takeStructural(provider, attribute: kAXHelpAttribute, key: "help", into: &values)

            if secure {
                values["redacted"] = .bool(true)
            } else {
                let value = take(
                    try read(
                        provider,
                        attribute: kAXValueAttribute,
                        maximumBytes: maximumAXTextDetailValueBytes
                    ),
                    byteLimit: maximumAXTextDetailValueBytes,
                    reason: "value_limit"
                ).value
                if let value { values["value"] = .string(value) }
            }

            try takeBool(provider, attribute: kAXEnabledAttribute, key: "enabled", into: &values)
            try takeBool(provider, attribute: kAXFocusedAttribute, key: "focused", into: &values)

            var children: [DetailNode] = []
            if depth >= maximumAXDepth {
                guard let childResult = try providerCall(
                    { provider.children(maximumCount: 1) },
                    classifyFailure: childrenFailure
                ) else {
                    return DetailNode(ordinal: ordinal, values: values, children: children)
                }
                guard childResult.values.count <= 1 else {
                    throw AXTextDetailSerializationError.childrenReadFailed
                }
                switch childResult.status {
                case .failed:
                    throw AXTextDetailSerializationError.childrenReadFailed
                case .truncated:
                    reasons.insert("depth_limit")
                case .complete:
                    if !childResult.values.isEmpty { reasons.insert("depth_limit") }
                }
            } else if hasTimeRemaining() {
                let remaining = maximumAXNodes - nodeCount
                guard let childResult = try providerCall(
                    { provider.children(maximumCount: remaining) },
                    classifyFailure: childrenFailure
                ) else {
                    return DetailNode(ordinal: ordinal, values: values, children: children)
                }
                guard childResult.values.count <= remaining else {
                    throw AXTextDetailSerializationError.childrenReadFailed
                }
                switch childResult.status {
                case .failed:
                    throw AXTextDetailSerializationError.childrenReadFailed
                case .truncated:
                    reasons.insert("node_limit")
                case .complete:
                    break
                }
                for child in childResult.values.prefix(remaining) {
                    guard hasTimeRemaining() else { break }
                    guard nodeCount < maximumAXNodes else {
                        reasons.insert("node_limit")
                        break
                    }
                    if let child = try visit(child, depth: depth + 1) {
                        children.append(child)
                    }
                }
            }
            return DetailNode(ordinal: ordinal, values: values, children: children)
        }

        private mutating func read(
            _ provider: any AXTextDetailAttributeProvider,
            attribute: String,
            maximumBytes: Int
        ) throws -> AXTextDetailStringResult? {
            guard let result = try providerCall(
                { provider.stringValue(for: attribute, maximumBytes: maximumBytes) },
                classifyFailure: attributeFailure
            ) else { return nil }
            return result
        }

        private mutating func takeStructural(
            _ provider: any AXTextDetailAttributeProvider,
            attribute: String,
            key: String,
            into result: inout [String: JSONValue]
        ) throws {
            let value = take(
                try read(
                    provider,
                    attribute: attribute,
                    maximumBytes: maximumAXTextDetailStructuralStringBytes
                ),
                byteLimit: maximumAXTextDetailStructuralStringBytes,
                reason: "structural_string_limit"
            ).value
            if let value { result[key] = .string(value) }
        }

        private mutating func takeBool(
            _ provider: any AXTextDetailAttributeProvider,
            attribute: String,
            key: String,
            into result: inout [String: JSONValue]
        ) throws {
            guard let value = try providerCall(
                { provider.boolValue(for: attribute) },
                classifyFailure: attributeFailure
            ) else { return }
            switch value.status {
            case .hardFailure:
                throw AXTextDetailSerializationError.attributeReadFailed
            case .invalid:
                throw AXTextDetailSerializationError.invalidGeometry
            case .complete:
                if let bool = value.value { result[key] = .bool(bool) }
            case .missing, .unreadable, .truncated:
                break
            }
        }

        private mutating func take(
            _ source: AXTextDetailStringResult?,
            byteLimit: Int,
            reason: String
        ) -> TakenText {
            guard let source,
                  source.status == .complete || source.status == .truncated,
                  let sourceValue = source.value,
                  !sourceValue.isEmpty
            else {
                return TakenText(value: nil, completeAndNonempty: false)
            }
            let field = truncateUTF8WithStatus(
                sourceValue,
                maximumCharacters: Int.max,
                maximumBytes: byteLimit
            )
            if source.status == .truncated || field.truncated { reasons.insert(reason) }
            let aggregate = truncateUTF8WithStatus(
                field.value,
                maximumCharacters: Int.max,
                maximumBytes: remainingAggregateTextBytes
            )
            if aggregate.truncated { reasons.insert("aggregate_text_limit") }
            remainingAggregateTextBytes -= aggregate.value.utf8.count
            return TakenText(
                value: aggregate.value.isEmpty ? nil : aggregate.value,
                completeAndNonempty: source.status == .complete &&
                    !field.truncated &&
                    !aggregate.truncated &&
                    !aggregate.value.isEmpty
            )
        }

        private mutating func canonicalizeRole(_ role: TakenText) -> TakenText {
            guard !role.completeAndNonempty else { return role }
            refund(role.value)
            remainingAggregateTextBytes -= canonicalAXUnknownIdentity.utf8.count
            return TakenText(value: canonicalAXUnknownIdentity, completeAndNonempty: false)
        }

        private mutating func canonicalizeSubrole(_ subrole: TakenText) -> TakenText {
            guard !subrole.completeAndNonempty else { return subrole }
            refund(subrole.value)
            return TakenText(value: nil, completeAndNonempty: false)
        }

        private mutating func refund(_ value: String?) {
            remainingAggregateTextBytes += value?.utf8.count ?? 0
        }

        private mutating func hasTimeRemaining() -> Bool {
            guard !expired else { return false }
            let now = clock()
            guard now.isFinite, now < deadline else {
                expired = true
                reasons.insert("wall_clock_limit")
                return false
            }
            return true
        }

        private func attributeFailure(
            _ result: AXTextDetailStringResult
        ) -> AXTextDetailSerializationError? {
            attributeFailure(result.status)
        }

        private func attributeFailure(
            _ result: AXTextDetailBoolResult
        ) -> AXTextDetailSerializationError? {
            attributeFailure(result.status)
        }

        private func attributeFailure(
            _ result: AXTextDetailBoundsResult
        ) -> AXTextDetailSerializationError? {
            attributeFailure(result.status)
        }

        private func attributeFailure(
            _ status: AXTextDetailAttributeStatus
        ) -> AXTextDetailSerializationError? {
            switch status {
            case .hardFailure:
                return .attributeReadFailed
            case .invalid:
                return .invalidGeometry
            case .complete, .truncated, .missing, .unreadable:
                return nil
            }
        }

        private func childrenFailure(
            _ result: AXTextDetailChildrenResult
        ) -> AXTextDetailSerializationError? {
            result.status == .failed ? .childrenReadFailed : nil
        }

        private mutating func providerCall<Value>(
            _ body: () -> Value,
            classifyFailure: (Value) -> AXTextDetailSerializationError?
        ) throws -> Value? {
            guard hasTimeRemaining() else { return nil }
            let value = body()
            if let failure = classifyFailure(value) {
                throw failure
            }
            guard hasTimeRemaining() else { return nil }
            return value
        }
    }
}

private enum AXTextDetailCallResult<Value> {
    case value(Value)
    case expired
    case failed
}

final class SystemAXTextDetailAttributeProvider: AXTextDetailAttributeProvider {
    private let copyAttributeValue: (String) -> (AXError, CFTypeRef?)
    private let childrenCount: () -> (AXError, CFIndex)
    private let copyChildren: (CFIndex) -> (AXError, CFArray?)
    private let makeChildProvider: ((AXUIElement) -> any AXTextDetailAttributeProvider)?
    private let convertString: (CFTypeRef?, Int) -> AXTextDetailStringResult
    private let remainingTime: () -> TimeInterval?
    private let setMessagingTimeout: (Float) -> AXError

    init(element: AXUIElement, remainingTime: @escaping () -> TimeInterval?) {
        self.copyAttributeValue = { attribute in
            observationAXAttribute(element, attribute)
        }
        self.childrenCount = {
            var available: CFIndex = 0
            let error = observationAXCall(element: element, fallback: AXError.cannotComplete) {
                AXUIElementGetAttributeValueCount(element, kAXChildrenAttribute as CFString, &available)
            }
            return (error, available)
        }
        self.copyChildren = { requested in
            var values: CFArray?
            let error = observationAXCall(element: element, fallback: AXError.cannotComplete) {
                AXUIElementCopyAttributeValues(element, kAXChildrenAttribute as CFString, 0, requested, &values)
            }
            return (error, values)
        }
        self.makeChildProvider = { child in
            SystemAXTextDetailAttributeProvider(
                element: child,
                remainingTime: remainingTime
            )
        }
        self.convertString = boundedAXTextDetailStringResult
        self.remainingTime = remainingTime
        self.setMessagingTimeout = { timeout in
            AXUIElementSetMessagingTimeout(element, timeout)
        }
    }

    init(
        copyAttributeValue: @escaping (String) -> (AXError, CFTypeRef?),
        childrenCount: @escaping () -> (AXError, CFIndex),
        copyChildren: @escaping (CFIndex) -> (AXError, CFArray?),
        makeChildProvider: ((AXUIElement) -> any AXTextDetailAttributeProvider)? = nil,
        convertString: @escaping (CFTypeRef?, Int) -> AXTextDetailStringResult = boundedAXTextDetailStringResult,
        shouldContinue: @escaping () -> Bool
    ) {
        self.copyAttributeValue = copyAttributeValue
        self.childrenCount = childrenCount
        self.copyChildren = copyChildren
        self.makeChildProvider = makeChildProvider
        self.convertString = convertString
        self.remainingTime = {
            shouldContinue() ? maximumAXTextDetailDuration : nil
        }
        self.setMessagingTimeout = { _ in .success }
    }

    init(
        copyAttributeValue: @escaping (String) -> (AXError, CFTypeRef?),
        childrenCount: @escaping () -> (AXError, CFIndex),
        copyChildren: @escaping (CFIndex) -> (AXError, CFArray?),
        makeChildProvider: ((AXUIElement) -> any AXTextDetailAttributeProvider)? = nil,
        convertString: @escaping (CFTypeRef?, Int) -> AXTextDetailStringResult = boundedAXTextDetailStringResult,
        remainingTime: @escaping () -> TimeInterval?,
        setMessagingTimeout: @escaping (Float) -> AXError
    ) {
        self.copyAttributeValue = copyAttributeValue
        self.childrenCount = childrenCount
        self.copyChildren = copyChildren
        self.makeChildProvider = makeChildProvider
        self.convertString = convertString
        self.remainingTime = remainingTime
        self.setMessagingTimeout = setMessagingTimeout
    }

    func stringValue(for attribute: String, maximumBytes: Int) -> AXTextDetailStringResult {
        let read: (AXError, CFTypeRef?)
        switch performAXCall({ copyAttributeValue(attribute) }) {
        case let .value(value):
            read = value
        case .expired:
            return AXTextDetailStringResult(value: nil, status: .missing)
        case .failed:
            return AXTextDetailStringResult(value: nil, status: .hardFailure)
        }
        let (error, value) = read
        if error == .failure {
            return AXTextDetailStringResult(value: nil, status: .unreadable)
        }
        if isHardAXTextDetailError(error) {
            return AXTextDetailStringResult(value: nil, status: .hardFailure)
        }
        guard hasRemainingTime() else {
            return AXTextDetailStringResult(value: nil, status: .missing)
        }
        if error == .noValue || error == .attributeUnsupported {
            return AXTextDetailStringResult(value: nil, status: .missing)
        }
        guard error == .success else {
            return AXTextDetailStringResult(value: nil, status: .hardFailure)
        }
        return convertString(value, maximumBytes)
    }

    func boolValue(for attribute: String) -> AXTextDetailBoolResult {
        let read: (AXError, CFTypeRef?)
        switch performAXCall({ copyAttributeValue(attribute) }) {
        case let .value(value):
            read = value
        case .expired:
            return AXTextDetailBoolResult(value: nil, status: .missing)
        case .failed:
            return AXTextDetailBoolResult(value: nil, status: .hardFailure)
        }
        let result = boundedAXTextDetailBoolAttribute(read)
        if result.status == .hardFailure { return result }
        guard hasRemainingTime() else {
            return AXTextDetailBoolResult(value: nil, status: .missing)
        }
        return result
    }

    func bounds() -> AXTextDetailBoundsResult {
        let position: (AXError, CFTypeRef?)
        switch performAXCall({ copyAttributeValue(kAXPositionAttribute) }) {
        case let .value(value):
            position = value
        case .expired:
            return AXTextDetailBoundsResult(value: nil, status: .missing)
        case .failed:
            return AXTextDetailBoundsResult(value: nil, status: .hardFailure)
        }
        if let failure = axTextDetailPositionFailure(position) {
            return AXTextDetailBoundsResult(value: nil, status: failure)
        }
        guard hasRemainingTime() else {
            return AXTextDetailBoundsResult(value: nil, status: .missing)
        }
        let size: (AXError, CFTypeRef?)
        switch performAXCall({ copyAttributeValue(kAXSizeAttribute) }) {
        case let .value(value):
            size = value
        case .expired:
            return AXTextDetailBoundsResult(value: nil, status: .missing)
        case .failed:
            return AXTextDetailBoundsResult(value: nil, status: .hardFailure)
        }
        let result = boundedAXTextDetailFrameAttribute(
            position: position,
            size: size
        )
        if result.status == .hardFailure || result.status == .invalid { return result }
        guard hasRemainingTime() else {
            return AXTextDetailBoundsResult(value: nil, status: .missing)
        }
        return result
    }

    func children(maximumCount: Int) -> AXTextDetailChildrenResult {
        guard maximumCount >= 0 else {
            return AXTextDetailChildrenResult(values: [], status: .failed)
        }
        let countRead: (AXError, CFIndex)
        switch performAXCall({ childrenCount() }) {
        case let .value(value):
            countRead = value
        case .expired:
            return AXTextDetailChildrenResult(values: [], status: .complete)
        case .failed:
            return AXTextDetailChildrenResult(values: [], status: .failed)
        }
        let (countError, available) = countRead
        let unavailable: Set<AXError> = [.noValue, .attributeUnsupported]
        guard countError == .success || unavailable.contains(countError),
              countError != .success || available >= 0
        else {
            return AXTextDetailChildrenResult(values: [], status: .failed)
        }
        guard hasRemainingTime() else {
            return AXTextDetailChildrenResult(values: [], status: .complete)
        }
        if unavailable.contains(countError) {
            return AXTextDetailChildrenResult(values: [], status: .complete)
        }
        let requested = min(available, CFIndex(maximumCount))
        guard requested > 0 else {
            return AXTextDetailChildrenResult(
                values: [],
                status: available > 0 ? .truncated : .complete
            )
        }
        let childrenRead: (AXError, CFArray?)
        switch performAXCall({ copyChildren(requested) }) {
        case let .value(value):
            childrenRead = value
        case .expired:
            return AXTextDetailChildrenResult(values: [], status: .complete)
        case .failed:
            return AXTextDetailChildrenResult(values: [], status: .failed)
        }
        let (copyError, values) = childrenRead
        guard copyError == .success else {
            return AXTextDetailChildrenResult(values: [], status: .failed)
        }
        guard hasRemainingTime() else {
            return AXTextDetailChildrenResult(values: [], status: .complete)
        }
        guard let values else {
            return AXTextDetailChildrenResult(values: [], status: .failed)
        }
        guard CFArrayGetCount(values) == requested else {
            return AXTextDetailChildrenResult(values: [], status: .failed)
        }
        guard let makeChildProvider else {
            return AXTextDetailChildrenResult(values: [], status: .failed)
        }
        var providers: [any AXTextDetailAttributeProvider] = []
        providers.reserveCapacity(CFArrayGetCount(values))
        for index in 0..<CFArrayGetCount(values) {
            let raw = CFArrayGetValueAtIndex(values, index)
            let value = unsafeBitCast(raw, to: CFTypeRef.self)
            guard CFGetTypeID(value) == AXUIElementGetTypeID() else {
                return AXTextDetailChildrenResult(values: [], status: .failed)
            }
            let child = unsafeBitCast(value, to: AXUIElement.self)
            providers.append(makeChildProvider(child))
        }
        return AXTextDetailChildrenResult(
            values: providers,
            status: available > requested ? .truncated : .complete
        )
    }

    private func performAXCall<Value>(
        _ body: () -> Value
    ) -> AXTextDetailCallResult<Value> {
        guard let remaining = remainingTime(),
              let timeout = boundedAXMessagingTimeout(remaining)
        else { return .expired }
        guard setMessagingTimeout(timeout) == .success else { return .failed }
        guard hasRemainingTime() else {
            return setMessagingTimeout(0) == .success ? .expired : .failed
        }
        let value = body()
        guard setMessagingTimeout(0) == .success else { return .failed }
        guard hasRemainingTime() else { return .expired }
        return .value(value)
    }

    private func hasRemainingTime() -> Bool {
        guard let remaining = remainingTime() else { return false }
        return remaining.isFinite && remaining > 0
    }
}

private func boundedAXMessagingTimeout(_ remaining: TimeInterval) -> Float? {
    guard remaining.isFinite, remaining > 0 else { return nil }
    var timeout = Float(remaining)
    guard timeout.isFinite, timeout > 0 else { return nil }
    if TimeInterval(timeout) > remaining { timeout = timeout.nextDown }
    return timeout > 0 ? timeout : nil
}

private func boundedAXTextDetailBoolAttribute(
    _ read: (AXError, CFTypeRef?)
) -> AXTextDetailBoolResult {
    let (error, value) = read
    if error == .noValue || error == .attributeUnsupported {
        return AXTextDetailBoolResult(value: nil, status: .missing)
    }
    guard error == .success else {
        return AXTextDetailBoolResult(value: nil, status: .hardFailure)
    }
    guard let value = value as? Bool else {
        return AXTextDetailBoolResult(value: nil, status: .unreadable)
    }
    return AXTextDetailBoolResult(value: value, status: .complete)
}

private func boundedAXTextDetailFrameAttribute(
    position: (AXError, CFTypeRef?),
    size: (AXError, CFTypeRef?)
) -> AXTextDetailBoundsResult {
    let (positionError, positionValue) = position
    let (sizeError, sizeValue) = size
    let unavailable: Set<AXError> = [.noValue, .attributeUnsupported]
    let accepted = unavailable.union([.success])
    guard accepted.contains(positionError), accepted.contains(sizeError) else {
        return AXTextDetailBoundsResult(value: nil, status: .hardFailure)
    }
    if unavailable.contains(positionError) || unavailable.contains(sizeError) {
        return AXTextDetailBoundsResult(value: nil, status: .missing)
    }
    guard let positionValue, CFGetTypeID(positionValue) == AXValueGetTypeID(),
          let sizeValue, CFGetTypeID(sizeValue) == AXValueGetTypeID()
    else { return AXTextDetailBoundsResult(value: nil, status: .unreadable) }
    let positionAXValue = unsafeBitCast(positionValue, to: AXValue.self)
    let sizeAXValue = unsafeBitCast(sizeValue, to: AXValue.self)
    var position = CGPoint.zero
    var size = CGSize.zero
    guard AXValueGetValue(positionAXValue, .cgPoint, &position),
          AXValueGetValue(sizeAXValue, .cgSize, &size)
    else { return AXTextDetailBoundsResult(value: nil, status: .unreadable) }
    guard position.x.isFinite, position.y.isFinite,
          size.width.isFinite, size.height.isFinite,
          size.width >= 0, size.height >= 0,
          size.width <= 1_000_000, size.height <= 1_000_000
    else { return AXTextDetailBoundsResult(value: nil, status: .invalid) }
    return AXTextDetailBoundsResult(
        value: CGRect(origin: position, size: size),
        status: .complete
    )
}

private func boundedAXTextDetailStringResult(
    _ value: CFTypeRef?,
    maximumBytes: Int
) -> AXTextDetailStringResult {
    guard let value, CFGetTypeID(value) == CFStringGetTypeID(), maximumBytes > 0 else {
        return AXTextDetailStringResult(value: nil, status: .unreadable)
    }
    let source = unsafeBitCast(value, to: CFString.self)
    let sourceLength = CFStringGetLength(source)
    var index = 0
    var bytes: [UInt8] = []
    bytes.reserveCapacity(min(maximumBytes, sourceLength))
    while index < sourceLength, bytes.count < maximumBytes {
        let first = CFStringGetCharacterAtIndex(source, index)
        let scalarValue: UInt32
        let consumedUTF16Units: Int
        switch first {
        case 0xD800...0xDBFF:
            guard index + 1 < sourceLength else {
                return AXTextDetailStringResult(value: nil, status: .unreadable)
            }
            let second = CFStringGetCharacterAtIndex(source, index + 1)
            guard (0xDC00...0xDFFF).contains(second) else {
                return AXTextDetailStringResult(value: nil, status: .unreadable)
            }
            scalarValue = 0x10000 +
                (UInt32(first) - 0xD800) * 0x400 +
                (UInt32(second) - 0xDC00)
            consumedUTF16Units = 2
        case 0xDC00...0xDFFF:
            return AXTextDetailStringResult(value: nil, status: .unreadable)
        default:
            scalarValue = UInt32(first)
            consumedUTF16Units = 1
        }
        guard let scalar = Unicode.Scalar(scalarValue) else {
            return AXTextDetailStringResult(value: nil, status: .unreadable)
        }
        let encoded = String(scalar).utf8
        guard bytes.count + encoded.count <= maximumBytes else {
            let result = String(bytes: bytes, encoding: .utf8)
            return AXTextDetailStringResult(
                value: result?.isEmpty == false ? result : nil,
                status: .truncated
            )
        }
        bytes.append(contentsOf: encoded)
        index += consumedUTF16Units
    }
    guard let result = String(bytes: bytes, encoding: .utf8) else {
        return AXTextDetailStringResult(value: nil, status: .unreadable)
    }
    return AXTextDetailStringResult(
        value: result.isEmpty ? nil : result,
        status: index < sourceLength ? .truncated : .complete
    )
}

private func isHardAXTextDetailError(_ error: AXError) -> Bool {
    error != .success && error != .noValue && error != .attributeUnsupported
}

private func axTextDetailPositionFailure(
    _ read: (AXError, CFTypeRef?)
) -> AXTextDetailAttributeStatus? {
    let (error, rawValue) = read
    if isHardAXTextDetailError(error) { return .hardFailure }
    guard error == .success,
          let rawValue,
          CFGetTypeID(rawValue) == AXValueGetTypeID()
    else { return nil }
    let value = unsafeBitCast(rawValue, to: AXValue.self)
    var position = CGPoint.zero
    guard AXValueGetValue(value, .cgPoint, &position) else { return nil }
    return position.x.isFinite && position.y.isFinite ? nil : .invalid
}

enum CGRectJSON {
    static func encode(_ rect: CGRect) -> JSONValue {
        .object([
            "x": .number(Double(rect.origin.x)),
            "y": .number(Double(rect.origin.y)),
            "width": .number(Double(rect.width)),
            "height": .number(Double(rect.height)),
        ])
    }
}

enum BoundedAXStringStatus: Equatable {
    case complete
    case truncated
    case failed
}

struct BoundedAXStringResult: Equatable {
    let value: String?
    let status: BoundedAXStringStatus
}

protocol AXNodeAttributeProvider: AnyObject {
    func stringValue(for attribute: String) -> BoundedAXStringResult
    func boolValue(for attribute: String) -> Bool?
    func bounds() -> CGRect
    func actions() -> [BoundedAXStringResult]
    func children(remaining: Int) -> [any AXNodeAttributeProvider]
    func sourceElement() -> AXUIElement?
    /// Whether the last `children(remaining:)` left out children that are off screen.
    var omittedOffscreenChildren: Bool { get }
}

extension AXNodeAttributeProvider {
    var omittedOffscreenChildren: Bool { false }
}

/// A table's children with its off-screen rows left out, in their original order. Nothing is
/// filtered unless the visible rows are a proper subset of the rows.
func onScreenChildren<Element>(
    _ children: [Element], rows: [Element], visibleRows: [Element],
    hash: (Element) -> Int, same: (Element, Element) -> Bool
) -> [Element]? {
    guard !visibleRows.isEmpty, rows.count > visibleRows.count else { return nil }
    func index(_ elements: [Element]) -> [Int: [Element]] {
        Dictionary(grouping: elements, by: hash)
    }
    let rowIndex = index(rows)
    let visibleIndex = index(visibleRows)
    func member(_ element: Element, of index: [Int: [Element]]) -> Bool {
        index[hash(element)]?.contains { same($0, element) } ?? false
    }
    guard visibleRows.allSatisfy({ member($0, of: rowIndex) }) else { return nil }
    return children.filter { !member($0, of: rowIndex) || member($0, of: visibleIndex) }
}

/// A window's toolbar holds its primary controls (live Activity Monitor: the search field) but can
/// follow a content group that uses up the observation budget, so it is read first.
func prioritizeWindowToolbar<Element>(_ children: [Element], isToolbar: (Element) -> Bool) -> [Element] {
    children.filter(isToolbar) + children.filter { !isToolbar($0) }
}

// Some AX windows omit their focused content branch from AXChildren. Recover only
// an immediate child proven by a bounded, same-process chain to this exact root.
func focusedAncestorBranch<Element>(
    root: Element, focused: Element,
    same: (Element, Element) -> Bool,
    isOwned: (Element) -> Bool,
    parent: (Element) -> Element?
) -> Element? {
    focusedAncestorPath(root: root, focused: focused, same: same,
        isOwned: isOwned, parent: parent).last
}

func focusedAncestorPath<Element>(
    root: Element, focused: Element,
    same: (Element, Element) -> Bool,
    isOwned: (Element) -> Bool,
    parent: (Element) -> Element?
) -> [Element] {
    guard isOwned(root), isOwned(focused), !same(root, focused) else { return [] }
    var current = focused
    var visited: [Element] = []
    for _ in 0..<32 {
        guard !visited.contains(where: { same($0, current) }) else { return [] }
        visited.append(current)
        guard let next = parent(current), isOwned(next) else { return [] }
        if same(next, root) { return visited }
        current = next
    }
    return []
}

func insertingFocusedBranch<Element>(
    children: [Element], remaining: Int,
    same: (Element, Element) -> Bool, recover: () -> Element?
) -> [Element] {
    guard remaining > children.count, let branch = recover(),
          !children.contains(where: { same($0, branch) }) else { return children }
    return [branch] + children
}

enum AXNodeReader {
    static func read(
        root: AXUIElement,
        windowBounds: CGRect,
        maximumDepth: Int = maximumAXDepth,
        recoverFocusedBranch: Bool = false,
        budget: AXObservationBudget? = nil
    ) -> AXNode {
        let restore = budget?.install()
        defer { restore?() }
        let tree = read(
            provider: SystemAXNodeAttributeProvider(element: root, recoverFocusedBranch: recoverFocusedBranch),
            windowBounds: windowBounds,
            maximumDepth: maximumDepth,
            budget: budget
        )
        PopupAXDiagnostics.recordIfNeeded(root: root, tree: tree, windowBounds: windowBounds)
        return tree
    }

    static func read(
        provider: any AXNodeAttributeProvider,
        windowBounds: CGRect,
        maximumDepth: Int = maximumAXDepth,
        budget: AXObservationBudget? = nil
    ) -> AXNode {
        let restore = budget?.install()
        defer { restore?() }
        var state = ReadState()
        return read(
            provider: provider,
            windowBounds: windowBounds,
            depth: 0,
            maximumDepth: max(1, min(maximumDepth, maximumAXTraversalDepth)),
            state: &state
        )
    }

    private struct BoundedRead {
        let value: String?
        let dropped: Bool
    }

    private struct ReadState {
        var visited = 0
        let budget = AXObservationBudget.current
        var remainingStringBytes = maximumAXReadTreeStringBytes

        mutating func take(
            _ boundedSource: @autoclosure () -> BoundedAXStringResult,
            byteLimit: Int = maximumAXReadStringBytes
        ) -> BoundedRead {
            guard budget?.available != false else { return BoundedRead(value: nil, dropped: true) }
            let boundedSource = boundedSource()
            guard budget?.available != false else { return BoundedRead(value: nil, dropped: true) }
            guard boundedSource.status != .failed else { return BoundedRead(value: nil, dropped: true) }
            guard let source = boundedSource.value else {
                return BoundedRead(value: nil, dropped: boundedSource.status == .truncated)
            }
            let limit = min(byteLimit, remainingStringBytes)
            guard limit > 0 else { return BoundedRead(value: nil, dropped: !source.isEmpty) }
            let bounded = truncateUTF8WithStatus(
                source,
                maximumCharacters: maximumAXStringCharacters,
                maximumBytes: limit
            )
            let result = bounded.value
            remainingStringBytes -= result.utf8.count
            return BoundedRead(
                value: result.isEmpty ? nil : result,
                dropped: boundedSource.status == .truncated || bounded.truncated
            )
        }
    }

    private static func read(
        provider: any AXNodeAttributeProvider,
        windowBounds: CGRect,
        depth: Int,
        maximumDepth: Int,
        state: inout ReadState
    ) -> AXNode {
        // Read role/subrole before the value. AX secure fields frequently use
        // AXTextField + AXSecureTextField, so merely checking role leaks text.
        let roleRead = state.take(provider.stringValue(for: kAXRoleAttribute), byteLimit: 1_024)
        let subroleRead = state.take(provider.stringValue(for: kAXSubroleAttribute), byteLimit: 1_024)
        let role = roleRead.value ?? "AXUnknown"
        // Web accessibility trees contain many structural wrappers. Extend only
        // this content branch; keep application chrome and menus on their small
        // default budget, with the same total node/string/time limits.
        let maximumDepth = role == "AXWebArea"
            ? min(maximumAXTraversalDepth, max(maximumDepth, depth + maximumAXSubtreeDepth))
            : maximumDepth
        let subrole = subroleRead.value
        let secure = roleRead.dropped || subroleRead.dropped || role.localizedCaseInsensitiveContains("secure") || (subrole?.localizedCaseInsensitiveContains("secure") ?? false)
        let elementBounds = state.budget?.available != false ? provider.bounds() : .zero
        state.visited += 1
        var children: [AXNode] = []
        var childrenTruncated = depth + 1 >= maximumDepth || state.visited >= maximumAXNodes || state.budget?.available == false
        if !childrenTruncated {
            let remaining = maximumAXNodes - state.visited
            for child in provider.children(remaining: remaining).prefix(remaining) {
                guard state.visited < maximumAXNodes, state.budget?.available != false else { childrenTruncated = true; break }
                children.append(read(
                    provider: child,
                    windowBounds: windowBounds,
                    depth: depth + 1,
                    maximumDepth: maximumDepth,
                    state: &state
                ))
            }
            if provider.omittedOffscreenChildren { childrenTruncated = true }
        }
        let label = state.take(provider.stringValue(for: kAXDescriptionAttribute)).value
        let title = state.take(provider.stringValue(for: kAXTitleAttribute)).value
        let help = state.take(provider.stringValue(for: kAXHelpAttribute)).value
        let value = secure ? "<redacted>" : state.take(provider.stringValue(for: kAXValueAttribute)).value
        let actions = state.budget?.available != false ? provider.actions().prefix(32).compactMap { state.take($0).value } : []
        let enabled = state.budget?.available != false ? provider.boolValue(for: kAXEnabledAttribute) : nil
        let focused = state.budget?.available != false ? provider.boolValue(for: kAXFocusedAttribute) : nil
        let sourceElement = state.budget?.available != false ? provider.sourceElement() : nil
        // Any accessor, including empty children or the final source lookup,
        // can consume the soft budget. Publish partial evidence, not a complete
        // empty tree; do not expose a reference obtained past the deadline.
        let contentExpired = state.budget?.available == false
        return AXNode(
            role: role,
            subrole: subrole,
            label: label,
            title: title,
            help: help,
            value: value,
            enabled: enabled,
            focused: focused,
            actions: actions,
            bounds: CGRect(
                x: elementBounds.origin.x - windowBounds.origin.x,
                y: elementBounds.origin.y - windowBounds.origin.y,
                width: elementBounds.width,
                height: elementBounds.height
            ),
            children: children,
            sourceElement: contentExpired ? nil : sourceElement,
            childrenTruncated: childrenTruncated || contentExpired
        )
    }

    static func stringAttribute(_ element: AXUIElement, _ attribute: String) -> BoundedAXStringResult {
        let (error, value) = observationAXAttribute(element, attribute)
        if error == .noValue || error == .attributeUnsupported {
            return BoundedAXStringResult(value: nil, status: .complete)
        }
        guard error == .success else { return BoundedAXStringResult(value: nil, status: .failed) }
        return attribute == kAXValueAttribute ? boundedAXValueResult(value) : boundedAXStringResult(value)
    }

    static func boolAttribute(_ element: AXUIElement, _ attribute: String) -> Bool? {
        let (error, value) = observationAXAttribute(element, attribute)
        guard error == .success else { return nil }
        return value as? Bool
    }

    static func frameAttribute(_ element: AXUIElement) -> CGRect? {
        guard let position = pointAttribute(element, kAXPositionAttribute),
              let size = sizeAttribute(element, kAXSizeAttribute)
        else { return nil }
        return CGRect(origin: position, size: size)
    }

    private static func pointAttribute(_ element: AXUIElement, _ attribute: String) -> CGPoint? {
        let (error, value) = observationAXAttribute(element, attribute)
        guard error == .success else { return nil }
        return decodeAXPoint(value)
    }

    private static func sizeAttribute(_ element: AXUIElement, _ attribute: String) -> CGSize? {
        let (error, value) = observationAXAttribute(element, attribute)
        guard error == .success else { return nil }
        return decodeAXSize(value)
    }

    static func elementArrayAttribute(_ element: AXUIElement, _ attribute: String, remaining: Int) -> [AXUIElement] {
        guard remaining > 0 else { return [] }
        var available: CFIndex = 0
        guard observationAXCall(element: element, fallback: AXError.cannotComplete, {
            AXUIElementGetAttributeValueCount(element, attribute as CFString, &available)
        }) == .success,
              available > 0
        else { return [] }
        let requested = min(available, CFIndex(remaining))
        var values: CFArray?
        guard observationAXCall(element: element, fallback: AXError.cannotComplete, {
            AXUIElementCopyAttributeValues(element, attribute as CFString, 0, requested, &values)
        }) == .success,
              let values
        else { return [] }
        var result: [AXUIElement] = []
        result.reserveCapacity(min(CFArrayGetCount(values), requested))
        for index in 0..<min(CFArrayGetCount(values), requested) {
            let raw = CFArrayGetValueAtIndex(values, index)
            let value = unsafeBitCast(raw, to: CFTypeRef.self)
            guard CFGetTypeID(value) == AXUIElementGetTypeID() else { continue }
            result.append(unsafeBitCast(value, to: AXUIElement.self))
        }
        return result
    }

    static func actionNames(_ element: AXUIElement) -> [BoundedAXStringResult] {
        actionNameResults(element).values
    }

    static func actionNameResults(_ element: AXUIElement) -> ActionNameResults {
        var names: CFArray?
        let error = observationAXCall(element: element, fallback: AXError.cannotComplete) {
            AXUIElementCopyActionNames(element, &names)
        }
        return boundedAXActionNameResults(error: error, names: names)
    }
}

func boundedAXActionNameResults(error: AXError, names: CFArray?) -> ActionNameResults {
    guard error == .success, let names else {
        return ActionNameResults(values: [], status: .failed, error: error == .success ? .failure : error)
    }
    let status: BoundedAXStringStatus = CFArrayGetCount(names) > 32 ? .truncated : .complete
    return ActionNameResults(values: boundedAXActionResults(names), status: status)
}

/// AXValue may be a CFNumber (for example a scrollbar position). Keep the
/// existing bounded string representation without describing arbitrary objects.
func boundedAXValueResult(_ value: CFTypeRef?) -> BoundedAXStringResult {
    guard let value else { return BoundedAXStringResult(value: nil, status: .failed) }
    if CFGetTypeID(value) == CFBooleanGetTypeID() {
        return BoundedAXStringResult(value: CFEqual(value, kCFBooleanTrue) ? "true" : "false", status: .complete)
    }
    if CFGetTypeID(value) == CFNumberGetTypeID() {
        let number = unsafeBitCast(value, to: CFNumber.self)
        var scalar: Double = 0
        guard CFNumberGetValue(number, .doubleType, &scalar), scalar.isFinite else {
            return BoundedAXStringResult(value: nil, status: .failed)
        }
        return boundedAXStringResult(String(scalar) as CFString)
    }
    return boundedAXStringResult(value)
}

func boundedAXStringResult(_ value: CFTypeRef?) -> BoundedAXStringResult {
    guard let value, CFGetTypeID(value) == CFStringGetTypeID() else {
        return BoundedAXStringResult(value: nil, status: .failed)
    }
    let source = unsafeBitCast(value, to: CFString.self)
    let sourceLength = CFStringGetLength(source)
    let maximumUTF16Units = maximumAXReadStringBytes
    let range = CFRange(location: 0, length: min(sourceLength, maximumUTF16Units))
    guard let bounded = CFStringCreateWithSubstring(kCFAllocatorDefault, source, range) else {
        return BoundedAXStringResult(value: nil, status: .failed)
    }
    let result = truncateUTF8WithStatus(
        bounded as String,
        maximumCharacters: maximumAXStringCharacters,
        maximumBytes: maximumAXReadStringBytes
    )
    return BoundedAXStringResult(
        value: result.value.isEmpty ? nil : result.value,
        status: sourceLength > maximumUTF16Units || result.truncated ? .truncated : .complete
    )
}

func boundedAXActionResults(
    _ values: CFArray,
    didAccess: ((Int) -> Void)? = nil
) -> [BoundedAXStringResult] {
    let requested = min(CFArrayGetCount(values), 32)
    var results: [BoundedAXStringResult] = []
    results.reserveCapacity(requested)
    for index in 0..<requested {
        didAccess?(index)
        let raw = CFArrayGetValueAtIndex(values, index)
        let value = unsafeBitCast(raw, to: CFTypeRef.self)
        guard CFGetTypeID(value) == CFStringGetTypeID() else {
            results.append(BoundedAXStringResult(value: nil, status: .failed))
            continue
        }
        let result = boundedAXStringResult(value)
        results.append(result)
    }
    return results
}

func decodeAXPoint(_ value: CFTypeRef?) -> CGPoint? {
    guard let value, CFGetTypeID(value) == AXValueGetTypeID() else { return nil }
    let axValue = unsafeBitCast(value, to: AXValue.self)
    var point = CGPoint.zero
    guard AXValueGetValue(axValue, .cgPoint, &point), point.x.isFinite, point.y.isFinite else { return nil }
    return point
}

func decodeAXSize(_ value: CFTypeRef?) -> CGSize? {
    guard let value, CFGetTypeID(value) == AXValueGetTypeID() else { return nil }
    let axValue = unsafeBitCast(value, to: AXValue.self)
    var size = CGSize.zero
    let maximumDimension: CGFloat = 1_000_000
    guard AXValueGetValue(axValue, .cgSize, &size),
          size.width.isFinite, size.height.isFinite,
          size.width >= 0, size.height >= 0,
          size.width <= maximumDimension, size.height <= maximumDimension
    else { return nil }
    return size
}

func truncateUTF8Linearly(_ value: String, maximumCharacters: Int, maximumBytes: Int) -> String {
    truncateUTF8WithStatus(value, maximumCharacters: maximumCharacters, maximumBytes: maximumBytes).value
}

func truncateUTF8WithStatus(
    _ value: String,
    maximumCharacters: Int,
    maximumBytes: Int
) -> (value: String, truncated: Bool) {
    guard maximumCharacters > 0, maximumBytes > 0 else { return ("", !value.isEmpty) }
    var result = ""
    result.reserveCapacity(maximumBytes)
    var byteCount = 0
    var characterCount = 0
    for character in value {
        guard characterCount < maximumCharacters else { return (result, true) }
        let characterBytes = character.utf8.count
        guard byteCount + characterBytes <= maximumBytes else { return (result, true) }
        result.append(character)
        byteCount += characterBytes
        characterCount += 1
    }
    return (result, false)
}

func prioritizeSheetChildren<Element>(
    _ children: [Element], isButton: (Element) -> Bool,
    isFocusedBranch: (Element) -> Bool
) -> [Element] {
    var buttons: [Element] = []
    var focused: [Element] = []
    var others: [Element] = []
    for child in children {
        if isButton(child) { buttons.append(child) }
        else if isFocusedBranch(child) { focused.append(child) }
        else { others.append(child) }
    }
    return buttons + focused + others
}

private final class SystemAXNodeAttributeProvider: AXNodeAttributeProvider {
    private let element: AXUIElement

    private let recoverFocusedBranch: Bool
    private let inSheet: Bool
    private let sheetFocusedPath: [AXUIElement]?
    private(set) var omittedOffscreenChildren = false

    init(element: AXUIElement, recoverFocusedBranch: Bool = false, inSheet: Bool = false,
         sheetFocusedPath: [AXUIElement]? = nil) {
        self.element = element
        self.recoverFocusedBranch = recoverFocusedBranch
        self.inSheet = inSheet
        self.sheetFocusedPath = sheetFocusedPath
    }

    func stringValue(for attribute: String) -> BoundedAXStringResult {
        AXNodeReader.stringAttribute(element, attribute)
    }

    func boolValue(for attribute: String) -> Bool? {
        AXNodeReader.boolAttribute(element, attribute)
    }

    func bounds() -> CGRect {
        AXNodeReader.frameAttribute(element) ?? .zero
    }

    func actions() -> [BoundedAXStringResult] {
        AXNodeReader.actionNames(element)
    }

    func children(remaining: Int) -> [any AXNodeAttributeProvider] {
        let role = AXNodeReader.stringAttribute(element, kAXRoleAttribute)
        let onScreenRows = onScreenTableChildren(role: role)
        omittedOffscreenChildren = onScreenRows != nil
        let children = onScreenRows.map { Array($0.prefix(max(0, remaining))) }
            ?? AXNodeReader.elementArrayAttribute(element, kAXChildrenAttribute, remaining: remaining)
        let complete = recoverFocusedBranch ? insertingFocusedBranch(
            children: children, remaining: remaining, same: { CFEqual($0, $1) },
            recover: missingFocusedBranch
        ) : children
        let sheet = inSheet || (role.status == .complete && role.value == kAXSheetRole)
        // Resolve the same-process ancestry once per sheet observation. It only
        // changes read order for children already returned by AXChildren.
        let focusedPath = sheetFocusedPath ?? (sheet ? ownedFocusedPath() : [])
        // File-column trees can exhaust the AX deadline before later Open/Cancel
        // siblings. Read those immediate controls first without expanding the
        // budget or inventing descendants; preserve order within both groups.
        let window = role.status == .complete && role.value == kAXWindowRole
        let ordered = sheet ? prioritizeSheetChildren(complete, isButton: {
            let role = AXNodeReader.stringAttribute($0, kAXRoleAttribute)
            return role.status == .complete && role.value == kAXButtonRole
        }, isFocusedBranch: { child in focusedPath.contains { CFEqual($0, child) } })
            : window ? prioritizeWindowToolbar(complete, isToolbar: {
                let role = AXNodeReader.stringAttribute($0, kAXRoleAttribute)
                return role.status == .complete && role.value == kAXToolbarRole
            }) : complete
        return ordered.map { SystemAXNodeAttributeProvider(element: $0, inSheet: sheet,
            sheetFocusedPath: sheet ? focusedPath : nil) }
    }

    /// Tables and outlines list every row, on screen or not (live Activity Monitor: 584 rows, 19 on
    /// screen, 12,878 nodes taking 69 s). Keep only the rows on screen so the rest of the window,
    /// such as its toolbar search field, fits the observation budget.
    private func onScreenTableChildren(role: BoundedAXStringResult) -> [AXUIElement]? {
        guard role.status == .complete, role.value == kAXTableRole || role.value == kAXOutlineRole else { return nil }
        let visible = AXNodeReader.elementArrayAttribute(element, kAXVisibleRowsAttribute, remaining: maximumAXNodes)
        guard !visible.isEmpty else { return nil }
        let rows = AXNodeReader.elementArrayAttribute(element, kAXRowsAttribute, remaining: maximumAXNodes)
        guard rows.count > visible.count else { return nil }
        return onScreenChildren(
            AXNodeReader.elementArrayAttribute(element, kAXChildrenAttribute, remaining: maximumAXNodes),
            rows: rows, visibleRows: visible, hash: { Int(bitPattern: CFHash($0)) }, same: { CFEqual($0, $1) }
        )
    }

    private func missingFocusedBranch() -> AXUIElement? {
        guard AXNodeReader.stringAttribute(element, kAXRoleAttribute).value == kAXWindowRole else { return nil }
        return ownedFocusedPath().last
    }

    private func ownedFocusedPath() -> [AXUIElement] {
        func pid(_ node: AXUIElement) -> pid_t? {
            observationAXCall(element: node, fallback: nil as pid_t?) {
                var result: pid_t = 0
                return AXUIElementGetPid(node, &result) == .success && result > 0 ? result : nil
            }
        }
        guard let owner = pid(element) else { return [] }
        let app = AXUIElementCreateApplication(owner)
        let (error, value) = observationAXAttribute(app, kAXFocusedUIElementAttribute)
        guard error == .success, let focused = decodeAXElement(value) else { return [] }
        return focusedAncestorPath(root: element, focused: focused, same: { CFEqual($0, $1) },
            isOwned: { pid($0) == owner }, parent: { node in
                let (error, value) = observationAXAttribute(node, kAXParentAttribute)
                return error == .success ? decodeAXElement(value) : nil
            })
    }

    func sourceElement() -> AXUIElement? { element }
}
