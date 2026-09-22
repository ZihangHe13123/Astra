@testable import AstraMacComputerHelperCore
import Foundation
import Testing

@Test func wrongVersionReturnsProtocolMismatch() {
    let response = Dispatcher().handle(
        #"{"protocol_version":1,"request_id":"r1","operation":"status","payload":{}}"#
    )

    #expect(response.error?.code == "protocol_mismatch")
}

@Test func pingReturnsCurrentProtocolVersion() {
    let response = Dispatcher().handle(
        #"{"protocol_version":4,"request_id":"ping-1","operation":"ping","payload":{}}"#
    )
    let incompatibleV3 = Dispatcher().handle(
        #"{"protocol_version":3,"request_id":"old-v3","operation":"ping","payload":{}}"#
    )

    #expect(response.ok)
    #expect(response.protocolVersion == 4)
    #expect(boolValue(response.result, keys: ["pong"]) == true)
    #expect(incompatibleV3.error?.code == "protocol_mismatch")
}

@Test func getAppStateAcceptsOnlyExactCatalogBoundRequestVariantsAndResponseShape() {
    let offPayload: JSONValue = .object([
        "app_ref": .string("app_a"),
        "window_ref": .string("win_a"),
        "catalog_generation": .number(7),
        "scope": .string("target_window"),
        "artifact_name": .string("snapshot-token.png"),
    ])
    let onPayload: JSONValue = .object([
        "app_ref": .string("app_a"),
        "window_ref": .string("win_a"),
        "catalog_generation": .number(7),
        "scope": .string("target_window"),
        "artifact_name": .string("snapshot-token.png"),
        "text_detail": .string("on"),
        "text_detail_artifact_name": .string("snapshot-token.ax.json"),
    ])
    let displayPayload: JSONValue = .object([
        "app_ref": .string("app_a"),
        "window_ref": .string("win_a"),
        "catalog_generation": .number(7),
        "scope": .string("display"),
        "artifact_name": .string("snapshot-token.png"),
    ])
    let off = getAppStateRequest(HelperRequest(
        protocolVersion: 4, requestID: "get-off", operation: "get_app_state",
        payload: offPayload,
        payloadData: Data(#"{"app_ref":"app_a","window_ref":"win_a","catalog_generation":7,"scope":"target_window","artifact_name":"snapshot-token.png"}"#.utf8)
    ))
    let on = getAppStateRequest(HelperRequest(
        protocolVersion: 4, requestID: "get-on", operation: "get_app_state",
        payload: onPayload,
        payloadData: Data(#"{"app_ref":"app_a","window_ref":"win_a","catalog_generation":7,"scope":"target_window","artifact_name":"snapshot-token.png","text_detail":"on","text_detail_artifact_name":"snapshot-token.ax.json"}"#.utf8)
    ))
    let display = getAppStateRequest(HelperRequest(
        protocolVersion: 4, requestID: "get-display", operation: "get_app_state",
        payload: displayPayload,
        payloadData: Data(#"{"app_ref":"app_a","window_ref":"win_a","catalog_generation":7,"scope":"display","artifact_name":"snapshot-token.png"}"#.utf8)
    ))
    let result: JSONValue = .object([
        "app_ref": .string("app_a"),
        "window_ref": .string("win_a"),
        "catalog_generation": .number(7),
        "interaction_mode": .string("background"),
    ])
    let responseRequest = GetAppStateRequest(
        appRef: "app_a",
        windowRef: "win_a",
        catalogGeneration: 7,
        scope: "target_window",
        artifactName: "snapshot-0123456789abcdef0123456789abcdef.png",
        textDetail: .on(
            artifactName: "snapshot-0123456789abcdef0123456789abcdef.ax.json"
        )
    )
    let snapshot = snapshotProtocolValue(
        detailName: "snapshot-0123456789abcdef0123456789abcdef.ax.json"
    )

    #expect(off?.catalogGeneration == 7)
    #expect(off?.textDetail == .off)
    #expect(on?.textDetail == .on(artifactName: "snapshot-token.ax.json"))
    #expect(display?.scope == "display")
    #expect(validGetAppStateResponse(result: result, snapshot: snapshot, request: responseRequest))
}

@Test func productionSnapshotDefaultButtonMetadataIsAcceptedAcrossSnapshotAndGetAppStateValidation() {
    let imageName = "snapshot-0123456789abcdef0123456789abcdef.png"
    let defaultReference = "snapshot_detail:default"
    let productionSnapshot = snapshotProtocolValue(payloadExtra: [
        "ax_tree": .object([
            "role": .string("AXWindow"),
            "element_ref": .string("snapshot_detail:window"),
            "children": .array([
                .object([
                    "role": .string("AXButton"),
                    "element_ref": .string(defaultReference),
                ]),
            ]),
        ]),
        "has_default_button": .bool(true),
        "default_button_element_ref": .string(defaultReference),
    ])
    let windows = CooperativeProtocolWindows()
    windows.snapshotResult = productionSnapshot
    let dispatcher = Dispatcher(windows: windows)
    let direct = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"production-snapshot","operation":"snapshot","payload":{"app_ref":"app","window_ref":"window","scope":"target_window","artifact_name":"snapshot-0123456789abcdef0123456789abcdef.png"}}"#
    )
    let stateRequest = GetAppStateRequest(
        appRef: "app", windowRef: "window", catalogGeneration: 7,
        scope: "target_window", artifactName: imageName, textDetail: .off
    )
    let stateResult: JSONValue = .object([
        "app_ref": .string("app"),
        "window_ref": .string("window"),
        "catalog_generation": .number(7),
        "interaction_mode": .string("background"),
    ])

    #expect(direct.ok)
    #expect(validGetAppStateResponse(
        result: stateResult,
        snapshot: productionSnapshot,
        request: stateRequest
    ))
}

