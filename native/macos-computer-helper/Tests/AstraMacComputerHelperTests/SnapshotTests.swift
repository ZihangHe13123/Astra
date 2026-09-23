@testable import AstraMacComputerHelperCore
@preconcurrency import ApplicationServices
import CoreGraphics
import Dispatch
import Foundation
import Testing

@Test func scrollbarAXPressSelectionPrefersOneDirectionalPageButton() {
    let references = scrollbarAXPressReferences(
        pageSubroles: ["AXIncrementPage"],
        arrowSubroles: ["AXIncrementArrow"]
    )

    let selected = selectAXScrollPressReferenceTarget(
        rootReference: "list",
        direction: .increment,
        references: references
    )

    #expect(selected?.ownerReference == "scrollbar")
    #expect(selected?.buttonReference == "page-0")
}

@Test func scrollbarAXPressSelectionFailsClosedForAmbiguousPreferredTier() {
    let references = scrollbarAXPressReferences(
        pageSubroles: ["AXDecrementPage", "AXDecrementPage"],
        arrowSubroles: ["AXDecrementArrow"]
    )

    #expect(selectAXScrollPressReferenceTarget(
        rootReference: "list",
        direction: .decrement,
        references: references
    ) == nil)
}

@Test func scrollbarAXPressSelectionUsesArrowOnlyWhenNoPageCandidateExists() {
    let references = scrollbarAXPressReferences(
        pageSubroles: [],
        arrowSubroles: ["AXIncrementArrow"]
    )

    #expect(selectAXScrollPressReferenceTarget(
        rootReference: "list",
        direction: .increment,
        references: references
    )?.buttonReference == "arrow-0")
}

@Test func scrollbarAXPressSelectionAcceptsUnknownEnabledButRejectsExplicitlyDisabledButton() {
    let unknown = scrollbarAXPressReferences(
        pageSubroles: ["AXIncrementPage"],
        arrowSubroles: [],
        buttonEnabled: nil
    )
    let disabled = scrollbarAXPressReferences(
        pageSubroles: ["AXIncrementPage"],
        arrowSubroles: [],
        buttonEnabled: false
    )

    #expect(selectAXScrollPressReferenceTarget(
        rootReference: "list",
        direction: .increment,
        references: unknown
    )?.buttonReference == "page-0")
    #expect(selectAXScrollPressReferenceTarget(
        rootReference: "list",
        direction: .increment,
        references: disabled
    ) == nil)
}

private func scrollbarAXPressReferences(
    pageSubroles: [String],
    arrowSubroles: [String],
    buttonEnabled: Bool? = true
) -> [String: SnapshotElement] {
    let pageReferences = pageSubroles.indices.map { "page-\($0)" }
    let arrowReferences = arrowSubroles.indices.map { "arrow-\($0)" }
    var references: [String: SnapshotElement] = [
        "list": SnapshotElement(
            element: nil,
            bounds: CGRect(x: 0, y: 0, width: 100, height: 100),
            role: "AXList",
            enabled: true,
            childReferences: ["scrollbar"]
        ),
        "scrollbar": SnapshotElement(
            element: nil,
            bounds: CGRect(x: 90, y: 0, width: 10, height: 100),
            role: "AXScrollBar",
            enabled: true,
            childReferences: pageReferences + arrowReferences
        ),
    ]
    for (index, subrole) in pageSubroles.enumerated() {
        references["page-\(index)"] = SnapshotElement(
            element: nil,
            bounds: CGRect(x: 90, y: 10, width: 10, height: 20),
            role: "AXButton",
            subrole: subrole,
            enabled: buttonEnabled,
            actions: ["AXPress"]
        )
    }
    for (index, subrole) in arrowSubroles.enumerated() {
        references["arrow-\(index)"] = SnapshotElement(
            element: nil,
            bounds: CGRect(x: 90, y: 70, width: 10, height: 10),
            role: "AXButton",
            subrole: subrole,
            enabled: buttonEnabled,
            actions: ["AXPress"]
        )
    }
    return references
}

@Test func secureValueIsRedacted() {
    let node = AXNode(role: "AXSecureTextField", value: "secret", bounds: .zero)

    #expect(AXSerializer.serialize(node).value == "<redacted>")
}

@Test func secureSubroleValueIsRedactedWithTheSameParityAsSecureRole() {
    let node = AXNode(
        role: "AXTextField",
        subrole: "AXSecureTextField",
        value: "secret",
        bounds: .zero
    )

    #expect(AXSerializer.serialize(node).value == "<redacted>")
}

@Test func elementRefCannotCrossSnapshots() {
    let reference = ElementReference(snapshotID: "s1", ordinal: 3)

    #expect(!reference.isValid(for: "s2"))
}

@Test func trustedDefaultButtonDisclosureRequiresSameWindowRegisteredElement() {
    let window = AXUIElementCreateApplication(10_001)
    let defaultButton = AXUIElementCreateApplication(10_002)
    let otherWindow = AXUIElementCreateApplication(10_003)
    let registeredTree = AXSerializer.serialize(
        AXNode(
            role: "AXWindow",
            bounds: .zero,
            children: [AXNode(
                role: "AXButton",
                bounds: .zero,
                sourceElement: defaultButton
            )],
            sourceElement: window
        ),
        snapshotID: "default-button"
    )
    let absentTree = AXSerializer.serialize(
        AXNode(role: "AXWindow", bounds: .zero, sourceElement: window),
        snapshotID: "default-button"
    )

    func disclosure(
        tree: SerializedAXTree = registeredTree,
        defaultRead: (AXError, CFTypeRef?),
        ownerRead: (AXError, CFTypeRef?) = (.success, window)
    ) -> DefaultButtonDisclosure {
        trustedDefaultButtonDisclosure(window: window, tree: tree) { element, attribute in
            if CFEqual(element, window), attribute == kAXDefaultButtonAttribute {
                return defaultRead
            }
            if CFEqual(element, defaultButton), attribute == kAXWindowAttribute {
                return ownerRead
            }
            return (.attributeUnsupported, nil)
        }
    }

    let absent = disclosure(defaultRead: (.attributeUnsupported, nil))
    #expect(absent == DefaultButtonDisclosure(hasDefaultButton: false, elementRef: nil))

    let registered = disclosure(defaultRead: (.success, defaultButton))
    #expect(registered.hasDefaultButton)
    #expect(registered.elementRef == registeredTree.root.children.first?.elementRef.wireValue)

    let truncated = disclosure(
        tree: absentTree,
        defaultRead: (.success, defaultButton)
    )
    #expect(truncated == DefaultButtonDisclosure(hasDefaultButton: true, elementRef: nil))

    let wrongWindow = disclosure(
        defaultRead: (.success, defaultButton),
        ownerRead: (.success, otherWindow)
    )
    #expect(wrongWindow == DefaultButtonDisclosure(hasDefaultButton: true, elementRef: nil))

    let unreadable = disclosure(defaultRead: (.cannotComplete, nil))
    #expect(unreadable == DefaultButtonDisclosure(hasDefaultButton: false, elementRef: nil))
}

@Test func serializerBoundsDepthNodesStringsAndPayload() {
    let longText = String(repeating: "a", count: 700)
    let chain = AXNode(role: "AXButton", label: longText, bounds: .zero, children: [
        AXNode(role: "AXStaticText", label: longText, bounds: .zero),
    ])

    let serialized = AXSerializer.serialize(chain)

    #expect(serialized.label?.count ?? 0 <= 512)
    #expect(serialized.nodeCount <= 2_000)
    #expect(serialized.jsonByteCount <= 512 * 1024)
}

@Test func textDetailEnvelopeIsDeterministicSchemaOneAndObservationOnly() throws {
    let child = DetailAXProvider(role: "AXStaticText", subrole: "AXUnknown", value: "child")
    let root = DetailAXProvider(
        role: "AXWindow",
        subrole: "AXStandardWindow",
        label: "Document",
        children: [child]
    )

    let first = try AXTextDetailSerializer.serialize(
        provider: root,
        snapshotID: "snapshot-detail",
        clock: { 10 }
    )
    let second = try AXTextDetailSerializer.serialize(
        provider: root,
        snapshotID: "snapshot-detail",
        clock: { 10 }
    )
    let json = try textDetailJSONObject(first.data)
    let limits = try #require(json["limits"] as? [String: Any])
    let stats = try #require(json["stats"] as? [String: Any])
    let rootJSON = try #require(json["root"] as? [String: Any])
    let children = try #require(rootJSON["children"] as? [[String: Any]])

    #expect(first.data == second.data)
    #expect(Set(json.keys) == ["schema_version", "snapshot_id", "coverage", "limits", "stats", "root"])
    #expect(json["schema_version"] as? Int == 1)
    #expect(json["snapshot_id"] as? String == "snapshot-detail")
    #expect(json["coverage"] as? String == "reported_ax_subtree")
    #expect(limits["maximum_depth"] as? Int == 20)
    #expect(limits["maximum_nodes"] as? Int == 4_000)
    #expect(limits["maximum_structural_string_bytes"] as? Int == 4 * 1_024)
    #expect(limits["maximum_value_bytes"] as? Int == 256 * 1_024)
    #expect(limits["maximum_aggregate_text_bytes"] as? Int == 4 * 1_024 * 1_024)
    #expect(limits["maximum_final_bytes"] as? Int == 8 * 1_024 * 1_024)
    #expect(limits["wall_clock_ms"] as? Int == 5_000)
    #expect(stats["node_count"] as? Int == 2)
    #expect(stats["max_depth_observed"] as? Int == 1)
    #expect(stats["truncated"] as? Bool == false)
    #expect((stats["truncation_reasons"] as? [String]) == [])
    #expect(rootJSON["node_id"] as? String == "node_0")
    let firstChild = try #require(children.first)
    #expect(firstChild["node_id"] as? String == "node_1")
    #expect(!containsJSONKey(json, key: "element_ref"))
    #expect(root.stringReads.contains(kAXValueAttribute))
    #expect(!root.stringReads.contains(kAXSelectedTextAttribute))
    #expect(!root.stringReads.contains(kAXSelectedTextRangeAttribute))
    #expect(!root.stringReads.contains(kAXVisibleCharacterRangeAttribute))
    #expect(!root.stringReads.contains("AXAttributedStringForRange"))
}