@Test func snapshotDefaultButtonMetadataRequiresExactPresenceAndAXChildrenRegistration() {
    let defaultReference = "snapshot_detail:default"
    let registeredTree: JSONValue = .object([
        "role": .string("AXWindow"),
        "children": .array([
            .object([
                "role": .string("AXButton"),
                "element_ref": .string(defaultReference),
            ]),
        ]),
    ])
    let invalidPayloads: [[String: JSONValue]] = [
        ["has_default_button": .string("yes")],
        ["default_button_element_ref": .string(defaultReference)],
        [
            "has_default_button": .bool(false),
            "default_button_element_ref": .string(defaultReference),
        ],
        [
            "has_default_button": .bool(true),
            "default_button_element_ref": .string("snapshot_detail:missing"),
            "ax_tree": registeredTree,
        ],
        [
            "has_default_button": .bool(true),
            "default_button_element_ref": .string(defaultReference),
            "ax_tree": .object([
                "role": .string("AXWindow"),
                "metadata": .object(["element_ref": .string(defaultReference)]),
            ]),
        ],
    ]

    for payload in invalidPayloads {
        #expect(!validSnapshotResponseInvariants(
            snapshotProtocolValue(payloadExtra: payload),
            textDetail: .off
        ))
    }
}

@Test func snapshotInvariantsAcceptTheVirtualCursorPointer() {
    // 虚拟游标只在 foreground takeover 期间才有位置 ⇒ 快照 payload 会多出 virtual_pointer。
    // 两端自检（这里 + Python validate_snapshot_payload）都漏登记该键，实机表现为
    // **"每次接管之后的捕获必坏"** —— helper 把自己刚生成的快照判为非法，真因还被
    // protocol_mismatch 文案吃掉。放行时必须连带校形状，不能只加白名单。
    #expect(validSnapshotResponseInvariants(
        snapshotProtocolValue(payloadExtra: [
            "virtual_pointer": .object(["x": .number(640), "y": .number(390)]),
        ]),
        textDetail: .off
    ))
    for malformed in [
        JSONValue.object(["x": .number(1)]),
        JSONValue.object(["x": .number(1), "y": .string("2")]),
        JSONValue.object(["x": .number(1), "y": .number(2), "z": .number(3)]),
        JSONValue.bool(true),
    ] {
        #expect(!validSnapshotResponseInvariants(
            snapshotProtocolValue(payloadExtra: ["virtual_pointer": malformed]),
            textDetail: .off
        ))
    }
}

@Test func getAppStateResponseRequiresRequestedArtifactAndExactDetailPairing() {
    let imageName = "snapshot-0123456789abcdef0123456789abcdef.png"
    let detailName = "snapshot-0123456789abcdef0123456789abcdef.ax.json"
    let result: JSONValue = .object([
        "app_ref": .string("app_a"),
        "window_ref": .string("win_a"),
        "catalog_generation": .number(7),
        "interaction_mode": .string("background"),
    ])
    let off = GetAppStateRequest(
        appRef: "app_a", windowRef: "win_a", catalogGeneration: 7,
        scope: "target_window", artifactName: imageName, textDetail: .off
    )
    let on = GetAppStateRequest(
        appRef: "app_a", windowRef: "win_a", catalogGeneration: 7,
        scope: "target_window", artifactName: imageName,
        textDetail: .on(artifactName: detailName)
    )

    #expect(validGetAppStateResponse(
        result: result,
        snapshot: snapshotProtocolValue(imageName: imageName),
        request: off
    ))
    #expect(validGetAppStateResponse(
        result: result,
        snapshot: snapshotProtocolValue(imageName: imageName, detailName: detailName),
        request: on
    ))
    #expect(!validGetAppStateResponse(
        result: result,
        snapshot: snapshotProtocolValue(imageName: "snapshot-ffffffffffffffffffffffffffffffff.png"),
        request: off
    ))
    #expect(!validGetAppStateResponse(
        result: result,
        snapshot: snapshotProtocolValue(imageName: imageName, detailName: detailName),
        request: off
    ))
    #expect(!validGetAppStateResponse(
        result: result,
        snapshot: snapshotProtocolValue(imageName: imageName),
        request: on
    ))
    #expect(!validGetAppStateResponse(
        result: result,
        snapshot: snapshotProtocolValue(
            imageName: imageName,
            detailName: "snapshot-ffffffffffffffffffffffffffffffff.ax.json"
        ),
        request: on
    ))
    #expect(!validGetAppStateResponse(
        result: result,
        snapshot: snapshotProtocolValue(
            imageName: imageName,
            detailName: detailName,
            omitMetadata: true
        ),
        request: on
    ))
    #expect(!validGetAppStateResponse(
        result: result,
        snapshot: snapshotProtocolValue(
            imageName: imageName,
            detailName: detailName,
            metadataExtra: ["snapshot_id": .string("wrong")]
        ),
        request: on
    ))
}

@Test func getAppStateResponseBindsDisplayMetadataToRequestedScope() {
    let imageName = "snapshot-0123456789abcdef0123456789abcdef.png"
    let result: JSONValue = .object([
        "app_ref": .string("app_a"),
        "window_ref": .string("win_a"),
        "catalog_generation": .number(7),
        "interaction_mode": .string("background"),
    ])
    let targetWindow = GetAppStateRequest(
        appRef: "app_a", windowRef: "win_a", catalogGeneration: 7,
        scope: "target_window", artifactName: imageName, textDetail: .off
    )
    let display = GetAppStateRequest(
        appRef: "app_a", windowRef: "win_a", catalogGeneration: 7,
        scope: "display", artifactName: imageName, textDetail: .off
    )
    let displayMetadata: [String: JSONValue] = [
        "display_id": .number(7),
        "target_window_bounds": .object([
            "x": .number(0), "y": .number(0),
            "width": .number(1), "height": .number(1),
        ]),
    ]

    #expect(validGetAppStateResponse(
        result: result,
        snapshot: snapshotProtocolValue(imageName: imageName),
        request: targetWindow
    ))
    #expect(!validGetAppStateResponse(
        result: result,
        snapshot: snapshotProtocolValue(imageName: imageName, payloadExtra: displayMetadata),
        request: targetWindow
    ))
    #expect(!validGetAppStateResponse(
        result: result,
        snapshot: snapshotProtocolValue(imageName: imageName),
        request: display
    ))
    #expect(validGetAppStateResponse(
        result: result,
        snapshot: snapshotProtocolValue(imageName: imageName, payloadExtra: displayMetadata),
        request: display
    ))
}