@Test func textDetailTraversalBoundsDeepAndWideTrees() throws {
    let exactDepth = try AXTextDetailSerializer.serialize(
        provider: detailProviderChain(length: 21),
        snapshotID: "exact-depth",
        clock: { 0 }
    )
    let exactDepthStats = try textDetailStats(exactDepth.data)
    #expect(exactDepthStats["node_count"] as? Int == 21)
    #expect(exactDepthStats["max_depth_observed"] as? Int == 20)
    #expect(exactDepthStats["truncation_reasons"] as? [String] == [])

    let deep = detailProviderChain(length: 22)
    let deepEnvelope = try AXTextDetailSerializer.serialize(
        provider: deep,
        snapshotID: "deep",
        clock: { 0 }
    )
    let deepStats = try textDetailStats(deepEnvelope.data)
    #expect(deepStats["node_count"] as? Int == 21)
    #expect(deepStats["max_depth_observed"] as? Int == 20)
    #expect(deepStats["truncation_reasons"] as? [String] == ["depth_limit"])

    let wide = DetailAXProvider(
        role: "AXWindow",
        subrole: "AXStandardWindow",
        children: (0..<4_000).map { index in
            DetailAXProvider(role: "AXStaticText", subrole: "AXUnknown", value: "child-\(index)")
        }
    )
    let wideEnvelope = try AXTextDetailSerializer.serialize(
        provider: wide,
        snapshotID: "wide",
        clock: { 0 }
    )
    let wideStats = try textDetailStats(wideEnvelope.data)
    #expect(wideStats["node_count"] as? Int == 4_000)
    #expect(wideStats["max_depth_observed"] as? Int == 1)
    #expect(wideStats["truncation_reasons"] as? [String] == ["node_limit"])
}

@Test func textDetailStringBudgetsPreserveUTF8BoundariesAndCanonicalReasons() throws {
    let exactStructural = String(repeating: "😀", count: 1_024)
    let overStructural = exactStructural + "😀"
    let overValue = String(repeating: "界", count: 256 * 1_024 / 3 + 1)
    let root = DetailAXProvider(
        role: "AXWindow",
        subrole: "AXStandardWindow",
        label: overStructural,
        value: overValue
    )

    let envelope = try AXTextDetailSerializer.serialize(
        provider: root,
        snapshotID: "utf8",
        clock: { 0 }
    )
    let json = try textDetailJSONObject(envelope.data)
    let rootJSON = try #require(json["root"] as? [String: Any])
    let label = try #require(rootJSON["label"] as? String)
    let value = try #require(rootJSON["value"] as? String)
    let stats = try #require(json["stats"] as? [String: Any])

    #expect(label == exactStructural)
    #expect(label.utf8.count == 4 * 1_024)
    #expect(value.utf8.count <= 256 * 1_024)
    #expect(Array(value).count == 256 * 1_024 / 3)
    #expect(stats["truncation_reasons"] as? [String] == [
        "structural_string_limit", "value_limit",
    ])
}

@Test func textDetailAggregateAndFinalByteBudgetsProduceCanonicalPartialArtifacts() throws {
    let aggregateRoot = DetailAXProvider(
        role: "AXWindow",
        subrole: "AXStandardWindow",
        children: (0..<20).map { _ in
            DetailAXProvider(
                role: "AXTextArea",
                subrole: "AXStandardTextField",
                value: String(repeating: "v", count: 256 * 1_024)
            )
        }
    )
    let aggregate = try AXTextDetailSerializer.serialize(
        provider: aggregateRoot,
        snapshotID: "aggregate",
        clock: { 0 }
    )
    let aggregateStats = try textDetailStats(aggregate.data)
    #expect(aggregateStats["truncation_reasons"] as? [String] == ["aggregate_text_limit"])

    let expansionRoot = DetailAXProvider(
        role: "AXWindow",
        subrole: "AXStandardWindow",
        children: (0..<20).map { _ in
            DetailAXProvider(
                role: "AXTextArea",
                subrole: "AXStandardTextField",
                value: String(repeating: "\u{0001}", count: 256 * 1_024)
            )
        }
    )
    let final = try AXTextDetailSerializer.serialize(
        provider: expansionRoot,
        snapshotID: "final",
        clock: { 0 }
    )
    let finalStats = try textDetailStats(final.data)
    let finalJSON = try textDetailJSONObject(final.data)
    let retainedCount = try #require(finalStats["node_count"] as? Int)
    #expect(final.data.count <= 8 * 1_024 * 1_024)
    #expect((finalStats["truncation_reasons"] as? [String])?.contains("final_byte_limit") == true)
    #expect(retainedCount > 1)
    #expect(preorderNodeIDs(finalJSON["root"] as Any) == (0..<retainedCount).map { "node_\($0)" })
    #expect(expansionRoot.childProviders.allSatisfy { provider in
        provider.stringReads.filter { $0 == kAXValueAttribute }.count <= 1
    })
}

@Test func textDetailClockExpiryStopsTraversalAtFiveSeconds() throws {
    let clock = DetailClock(values: [0, 0, 5])
    let root = DetailAXProvider(
        role: "AXWindow",
        subrole: "AXStandardWindow",
        value: "must-not-be-read-after-expiry"
    )

    let envelope = try AXTextDetailSerializer.serialize(
        provider: root,
        snapshotID: "clock",
        clock: { clock.now() }
    )
    let stats = try textDetailStats(envelope.data)

    #expect(stats["truncated"] as? Bool == true)
    #expect((stats["truncation_reasons"] as? [String])?.contains("wall_clock_limit") == true)
    #expect(!root.stringReads.contains(kAXValueAttribute))
}

@Test func textDetailDiscardsValueWhoseAccessorCrossesDeadlineAndMakesNoFurtherProviderCalls() throws {
    let clock = MutableDetailClock()
    let root = DetailAXProvider(
        role: "AXTextArea",
        subrole: "AXStandardTextField",
        value: "must-be-discarded",
        onStringRead: { attribute in
            if attribute == kAXValueAttribute { clock.value = 5 }
        }
    )

    let envelope = try AXTextDetailSerializer.serialize(
        provider: root,
        snapshotID: "value-deadline",
        clock: { clock.value }
    )
    let json = try textDetailJSONObject(envelope.data)
    let rootJSON = try #require(json["root"] as? [String: Any])
    let stats = try #require(json["stats"] as? [String: Any])

    #expect(rootJSON["value"] == nil)
    #expect(stats["truncation_reasons"] as? [String] == ["wall_clock_limit"])
    #expect(root.boolReads.isEmpty)
    #expect(root.childrenReadCount == 0)
}

@Test func textDetailDiscardsBoundsAndChildrenWhenTheirAccessorsCrossDeadline() throws {
    let boundsClock = MutableDetailClock()
    let boundsProvider = DetailAXProvider(
        role: "AXWindow",
        subrole: "AXStandardWindow",
        onBoundsRead: { boundsClock.value = 5 }
    )
    let boundsEnvelope = try AXTextDetailSerializer.serialize(
        provider: boundsProvider,
        snapshotID: "bounds-deadline",
        clock: { boundsClock.value }
    )
    let boundsJSON = try textDetailJSONObject(boundsEnvelope.data)
    let boundsRoot = try #require(boundsJSON["root"] as? [String: Any])
    #expect(boundsRoot["bounds"] == nil)
    #expect(boundsProvider.stringReads == [kAXRoleAttribute, kAXSubroleAttribute])
    #expect(boundsProvider.boolReads.isEmpty)
    #expect(boundsProvider.childrenReadCount == 0)

    let childrenClock = MutableDetailClock()
    let child = DetailAXProvider(role: "AXStaticText", subrole: "AXUnknown", value: "child")
    let childrenProvider = DetailAXProvider(
        role: "AXWindow",
        subrole: "AXStandardWindow",
        children: [child],
        onChildrenRead: { childrenClock.value = 5 }
    )
    let childrenEnvelope = try AXTextDetailSerializer.serialize(
        provider: childrenProvider,
        snapshotID: "children-deadline",
        clock: { childrenClock.value }
    )
    let childrenJSON = try textDetailJSONObject(childrenEnvelope.data)
    let childrenRoot = try #require(childrenJSON["root"] as? [String: Any])
    #expect(childrenRoot["children"] == nil)
    #expect(child.stringReads.isEmpty)
    #expect(childrenProvider.childrenReadCount == 1)
    #expect(try textDetailStats(childrenEnvelope.data)["truncation_reasons"] as? [String] == ["wall_clock_limit"])
}

@Test func textDetailSystemBoundsStopsBeforeSizeReadWhenPositionCrossesDeadline() {
    let clock = MutableDetailClock()
    var positionReadCount = 0
    var sizeReadCount = 0
    let provider = SystemAXTextDetailAttributeProvider(
        copyAttributeValue: { attribute in
            if attribute == kAXPositionAttribute {
                positionReadCount += 1
                clock.value = 5
            } else if attribute == kAXSizeAttribute {
                sizeReadCount += 1
            }
            return (.success, nil)
        },
        childrenCount: { (.success, 0) },
        copyChildren: { _ in (.success, nil) },
        shouldContinue: { clock.value < 5 }
    )

    _ = provider.bounds()

    #expect(positionReadCount == 1)
    #expect(sizeReadCount == 0)
}