@Test func getAppStateRejectsInvalidCatalogGenerationAndPayloadShapes() {
    let invalidPayloads = [
        #"{"window_ref":"win_a","catalog_generation":7,"scope":"target_window","artifact_name":"snapshot-token.png"}"#,
        #"{"app_ref":"app_a","catalog_generation":7,"scope":"target_window","artifact_name":"snapshot-token.png"}"#,
        #"{"app_ref":"app_a","window_ref":"win_a","scope":"target_window","artifact_name":"snapshot-token.png"}"#,
        #"{"app_ref":"app_a","window_ref":"win_a","catalog_generation":7,"artifact_name":"snapshot-token.png"}"#,
        #"{"app_ref":"app_a","window_ref":"win_a","catalog_generation":7,"scope":"window","artifact_name":"snapshot-token.png"}"#,
        #"{"app_ref":"app_a","window_ref":"win_a","catalog_generation":7,"scope":"target_window"}"#,
        #"{"app_ref":"app_a","window_ref":"win_a","catalog_generation":0,"scope":"target_window","artifact_name":"snapshot-token.png"}"#,
        #"{"app_ref":"app_a","window_ref":"win_a","catalog_generation":-1,"scope":"target_window","artifact_name":"snapshot-token.png"}"#,
        #"{"app_ref":"app_a","window_ref":"win_a","catalog_generation":7.0,"scope":"target_window","artifact_name":"snapshot-token.png"}"#,
        #"{"app_ref":"app_a","window_ref":"win_a","catalog_generation":true,"scope":"target_window","artifact_name":"snapshot-token.png"}"#,
        #"{"app_ref":"app_a","window_ref":"win_a","catalog_generation":7,"scope":"target_window","artifact_name":"snapshot-token.png","text_detail":"off","text_detail_artifact_name":"snapshot-token.ax.json"}"#,
        #"{"app_ref":"app_a","window_ref":"win_a","catalog_generation":7,"scope":"target_window","artifact_name":"snapshot-token.png","text_detail":"on","text_detail_artifact_name":"snapshot-other.ax.json"}"#,
        #"{"app_ref":"app_a","window_ref":"win_a","catalog_generation":7,"scope":"target_window","artifact_name":"../snapshot-token.png"}"#,
        #"{"app_ref":"app_a","window_ref":"win_a","catalog_generation":7,"scope":"target_window","artifact_name":"snapshot-token.png","text_detail":"on","text_detail_artifact_name":"../snapshot-token.ax.json"}"#,
    ]

    for (index, raw) in invalidPayloads.enumerated() {
        #expect(getAppStateRequest(HelperRequest(
            protocolVersion: 4, requestID: "invalid-\(index)", operation: "get_app_state",
            payload: .object([:]), payloadData: Data(raw.utf8)
        )) == nil)
    }
    let duplicate = Dispatcher().handle(
        #"{"protocol_version":4,"request_id":"duplicate","operation":"get_app_state","payload":{"app_ref":"app_a","app_ref":"other","window_ref":"win_a","catalog_generation":7,"scope":"target_window","artifact_name":"snapshot-token.png"}}"#
    )

    #expect(!duplicate.ok)
    #expect(duplicate.error?.code == "protocol_mismatch")
}

@Test func getAppStateUsesDistinctBoundedNativeErrorCodes() {
    #expect(GetAppStateErrorCode.staleTarget.rawValue == "stale_target")
    #expect(GetAppStateErrorCode.snapshotFailed.rawValue == "snapshot_failed")
}

@Test func smartSnapshotQuotaAndPoisonRemainExistingHelperFailedErrors() {
    for error in [
        WindowObservationError.artifactQuotaExceeded,
        WindowObservationError.artifactPublisherFailed,
    ] {
        let dispatcher = Dispatcher(
            permissions: StaticPermissions(accessibilityTrusted: true, screenRecordingAllowed: true),
            windows: ErrorSnapshotWindows(error: error)
        )
        let response = dispatcher.handle(
            #"{"protocol_version":4,"request_id":"detail-error","operation":"snapshot","payload":{"app_ref":"app","window_ref":"window","scope":"target_window","artifact_name":"snapshot-0123456789abcdef0123456789abcdef.png","text_detail":"on","text_detail_artifact_name":"snapshot-0123456789abcdef0123456789abcdef.ax.json"}}"#
        )

        #expect(!response.ok)
        #expect(response.error?.code == "helper_failed")
        #expect(response.snapshot == nil)
    }
}

@Test func protocolV4AcceptsStrictInitialAndContinuingFragmentFrames() {
    let windows = CooperativeProtocolWindows()
    let dispatcher = Dispatcher(
        permissions: StaticPermissions(accessibilityTrusted: true, screenRecordingAllowed: true),
        windows: windows
    )
    let initial = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"initial","operation":"plan_actions","payload":{"interaction_mode":"foreground_takeover","snapshot_id":"snapshot-0","actions":[{"type":"click","element_ref":"button"}],"fragment_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","stage_index":0,"stage_hash":"0000000000000000000000000000000000000000000000000000000000000000"}}"#
    )
    let continuing = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"continuing","operation":"plan_actions","payload":{"interaction_mode":"foreground_takeover","snapshot_id":"snapshot-1","actions":[{"type":"wait","duration_ms":0}],"takeover_ref":"takeover","fragment_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","stage_index":1,"stage_hash":"1111111111111111111111111111111111111111111111111111111111111111"}}"#
    )

    #expect(initial.ok)
    #expect(continuing.ok)
    #expect(windows.fragmentPlans.count == 2)
}

@Test func protocolV4TakeoverBeginCarriesCompleteBoundedDeclaration() {
    let windows = CooperativeProtocolWindows()
    let dispatcher = Dispatcher(
        permissions: StaticPermissions(accessibilityTrusted: true, screenRecordingAllowed: true),
        windows: windows
    )
    let response = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"begin","operation":"takeover_begin","payload":{"snapshot_id":"snapshot-0","plan_ref":"plan-0","fragment":{"fragment_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","stages":[{"stage_hash":"0000000000000000000000000000000000000000000000000000000000000000","expected_action_count":1,"requirements":[{"backend":"ax_press","action_class":"press","intent":null}]}],"max_actions":1,"wall_clock_limit_ms":1000,"restore_previous_focus":true}}}"#
    )

    #expect(response.ok)
    #expect(windows.takeoverDeclarations.count == 1)
}