@Test func textDetailSystemChildrenStopsBeforeCopyWhenCountCrossesDeadline() {
    let clock = MutableDetailClock()
    var countReadCount = 0
    var copyReadCount = 0
    let provider = SystemAXTextDetailAttributeProvider(
        copyAttributeValue: { _ in (.success, nil) },
        childrenCount: {
            countReadCount += 1
            clock.value = 5
            return (.success, 1)
        },
        copyChildren: { _ in
            copyReadCount += 1
            return (.success, nil)
        },
        shouldContinue: { clock.value < 5 }
    )

    _ = provider.children(maximumCount: 1)

    #expect(countReadCount == 1)
    #expect(copyReadCount == 0)
}

@Test func textDetailSystemProviderSetsRemainingTimeoutBeforeEveryAXAccessor() {
    var events: [String] = []
    let child = AXUIElementCreateApplication(getpid())
    let provider = SystemAXTextDetailAttributeProvider(
        copyAttributeValue: { attribute in
            events.append("attribute:\(attribute)")
            return (.attributeUnsupported, nil)
        },
        childrenCount: {
            events.append("children:count")
            return (.success, 1)
        },
        copyChildren: { _ in
            events.append("children:copy")
            return (.success, [child] as CFArray)
        },
        makeChildProvider: { _ in
            DetailAXProvider(role: "AXStaticText", subrole: "AXStandardContent")
        },
        remainingTime: { 1.75 },
        setMessagingTimeout: { timeout in
            events.append(timeout > 0 ? "timeout:\(timeout)" : "reset")
            return .success
        }
    )

    _ = provider.stringValue(for: kAXRoleAttribute, maximumBytes: 4_096)
    _ = provider.boolValue(for: kAXEnabledAttribute)
    _ = provider.bounds()
    _ = provider.children(maximumCount: 1)

    #expect(events.count == 18)
    for index in stride(from: 0, to: events.count, by: 3) {
        #expect(events[index].hasPrefix("timeout:"))
        #expect(events[index + 2] == "reset")
    }
    #expect(events.enumerated().compactMap { index, event in
        event.hasPrefix("timeout:") ? index : nil
    } == [0, 3, 6, 9, 12, 15])
}

@Test func textDetailSystemProviderFailsClosedWhenMessagingTimeoutCannotBeSet() {
    var underlyingCalls = 0
    let provider = SystemAXTextDetailAttributeProvider(
        copyAttributeValue: { _ in
            underlyingCalls += 1
            return (.success, "secret" as CFString)
        },
        childrenCount: {
            underlyingCalls += 1
            return (.success, 0)
        },
        copyChildren: { _ in
            underlyingCalls += 1
            return (.success, nil)
        },
        remainingTime: { 1 },
        setMessagingTimeout: { _ in .illegalArgument }
    )

    #expect(provider.stringValue(for: kAXRoleAttribute, maximumBytes: 4_096).status == .hardFailure)
    #expect(provider.boolValue(for: kAXEnabledAttribute).status == .hardFailure)
    #expect(provider.bounds().status == .hardFailure)
    #expect(provider.children(maximumCount: 1).status == .failed)
    #expect(underlyingCalls == 0)
}

@Test func textDetailSystemProviderFailsClosedWhenMessagingTimeoutCannotBeReset() {
    var underlyingCalls = 0
    let provider = SystemAXTextDetailAttributeProvider(
        copyAttributeValue: { _ in
            underlyingCalls += 1
            return (.success, "AXWindow" as CFString)
        },
        childrenCount: { (.success, 0) },
        copyChildren: { _ in (.success, nil) },
        remainingTime: { 1 },
        setMessagingTimeout: { timeout in timeout > 0 ? .success : .failure }
    )

    #expect(provider.stringValue(for: kAXRoleAttribute, maximumBytes: 4_096).status == .hardFailure)
    #expect(underlyingCalls == 1)
}

@Test func textDetailSerializerTurnsBlockingSystemProviderDeadlineIntoPartialArtifact() throws {
    let blocker = DispatchSemaphore(value: 0)
    let started = ProcessInfo.processInfo.systemUptime
    let deadline = started + 0.05
    var configuredTimeout: TimeInterval?
    var logicalDeadlineExpired = false
    var valueReadCount = 0
    let provider = SystemAXTextDetailAttributeProvider(
        copyAttributeValue: { attribute in
            switch attribute {
            case kAXRoleAttribute:
                return (.success, "AXTextArea" as CFString)
            case kAXSubroleAttribute:
                return (.success, "AXStandardTextField" as CFString)
            case kAXValueAttribute:
                valueReadCount += 1
                let timeout = configuredTimeout ?? 1
                _ = blocker.wait(timeout: .now() + timeout)
                let remaining = deadline - ProcessInfo.processInfo.systemUptime
                if remaining > 0 { Thread.sleep(forTimeInterval: remaining) }
                logicalDeadlineExpired = true
                return (.cannotComplete, "must-not-persist" as CFString)
            default:
                return (.attributeUnsupported, nil)
            }
        },
        childrenCount: { (.success, 0) },
        copyChildren: { _ in (.success, nil) },
        remainingTime: {
            let remaining = deadline - ProcessInfo.processInfo.systemUptime
            return remaining > 0 ? remaining : nil
        },
        setMessagingTimeout: { timeout in
            if timeout > 0 { configuredTimeout = TimeInterval(timeout) }
            return .success
        }
    )

    let envelope = try AXTextDetailSerializer.serialize(
        provider: provider,
        snapshotID: "blocking-deadline",
        clock: { logicalDeadlineExpired ? maximumAXTextDetailDuration : 0 }
    )
    let elapsed = ProcessInfo.processInfo.systemUptime - started
    let json = try textDetailJSONObject(envelope.data)
    let root = try #require(json["root"] as? [String: Any])

    #expect(envelope.truncated == true)
    #expect(envelope.truncationReasons.contains("wall_clock_limit"))
    #expect(root["value"] == nil)
    #expect(!String(decoding: envelope.data, as: UTF8.self).contains("must-not-persist"))
    #expect(valueReadCount == 1)
    #expect(configuredTimeout != nil)
    #expect(configuredTimeout.map { $0 > 0 && $0 <= 0.05 } == true)
    #expect(elapsed >= 0.03)
    #expect(elapsed < 0.25)
}

@Test func textDetailSystemProviderPreservesCannotCompleteBeforeDeadlineAsHardFailure() {
    let provider = SystemAXTextDetailAttributeProvider(
        copyAttributeValue: { _ in (.cannotComplete, nil) },
        childrenCount: { (.success, 0) },
        copyChildren: { _ in (.success, nil) },
        remainingTime: { 1 },
        setMessagingTimeout: { _ in .success }
    )

    #expect(provider.stringValue(for: kAXRoleAttribute, maximumBytes: 4_096).status == .hardFailure)
}

@Test func textDetailSerializerTreatsGenericFailureForOptionalStringAsUnreadable() throws {
    let provider = SystemAXTextDetailAttributeProvider(
        copyAttributeValue: { attribute in
            switch attribute {
            case kAXRoleAttribute:
                return (.success, "AXTextArea" as CFString)
            case kAXSubroleAttribute:
                return (.success, "AXStandardTextField" as CFString)
            case kAXDescriptionAttribute:
                return (.failure, nil)
            case kAXValueAttribute:
                return (.success, "fixture marker" as CFString)
            default:
                return (.attributeUnsupported, nil)
            }
        },
        childrenCount: { (.success, 0) },
        copyChildren: { _ in (.success, nil) },
        shouldContinue: { true }
    )

    let envelope = try AXTextDetailSerializer.serialize(
        provider: provider,
        snapshotID: "generic-optional-string-failure",
        clock: { 0 }
    )
    let root = try #require(textDetailJSONObject(envelope.data)["root"] as? [String: Any])

    #expect(root["role"] as? String == "AXTextArea")
    #expect(root["label"] == nil)
    #expect(root["value"] as? String == "fixture marker")
    #expect(root["redacted"] == nil)
}

@Test func textDetailSerializerRedactsGenericIdentityFailureWithoutReadingValue() throws {
    var valueReadCount = 0
    let provider = SystemAXTextDetailAttributeProvider(
        copyAttributeValue: { attribute in
            switch attribute {
            case kAXRoleAttribute:
                return (.failure, "must-not-persist" as CFString)
            case kAXSubroleAttribute:
                return (.success, "AXStandardTextField" as CFString)
            case kAXValueAttribute:
                valueReadCount += 1
                return (.success, "secret" as CFString)
            default:
                return (.attributeUnsupported, nil)
            }
        },
        childrenCount: { (.success, 0) },
        copyChildren: { _ in (.success, nil) },
        shouldContinue: { true }
    )

    let envelope = try AXTextDetailSerializer.serialize(
        provider: provider,
        snapshotID: "generic-identity-failure",
        clock: { 0 }
    )
    let root = try #require(textDetailJSONObject(envelope.data)["root"] as? [String: Any])
    let persisted = String(decoding: envelope.data, as: UTF8.self)

    #expect(root["role"] as? String == "AXUnknown")
    #expect(root["redacted"] as? Bool == true)
    #expect(root["value"] == nil)
    #expect(valueReadCount == 0)
    #expect(!persisted.contains("must-not-persist"))
    #expect(!persisted.contains("secret"))
}

@Test func textDetailSerializerOmitsGenericValueFailurePayload() throws {
    let provider = SystemAXTextDetailAttributeProvider(
        copyAttributeValue: { attribute in
            switch attribute {
            case kAXRoleAttribute:
                return (.success, "AXTextArea" as CFString)
            case kAXSubroleAttribute:
                return (.success, "AXStandardTextField" as CFString)
            case kAXValueAttribute:
                return (.failure, "must-not-persist" as CFString)
            default:
                return (.attributeUnsupported, nil)
            }
        },
        childrenCount: { (.success, 0) },
        copyChildren: { _ in (.success, nil) },
        shouldContinue: { true }
    )

    let envelope = try AXTextDetailSerializer.serialize(
        provider: provider,
        snapshotID: "generic-value-failure",
        clock: { 0 }
    )
    let root = try #require(textDetailJSONObject(envelope.data)["root"] as? [String: Any])

    #expect(root["role"] as? String == "AXTextArea")
    #expect(root["value"] == nil)
    #expect(!String(decoding: envelope.data, as: UTF8.self).contains("must-not-persist"))
}