@Test func protocolV4RejectsNestedDuplicateKeysAndNonIntegerFragmentTokens() {
    let dispatcher = Dispatcher(
        permissions: StaticPermissions(accessibilityTrusted: true, screenRecordingAllowed: true),
        windows: CooperativeProtocolWindows()
    )
    let duplicate = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"dup","operation":"takeover_begin","payload":{"snapshot_id":"snapshot-0","plan_ref":"plan-0","fragment":{"fragment_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","fragment_hash":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","stages":[],"max_actions":1,"wall_clock_limit_ms":1,"restore_previous_focus":true}}}"#
    )
    let floatIndex = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"float","operation":"plan_actions","payload":{"interaction_mode":"foreground_takeover","snapshot_id":"snapshot-0","actions":[],"fragment_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","stage_index":0.0,"stage_hash":"0000000000000000000000000000000000000000000000000000000000000000"}}"#
    )

    #expect(!duplicate.ok)
    #expect(!floatIndex.ok)
}

@Test func protocolV4BoundsEveryFragmentStageIndexToZeroThroughThirtyOne() {
    let windows = CooperativeProtocolWindows()
    let dispatcher = Dispatcher(
        permissions: StaticPermissions(accessibilityTrusted: true, screenRecordingAllowed: true),
        windows: windows
    )
    let hash = String(repeating: "a", count: 64)
    let stageHash = String(repeating: "1", count: 64)
    let invalidTokens = ["-1", "32", "999999999999999", "2.0", "true"]

    for (offset, token) in invalidTokens.enumerated() {
        let plan = dispatcher.handle(
            #"{"protocol_version":4,"request_id":"plan-\#(offset)","operation":"plan_actions","payload":{"interaction_mode":"foreground_takeover","snapshot_id":"snapshot","actions":[],"takeover_ref":"takeover","fragment_hash":"\#(hash)","stage_index":\#(token),"stage_hash":"\#(stageHash)"}}"#
        )
        let act = dispatcher.handle(
            #"{"protocol_version":4,"request_id":"act-\#(offset)","operation":"act","payload":{"interaction_mode":"foreground_takeover","snapshot_id":"snapshot","plan_ref":"plan","takeover_ref":"takeover","actions":[],"fragment_hash":"\#(hash)","stage_index":\#(token),"stage_hash":"\#(stageHash)"}}"#
        )
        let commit = dispatcher.handle(
            #"{"protocol_version":4,"request_id":"commit-\#(offset)","operation":"fragment_stage_commit","payload":{"takeover_ref":"takeover","fragment_hash":"\#(hash)","stage_index":\#(token),"stage_hash":"\#(stageHash)","plan_ref":"plan","fresh_snapshot_id":"fresh","postcondition_verified":true}}"#
        )

        #expect(!plan.ok)
        #expect(!act.ok)
        #expect(!commit.ok)
    }

    let maximumPlan = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"plan-max","operation":"plan_actions","payload":{"interaction_mode":"foreground_takeover","snapshot_id":"snapshot","actions":[],"takeover_ref":"takeover","fragment_hash":"\#(hash)","stage_index":31,"stage_hash":"\#(stageHash)"}}"#
    )
    let maximumAct = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"act-max","operation":"act","payload":{"interaction_mode":"foreground_takeover","snapshot_id":"snapshot","plan_ref":"plan","takeover_ref":"takeover","actions":[],"fragment_hash":"\#(hash)","stage_index":31,"stage_hash":"\#(stageHash)"}}"#
    )
    let maximumCommit = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"commit-max","operation":"fragment_stage_commit","payload":{"takeover_ref":"takeover","fragment_hash":"\#(hash)","stage_index":31,"stage_hash":"\#(stageHash)","plan_ref":"plan","fresh_snapshot_id":"fresh","postcondition_verified":true}}"#
    )

    #expect(maximumPlan.ok)
    #expect(maximumAct.ok)
    #expect(maximumCommit.ok)
}

@Test func protocolV4KeepsOrdinaryForegroundVariantsAndExactFragmentVariants() {
    let windows = CooperativeProtocolWindows()
    let dispatcher = Dispatcher(
        permissions: StaticPermissions(accessibilityTrusted: true, screenRecordingAllowed: true),
        windows: windows
    )
    let ordinaryPlan = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"ordinary-plan","operation":"plan_actions","payload":{"interaction_mode":"foreground_takeover","snapshot_id":"snapshot","actions":[]}}"#
    )
    let ordinaryBegin = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"ordinary-begin","operation":"takeover_begin","payload":{"snapshot_id":"snapshot","plan_ref":"plan"}}"#
    )
    let ordinaryAct = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"missing","operation":"act","payload":{"interaction_mode":"foreground_takeover","snapshot_id":"snapshot","plan_ref":"plan","takeover_ref":"takeover","actions":[]}}"#
    )
    let background = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"background","operation":"act","payload":{"interaction_mode":"background","snapshot_id":"snapshot","plan_ref":"plan","actions":[]}}"#
    )

    #expect(ordinaryPlan.ok)
    #expect(ordinaryBegin.ok)
    #expect(ordinaryAct.ok)
    #expect(background.ok)
    #expect(windows.ordinaryTakeoverBegins == 1)
}

@Test func protocolV4RoutesExactFragmentStageCommit() {
    let windows = CooperativeProtocolWindows()
    let dispatcher = Dispatcher(
        permissions: StaticPermissions(accessibilityTrusted: true, screenRecordingAllowed: true),
        windows: windows
    )
    let response = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"commit","operation":"fragment_stage_commit","payload":{"takeover_ref":"takeover","fragment_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","stage_index":0,"stage_hash":"0000000000000000000000000000000000000000000000000000000000000000","plan_ref":"plan","fresh_snapshot_id":"fresh","postcondition_verified":true}}"#
    )

    #expect(response.ok)
    #expect(windows.fragmentCommits.count == 1)
    #expect(stringValue(response.result, keys: ["restoration"]) == "restored")
}

@Test func statusReportsEachPermissionWithoutRequiringEitherOne() {
    let response = Dispatcher(permissions: StaticPermissions(accessibilityTrusted: true, screenRecordingAllowed: false)).handle(
        #"{"protocol_version":4,"request_id":"status-1","operation":"status","payload":{}}"#
    )

    #expect(response.ok)
    #expect(boolValue(response.result, keys: ["permissions", "accessibility"]) == true)
    #expect(boolValue(response.result, keys: ["permissions", "screen_recording"]) == false)
}