@Test func textDetailSystemStringConversionRejectsMalformedUTF16AndKeepsScalarBoundaries() {
    let malformed: [[UniChar]] = [
        [0xD800],
        [0xDC00],
        [0xD800, 0x0041],
    ]
    for units in malformed {
        let raw = detailCFString(units)
        let provider = SystemAXTextDetailAttributeProvider(
            copyAttributeValue: { _ in (.success, raw) },
            childrenCount: { (.success, 0) },
            copyChildren: { _ in (.success, nil) },
            shouldContinue: { true }
        )

        let result = provider.stringValue(for: kAXRoleAttribute, maximumBytes: 4_096)

        #expect(result.status == .unreadable)
        #expect(result.value == nil)
    }

    let valid = "A😀B" as CFString
    let provider = SystemAXTextDetailAttributeProvider(
        copyAttributeValue: { _ in (.success, valid) },
        childrenCount: { (.success, 0) },
        copyChildren: { _ in (.success, nil) },
        shouldContinue: { true }
    )
    #expect(provider.stringValue(for: kAXRoleAttribute, maximumBytes: 5) == AXTextDetailStringResult(
        value: "A😀",
        status: .truncated
    ))
    #expect(provider.stringValue(for: kAXRoleAttribute, maximumBytes: 4) == AXTextDetailStringResult(
        value: "A",
        status: .truncated
    ))

    let oversizedScalar = "😀tail" as CFString
    let oversizedScalarProvider = SystemAXTextDetailAttributeProvider(
        copyAttributeValue: { _ in (.success, oversizedScalar) },
        childrenCount: { (.success, 0) },
        copyChildren: { _ in (.success, nil) },
        shouldContinue: { true }
    )
    #expect(oversizedScalarProvider.stringValue(
        for: kAXRoleAttribute,
        maximumBytes: 3
    ) == AXTextDetailStringResult(value: nil, status: .truncated))
}

@Test func textDetailSystemStringSkipsConversionWhenAXCopyCrossesDeadline() {
    let clock = MutableDetailClock()
    var units = [UniChar](repeating: 0x0041, count: maximumAXTextDetailValueBytes + 1)
    units.append(0xD800)
    let raw = detailCFString(units)
    var conversionCount = 0
    let provider = SystemAXTextDetailAttributeProvider(
        copyAttributeValue: { _ in
            clock.value = 5
            return (.success, raw)
        },
        childrenCount: { (.success, 0) },
        copyChildren: { _ in (.success, nil) },
        convertString: { _, _ in
            conversionCount += 1
            return AXTextDetailStringResult(value: nil, status: .unreadable)
        },
        shouldContinue: { clock.value < 5 }
    )

    let result = provider.stringValue(
        for: kAXValueAttribute,
        maximumBytes: maximumAXTextDetailValueBytes
    )

    #expect(result.status == .missing)
    #expect(result.value == nil)
    #expect(conversionCount == 0)
}

@Test func textDetailSystemStringValidationIsBoundedByStructuralAndValueBudgets() {
    for maximumBytes in [
        maximumAXTextDetailStructuralStringBytes,
        maximumAXTextDetailValueBytes,
    ] {
        var outsideUnits = [UniChar](repeating: 0x0041, count: maximumBytes)
        outsideUnits.append(0xD800)
        let outsideMalformed = detailCFString(outsideUnits)
        let outsideProvider = SystemAXTextDetailAttributeProvider(
            copyAttributeValue: { _ in (.success, outsideMalformed) },
            childrenCount: { (.success, 0) },
            copyChildren: { _ in (.success, nil) },
            shouldContinue: { true }
        )

        let outsideResult = outsideProvider.stringValue(
            for: kAXValueAttribute,
            maximumBytes: maximumBytes
        )

        #expect(outsideResult.status == .truncated)
        #expect(outsideResult.value == String(repeating: "A", count: maximumBytes))

        var insideUnits = [UniChar](repeating: 0x0041, count: maximumBytes - 1)
        insideUnits.append(0xD800)
        let insideMalformed = detailCFString(insideUnits)
        let insideProvider = SystemAXTextDetailAttributeProvider(
            copyAttributeValue: { _ in (.success, insideMalformed) },
            childrenCount: { (.success, 0) },
            copyChildren: { _ in (.success, nil) },
            shouldContinue: { true }
        )

        let insideResult = insideProvider.stringValue(
            for: kAXValueAttribute,
            maximumBytes: maximumBytes
        )

        #expect(insideResult.status == .unreadable)
        #expect(insideResult.value == nil)
    }
}

@Test func textDetailMalformedSystemIdentityRedactsWithoutReadingValue() throws {
    for malformedAttribute in [kAXRoleAttribute, kAXSubroleAttribute] {
        let malformed = detailCFString([0xD800])
        var valueReadCount = 0
        let provider = SystemAXTextDetailAttributeProvider(
            copyAttributeValue: { attribute in
                switch attribute {
                case malformedAttribute:
                    return (.success, malformed)
                case kAXRoleAttribute:
                    return (.success, "AXTextField" as CFString)
                case kAXSubroleAttribute:
                    return (.success, "AXUnknown" as CFString)
                case kAXValueAttribute:
                    valueReadCount += 1
                    return (.success, "secret" as CFString)
                default:
                    return (.attributeUnsupported, nil)
                }
            },
            childrenCount: { (.attributeUnsupported, 0) },
            copyChildren: { _ in (.success, nil) },
            shouldContinue: { true }
        )

        let envelope = try AXTextDetailSerializer.serialize(
            provider: provider,
            snapshotID: "malformed-identity",
            clock: { 0 }
        )
        let root = try #require(textDetailJSONObject(envelope.data)["root"] as? [String: Any])

        #expect(root["redacted"] as? Bool == true)
        #expect(root["value"] == nil)
        #expect(valueReadCount == 0)
    }
}

@Test func textDetailPreDeadlineHardAndInvalidResultsFailSerialization() {
    let hardAttributes = [
        kAXRoleAttribute,
        kAXEnabledAttribute,
    ]
    for attribute in hardAttributes {
        let provider = SystemAXTextDetailAttributeProvider(
            copyAttributeValue: { requested in
                if requested == attribute {
                    return (.cannotComplete, nil)
                }
                switch requested {
                case kAXRoleAttribute: return (.success, "AXWindow" as CFString)
                case kAXSubroleAttribute: return (.success, "AXStandardWindow" as CFString)
                default: return (.attributeUnsupported, nil)
                }
            },
            childrenCount: { (.attributeUnsupported, 0) },
            copyChildren: { _ in (.success, nil) },
            shouldContinue: { true }
        )

        do {
            _ = try AXTextDetailSerializer.serialize(
                provider: provider,
                snapshotID: "hard-before-deadline",
                clock: { 0 }
            )
            Issue.record("expected hard result before deadline for \(attribute)")
        } catch {
            #expect(error as? AXTextDetailSerializationError == .attributeReadFailed)
        }
    }

    var invalidPosition = CGPoint(x: CGFloat.nan, y: 0)
    var validSize = CGSize(width: 10, height: 10)
    let invalidPositionValue = AXValueCreate(.cgPoint, &invalidPosition)!
    let validSizeValue = AXValueCreate(.cgSize, &validSize)!
    let invalidProvider = SystemAXTextDetailAttributeProvider(
        copyAttributeValue: { attribute in
            switch attribute {
            case kAXRoleAttribute: return (.success, "AXWindow" as CFString)
            case kAXSubroleAttribute: return (.success, "AXStandardWindow" as CFString)
            case kAXPositionAttribute: return (.success, invalidPositionValue)
            case kAXSizeAttribute:
                return (.success, validSizeValue)
            default: return (.attributeUnsupported, nil)
            }
        },
        childrenCount: { (.attributeUnsupported, 0) },
        copyChildren: { _ in (.success, nil) },
        shouldContinue: { true }
    )
    do {
        _ = try AXTextDetailSerializer.serialize(
            provider: invalidProvider,
            snapshotID: "invalid-before-deadline",
            clock: { 0 }
        )
        Issue.record("expected invalid geometry before deadline")
    } catch {
        #expect(error as? AXTextDetailSerializationError == .invalidGeometry)
    }
}