@Test func closeAcknowledgesTheRequest() {
    let response = Dispatcher().handle(
        #"{"protocol_version":4,"request_id":"close-1","operation":"close","payload":{}}"#
    )

    #expect(response.ok)
    #expect(boolValue(response.result, keys: ["closed"]) == true)
}

@Test func encodedResponseIsACompactSingleJSONLine() throws {
    let encoded = try encodeResponse(
        HelperResponse(
            protocolVersion: 4,
            requestID: "line-1",
            ok: true,
            result: .object(["pong": .bool(true)]),
            error: nil
        )
    )

    #expect(!encoded.contains("\n"))
    #expect(!encoded.contains("\r"))
    #expect(encoded.contains(#""protocol_version":4"#))
}

@Test func oversizedRequestIsRejectedBeforeDispatch() {
    let prefix = #"{"protocol_version":4,"request_id":"large-1","operation":"ping","payload":{"padding":""#
    let suffix = #""}}"#
    let request = prefix + String(repeating: "x", count: maxRequestLineBytes - prefix.utf8.count - suffix.utf8.count + 1) + suffix

    let response = Dispatcher().handle(request)

    #expect(!response.ok)
    #expect(response.error?.message == "request exceeds 4 MiB")
}

@Test func pointerCheckpointMessageDoesNotInferKeyboardFocusAndKeepsWireBoundary() {
    for hasSuffix in [false, true] {
        let windows = CooperativeProtocolWindows()
        windows.cooperativeResultOverride = CooperativeActionResult(
            batch: ActionBatchResult(outcomes: [ActionOutcome(index: 0, ok: true, error: nil, observationRequired: true)],
                lastAcknowledgedAction: 0, error: nil),
            error: hasSuffix ? .cooperative(.observationRequired) : nil)
        let dispatcher = Dispatcher(permissions: StaticPermissions(accessibilityTrusted: true, screenRecordingAllowed: true), windows: windows)
        let suffix = hasSuffix ? #",{"type":"wait","duration_ms":0}"# : ""
        let response = dispatcher.handle(
            #"{"protocol_version":4,"request_id":"checkpoint","operation":"act","payload":{"interaction_mode":"foreground_takeover","snapshot_id":"snapshot","plan_ref":"plan_test","takeover_ref":"takeover","actions":[{"type":"right_click","x":10,"y":10}\#(suffix)]}}"#
        )
        #expect(response.ok == !hasSuffix)
        #expect(response.result == .object([
            "outcomes": .array([.object(["index": .number(0), "ok": .bool(true), "observation_required": .bool(true)])]),
            "last_acknowledged_action": .number(0),
        ]))
        if hasSuffix {
            #expect(response.error?.code == "observation_required")
            let message = response.error?.message.lowercased() ?? ""
            #expect(!message.contains("keyboard focus"))
            #expect(message.contains("acknowledged"))
            #expect(message.contains("remaining actions were not sent"))
            #expect(message.contains("do not replay"))
        } else {
            #expect(response.error == nil)
        }
    }
}

@Test func cooperativePlanAndBackgroundActRouteThroughTheWindowObserver() {
    let windows = CooperativeProtocolWindows()
    let dispatcher = Dispatcher(permissions: StaticPermissions(accessibilityTrusted: true, screenRecordingAllowed: true), windows: windows)
    let plan = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"plan-1","operation":"plan_actions","payload":{"interaction_mode":"background","snapshot_id":"snapshot","actions":[{"type":"wait","duration_ms":0}]}}"#
    )

    #expect(plan.ok)
    #expect(stringValue(plan.result, keys: ["plan_ref"]) == "plan_test")
    #expect(windows.planCalls == 1)

    let act = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"act-1","operation":"act","payload":{"interaction_mode":"background","snapshot_id":"snapshot","plan_ref":"plan_test","actions":[{"type":"wait","duration_ms":0}]}}"#
    )
    #expect(act.ok)
    #expect(numberValue(act.result, keys: ["last_acknowledged_action"]) == 0)
    #expect(windows.backgroundActCalls == 1)

    let ordinaryForeground = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"act-2","operation":"act","payload":{"interaction_mode":"foreground_takeover","snapshot_id":"snapshot","plan_ref":"plan_test","takeover_ref":"takeover","actions":[]}}"#
    )
    #expect(ordinaryForeground.ok)
    #expect(windows.backgroundActCalls == 2)

    let malformedFragment = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"act-3","operation":"act","payload":{"interaction_mode":"foreground_takeover","snapshot_id":"snapshot","plan_ref":"plan_test","takeover_ref":"takeover","actions":[],"fragment_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}"#
    )
    #expect(!malformedFragment.ok)
    #expect(windows.backgroundActCalls == 2)
}

@Test func snapshotDefaultOffKeepsExactOldRequestAndResponseShapes() {
    let windows = CooperativeProtocolWindows()
    windows.snapshotResult = snapshotProtocolValue()
    let dispatcher = Dispatcher(windows: windows)
    let old = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"off","operation":"snapshot","payload":{"app_ref":"app","window_ref":"window","scope":"target_window","artifact_name":"snapshot-0123456789abcdef0123456789abcdef.png"}}"#
    )
    let explicitOff = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"explicit-off","operation":"snapshot","payload":{"app_ref":"app","window_ref":"window","scope":"target_window","artifact_name":"snapshot-0123456789abcdef0123456789abcdef.png","text_detail":"off"}}"#
    )

    #expect(old.ok)
    #expect(windows.snapshotDetails == [.off])
    #expect(stringValue(old.snapshot, keys: ["payload", "image_artifact"])?.hasSuffix(".png") == true)
    #expect(stringValue(old.snapshot, keys: ["payload", "text_detail_artifact"]) == nil)
    #expect(!explicitOff.ok)
}