@Test func textDetailCompositeHardAndInvalidResultsFailBeforeDeadline() {
    let boundsProvider = SystemAXTextDetailAttributeProvider(
        copyAttributeValue: { attribute in
            if attribute == kAXPositionAttribute {
                return (.cannotComplete, nil)
            }
            switch attribute {
            case kAXRoleAttribute: return (.success, "AXWindow" as CFString)
            case kAXSubroleAttribute: return (.success, "AXStandardWindow" as CFString)
            default: return (.attributeUnsupported, nil)
            }
        },
        childrenCount: { (.attributeUnsupported, 0) },
        copyChildren: { _ in (.success, nil) },
        shouldContinue: { true }
    )
    do {
        _ = try AXTextDetailSerializer.serialize(
            provider: boundsProvider,
            snapshotID: "bounds-hard-before-deadline",
            clock: { 0 }
        )
        Issue.record("expected bounds hard failure before deadline")
    } catch {
        #expect(error as? AXTextDetailSerializationError == .attributeReadFailed)
    }

    var invalidPosition = CGPoint(x: CGFloat.nan, y: 0)
    let invalidPositionValue = AXValueCreate(.cgPoint, &invalidPosition)!
    var sizeReadCount = 0
    let invalidBoundsProvider = SystemAXTextDetailAttributeProvider(
        copyAttributeValue: { attribute in
            switch attribute {
            case kAXRoleAttribute: return (.success, "AXWindow" as CFString)
            case kAXSubroleAttribute: return (.success, "AXStandardWindow" as CFString)
            case kAXPositionAttribute:
                return (.success, invalidPositionValue)
            case kAXSizeAttribute:
                sizeReadCount += 1
                return (.attributeUnsupported, nil)
            default: return (.attributeUnsupported, nil)
            }
        },
        childrenCount: { (.attributeUnsupported, 0) },
        copyChildren: { _ in (.success, nil) },
        shouldContinue: { true }
    )
    do {
        _ = try AXTextDetailSerializer.serialize(
            provider: invalidBoundsProvider,
            snapshotID: "bounds-invalid-before-deadline",
            clock: { 0 }
        )
        Issue.record("expected first bounds component invalidity before deadline")
    } catch {
        #expect(error as? AXTextDetailSerializationError == .invalidGeometry)
    }
    #expect(sizeReadCount == 0)

    let childrenProvider = SystemAXTextDetailAttributeProvider(
        copyAttributeValue: { attribute in
            switch attribute {
            case kAXRoleAttribute: return (.success, "AXWindow" as CFString)
            case kAXSubroleAttribute: return (.success, "AXStandardWindow" as CFString)
            default: return (.attributeUnsupported, nil)
            }
        },
        childrenCount: {
            return (.cannotComplete, 0)
        },
        copyChildren: { _ in (.success, nil) },
        shouldContinue: { true }
    )
    do {
        _ = try AXTextDetailSerializer.serialize(
            provider: childrenProvider,
            snapshotID: "children-hard-before-deadline",
            clock: { 0 }
        )
        Issue.record("expected children hard failure before deadline")
    } catch {
        #expect(error as? AXTextDetailSerializationError == .childrenReadFailed)
    }

    let copyProvider = SystemAXTextDetailAttributeProvider(
        copyAttributeValue: { attribute in
            switch attribute {
            case kAXRoleAttribute: return (.success, "AXWindow" as CFString)
            case kAXSubroleAttribute: return (.success, "AXStandardWindow" as CFString)
            default: return (.attributeUnsupported, nil)
            }
        },
        childrenCount: { (.success, 1) },
        copyChildren: { _ in
            return (.cannotComplete, nil)
        },
        shouldContinue: { true }
    )
    do {
        _ = try AXTextDetailSerializer.serialize(
            provider: copyProvider,
            snapshotID: "copy-hard-before-deadline",
            clock: { 0 }
        )
        Issue.record("expected child-copy hard failure before deadline")
    } catch {
        #expect(error as? AXTextDetailSerializationError == .childrenReadFailed)
    }
}

@Test func textDetailSystemChildrenExpiryAfterCopyDoesNotTraverseOrBuildProviders() {
    let clock = MutableDetailClock()
    let child = AXUIElementCreateApplication(getpid())
    let children = Array(repeating: child, count: 4_000) as CFArray
    var childProviderBuildCount = 0
    let provider = SystemAXTextDetailAttributeProvider(
        copyAttributeValue: { _ in (.attributeUnsupported, nil) },
        childrenCount: { (.success, 4_000) },
        copyChildren: { _ in
            clock.value = 5
            return (.success, children)
        },
        makeChildProvider: { _ in
            childProviderBuildCount += 1
            return DetailAXProvider(role: "AXStaticText", subrole: "AXUnknown")
        },
        shouldContinue: { clock.value < 5 }
    )

    let result = provider.children(maximumCount: 4_000)

    #expect(result.status == .complete)
    #expect(result.values.isEmpty)
    #expect(childProviderBuildCount == 0)
}

@Test func textDetailHardAttributeFailuresFailTheWholeSerialization() {
    let cases: [(String, DetailAXProvider)] = [
        ("role", DetailAXProvider(
            role: "AXWindow",
            subrole: "AXStandardWindow",
            hardStringFailures: [kAXRoleAttribute]
        )),
        ("label", DetailAXProvider(
            role: "AXWindow",
            subrole: "AXStandardWindow",
            hardStringFailures: [kAXDescriptionAttribute]
        )),
        ("value", DetailAXProvider(
            role: "AXTextArea",
            subrole: "AXStandardTextField",
            value: "value",
            hardStringFailures: [kAXValueAttribute]
        )),
        ("bool", DetailAXProvider(
            role: "AXWindow",
            subrole: "AXStandardWindow",
            hardBoolFailures: [kAXEnabledAttribute]
        )),
        ("bounds", DetailAXProvider(
            role: "AXWindow",
            subrole: "AXStandardWindow",
            boundsStatus: .hardFailure
        )),
    ]

    for (label, provider) in cases {
        do {
            _ = try AXTextDetailSerializer.serialize(
                provider: provider,
                snapshotID: "hard-\(label)",
                clock: { 0 }
            )
            Issue.record("expected hard \(label) failure")
        } catch {
            #expect(error as? AXTextDetailSerializationError == .attributeReadFailed)
        }
    }
}

@Test func textDetailRejectsNonfiniteGeometry() {
    for bounds in [
        CGRect(x: CGFloat.nan, y: 0, width: 1, height: 1),
        CGRect(x: 0, y: CGFloat.infinity, width: 1, height: 1),
        CGRect(x: 0, y: 0, width: CGFloat.nan, height: 1),
        CGRect(x: 0, y: 0, width: 1, height: CGFloat.infinity),
    ] {
        let provider = DetailAXProvider(
            role: "AXWindow",
            subrole: "AXStandardWindow",
            bounds: bounds
        )
        #expect(throws: AXTextDetailSerializationError.self) {
            try AXTextDetailSerializer.serialize(provider: provider, snapshotID: "geometry", clock: { 0 })
        }
    }
}

@Test func textDetailFailedChildrenAccessorFailsTheWholeSerialization() {
    let provider = DetailAXProvider(
        role: "AXWindow",
        subrole: "AXStandardWindow",
        childrenStatus: .failed
    )

    #expect(throws: AXTextDetailSerializationError.self) {
        try AXTextDetailSerializer.serialize(provider: provider, snapshotID: "children", clock: { 0 })
    }
}

@Test func textDetailSecureAndIndeterminateIdentityNeverReadsSensitiveAttributes() throws {
    let identities: [(AXTextDetailStringResult, AXTextDetailStringResult)] = [
        (.complete("AXSecureTextField"), .complete("AXUnknown")),
        (.complete("AXTextField"), .complete("AXSecureTextField")),
        (.complete("AXUnknown"), .complete("AXStandardContent")),
        (.complete("AXTextField"), .complete("AXUnknown")),
        (.complete(""), .complete("AXUnknown")),
        (.complete("AXTextField"), .complete("")),
        (.missing, .complete("AXUnknown")),
        (.complete("AXTextField"), .missing),
        (.truncated("AXTextField"), .complete("AXUnknown")),
        (.complete("AXTextField"), .truncated("AXUnknown")),
        (.complete(String(repeating: "R", count: 4 * 1_024 + 1)), .complete("AXUnknown")),
        (.unreadable, .complete("AXUnknown")),
        (.complete("AXTextField"), .unreadable),
    ]
    let sensitive = Set([
        kAXValueAttribute,
        kAXSelectedTextAttribute,
        kAXSelectedTextRangeAttribute,
        kAXVisibleCharacterRangeAttribute,
        "AXAttributedStringForRange",
    ])

    for (index, identity) in identities.enumerated() {
        let provider = DetailAXProvider(roleResult: identity.0, subroleResult: identity.1)
        let envelope = try AXTextDetailSerializer.serialize(
            provider: provider,
            snapshotID: "secure-\(index)",
            clock: { 0 }
        )
        let json = try textDetailJSONObject(envelope.data)
        let root = try #require(json["root"] as? [String: Any])

        #expect(sensitive.isDisjoint(with: provider.stringReads))
        #expect(root["redacted"] as? Bool == true)
        #expect(root["value"] == nil)
        #expect(root["selected_text"] == nil)
        #expect(root["selected_text_range"] == nil)
        #expect(root["visible_character_range"] == nil)
        #expect(root["attributed_text"] == nil)
    }
}

@Test func textDetailCanonicalizesIncompleteIdentityBeforeSerialization() throws {
    let cases: [(
        role: AXTextDetailStringResult,
        subrole: AXTextDetailStringResult,
        expectedRole: String,
        expectedSubrole: String?
    )] = [
        (.truncated("AXTextFie"), .complete("AXStandardTextField"), "AXUnknown", "AXStandardTextField"),
        (.complete("AXTextField"), .truncated("AXStandard"), "AXTextField", nil),
        (.missing, .complete("AXStandardTextField"), "AXUnknown", "AXStandardTextField"),
        (.complete("AXTextField"), .missing, "AXTextField", nil),
    ]

    for (index, identity) in cases.enumerated() {
        let provider = DetailAXProvider(
            roleResult: identity.role,
            subroleResult: identity.subrole
        )
        let envelope = try AXTextDetailSerializer.serialize(
            provider: provider,
            snapshotID: "canonical-\(index)",
            clock: { 0 }
        )
        let json = try textDetailJSONObject(envelope.data)
        let root = try #require(json["root"] as? [String: Any])

        #expect(root["role"] as? String == identity.expectedRole)
        #expect(root["subrole"] as? String == identity.expectedSubrole)
        #expect(root["redacted"] as? Bool == true)
        #expect(root["value"] == nil)
    }
}

@Test func textDetailChargesSynthesizedUnknownRoleToAggregateBudget() throws {
    let root = DetailAXProvider(
        role: "",
        subrole: "AXStandardWindow",
        children: (0..<20).map { _ in
            DetailAXProvider(
                role: "AXTextArea",
                subrole: "AXStandardTextField",
                value: String(repeating: "v", count: maximumAXTextDetailValueBytes)
            )
        }
    )

    let envelope = try AXTextDetailSerializer.serialize(
        provider: root,
        snapshotID: "fallback-budget",
        clock: { 0 }
    )
    let json = try textDetailJSONObject(envelope.data)
    let rootJSON = try #require(json["root"] as? [String: Any])

    #expect(rootJSON["role"] as? String == "AXUnknown")
    #expect(textDetailAggregateTextBytes(rootJSON) <= maximumAXTextDetailAggregateTextBytes)
}