@Test func snapshotTextDetailOnRequiresExactFilenameAndPairedMetadata() {
    let detailName = "snapshot-0123456789abcdef0123456789abcdef.ax.json"
    let windows = CooperativeProtocolWindows()
    windows.snapshotResult = snapshotProtocolValue(detailName: detailName)
    let dispatcher = Dispatcher(windows: windows)
    let accepted = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"on","operation":"snapshot","payload":{"app_ref":"app","window_ref":"window","scope":"target_window","artifact_name":"snapshot-0123456789abcdef0123456789abcdef.png","text_detail":"on","text_detail_artifact_name":"snapshot-0123456789abcdef0123456789abcdef.ax.json"}}"#
    )
    let badName = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"bad-name","operation":"snapshot","payload":{"app_ref":"app","window_ref":"window","scope":"target_window","artifact_name":"snapshot-0123456789abcdef0123456789abcdef.png","text_detail":"on","text_detail_artifact_name":"../snapshot-0123456789abcdef0123456789abcdef.ax.json"}}"#
    )
    windows.snapshotResult = snapshotProtocolValue(detailName: detailName, omitMetadata: true)
    let partial = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"partial","operation":"snapshot","payload":{"app_ref":"app","window_ref":"window","scope":"target_window","artifact_name":"snapshot-0123456789abcdef0123456789abcdef.png","text_detail":"on","text_detail_artifact_name":"snapshot-0123456789abcdef0123456789abcdef.ax.json"}}"#
    )

    #expect(accepted.ok)
    #expect(windows.snapshotDetails.first == .on(artifactName: detailName))
    #expect(stringValue(accepted.snapshot, keys: ["payload", "text_detail_artifact"]) == detailName)
    #expect(!badName.ok)
    #expect(!partial.ok)
}

@Test func snapshotTextDetailRejectsUnknownDuplicateAndOutOfBoundsMetadata() {
    let detailName = "snapshot-0123456789abcdef0123456789abcdef.ax.json"
    let windows = CooperativeProtocolWindows()
    let dispatcher = Dispatcher(windows: windows)
    windows.snapshotResult = snapshotProtocolValue(detailName: detailName, metadataExtra: ["unexpected": .bool(true)])
    let unknown = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"unknown","operation":"snapshot","payload":{"app_ref":"app","window_ref":"window","scope":"target_window","artifact_name":"snapshot-0123456789abcdef0123456789abcdef.png","text_detail":"on","text_detail_artifact_name":"snapshot-0123456789abcdef0123456789abcdef.ax.json"}}"#
    )
    windows.snapshotResult = snapshotProtocolValue(detailName: detailName, metadataExtra: ["node_count": .number(4001)])
    let bounded = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"bounded","operation":"snapshot","payload":{"app_ref":"app","window_ref":"window","scope":"target_window","artifact_name":"snapshot-0123456789abcdef0123456789abcdef.png","text_detail":"on","text_detail_artifact_name":"snapshot-0123456789abcdef0123456789abcdef.ax.json"}}"#
    )
    let duplicate = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"duplicate","operation":"snapshot","payload":{"app_ref":"app","window_ref":"window","scope":"target_window","artifact_name":"snapshot-0123456789abcdef0123456789abcdef.png","text_detail":"on","text_detail":"on","text_detail_artifact_name":"snapshot-0123456789abcdef0123456789abcdef.ax.json"}}"#
    )
    let wrongVersion = dispatcher.handle(
        #"{"protocol_version":2,"request_id":"old","operation":"snapshot","payload":{"app_ref":"app","window_ref":"window","scope":"target_window","artifact_name":"snapshot-0123456789abcdef0123456789abcdef.png","text_detail":"on","text_detail_artifact_name":"snapshot-0123456789abcdef0123456789abcdef.ax.json"}}"#
    )

    #expect(!unknown.ok)
    #expect(!bounded.ok)
    #expect(!duplicate.ok)
    #expect(!wrongVersion.ok)
    #expect(wrongVersion.error?.code == "protocol_mismatch")
}

@Test func snapshotTextDetailRequiresStrictMetadataConsistencyAndCursorBoolean() {
    let detailName = "snapshot-0123456789abcdef0123456789abcdef.ax.json"
    let windows = CooperativeProtocolWindows()
    let dispatcher = Dispatcher(windows: windows)
    let request = #"{"protocol_version":4,"request_id":"strict","operation":"snapshot","payload":{"app_ref":"app","window_ref":"window","scope":"target_window","artifact_name":"snapshot-0123456789abcdef0123456789abcdef.png","text_detail":"on","text_detail_artifact_name":"snapshot-0123456789abcdef0123456789abcdef.ax.json"}}"#
    let invalidMetadata: [[String: JSONValue]] = [
        ["schema_version": .number(1.5)],
        ["snapshot_id": .string("other")],
        ["node_count": .number(-1)],
        ["max_depth_observed": .number(21)],
        ["byte_count": .number(8 * 1024 * 1024 + 1)],
        ["sha256": .string(String(repeating: "A", count: 64))],
        ["sha256": .string(String(repeating: "١", count: 64))],
        ["truncated": .bool(true)],
        ["truncation_reasons": .array([.string("depth_limit")])],
        ["truncated": .bool(true), "truncation_reasons": .array([])],
        [
            "truncated": .bool(true),
            "truncation_reasons": .array([.string("depth_limit"), .string("depth_limit")]),
        ],
        ["truncated": .bool(true), "truncation_reasons": .array([.string("unknown_limit")])],
    ]

    for mutation in invalidMetadata {
        windows.snapshotResult = snapshotProtocolValue(detailName: detailName, metadataExtra: mutation)
        #expect(!dispatcher.handle(request).ok)
    }
    windows.snapshotResult = snapshotProtocolValue(
        detailName: detailName,
        payloadExtra: ["cursor_visible": .number(0)]
    )
    #expect(!dispatcher.handle(request).ok)
}

@Test func snapshotResponseRejectsEveryMalformedBaseMetadataField() {
    let windows = CooperativeProtocolWindows()
    let dispatcher = Dispatcher(windows: windows)
    let request = #"{"protocol_version":4,"request_id":"base-metadata","operation":"snapshot","payload":{"app_ref":"app","window_ref":"window","scope":"target_window","artifact_name":"snapshot-0123456789abcdef0123456789abcdef.png"}}"#
    let bounds = JSONValue.object([
        "x": .number(0), "y": .number(0), "width": .number(1), "height": .number(1),
    ])
    let invalidPayloads: [(label: String, mutation: [String: JSONValue])] = [
        ("logical-size-object", ["logical_size": .array([])]),
        ("logical-size-exact-fields", [
            "logical_size": .object(["width": .number(1), "height": .number(1), "extra": .number(1)]),
        ]),
        ("pixel-size-positive", [
            "pixel_size": .object(["width": .number(0), "height": .number(1)]),
        ]),
        ("pixel-size-finite", [
            "pixel_size": .object(["width": .number(.infinity), "height": .number(1)]),
        ]),
        ("backing-scale-numeric", ["backing_scale": .bool(true)]),
        ("backing-scale-positive", ["backing_scale": .number(0)]),
        ("capture-bounds-exact-fields", [
            "capture_bounds": .object([
                "x": .number(0), "y": .number(0), "width": .number(1),
            ]),
        ]),
        ("capture-bounds-finite", [
            "capture_bounds": .object([
                "x": .number(.nan), "y": .number(0), "width": .number(1), "height": .number(1),
            ]),
        ]),
        ("display-pair", ["display_id": .number(1)]),
        ("display-id-strict-integer", [
            "display_id": .number(1.5), "target_window_bounds": bounds,
        ]),
        ("display-id-positive", [
            "display_id": .number(0), "target_window_bounds": bounds,
        ]),
        ("target-window-bounds", [
            "display_id": .number(1),
            "target_window_bounds": .object([
                "x": .number(0), "y": .number(0), "width": .number(-1), "height": .number(1),
            ]),
        ]),
        ("ax-tree-object", ["ax_tree": .array([])]),
        ("cursor-strict-bool", ["cursor_visible": .number(0)]),
    ]

    for invalid in invalidPayloads {
        windows.snapshotResult = snapshotProtocolValue(payloadExtra: invalid.mutation)
        #expect(!dispatcher.handle(request).ok, "base snapshot field: \(invalid.label)")
    }
}

@Test func snapshotArtifactFilenamesMatchSharedAdversarialVectors() throws {
    let vectors = try snapshotArtifactFilenameVectors()
    let windows = CooperativeProtocolWindows()
    windows.snapshotResult = snapshotProtocolValue()
    let dispatcher = Dispatcher(windows: windows)
    let plainNames = try #require(vectors["plain_names"] as? [[String: Any]])

    for vector in plainNames {
        let label = try #require(vector["label"] as? String)
        let encodedName = try #require(encodedFilenameVector(vector))
        let expected = try #require(vector["valid"] as? Bool)
        let response = dispatcher.handle(
            #"{"protocol_version":4,"request_id":"plain","operation":"snapshot","payload":{"app_ref":"app","window_ref":"window","scope":"target_window","artifact_name":\#(encodedName)}}"#
        )
        #expect(response.ok == expected, "plain filename vector: \(label)")
    }
}

@Test func snapshotDetailFilenamesMatchSharedAdversarialVectors() throws {
    let vectors = try snapshotArtifactFilenameVectors()
    let windows = CooperativeProtocolWindows()
    let dispatcher = Dispatcher(windows: windows)
    let pairs = try #require(vectors["detail_pairs"] as? [[String: Any]])

    for vector in pairs {
        let label = try #require(vector["label"] as? String)
        let imageName = try #require(vector["image"] as? String)
        let detailName = try #require(vector["detail"] as? String)
        let expected = try #require(vector["valid"] as? Bool)
        windows.snapshotResult = snapshotProtocolValue(
            imageName: imageName,
            detailName: detailName
        )
        let imageJSON = try #require(encodedJSONString(imageName))
        let detailJSON = try #require(encodedJSONString(detailName))
        let response = dispatcher.handle(
            #"{"protocol_version":4,"request_id":"detail","operation":"snapshot","payload":{"app_ref":"app","window_ref":"window","scope":"target_window","artifact_name":\#(imageJSON),"text_detail":"on","text_detail_artifact_name":\#(detailJSON)}}"#
        )
        #expect(response.ok == expected, "detail filename vector: \(label)")
    }
}

@Test func snapshotTruncationReasonsMatchSharedCanonicalOrderVectors() throws {
    let vectors = try snapshotArtifactFilenameVectors()
    let reasonVectors = try #require(vectors["truncation_reason_vectors"] as? [[String: Any]])
    let detailName = "snapshot-0123456789abcdef0123456789abcdef.ax.json"
    let windows = CooperativeProtocolWindows()
    let dispatcher = Dispatcher(windows: windows)
    let request = #"{"protocol_version":4,"request_id":"reasons","operation":"snapshot","payload":{"app_ref":"app","window_ref":"window","scope":"target_window","artifact_name":"snapshot-0123456789abcdef0123456789abcdef.png","text_detail":"on","text_detail_artifact_name":"snapshot-0123456789abcdef0123456789abcdef.ax.json"}}"#

    for vector in reasonVectors {
        let label = try #require(vector["label"] as? String)
        let truncated = try #require(vector["truncated"] as? Bool)
        let reasons = try #require(vector["reasons"] as? [String])
        let expected = try #require(vector["valid"] as? Bool)
        windows.snapshotResult = snapshotProtocolValue(
            detailName: detailName,
            metadataExtra: [
                "truncated": .bool(truncated),
                "truncation_reasons": .array(reasons.map(JSONValue.string)),
            ]
        )
        #expect(dispatcher.handle(request).ok == expected, "truncation reasons vector: \(label)")
    }
}

private struct StaticPermissions: PermissionStatusProviding {
    let accessibilityTrusted: Bool
    let screenRecordingAllowed: Bool
}

private func boolValue(_ value: JSONValue?, keys: [String]) -> Bool? {
    var current = value
    for key in keys {
        guard case let .object(object)? = current else {
            return nil
        }
        current = object[key]
    }
    guard case let .bool(result)? = current else {
        return nil
    }
    return result
}

private func stringValue(_ value: JSONValue?, keys: [String]) -> String? {
    var current = value
    for key in keys {
        guard case let .object(object)? = current else { return nil }
        current = object[key]
    }
    guard case let .string(result)? = current else { return nil }
    return result
}

private func numberValue(_ value: JSONValue?, keys: [String]) -> Double? {
    var current = value
    for key in keys {
        guard case let .object(object)? = current else { return nil }
        current = object[key]
    }
    guard case let .number(result)? = current else { return nil }
    return result
}

private func snapshotArtifactFilenameVectors() throws -> [String: Any] {
    let testsRoot = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
    let data = try Data(contentsOf: testsRoot.appendingPathComponent(
        "Fixtures/snapshot_artifact_filename_vectors.json"
    ))
    return try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
}