@Test func timedOutPrimaryCaptureFallsBackToTheSameWindowID() throws {
    let provider = RecordingImageProvider(primaryError: .timedOut, fallbackImage: testImage())

    _ = try captureExactWindowImage(windowID: 73, provider: provider)

    #expect(provider.fallbackWindowIDs == [73])
}

@Test func popupPrimaryContentCannotBeScaledParentPixels() throws {
    let popup = CGRect(x: 89, y: 70, width: 1038, height: 159)
    #expect(!primaryWindowContentHasExactSize(
        contentRect: CGRect(x: 0, y: 0, width: 1319, height: 768), expectedBounds: popup
    ))
    #expect(primaryWindowContentHasExactSize(
        contentRect: CGRect(x: 0, y: 0, width: 1038, height: 159), expectedBounds: popup
    ))
    #expect(!primaryWindowContentHasExactSize(contentRect: .null, expectedBounds: popup))
    #expect(!primaryWindowContentHasExactSize(contentRect: popup, expectedBounds: .zero))
    #expect(!primaryWindowContentHasExactSize(
        contentRect: CGRect(x: 0, y: 0, width: 1039, height: 159), expectedBounds: popup
    ))
    let provider = RecordingImageProvider(primaryError: .contentMismatch, fallbackImage: testImage())
    _ = try captureExactWindowImage(windowID: 73, provider: provider)
    #expect(provider.fallbackWindowIDs == [73])
}

@Test func paddedScaledParentPixelsFallBackToExactPopupWindow() throws {
    let popup = CGRect(x: 89, y: 70, width: 1038, height: 199)
    let parent = CGRect(x: 25, y: 30, width: 1319, height: 768)
    // The live Edge popup produced a 2076x398 canvas containing the entire
    // parent browser shrunk to 684x398, with the remaining width transparent.
    let wrong = try popupCoverageImage(opaqueRect: CGRect(x: 0, y: 0, width: 684, height: 398))
    let exact = try popupCoverageImage(opaqueRect: CGRect(x: 0, y: 0, width: 2076, height: 398))
    #expect(windowImageHasScaledParentPadding(
        wrong, expectedBounds: popup, sameApplicationWindowFrames: [parent]
    ))
    let provider = RecordingImageProvider(primaryError: nil, primaryImage: wrong, fallbackImage: exact)

    let captured = try captureExactWindowImage(
        windowID: 73, provider: provider,
        expectedBounds: popup, sameApplicationWindowFrames: [parent]
    )

    #expect(provider.fallbackWindowIDs == [73])
    #expect(captured.width == 2076 && captured.height == 398)
    #expect(!windowImageHasScaledParentPadding(
        captured, expectedBounds: popup, sameApplicationWindowFrames: [parent]
    ))
}

@Test func paddedScaledParentFallbackCannotBePublishedAsPopup() throws {
    let popup = CGRect(x: 89, y: 70, width: 1038, height: 199)
    let parent = CGRect(x: 25, y: 30, width: 1319, height: 768)
    let wrong = try popupCoverageImage(opaqueRect: CGRect(x: 0, y: 0, width: 684, height: 398))
    let provider = RecordingImageProvider(primaryError: nil, primaryImage: wrong, fallbackImage: wrong)

    #expect(throws: WindowObservationError.self) {
        try captureExactWindowImage(
            windowID: 73, provider: provider,
            expectedBounds: popup, sameApplicationWindowFrames: [parent]
        )
    }
    #expect(provider.fallbackWindowIDs == [73])
}

@Test func largePaddedScaledParentStillFallsBackAfterBoundedSampling() throws {
    let popup = CGRect(x: 500, y: 100, width: 2500, height: 500)
    let parent = CGRect(x: 0, y: 0, width: 4000, height: 2000)
    let canvas = CGSize(width: 5000, height: 1000)
    let wrong = try popupCoverageImage(
        opaqueRect: CGRect(x: 0, y: 0, width: 2000, height: 1000), canvasSize: canvas
    )
    let exact = try popupCoverageImage(
        opaqueRect: CGRect(x: 0, y: 0, width: 5000, height: 1000), canvasSize: canvas
    )
    #expect(windowImageHasScaledParentPadding(
        wrong, expectedBounds: popup, sameApplicationWindowFrames: [parent]
    ))
    let provider = RecordingImageProvider(primaryError: nil, primaryImage: wrong, fallbackImage: exact)

    _ = try captureExactWindowImage(
        windowID: 73, provider: provider,
        expectedBounds: popup, sameApplicationWindowFrames: [parent]
    )

    #expect(provider.fallbackWindowIDs == [73])
}

@Test func genuineTransparentPopupAndOpaqueBlackDoNotTriggerParentPaddingGuard() throws {
    let popup = CGRect(x: 89, y: 70, width: 1038, height: 199)
    let parent = CGRect(x: 25, y: 30, width: 1319, height: 768)
    let transparent = try popupCoverageImage(opaqueRect: CGRect(x: 300, y: 50, width: 200, height: 100))
    let opaqueBlack = try popupCoverageImage(opaqueRect: CGRect(x: 0, y: 0, width: 2076, height: 398))
    for image in [transparent, opaqueBlack] {
        #expect(!windowImageHasScaledParentPadding(
            image, expectedBounds: popup, sameApplicationWindowFrames: [parent]
        ))
        let provider = RecordingImageProvider(primaryError: nil, primaryImage: image, fallbackImage: nil)
        _ = try captureExactWindowImage(
            windowID: 73, provider: provider,
            expectedBounds: popup, sameApplicationWindowFrames: [parent]
        )
        #expect(provider.fallbackWindowIDs.isEmpty)
    }
}

@Test func leftTransparentPopupWithDifferentParentAspectUsesPrimaryImage() throws {
    let popup = CGRect(x: 89, y: 70, width: 1038, height: 199)
    let differentlyShapedParent = CGRect(x: 25, y: 30, width: 1200, height: 800)
    // Its alpha coverage is identical to the bad Edge image, but the nearby
    // parent cannot account for the 684-pixel visible width when scaled.
    let legitimate = try popupCoverageImage(opaqueRect: CGRect(x: 0, y: 0, width: 684, height: 398))
    #expect(!windowImageHasScaledParentPadding(
        legitimate, expectedBounds: popup, sameApplicationWindowFrames: [differentlyShapedParent]
    ))
    let provider = RecordingImageProvider(primaryError: nil, primaryImage: legitimate, fallbackImage: nil)

    let captured = try captureExactWindowImage(
        windowID: 73, provider: provider,
        expectedBounds: popup, sameApplicationWindowFrames: [differentlyShapedParent]
    )

    #expect(captured.width == 2076 && captured.height == 398)
    #expect(provider.fallbackWindowIDs.isEmpty)
}

@Test func leftTransparentPopupMatchingParentAspectFailsClosedIfFallbackMatches() throws {
    let popup = CGRect(x: 89, y: 70, width: 1038, height: 199)
    let parent = CGRect(x: 25, y: 30, width: 1319, height: 768)
    // This is a legitimate transparent popup, but its pixels and geometry are
    // identical to a scaled parent image. Pixel-only validation cannot tell.
    let legitimate = try popupCoverageImage(opaqueRect: CGRect(x: 0, y: 0, width: 684, height: 398))
    let provider = RecordingImageProvider(
        primaryError: nil, primaryImage: legitimate, fallbackImage: legitimate
    )

    #expect(throws: WindowObservationError.self) {
        try captureExactWindowImage(
            windowID: 73, provider: provider,
            expectedBounds: popup, sameApplicationWindowFrames: [parent]
        )
    }
    #expect(provider.fallbackWindowIDs == [73])
}

@Test func popupGeometryFallbackFailureDoesNotCaptureAnotherSurface() {
    let provider = RecordingImageProvider(primaryError: .contentMismatch, fallbackImage: nil)
    #expect(throws: WindowObservationError.self) {
        try captureExactWindowImage(windowID: 73, provider: provider)
    }
    #expect(provider.fallbackWindowIDs == [73])
}

@Test func exactWindowFallbackFailureFailsClosed() {
    let provider = RecordingImageProvider(primaryError: .timedOut, fallbackImage: nil)

    #expect(throws: WindowObservationError.self) {
        try captureExactWindowImage(windowID: 73, provider: provider)
    }
    #expect(provider.fallbackWindowIDs == [73])
}

@Test func targetChangeAfterExactWindowFallbackFailsClosed() throws {
    let bounds = CGRect(x: 20, y: 30, width: 400, height: 300)
    let target = CaptureIdentity(pid: 42, windowID: 73, bounds: bounds, axIdentity: 91)
    let switched = CaptureIdentity(pid: 42, windowID: 74, bounds: bounds, axIdentity: 92)
    let provider = RecordingImageProvider(primaryError: .timedOut, fallbackImage: testImage())

    _ = try captureExactWindowImage(windowID: target.windowID, provider: provider)

    #expect(throws: WindowObservationError.self) {
        try verifyCaptureIdentity(target: target, before: target, after: switched)
    }
    #expect(provider.fallbackWindowIDs == [target.windowID])
}

@Test func dialogSnapshotsUseBoundedAccessibilityDepth() {
    #expect(
        focusedObservationMaximumDepth(
            preference: .selectedWindow,
            rootSubrole: "AXDialog"
        ) == 5
    )
    #expect(
        focusedObservationMaximumDepth(
            preference: .selectedWindow,
            rootSubrole: "AXStandardWindow"
        ) == maximumAXDepth
    )
    #expect(
        focusedObservationMaximumDepth(
            preference: .containedOverlay,
            rootSubrole: nil,
            rootIsExpectedWindow: true
        ) == 5
    )
    #expect(
        focusedObservationMaximumDepth(
            preference: .containedOverlay,
            rootSubrole: nil,
            rootIsExpectedWindow: false
        ) == 2
    )
}