private func encodedJSONString(_ value: String) -> String? {
    guard let data = try? JSONEncoder().encode(value) else { return nil }
    return String(data: data, encoding: .utf8)
}

private func encodedFilenameVector(_ vector: [String: Any]) -> String? {
    if let encoded = vector["json_string"] as? String { return encoded }
    if let name = vector["name"] as? String { return encodedJSONString(name) }
    guard let repeated = vector["repeat"] as? String,
          let count = vector["count"] as? Int,
          let suffix = vector["suffix"] as? String
    else { return nil }
    return encodedJSONString(String(repeating: repeated, count: count) + suffix)
}

private func snapshotProtocolValue(
    imageName: String = "snapshot-0123456789abcdef0123456789abcdef.png",
    detailName: String? = nil,
    omitMetadata: Bool = false,
    metadataExtra: [String: JSONValue] = [:],
    payloadExtra: [String: JSONValue] = [:]
) -> JSONValue {
    var payload: [String: JSONValue] = [
        "image_artifact": .string(imageName),
        "logical_size": .object(["width": .number(1), "height": .number(1)]),
        "pixel_size": .object(["width": .number(1), "height": .number(1)]),
        "backing_scale": .number(1),
        "capture_bounds": .object([
            "x": .number(0), "y": .number(0), "width": .number(1), "height": .number(1),
        ]),
        "ax_tree": .object(["role": .string("AXWindow")]),
    ]
    if let detailName {
        payload["text_detail_artifact"] = .string(detailName)
        if !omitMetadata {
            var metadata: [String: JSONValue] = [
                "schema_version": .number(1),
                "snapshot_id": .string("snapshot_detail"),
                "coverage": .string("reported_ax_subtree"),
                "node_count": .number(1),
                "max_depth_observed": .number(0),
                "byte_count": .number(128),
                "sha256": .string(String(repeating: "a", count: 64)),
                "truncated": .bool(false),
                "truncation_reasons": .array([]),
            ]
            metadata.merge(metadataExtra) { _, replacement in replacement }
            payload["text_detail_metadata"] = .object(metadata)
        }
    }
    payload.merge(payloadExtra) { _, replacement in replacement }
    return .object([
        "snapshot_id": .string("snapshot_detail"),
        "payload": .object(payload),
    ])
}

private final class CooperativeProtocolWindows: WindowObserving {
    var planCalls = 0
    var backgroundActCalls = 0
    var cooperativeResultOverride: CooperativeActionResult? = nil
    var fragmentPlans: [ForegroundFragmentPlanRequest?] = []
    var fragmentCommits: [FragmentStageCommit] = []
    var takeoverDeclarations: [ForegroundFragmentDeclaration] = []
    var ordinaryTakeoverBegins = 0
    var snapshotResult: JSONValue = .object([:])
    var snapshotDetails: [SnapshotTextDetailRequest] = []

    func apps() throws -> JSONValue { .object(["apps": .array([])]) }
    func snapshot(
        appRef _: String,
        windowRef _: String,
        scope _: String,
        artifactName _: String?,
        textDetail: SnapshotTextDetailRequest
    ) throws -> JSONValue {
        snapshotDetails.append(textDetail)
        return snapshotResult
    }

    func planActions(snapshotID _: String, interactionMode _: InteractionMode, actions _: [NativeAction]) -> CooperativePlanResult {
        planCalls += 1
        return CooperativePlanResult(
            summary: DispatchPlanSummary(
                planRef: "plan_test",
                interactionMode: .background,
                requiresTakeover: false,
                reason: "background_ax_only",
                actionClasses: [],
                lastAcknowledgedAction: -1
            ),
            error: nil
        )
    }

    func planActions(
        snapshotID: String,
        interactionMode: InteractionMode,
        actions: [NativeAction],
        fragment: ForegroundFragmentPlanRequest?
    ) -> CooperativePlanResult {
        fragmentPlans.append(fragment)
        return planActions(snapshotID: snapshotID, interactionMode: interactionMode, actions: actions)
    }

    func fragmentStageCommit(
        takeoverRef _: String,
        commit: FragmentStageCommit
    ) throws -> FragmentStageCommitOutcome {
        fragmentCommits.append(commit)
        return FragmentStageCommitOutcome(
            terminal: true,
            takeover: TakeoverOutcome(started: true, restoration: .restored)
        )
    }

    func takeoverBegin(snapshotID _: String, planRef _: String) throws -> String {
        ordinaryTakeoverBegins += 1
        return "takeover"
    }

    func takeoverBegin(
        snapshotID _: String,
        planRef _: String,
        declaration: ForegroundFragmentDeclaration
    ) throws -> String {
        takeoverDeclarations.append(declaration)
        return "takeover"
    }

    func cooperativeAct(
        snapshotID _: String,
        interactionMode _: InteractionMode,
        planRef _: String,
        takeoverRef _: String?,
        actions _: [NativeAction]
    ) -> CooperativeActionResult {
        backgroundActCalls += 1
        if let cooperativeResultOverride { return cooperativeResultOverride }
        return CooperativeActionResult(
            batch: ActionBatchResult(
                outcomes: [ActionOutcome(index: 0, ok: true, error: nil)],
                lastAcknowledgedAction: 0,
                error: nil
            ),
            error: nil
        )
    }

    func cooperativeAct(
        snapshotID: String,
        interactionMode: InteractionMode,
        planRef: String,
        takeoverRef: String?,
        actions: [NativeAction],
        fragmentStage _: FragmentStageAuthority?
    ) -> CooperativeActionResult {
        return cooperativeAct(
            snapshotID: snapshotID,
            interactionMode: interactionMode,
            planRef: planRef,
            takeoverRef: takeoverRef,
            actions: actions
        )
    }
}

private final class ErrorSnapshotWindows: WindowObserving {
    let error: WindowObservationError

    init(error: WindowObservationError) { self.error = error }

    func apps() throws -> JSONValue { .object(["apps": .array([])]) }

    func snapshot(
        appRef _: String,
        windowRef _: String,
        scope _: String,
        artifactName _: String?,
        textDetail _: SnapshotTextDetailRequest
    ) throws -> JSONValue {
        throw error
    }
}