@Test func keyboardFocusAuthorityRequiresCompleteBoundedNonemptyIdentityAndContainedFiniteBounds() {
    let container = CGRect(x: 0, y: 0, width: 500, height: 400)
    let completeRole = BoundedAXStringResult(value: "AXTextArea", status: .complete)
    let completeSubrole = BoundedAXStringResult(value: nil, status: .complete)

    #expect(makeKeyboardFocusAuthority(
        identityToken: "ax:focus",
        bounds: CGRect(x: 20, y: 30, width: 200, height: 100),
        role: completeRole,
        subrole: completeSubrole,
        enabled: true,
        containerBounds: container
    ) == KeyboardFocusAuthority(
        identityToken: "ax:focus",
        bounds: CGRect(x: 20, y: 30, width: 200, height: 100),
        role: "AXTextArea",
        subrole: nil
    ))

    for authority in [
        makeKeyboardFocusAuthority(
            identityToken: "",
            bounds: CGRect(x: 20, y: 30, width: 200, height: 100),
            role: completeRole,
            subrole: completeSubrole,
            enabled: true,
            containerBounds: container
        ),
        makeKeyboardFocusAuthority(
            identityToken: "ax:focus",
            bounds: container,
            role: completeRole,
            subrole: completeSubrole,
            enabled: true,
            containerBounds: container
        ),
        makeKeyboardFocusAuthority(
            identityToken: "ax:focus",
            bounds: CGRect(x: 0, y: 30, width: 200, height: 100),
            role: completeRole,
            subrole: completeSubrole,
            enabled: true,
            containerBounds: container
        ),
        makeKeyboardFocusAuthority(
            identityToken: "ax:focus",
            bounds: CGRect(x: 450, y: 30, width: 200, height: 100),
            role: completeRole,
            subrole: completeSubrole,
            enabled: true,
            containerBounds: container
        ),
        makeKeyboardFocusAuthority(
            identityToken: "ax:focus",
            bounds: CGRect(x: CGFloat.nan, y: 30, width: 200, height: 100),
            role: completeRole,
            subrole: completeSubrole,
            enabled: true,
            containerBounds: container
        ),
        makeKeyboardFocusAuthority(
            identityToken: "ax:focus",
            bounds: CGRect(x: 20, y: 30, width: 200, height: 100),
            role: BoundedAXStringResult(value: "AXTextArea", status: .truncated),
            subrole: completeSubrole,
            enabled: true,
            containerBounds: container
        ),
        makeKeyboardFocusAuthority(
            identityToken: "ax:focus",
            bounds: CGRect(x: 20, y: 30, width: 200, height: 100),
            role: completeRole,
            subrole: completeSubrole,
            enabled: false,
            containerBounds: container
        ),
        makeKeyboardFocusAuthority(
            identityToken: "ax:focus",
            bounds: CGRect(x: 20, y: 30, width: 200, height: 100),
            role: completeRole,
            subrole: completeSubrole,
            enabled: nil,
            containerBounds: container
        ),
    ] {
        #expect(authority == nil)
    }
}

private final class RecordingImageProvider: ExactWindowImageProviding {
    let primaryError: PrimaryWindowCaptureError?
    let primary: CGImage?
    let image: CGImage?
    var fallbackWindowIDs: [CGWindowID] = []

    init(primaryError: PrimaryWindowCaptureError?, primaryImage: CGImage? = nil, fallbackImage: CGImage?) {
        self.primaryError = primaryError
        primary = primaryImage
        self.image = fallbackImage
    }

    func primaryImage() throws -> CGImage {
        if let primaryError { throw primaryError }
        return primary ?? testImage()
    }

    func fallbackImage(for windowID: CGWindowID) -> CGImage? {
        fallbackWindowIDs.append(windowID)
        return image
    }
}

private func popupCoverageImage(
    opaqueRect: CGRect,
    canvasSize: CGSize = CGSize(width: 2076, height: 398)
) throws -> CGImage {
    let width = Int(canvasSize.width)
    let height = Int(canvasSize.height)
    let context = try #require(CGContext(
        data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width * 4,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ))
    context.clear(CGRect(origin: .zero, size: canvasSize))
    context.setFillColor(CGColor(red: 0, green: 0, blue: 0, alpha: 1))
    context.fill(opaqueRect)
    return try #require(context.makeImage())
}

private func testImage() -> CGImage {
    let bytes = Data([0, 0, 0, 255]) as CFData
    let provider = CGDataProvider(data: bytes)!
    return CGImage(
        width: 1,
        height: 1,
        bitsPerComponent: 8,
        bitsPerPixel: 32,
        bytesPerRow: 4,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue),
        provider: provider,
        decode: nil,
        shouldInterpolate: false,
        intent: .defaultIntent
    )!
}

private final class DetailAXProvider: AXTextDetailAttributeProvider {
    let roleResult: AXTextDetailStringResult
    let subroleResult: AXTextDetailStringResult
    let label: String?
    let title: String?
    let help: String?
    let value: String?
    let elementBounds: CGRect
    let childProviders: [DetailAXProvider]
    let childrenStatus: AXTextDetailChildrenStatus?
    let hardStringFailures: Set<String>
    let hardBoolFailures: Set<String>
    let boundsStatus: AXTextDetailAttributeStatus
    let onStringRead: ((String) -> Void)?
    let onBoundsRead: (() -> Void)?
    let onChildrenRead: (() -> Void)?
    private(set) var stringReads: [String] = []
    private(set) var boolReads: [String] = []
    private(set) var boundsReadCount = 0
    private(set) var childrenReadCount = 0

    init(
        role: String,
        subrole: String,
        label: String? = nil,
        title: String? = nil,
        help: String? = nil,
        value: String? = nil,
        bounds: CGRect = CGRect(x: 0, y: 0, width: 10, height: 10),
        children: [DetailAXProvider] = [],
        childrenStatus: AXTextDetailChildrenStatus? = nil,
        hardStringFailures: Set<String> = [],
        hardBoolFailures: Set<String> = [],
        boundsStatus: AXTextDetailAttributeStatus = .complete,
        onStringRead: ((String) -> Void)? = nil,
        onBoundsRead: (() -> Void)? = nil,
        onChildrenRead: (() -> Void)? = nil
    ) {
        self.roleResult = .complete(role)
        self.subroleResult = .complete(subrole)
        self.label = label
        self.title = title
        self.help = help
        self.value = value
        self.elementBounds = bounds
        self.childProviders = children
        self.childrenStatus = childrenStatus
        self.hardStringFailures = hardStringFailures
        self.hardBoolFailures = hardBoolFailures
        self.boundsStatus = boundsStatus
        self.onStringRead = onStringRead
        self.onBoundsRead = onBoundsRead
        self.onChildrenRead = onChildrenRead
    }

    init(
        roleResult: AXTextDetailStringResult,
        subroleResult: AXTextDetailStringResult,
        bounds: CGRect = CGRect(x: 0, y: 0, width: 10, height: 10)
    ) {
        self.roleResult = roleResult
        self.subroleResult = subroleResult
        self.label = nil
        self.title = nil
        self.help = nil
        self.value = "secret"
        self.elementBounds = bounds
        self.childProviders = []
        self.childrenStatus = nil
        self.hardStringFailures = []
        self.hardBoolFailures = []
        self.boundsStatus = .complete
        self.onStringRead = nil
        self.onBoundsRead = nil
        self.onChildrenRead = nil
    }

    func stringValue(for attribute: String, maximumBytes _: Int) -> AXTextDetailStringResult {
        stringReads.append(attribute)
        onStringRead?(attribute)
        if hardStringFailures.contains(attribute) {
            return AXTextDetailStringResult(value: nil, status: .hardFailure)
        }
        switch attribute {
        case kAXRoleAttribute: return roleResult
        case kAXSubroleAttribute: return subroleResult
        case kAXDescriptionAttribute: return .valueOrMissing(label)
        case kAXTitleAttribute: return .valueOrMissing(title)
        case kAXHelpAttribute: return .valueOrMissing(help)
        case kAXValueAttribute: return .valueOrMissing(value)
        case kAXSelectedTextAttribute: return .valueOrMissing(value.map { "selected:\($0)" })
        case kAXSelectedTextRangeAttribute: return .valueOrMissing(value.map { _ in "0:6" })
        case kAXVisibleCharacterRangeAttribute: return .valueOrMissing(value.map { _ in "0:6" })
        case "AXAttributedStringForRange": return .valueOrMissing(value.map { "attributed:\($0)" })
        default: return .missing
        }
    }

    func boolValue(for attribute: String) -> AXTextDetailBoolResult {
        boolReads.append(attribute)
        if hardBoolFailures.contains(attribute) {
            return AXTextDetailBoolResult(value: nil, status: .hardFailure)
        }
        return AXTextDetailBoolResult(value: nil, status: .missing)
    }

    func bounds() -> AXTextDetailBoundsResult {
        boundsReadCount += 1
        onBoundsRead?()
        return AXTextDetailBoundsResult(
            value: boundsStatus == .complete ? elementBounds : nil,
            status: boundsStatus
        )
    }

    func children(maximumCount: Int) -> AXTextDetailChildrenResult {
        childrenReadCount += 1
        onChildrenRead?()
        if childrenStatus == .failed {
            return AXTextDetailChildrenResult(values: [], status: .failed)
        }
        let limit = max(0, maximumCount)
        let values: [any AXTextDetailAttributeProvider] = Array(childProviders.prefix(limit))
        let status = childrenStatus ?? (childProviders.count > limit ? .truncated : .complete)
        return AXTextDetailChildrenResult(values: values, status: status)
    }
}

private extension AXTextDetailStringResult {
    static func complete(_ value: String) -> AXTextDetailStringResult {
        AXTextDetailStringResult(value: value, status: .complete)
    }

    static func truncated(_ value: String) -> AXTextDetailStringResult {
        AXTextDetailStringResult(value: value, status: .truncated)
    }

    static func valueOrMissing(_ value: String?) -> AXTextDetailStringResult {
        value.map(complete) ?? missing
    }

    static var missing: AXTextDetailStringResult {
        AXTextDetailStringResult(value: nil, status: .missing)
    }

    static var unreadable: AXTextDetailStringResult {
        AXTextDetailStringResult(value: nil, status: .unreadable)
    }
}

private final class MutableDetailClock {
    var value: TimeInterval = 0
}

private final class DetailClock {
    private let values: [TimeInterval]
    private var index = 0

    init(values: [TimeInterval]) {
        self.values = values
    }

    func now() -> TimeInterval {
        defer { index += 1 }
        return values[min(index, values.count - 1)]
    }
}

private func detailProviderChain(length: Int) -> DetailAXProvider {
    precondition(length > 0)
    var node = DetailAXProvider(role: "AXGroup", subrole: "AXUnknown")
    for _ in 1..<length {
        node = DetailAXProvider(role: "AXGroup", subrole: "AXUnknown", children: [node])
    }
    return node
}

private func textDetailJSONObject(_ data: Data) throws -> [String: Any] {
    try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
}

private func textDetailStats(_ data: Data) throws -> [String: Any] {
    let json = try textDetailJSONObject(data)
    return try #require(json["stats"] as? [String: Any])
}

private func textDetailAggregateTextBytes(_ node: [String: Any]) -> Int {
    let textKeys = ["role", "subrole", "label", "title", "help", "value"]
    let ownBytes = textKeys.reduce(into: 0) { total, key in
        total += (node[key] as? String)?.utf8.count ?? 0
    }
    return ownBytes + (node["children"] as? [[String: Any]] ?? []).reduce(into: 0) { total, child in
        total += textDetailAggregateTextBytes(child)
    }
}

private func detailCFString(_ units: [UniChar]) -> CFString {
    units.withUnsafeBufferPointer { buffer in
        CFStringCreateWithCharacters(kCFAllocatorDefault, buffer.baseAddress, buffer.count)!
    }
}

private func containsJSONKey(_ value: Any, key: String) -> Bool {
    if let object = value as? [String: Any] {
        return object.keys.contains(key) || object.values.contains { containsJSONKey($0, key: key) }
    }
    if let array = value as? [Any] {
        return array.contains { containsJSONKey($0, key: key) }
    }
    return false
}

private func preorderNodeIDs(_ value: Any) -> [String] {
    guard let object = value as? [String: Any] else { return [] }
    var result = (object["node_id"] as? String).map { [$0] } ?? []
    for child in object["children"] as? [[String: Any]] ?? [] {
        result.append(contentsOf: preorderNodeIDs(child))
    }
    return result
}

@Test func nodeBudgetReservesQuotaForEachTopLevelBranch() {
    func flatBranch(_ role: String, size: Int, markedLeafLabel: String? = nil, markedLeafIndex: Int? = nil) -> AXNode {
        precondition(size >= 1)
        var children: [AXNode] = []
        for index in 0..<(size - 1) {
            let label = (markedLeafIndex == index) ? markedLeafLabel : nil
            children.append(AXNode(role: "AXStaticText", label: label, bounds: .zero))
        }
        return AXNode(role: role, bounds: .zero, children: children)
    }

    func countBranchNodes(_ node: SerializedAXNode) -> Int {
        1 + node.children.reduce(0) { $0 + countBranchNodes($1) }
    }

    // Same shape as the live WPS window that lost its toolbar children:
    // a huge menu bar, a toolbar whose paging controls sit past the old
    // budget cut, and a small content branch.
    let menu = flatBranch("AXMenuBar", size: 800)
    let toolbar = flatBranch("AXToolbar", size: 400, markedLeafLabel: "next page", markedLeafIndex: 248)
    let content = flatBranch("AXGroup", size: 30)
    let root = AXNode(role: "AXWindow", bounds: .zero, children: [menu, toolbar, content])

    let tree = AXSerializer.serialize(root)

    #expect(tree.nodeCount <= maximumSerializedAXNodes)
    let branches = tree.root.children
    #expect(branches.count == 3)

    let serializedSizes = branches.map(countBranchNodes)
    #expect(serializedSizes[2] == 30, "small content branch must survive")
    #expect(serializedSizes[1] >= 250, "toolbar branch must keep its paging controls reachable")
    #expect(serializedSizes[0] < 800, "oversized menu branch is still trimmed to fit the budget")

    func containsLabel(_ node: SerializedAXNode, _ label: String) -> Bool {
        node.label == label || node.children.contains { containsLabel($0, label) }
    }
    #expect(containsLabel(tree.root, "next page"), "paging control must be addressable in the serialized tree")
}

@Test func takeoverPlanGuardAcquiresKeyboardFocusAuthorityByLiveRead() {
    // 回归（实机 2026-09-03 04:17）：接管态计划曾把**非接管态观察**里的 keyboardFocus 原样抄进
    // modeGuard。而 captureSnapshot 只在 target.interactionMode == .foregroundTakeover 时才填这个
    // 字段（见 Windows.swift 快照路径）⇒ 抄来的必是 nil ⇒ 计划闸以
    // "foregroundKeyboard requires keyboardFocus" 拒、执行期又会 stale_snapshot，整条链自相锁死：
    // 网页输入框永远打不进字。修法是接管态**当场活读**权威。
    let authority = KeyboardFocusAuthority(
        identityToken: "ax:4242",
        bounds: CGRect(x: 500, y: 780, width: 700, height: 40),
        role: "AXTextArea",
        subrole: nil
    )
    var reads = 0
    #expect(planKeyboardFocusAuthority(
        source: nil,
        interactionMode: .foregroundTakeover,
        liveFocus: {
            reads += 1
            return .authority(authority)
        }
    ) == authority)
    #expect(reads == 1, "the takeover plan must take one fresh focus authority")

    // 后台态一律沿用来源 guard，且**绝不**为了让自己通过而顺手去读焦点（无副作用）。
    #expect(planKeyboardFocusAuthority(
        source: nil,
        interactionMode: .background,
        liveFocus: {
            reads += 1
            return .authority(authority)
        }
    ) == nil)
    #expect(reads == 1, "background planning must not read keyboard focus")

    // 活读回报 secure/stale 时权威仍必须是 nil：宁可诚实拒绝输入，不伪造一份权威骗过闸。
    #expect(planKeyboardFocusAuthority(
        source: authority,
        interactionMode: .foregroundTakeover,
        liveFocus: { .secureOrIndeterminate }
    ) == nil)
    #expect(planKeyboardFocusAuthority(
        source: authority,
        interactionMode: .foregroundTakeover,
        liveFocus: { .stale }
    ) == nil)
}

@Test func deliveryGuardAcquiresKeyboardFocusAuthorityOnlyWhenNeeded() {
    // 接管已经激活了 app，这才是读得到焦点权威的时机。执行期若继续用计划期继承来的 nil，
    // ForegroundKeyboardInput.validateEnvironment 会在投递前直接 stale_snapshot ——
    // 实机表现：focused:true 明明拿到了，type 永远进不去。
    let focusBase = ActionGuard(
        pid: 11,
        windowID: 22,
        bounds: CGRect(x: 0, y: 0, width: 1400, height: 900),
        axIdentity: 33,
        focusedAXBounds: CGRect(x: 40, y: 40, width: 1300, height: 820),
        keyboardFocus: nil,
        snapshotID: "snapshot",
        interactionMode: .foregroundTakeover
    )
    let authority = KeyboardFocusAuthority(
        identityToken: "ax:4242",
        bounds: CGRect(x: 500, y: 780, width: 700, height: 40),
        role: "AXTextArea",
        subrole: nil
    )
    var reads = 0
    let liveFocus: () -> KeyboardFocusObservation = {
        reads += 1
        return .authority(authority)
    }
    // 已经有权威就不再读第二次（不白碰 AX），后台态一律不读（与计划期同一纪律）。
    let acquired = deliveryKeyboardFocusGuard(base: focusBase, liveFocus: liveFocus)
    #expect(acquired.keyboardFocus == authority)
    #expect(reads == 1, "delivery must take exactly one fresh focus authority")
    #expect(deliveryKeyboardFocusGuard(base: acquired, liveFocus: liveFocus).keyboardFocus == authority)
    #expect(reads == 1, "an existing authority must not trigger another read")
    let background = ActionGuard(
        pid: 11,
        windowID: 22,
        bounds: CGRect(x: 0, y: 0, width: 1400, height: 900),
        axIdentity: 33,
        keyboardFocus: nil,
        snapshotID: "snapshot",
        interactionMode: .background
    )
    #expect(deliveryKeyboardFocusGuard(base: background, liveFocus: liveFocus).keyboardFocus == nil)
    #expect(reads == 1, "background delivery must not read keyboard focus to pass itself")

    // 活读回报 secure/stale ⇒ 权威保持 nil，让投递前复验诚实拒绝。
    #expect(deliveryKeyboardFocusGuard(base: focusBase, liveFocus: { .stale }).keyboardFocus == nil)
    #expect(deliveryKeyboardFocusGuard(base: focusBase, liveFocus: { .secureOrIndeterminate }).keyboardFocus == nil)
}

@Test func numericAXValuesRemainBoundedAndVisibleToObservation() {
    #expect(boundedAXValueResult(NSNumber(value: 0)).value == "0.0")
    #expect(boundedAXValueResult(NSNumber(value: 0.5)).value == "0.5")
    #expect(boundedAXValueResult(NSNumber(value: 1)).value == "1.0")
    #expect(boundedAXValueResult("name" as CFString).value == "name")
    #expect(boundedAXStringResult(NSNumber(value: 1)).status == .failed)
}

@Test func numericAXValuesRejectNonfiniteAndOtherObjectTypes() {
    for value: CFTypeRef in [NSNumber(value: Double.nan), NSNumber(value: Double.infinity),
                             ["secret": "value"] as CFDictionary] {
        #expect(boundedAXValueResult(value).status == .failed)
        #expect(boundedAXValueResult(value).value == nil)
    }
}
