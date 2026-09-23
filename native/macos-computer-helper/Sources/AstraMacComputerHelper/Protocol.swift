import Foundation

let protocolVersion = 4
let maxRequestLineBytes = 4 * 1024 * 1024
let maxRequestIDBytes = 256
let maxOperationBytes = 64
let textDetailTruncationReasonOrder = [
    "depth_limit", "node_limit", "structural_string_limit", "value_limit",
    "aggregate_text_limit", "wall_clock_limit", "final_byte_limit",
]

extension DispatchPlanSummary {
    var pidActionClasses: [DispatchActionClass] {
        plannedPIDActionClasses ?? actionClasses.filter { actionClass in
            switch actionClass {
            case .click, .doubleClick, .scroll, .drag: true
            case .press, .text: false
            }
        }
    }
}

struct HelperError: Codable, Equatable {
    let code: String
    let message: String
}

struct HelperResponse: Codable {
    let protocolVersion: Int
    let requestID: String
    let ok: Bool
    let result: JSONValue?
    let snapshot: JSONValue?
    let error: HelperError?

    enum CodingKeys: String, CodingKey {
        case protocolVersion = "protocol_version"
        case requestID = "request_id"
        case ok
        case result
        case snapshot
        case error
    }

    init(
        protocolVersion: Int,
        requestID: String,
        ok: Bool,
        result: JSONValue?,
        snapshot: JSONValue? = nil,
        error: HelperError?
    ) {
        self.protocolVersion = protocolVersion
        self.requestID = requestID
        self.ok = ok
        self.result = result
        self.snapshot = snapshot
        self.error = error
    }
}

struct HelperRequest {
    let protocolVersion: Int
    let requestID: String
    let operation: String
    let payload: JSONValue
    let payloadData: Data
}

enum GetAppStateErrorCode: String {
    case staleTarget = "stale_target"
    case snapshotFailed = "snapshot_failed"
}

struct GetAppStateRequest: Equatable {
    let appRef: String
    let windowRef: String
    let catalogGeneration: Int
    let scope: String
    let artifactName: String
    let textDetail: SnapshotTextDetailRequest
}

func getAppStateRequest(_ request: HelperRequest) -> GetAppStateRequest? {
    guard let values = try? JSONSerialization.jsonObject(with: request.payloadData) as? [String: Any] else {
        return nil
    }
    let base: Set<String> = [
        "app_ref", "window_ref", "catalog_generation", "scope", "artifact_name",
    ]
    let hasTextDetail = values["text_detail"] != nil || values["text_detail_artifact_name"] != nil
    guard Set(values.keys) == (hasTextDetail ? base.union(["text_detail", "text_detail_artifact_name"]) : base),
          let appRef = values["app_ref"] as? String,
          let windowRef = values["window_ref"] as? String,
          boundedGetAppStateReference(appRef),
          boundedGetAppStateReference(windowRef),
          let catalogGeneration = exactPositiveInteger(values["catalog_generation"]),
          let scope = values["scope"] as? String,
          scope == "target_window" || scope == "display",
          let artifactName = values["artifact_name"] as? String,
          plainGetAppStateArtifactName(artifactName),
          artifactName.hasSuffix(".png")
    else { return nil }

    let textDetail: SnapshotTextDetailRequest
    if hasTextDetail {
        guard values["text_detail"] as? String == "on",
              let detailName = values["text_detail_artifact_name"] as? String,
              validGetAppStateDetailArtifactName(detailName, imageArtifact: artifactName)
        else { return nil }
        textDetail = .on(artifactName: detailName)
    } else {
        textDetail = .off
    }
    return GetAppStateRequest(
        appRef: appRef,
        windowRef: windowRef,
        catalogGeneration: catalogGeneration,
        scope: scope,
        artifactName: artifactName,
        textDetail: textDetail
    )
}

func validGetAppStateResponse(
    result: JSONValue,
    snapshot: JSONValue,
    request: GetAppStateRequest
) -> Bool {
    guard case let .object(resultValues) = result,
          Set(resultValues.keys) == ["app_ref", "window_ref", "catalog_generation", "interaction_mode"],
          case let .string(appRef)? = resultValues["app_ref"],
          appRef == request.appRef,
          case let .string(windowRef)? = resultValues["window_ref"],
          windowRef == request.windowRef,
          exactWholeGetAppStateNumber(resultValues["catalog_generation"]) == request.catalogGeneration,
          case .string("background")? = resultValues["interaction_mode"],
          case let .object(snapshotValues) = snapshot,
          Set(snapshotValues.keys) == ["snapshot_id", "payload"],
          case let .string(snapshotID)? = snapshotValues["snapshot_id"],
          boundedGetAppStateReference(snapshotID),
          case let .object(payload)? = snapshotValues["payload"],
          (request.scope == "display"
              ? payload["display_id"] != nil && payload["target_window_bounds"] != nil
              : payload["display_id"] == nil && payload["target_window_bounds"] == nil)
    else { return false }
    return validSnapshotResponseInvariants(
        snapshot,
        textDetail: request.textDetail,
        expectedImageArtifact: request.artifactName
    )
}

func validSnapshotResponseInvariants(
    _ snapshot: JSONValue,
    textDetail: SnapshotTextDetailRequest,
    expectedImageArtifact: String? = nil
) -> Bool {
    guard case let .object(snapshotValues) = snapshot,
          Set(snapshotValues.keys) == ["snapshot_id", "payload"],
          case let .string(snapshotID)? = snapshotValues["snapshot_id"],
          snapshotResponseBoundedString(snapshotID),
          case let .object(payload)? = snapshotValues["payload"]
    else { return false }
    let required: Set<String> = [
        "image_artifact", "logical_size", "pixel_size", "backing_scale",
        "capture_bounds", "ax_tree",
    ]
    var allowed = required.union([
        "display_id", "target_window_bounds", "cursor_visible",
        "virtual_pointer",
        "has_default_button", "default_button_element_ref",
        "suggestion_popups",
    ])
    if case .on = textDetail {
        allowed.formUnion(["text_detail_artifact", "text_detail_metadata"])
    }
    guard required.isSubset(of: payload.keys),
          Set(payload.keys).isSubset(of: allowed),
          (payload["display_id"] == nil) == (payload["target_window_bounds"] == nil),
          case let .string(imageName)? = payload["image_artifact"],
          snapshotResponsePlainArtifactName(imageName),
          expectedImageArtifact.map({ $0 == imageName }) ?? true,
          snapshotResponseValidSize(payload["logical_size"]),
          snapshotResponseValidSize(payload["pixel_size"]),
          snapshotResponsePositiveFiniteNumber(payload["backing_scale"]) != nil,
          snapshotResponseValidBounds(payload["capture_bounds"]),
          payload["display_id"].map(snapshotResponsePositiveWholeNumber) ?? true,
          payload["target_window_bounds"].map(snapshotResponseValidBounds) ?? true,
          payload["virtual_pointer"].map(snapshotResponseValidPoint) ?? true,
          payload["suggestion_popups"].map(snapshotResponseValidSuggestionPopups) ?? true,
          case .object? = payload["ax_tree"]
    else { return false }
    if let cursor = payload["cursor_visible"], case .bool = cursor {} else if payload["cursor_visible"] != nil {
        return false
    }
    if let presence = payload["has_default_button"], case .bool = presence {} else if payload["has_default_button"] != nil {
        return false
    }
    if let referenceValue = payload["default_button_element_ref"] {
        guard case .bool(true)? = payload["has_default_button"],
              case let .string(reference) = referenceValue,
              snapshotResponseBoundedString(reference),
              snapshotResponseAXTreeContainsReference(payload["ax_tree"], reference: reference)
        else { return false }
    }
    switch textDetail {
    case .off:
        return payload["text_detail_artifact"] == nil && payload["text_detail_metadata"] == nil
    case let .on(expectedName):
        guard case let .string(detailName)? = payload["text_detail_artifact"],
              detailName == expectedName,
              snapshotResponseValidTextDetailArtifactName(
                  detailName,
                  imageArtifact: imageName
              ),
              case let .object(metadata)? = payload["text_detail_metadata"]
        else { return false }
        return snapshotResponseValidTextDetailMetadata(metadata, snapshotID: snapshotID)
    }
}

private func snapshotResponseAXTreeContainsReference(
    _ tree: JSONValue?,
    reference: String
) -> Bool {
    guard case let .object(root)? = tree else { return false }
    var remaining = maximumSerializedAXNodes
    var stack: [([String: JSONValue], Int)] = [(root, 0)]
    while let (node, depth) = stack.popLast(), remaining > 0 {
        remaining -= 1
        if case let .string(candidate)? = node["element_ref"], candidate == reference {
            return true
        }
        guard depth + 1 < maximumAXDepth,
              case let .array(children)? = node["children"]
        else { continue }
        for child in children.reversed() {
            if case let .object(childNode) = child {
                stack.append((childNode, depth + 1))
            }
        }
    }
    return false
}

private func snapshotResponseValidTextDetailMetadata(
    _ metadata: [String: JSONValue],
    snapshotID: String
) -> Bool {
    let keys: Set<String> = [
        "schema_version", "snapshot_id", "coverage", "node_count",
        "max_depth_observed", "byte_count", "sha256", "truncated",
        "truncation_reasons",
    ]
    guard Set(metadata.keys) == keys,
          snapshotResponseExactWholeNumber(metadata["schema_version"], minimum: 1, maximum: 1) != nil,
          case let .string(metadataSnapshotID)? = metadata["snapshot_id"],
          metadataSnapshotID == snapshotID,
          snapshotResponseBoundedString(metadataSnapshotID),
          case .string("reported_ax_subtree")? = metadata["coverage"],
          snapshotResponseExactWholeNumber(metadata["node_count"], minimum: 0, maximum: 4_000) != nil,
          snapshotResponseExactWholeNumber(metadata["max_depth_observed"], minimum: 0, maximum: 20) != nil,
          snapshotResponseExactWholeNumber(metadata["byte_count"], minimum: 1, maximum: 8 * 1024 * 1024) != nil,
          case let .string(sha256)? = metadata["sha256"],
          sha256.count == 64,
          sha256.allSatisfy({ "0123456789abcdef".contains($0) }),
          case let .bool(truncated)? = metadata["truncated"],
          case let .array(reasonValues)? = metadata["truncation_reasons"],
          reasonValues.count <= textDetailTruncationReasonOrder.count
    else { return false }
    var previousIndex = -1
    for value in reasonValues {
        guard case let .string(reason) = value,
              let index = textDetailTruncationReasonOrder.firstIndex(of: reason),
              index > previousIndex
        else { return false }
        previousIndex = index
    }
    return truncated == !reasonValues.isEmpty
}

private func snapshotResponseValidSize(_ value: JSONValue?) -> Bool {
    guard case let .object(values)? = value,
          Set(values.keys) == ["width", "height"],
          snapshotResponsePositiveFiniteNumber(values["width"]) != nil,
          snapshotResponsePositiveFiniteNumber(values["height"]) != nil
    else { return false }
    return true
}

private func snapshotResponseValidBounds(_ value: JSONValue?) -> Bool {
    guard case let .object(values)? = value,
          Set(values.keys) == ["x", "y", "width", "height"],
          snapshotResponseFiniteNumber(values["x"]) != nil,
          snapshotResponseFiniteNumber(values["y"]) != nil,
          snapshotResponsePositiveFiniteNumber(values["width"]) != nil,
          snapshotResponsePositiveFiniteNumber(values["height"]) != nil
    else { return false }
    return true
}

/// 虚拟游标位置（takeover 期间才会出现在 payload 里）。登记进 allowed 的同时必须校形状，
/// 否则自检等于形同虚设 —— 两端都漏登过一次，实机表现为"接管之后捕获必坏"。
private func snapshotResponseValidPoint(_ value: JSONValue?) -> Bool {
    guard case let .object(values)? = value,
          Set(values.keys) == ["x", "y"],
          snapshotResponseFiniteNumber(values["x"]) != nil,
          snapshotResponseFiniteNumber(values["y"]) != nil
    else { return false }
    return true
}

/// The focused field's open suggestion lists, window-local; present only when nonempty.
private func snapshotResponseValidSuggestionPopups(_ value: JSONValue?) -> Bool {
    guard case let .array(popups)? = value,
          (1...maximumReportedSuggestionPopups).contains(popups.count)
    else { return false }
    return popups.allSatisfy(snapshotResponseValidBounds)
}

private func snapshotResponseFiniteNumber(_ value: JSONValue?) -> Double? {
    guard case let .number(number)? = value, number.isFinite else { return nil }
    return number
}

private func snapshotResponsePositiveFiniteNumber(_ value: JSONValue?) -> Double? {
    guard let number = snapshotResponseFiniteNumber(value), number > 0 else { return nil }
    return number
}

private func snapshotResponsePositiveWholeNumber(_ value: JSONValue) -> Bool {
    guard case let .number(number) = value,
          number.isFinite,
          number.rounded(.towardZero) == number,
          number > 0
    else { return false }
    return true
}

private func snapshotResponseExactWholeNumber(
    _ value: JSONValue?,
    minimum: Int,
    maximum: Int
) -> Int? {
    guard case let .number(number)? = value,
          number.isFinite,
          number.rounded(.towardZero) == number,
          number >= Double(minimum),
          number <= Double(maximum)
    else { return nil }
    return Int(number)
}

private func snapshotResponseBoundedString(_ value: String) -> Bool {
    !value.isEmpty && value.unicodeScalars.count <= 256
}

private func snapshotResponsePlainArtifactName(_ value: String) -> Bool {
    let scalars = value.unicodeScalars
    return !scalars.isEmpty
        && scalars.count <= 128
        && value.utf8.count <= 128
        && value != "."
        && value != ".."
        && scalars.allSatisfy { (0x21 ... 0x7E).contains($0.value) }
        && !value.contains("/")
        && !value.contains("\\")
}

private func snapshotResponseValidTextDetailArtifactName(
    _ detailName: String,
    imageArtifact: String
) -> Bool {
    let prefix = "snapshot-"
    let suffix = ".png"
    guard snapshotResponsePlainArtifactName(detailName),
          imageArtifact.hasPrefix(prefix),
          imageArtifact.hasSuffix(suffix)
    else { return false }
    let start = imageArtifact.index(imageArtifact.startIndex, offsetBy: prefix.count)
    let end = imageArtifact.index(imageArtifact.endIndex, offsetBy: -suffix.count)
    let token = imageArtifact[start..<end]
    guard token.count == 32,
          token.allSatisfy({ "0123456789abcdef".contains($0) })
    else { return false }
    return detailName == String(imageArtifact.dropLast(4)) + ".ax.json"
}

private func exactPositiveInteger(_ value: Any?) -> Int? {
    guard let number = value as? NSNumber,
          CFGetTypeID(number) != CFBooleanGetTypeID(),
          !["f", "d"].contains(String(cString: number.objCType)),
          number.intValue > 0,
          number.intValue == Int(number.doubleValue)
    else { return nil }
    return number.intValue
}

private func exactWholeGetAppStateNumber(_ value: JSONValue?) -> Int? {
    guard case let .number(number)? = value,
          number.isFinite,
          number.rounded(.towardZero) == number,
          number > 0,
          number <= Double(Int.max)
    else { return nil }
    return Int(number)
}

private func boundedGetAppStateReference(_ value: String) -> Bool {
    !value.isEmpty && value.unicodeScalars.count <= 256
}

private func plainGetAppStateArtifactName(_ value: String) -> Bool {
    let scalars = value.unicodeScalars
    return !scalars.isEmpty
        && scalars.count <= 128
        && value.utf8.count <= 128
        && value != "."
        && value != ".."
        && scalars.allSatisfy { (0x21 ... 0x7E).contains($0.value) }
        && !value.contains("/")
        && !value.contains("\\")
}

private func validGetAppStateDetailArtifactName(
    _ detailName: String,
    imageArtifact: String
) -> Bool {
    plainGetAppStateArtifactName(detailName)
        && imageArtifact.hasSuffix(".png")
        && detailName == String(imageArtifact.dropLast(4)) + ".ax.json"
}

indirect enum JSONValue: Codable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else {
            self = .object(try container.decode([String: JSONValue].self))
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .null:
            try container.encodeNil()
        case let .bool(value):
            try container.encode(value)
        case let .number(value):
            try container.encode(value)
        case let .string(value):
            try container.encode(value)
        case let .array(value):
            try container.encode(value)
        case let .object(value):
            try container.encode(value)
        }
    }
}

func encodeResponse(_ response: HelperResponse) throws -> String {
    let encoded = try JSONEncoder().encode(response)
    guard encoded.count <= maxRequestLineBytes,
          let line = String(data: encoded, encoding: .utf8),
          !line.contains("\n"),
          !line.contains("\r")
    else {
        throw EncodingError.invalidValue(
            response,
            EncodingError.Context(codingPath: [], debugDescription: "response must be a single UTF-8 line no larger than 4 MiB")
        )
    }
    return line
}

final class Dispatcher {
    private let permissions: any PermissionStatusProviding
    private let windows: any WindowObserving

    init(
        permissions: any PermissionStatusProviding = SystemPermissionStatus(),
        windows: (any WindowObserving)? = nil
    ) {
        self.permissions = permissions
        self.windows = windows ?? SystemWindowObserver(permissions: permissions)
    }

    func handle(_ line: String) -> HelperResponse {
        let response = handleInner(line)
        if !response.ok, let err = response.error {
            logActionRejected("RESP-ERR code=\(err.code) message=\(err.message)")
        }
        return response
    }

    private func handleInner(_ line: String) -> HelperResponse {
        guard line.lengthOfBytes(using: .utf8) <= maxRequestLineBytes else {
            return Self.invalidRequest(message: "request exceeds 4 MiB")
        }
        var parser = StrictRequestParser(data: Data(line.utf8))
        let request: HelperRequest
        switch parser.parse() {
        case let .request(value):
            request = value
            logActionRejected("REQ-IN \(request.operation)")
        case let .invalid(requestID):
            return Self.failure(
                requestID: requestID ?? "invalid",
                message: "request must be a strict protocol v4 frame"
            )
        }
        guard request.protocolVersion == protocolVersion else {
            return Self.failure(requestID: request.requestID, message: "protocol version mismatch")
        }
        switch request.operation {
        case "ping":
            return success(requestID: request.requestID, result: .object(["pong": .bool(true)]))
        case "status":
            return success(
                requestID: request.requestID,
                result: .object([
                    "supported": .bool(true),
                    "build_identity": bundledHelperBuildIdentity(),
                    "permissions": .object([
                        "accessibility": .bool(permissions.accessibilityTrusted),
                        "screen_recording": .bool(permissions.screenRecordingAllowed),
                    ]),
                ])
            )
        case "close":
            close()
            return success(requestID: request.requestID, result: .object(["closed": .bool(true)]))
        case "apps":
            do {
                return success(requestID: request.requestID, result: try windows.apps())
            } catch {
                return observationFailure(requestID: request.requestID, error: error)
            }
        case "snapshot", "snapshot_subtree":
            var snapshotPayload = request.payload
            var subtree: (snapshotID: String, elementRef: String)?
            if request.operation == "snapshot_subtree" {
                guard case var .object(values) = request.payload,
                      let snapshotID = boundedReference(values.removeValue(forKey: "snapshot_id")),
                      let elementRef = boundedReference(values.removeValue(forKey: "subtree_ref"))
                else { return Self.failure(requestID: request.requestID, message: "snapshot_subtree requires current snapshot_id and subtree_ref") }
                subtree = (snapshotID, elementRef)
                snapshotPayload = .object(values)
            }
            guard let textDetail = snapshotTextDetailRequest(snapshotPayload),
                  let appRef = stringPayload(request.payload, "app_ref"),
                  let windowRef = stringPayload(request.payload, "window_ref"),
                  let scope = stringPayload(request.payload, "scope")
            else { return Self.failure(requestID: request.requestID, message: "snapshot requires app_ref, window_ref, and scope") }
            guard subtree == nil || scope == "target_window" else {
                return Self.failure(requestID: request.requestID, message: "snapshot_subtree requires target_window scope")
            }
            do {
                let snapshot: JSONValue
                if let subtree {
                    snapshot = try windows.snapshotSubtree(appRef: appRef, windowRef: windowRef,
                        snapshotID: subtree.snapshotID, elementRef: subtree.elementRef, scope: scope,
                        artifactName: stringPayload(request.payload, "artifact_name"), textDetail: textDetail)
                } else {
                    snapshot = try windows.snapshot(
                    appRef: appRef,
                    windowRef: windowRef,
                    scope: scope,
                    artifactName: stringPayload(request.payload, "artifact_name"),
                    textDetail: textDetail
                )
                }
                guard validSnapshotResponse(snapshot, textDetail: textDetail) else {
                    return Self.failure(
                        requestID: request.requestID,
                        message: "snapshot response does not match the requested protocol variant"
                    )
                }
                return HelperResponse(
                    protocolVersion: protocolVersion,
                    requestID: request.requestID,
                    ok: true,
                    result: nil,
                    snapshot: snapshot,
                    error: nil
                )
            } catch {
                return observationFailure(requestID: request.requestID, error: error)
            }
        case "get_app_state":
            guard let routed = getAppStateRequest(request) else {
                return Self.failure(
                    requestID: request.requestID,
                    message: "get_app_state has an invalid catalog-bound payload"
                )
            }
            do {
                let observation = try windows.getAppState(
                    appRef: routed.appRef,
                    windowRef: routed.windowRef,
                    catalogGeneration: routed.catalogGeneration,
                    scope: routed.scope,
                    artifactName: routed.artifactName,
                    textDetail: routed.textDetail
                )
                let result = observation.helperResultJSON
                return HelperResponse(
                    protocolVersion: protocolVersion,
                    requestID: request.requestID,
                    ok: true,
                    result: result,
                    snapshot: observation.snapshot,
                    error: nil
                )
            } catch {
                return appStateFailure(requestID: request.requestID, error: error)
            }
        case "select":
            guard validateSelectPayload(request.payload) else {
                return Self.failure(requestID: request.requestID, message: "select requires app_ref and window_ref")
            }
            guard let appRef = stringPayload(request.payload, "app_ref"),
                  let windowRef = stringPayload(request.payload, "window_ref")
            else { return Self.failure(requestID: request.requestID, message: "select requires app_ref and window_ref") }
            do {
                let target = try windows.select(appRef: appRef, windowRef: windowRef)
                return success(requestID: request.requestID, result: .object([
                    "app_ref": .string(target.appRef),
                    "window_ref": .string(target.windowRef),
                    "interaction_mode": .string(target.interactionMode.rawValue),
                ]))
            } catch {
                return observationFailure(requestID: request.requestID, error: error)
            }
        case "plan_actions":
            guard let fragment = fragmentPlanRequest(request), validatePlanActionsPayload(request.payload, fragment: fragment) else {
                return Self.failure(requestID: request.requestID, message: "plan_actions has an invalid cooperative payload")
            }
            guard case let .object(payload) = request.payload,
                  let mode = interactionMode(payload["interaction_mode"]),
                  case let .string(snapshotID)? = payload["snapshot_id"],
                  case let .array(actionValues)? = payload["actions"]
            else { return Self.failure(requestID: request.requestID, message: "plan_actions has an invalid cooperative payload") }
            let actions: [NativeAction]
            do { actions = try actionValues.map(NativeAction.parse) }
            catch { return Self.failure(requestID: request.requestID, message: "plan_actions contains an invalid action") }
            return cooperativePlanResponse(
                requestID: request.requestID,
                result: windows.planActions(
                    snapshotID: snapshotID,
                    interactionMode: mode,
                    actions: actions,
                    fragment: fragment.value
                )
            )
        case "takeover_begin":
            guard case let .object(payload) = request.payload,
                  case let .string(snapshotID)? = payload["snapshot_id"],
                  case let .string(planRef)? = payload["plan_ref"]
            else { return Self.failure(requestID: request.requestID, message: "takeover_begin has an invalid payload") }
            let declaration: ForegroundFragmentDeclaration?
            if payload["fragment"] == nil {
                guard validateTakeoverBeginPayload(request.payload, hasFragment: false) else {
                    return Self.failure(requestID: request.requestID, message: "takeover_begin has an invalid payload")
                }
                declaration = nil
            } else {
                guard validateTakeoverBeginPayload(request.payload, hasFragment: true),
                      let decoded = fragmentDeclaration(request)
                else { return Self.failure(requestID: request.requestID, message: "takeover_begin has an invalid fragment declaration") }
                declaration = decoded
            }
            do {
                if let declaration {
                    let reference = try windows.takeoverBegin(
                        snapshotID: snapshotID, planRef: planRef, declaration: declaration
                    )
                    return success(requestID: request.requestID, result: .object([
                        "takeover_ref": .string(reference),
                        "fragment_hash": .string(declaration.fragmentHash),
                        "stage_index": .number(0),
                        "stage_hash": .string(declaration.stages[0].stageHash),
                    ]))
                }
                let reference = try windows.takeoverBegin(snapshotID: snapshotID, planRef: planRef)
                return success(requestID: request.requestID, result: .object(["takeover_ref": .string(reference)]))
            } catch {
                return takeoverFailure(requestID: request.requestID, error: error)
            }
        case "act":
            guard let fragmentStage = fragmentActAuthority(request),
                  validateCooperativeActPayload(request.payload, fragmentStage: fragmentStage),
                  case let .object(payload) = request.payload,
                  let mode = interactionMode(payload["interaction_mode"]),
                  case let .string(snapshotID)? = payload["snapshot_id"],
                  case let .string(planRef)? = payload["plan_ref"],
                  case let .array(actionValues)? = payload["actions"]
            else { return Self.failure(requestID: request.requestID, message: "act has an invalid cooperative payload") }
            let actions: [NativeAction]
            do { actions = try actionValues.map(NativeAction.parse) }
            catch { return Self.failure(requestID: request.requestID, message: "act contains an invalid action") }
            let takeoverRef: String?
            if case let .string(value)? = payload["takeover_ref"] { takeoverRef = value } else { takeoverRef = nil }
            return cooperativeActionResponse(
                requestID: request.requestID,
                result: windows.cooperativeAct(
                    snapshotID: snapshotID,
                    interactionMode: mode,
                    planRef: planRef,
                    takeoverRef: takeoverRef,
                    actions: actions,
                    fragmentStage: fragmentStage.value
                )
            )
        case "fragment_stage_commit":
            guard let routed = fragmentStageCommit(request) else {
                return Self.failure(requestID: request.requestID, message: "fragment_stage_commit has an invalid payload")
            }
            do {
                let commitOutcome = try windows.fragmentStageCommit(
                    takeoverRef: routed.takeoverRef,
                    commit: routed.commit
                )
                var result: [String: JSONValue] = [
                    "fragment_hash": .string(routed.commit.fragmentHash),
                    "stage_index": .number(Double(routed.commit.stageIndex)),
                    "stage_hash": .string(routed.commit.stageHash),
                    "terminal": .bool(commitOutcome.terminal),
                ]
                if let takeover = commitOutcome.takeover {
                    result["restoration"] = .string(takeover.restoration.rawValue)
                }
                return success(requestID: request.requestID, result: .object(result))
            } catch {
                return takeoverFailure(requestID: request.requestID, error: error)
            }
        case "takeover_end":
            guard validateTakeoverEndPayload(request.payload) else {
                return Self.failure(requestID: request.requestID, message: "takeover_end requires takeover_ref")
            }
            guard case let .object(payload) = request.payload,
                  case let .string(takeoverRef)? = payload["takeover_ref"]
            else { return Self.failure(requestID: request.requestID, message: "takeover_end requires takeover_ref") }
            do {
                let restorePreviousFocus: Bool
                if case let .bool(value)? = payload["restore_previous_focus"] {
                    restorePreviousFocus = value
                } else { restorePreviousFocus = true }
                let outcome = try windows.takeoverEnd(
                    takeoverRef: takeoverRef, restorePreviousFocus: restorePreviousFocus
                )
                guard !outcome.cleanupFailed else { throw TakeoverError.cleanupFailed }
                return success(
                    requestID: request.requestID,
                    result: takeoverOutcomeJSON(outcome)
                )
            } catch {
                return takeoverFailure(requestID: request.requestID, error: error)
            }
        default:
            return Self.failure(requestID: request.requestID, message: "operation is not supported")
        }
    }

    func close() {
        windows.invalidateSnapshots()
    }

    private func success(requestID: String, result: JSONValue) -> HelperResponse {
        HelperResponse(
            protocolVersion: protocolVersion,
            requestID: requestID,
            ok: true,
            result: result,
            snapshot: nil,
            error: nil
        )
    }

    static func invalidRequest(message: String) -> HelperResponse {
        failure(requestID: "invalid", message: message)
    }

    private static func failure(requestID: String, message: String, code: String = "protocol_mismatch") -> HelperResponse {
        HelperResponse(
            protocolVersion: protocolVersion,
            requestID: requestID,
            ok: false,
            result: nil,
            snapshot: nil,
            error: HelperError(code: code, message: message)
        )
    }

    private func observationFailure(requestID: String, error: Error) -> HelperResponse {
        let pair: (String, String)
        switch error {
        case WindowObservationError.permissionDenied:
            pair = ("permission_denied", "required macOS permission is unavailable")
        case WindowObservationError.targetGone:
            pair = ("target_gone", "target application or window is no longer available")
        case WindowObservationError.overlayBlocked:
            pair = ("overlay_blocked", "an overlay or uncertain occluding window prevents exact target binding")
        case WindowObservationError.axWindowUnmatched:
            pair = ("ax_window_unmatched", "the window is present but has no unique matching Accessibility window")
        case WindowObservationError.windowContentUnavailable:
            pair = ("window_content_unavailable", "exact target-window capture returned no readable pixels (fully transparent)")
        case WindowObservationError.targetNotFrontmost:
            pair = ("target_not_frontmost", "target window could not be made frontmost")
        case WindowObservationError.invalidScope:
            pair = ("protocol_mismatch", "snapshot scope must be target_window or display")
        case WindowObservationError.displayUnavailable:
            pair = ("helper_failed", "target window is not contained by one bounded display")
        case WindowObservationError.invalidCapturePath:
            pair = ("protocol_mismatch", "capture output path is not an approved request-local file")
        case WindowObservationError.captureFailed:
            pair = ("helper_failed", "target window capture failed")
        case WindowObservationError.capturePublicationUncertain:
            pair = ("helper_failed", "capture is complete but directory durability could not be confirmed")
        case WindowObservationError.artifactQuotaExceeded:
            pair = ("helper_failed", "smart snapshot artifact quota is exhausted")
        case WindowObservationError.artifactPublisherFailed:
            pair = ("helper_failed", "smart snapshot artifact publisher is unavailable")
        case WindowObservationError.observationTimedOut:
            pair = ("observation_timeout", "Observation exceeded its native time budget")
        case WindowObservationError.axSerializationFailed:
            pair = ("helper_failed", "Accessibility tree preparation failed before artifact publication")
        default:
            pair = ("helper_failed", "native window observation failed")
        }
        return Self.failure(requestID: requestID, message: pair.1, code: pair.0)
    }

    private func appStateFailure(requestID: String, error: Error) -> HelperResponse {
        let pair: (String, String)
        switch error {
        case WindowObservationError.observationTimedOut:
            pair = ("observation_timeout", "Observation exceeded its native time budget")
        case WindowObservationError.staleTarget:
            pair = (GetAppStateErrorCode.staleTarget.rawValue, "catalog target identity is stale")
        case WindowObservationError.permissionDenied:
            pair = ("permission_denied", "required macOS permission is unavailable")
        case WindowObservationError.targetGone:
            pair = ("target_gone", "target application or window is no longer available")
        case WindowObservationError.overlayBlocked:
            pair = ("overlay_blocked", "an overlay or uncertain occluding window prevents exact target binding")
        case WindowObservationError.axWindowUnmatched:
            pair = ("ax_window_unmatched", "the window is present but has no unique matching Accessibility window")
        case WindowObservationError.windowContentUnavailable:
            pair = ("window_content_unavailable", "exact target-window capture returned no readable pixels (fully transparent)")
        default:
            // Keep the public transactional failure contract while retaining
            // bounded stage evidence locally; never log the raw exception.
            switch error {
            case WindowObservationError.axSerializationFailed:
                logActionRejected("app_state_failure=ax_serialization")
            case WindowObservationError.captureFailed:
                logActionRejected("app_state_failure=capture")
            case WindowObservationError.artifactPublisherFailed:
                logActionRejected("app_state_failure=artifact_publisher")
            case WindowObservationError.invalidCapturePath:
                logActionRejected("app_state_failure=capture_path")
            case WindowObservationError.displayUnavailable:
                logActionRejected("app_state_failure=display")
            default:
                logActionRejected("app_state_failure=other")
            }
            pair = (GetAppStateErrorCode.snapshotFailed.rawValue, "transactional target snapshot failed")
        }
        return Self.failure(requestID: requestID, message: pair.1, code: pair.0)
    }

    private func actionMessage(_ error: ActionExecutionError) -> String {
        switch error {
        case .invalidAction: return "action payload is invalid"
        case .permissionDenied: return "Accessibility permission is unavailable"
        case .targetGone: return "target application or window is no longer available"
        case .targetNotFrontmost: return "target window is no longer frontmost"
        case .staleSnapshot: return "snapshot or element reference is stale"
        case .outOfBounds: return "action coordinates are outside the target window"
        case .secureTarget: return "automatic interaction with a secure target is prohibited"
        case .inputFocusRequired: return "the intended input element is not focused"
        case .actionTimeout: return "action did not complete within its bound"
        case .helperFailed: return "native action failed"
        case .unknownOutcome: return "input may have been emitted; capture a fresh snapshot and do not replay"
        case .accessibilityActionRefused:
            return "the application refused this accessibility action; no input was dispatched"
        }
    }

    private func takeoverFailure(requestID: String, error: Error) -> HelperResponse {
        logActionRejected("TAKEOVER-FAIL \(error)")
        switch error {
        case TakeoverError.sidecarFailed:
            return Self.failure(requestID: requestID, message: "virtual cursor sidecar failed", code: CooperativeErrorCode.sidecarFailed.rawValue)
        case TakeoverError.targetChanged:
            return Self.failure(requestID: requestID, message: "target changed before takeover", code: ActionExecutionError.staleSnapshot.rawValue)
        case TakeoverError.targetNotFrontmost:
            return Self.failure(requestID: requestID, message: "target window could not be made frontmost", code: ActionExecutionError.targetNotFrontmost.rawValue)
        case TakeoverError.activityUnavailable:
            return Self.failure(requestID: requestID, message: "user activity monitor is unavailable", code: CooperativeErrorCode.userActivityPaused.rawValue)
        case TakeoverError.cleanupFailed:
            return Self.failure(requestID: requestID, message: "held input cleanup failed", code: ActionExecutionError.helperFailed.rawValue)
        case TakeoverError.stalePlan, TakeoverError.alreadyConsumed, TakeoverError.authorityMismatch:
            return Self.failure(requestID: requestID, message: "takeover authority is stale", code: ActionExecutionError.staleSnapshot.rawValue)
        default:
            return Self.failure(requestID: requestID, message: "takeover failed", code: "helper_failed")
        }
    }

    private func cooperativePlanResponse(requestID: String, result: CooperativePlanResult) -> HelperResponse {
        guard let error = result.error else {
            guard let summary = result.summary, validPlanSummary(summary) else {
                return Self.failure(requestID: requestID, message: "native action planning failed", code: CooperativeErrorCode.sidecarFailed.rawValue)
            }
            return success(
                requestID: requestID,
                result: fragmentBound(protocolJSON(summary), binding: result.fragmentBinding)
            )
        }
        let mapped = cooperativeError(error)
        return HelperResponse(
            protocolVersion: protocolVersion,
            requestID: requestID,
            ok: false,
            result: fragmentBound(
                result.summary.flatMap { validPlanSummary($0) ? protocolJSON($0) : nil } ?? .object([
                    "outcomes": .array([]),
                    "last_acknowledged_action": .number(-1),
                ]),
                binding: result.fragmentBinding
            ),
            snapshot: nil,
            error: HelperError(code: mapped.code, message: mapped.message)
        )
    }

    private func validPlanSummary(_ summary: DispatchPlanSummary) -> Bool {
        let actionClasses = summary.actionClasses
        let pidActionClasses = summary.pidActionClasses
        return !summary.planRef.isEmpty
            && !summary.reason.isEmpty
            && summary.lastAcknowledgedAction == -1
            && actionClasses.count <= 6
            && Set(actionClasses.map(\.rawValue)).count == actionClasses.count
            && pidActionClasses.count <= 6
            && Set(pidActionClasses.map(\.rawValue)).count == pidActionClasses.count
            && Set(pidActionClasses.map(\.rawValue)).isSubset(of: Set(actionClasses.map(\.rawValue)))
    }

    private func protocolJSON(_ summary: DispatchPlanSummary) -> JSONValue {
        guard case var .object(payload) = summary.asJSON() else { return .object([:]) }
        payload["pid_action_classes"] = .array(
            summary.pidActionClasses.map { .string($0.rawValue) }
        )
        return .object(payload)
    }

    private func cooperativeActionResponse(requestID: String, result: CooperativeActionResult) -> HelperResponse {
        guard let error = result.error else {
            return success(
                requestID: requestID,
                result: fragmentBound(result.batch.asJSON(), binding: result.fragmentBinding)
            )
        }
        let mapped = cooperativeError(error)
        return HelperResponse(
            protocolVersion: protocolVersion,
            requestID: requestID,
            ok: false,
            result: fragmentBound(result.batch.asJSON(), binding: result.fragmentBinding),
            snapshot: nil,
            error: HelperError(code: mapped.code, message: mapped.message)
        )
    }

    private func fragmentBound(_ value: JSONValue, binding: FragmentStageAuthority?) -> JSONValue {
        guard let binding, case var .object(payload) = value else { return value }
        payload["fragment_hash"] = .string(binding.fragmentHash)
        payload["stage_index"] = .number(Double(binding.stageIndex))
        payload["stage_hash"] = .string(binding.stageHash)
        return .object(payload)
    }

    private func cooperativeError(_ error: NativeCooperativeError) -> (code: String, message: String) {
        switch error {
        case let .cooperative(code):
            switch code {
            case .foregroundTakeoverRequired:
                return (code.rawValue, "the complete action batch requires foreground takeover")
            case .requiresActiveForegroundTakeover:
                return (code.rawValue, "the application requires activation; use foreground takeover")
            case .backgroundActionUnsupported:
                return (code.rawValue, "the action batch is unsafe for background or foreground automation")
            case .userActivityPaused:
                return (code.rawValue, "native input is paused because user activity was detected")
            case .observationRequired:
                return (code.rawValue, "input delivery acknowledged; remaining actions were not sent. Observe fresh state; do not replay the acknowledged prefix")
            case .sidecarFailed:
                return (code.rawValue, "native cooperative input failed")
            }
        case let .action(error):
            return (error.rawValue, actionMessage(error))
        }
    }

    private func validateSelectPayload(_ payload: JSONValue) -> Bool {
        guard let values = exactPayload(payload, keys: ["app_ref", "window_ref"]) else { return false }
        return boundedReference(values["app_ref"]) != nil && boundedReference(values["window_ref"]) != nil
    }

    private func snapshotTextDetailRequest(_ payload: JSONValue) -> SnapshotTextDetailRequest? {
        let base: Set<String> = ["app_ref", "window_ref", "scope", "artifact_name"]
        guard case let .object(values) = payload,
              let appRef = boundedReference(values["app_ref"]),
              let windowRef = boundedReference(values["window_ref"]),
              case let .string(scope)? = values["scope"],
              scope == "target_window" || scope == "display",
              case let .string(imageName)? = values["artifact_name"],
              plainArtifactName(imageName)
        else { return nil }
        if Set(values.keys) == base {
            _ = appRef
            _ = windowRef
            return .off
        }
        guard Set(values.keys) == base.union(["text_detail", "text_detail_artifact_name"]),
              case .string("on")? = values["text_detail"],
              case let .string(detailName)? = values["text_detail_artifact_name"],
              validTextDetailArtifactName(detailName, imageArtifact: imageName)
        else { return nil }
        return .on(artifactName: detailName)
    }

    private func validSnapshotResponse(
        _ snapshot: JSONValue,
        textDetail: SnapshotTextDetailRequest
    ) -> Bool {
        validSnapshotResponseInvariants(snapshot, textDetail: textDetail)
    }

    private func plainArtifactName(_ value: String) -> Bool {
        let scalars = value.unicodeScalars
        return !scalars.isEmpty
            && scalars.count <= 128
            && value.utf8.count <= 128
            && value != "."
            && value != ".."
            && scalars.allSatisfy { (0x21 ... 0x7E).contains($0.value) }
            && !value.contains("/")
            && !value.contains("\\")
    }

    private func validTextDetailArtifactName(
        _ detailName: String,
        imageArtifact: String
    ) -> Bool {
        let prefix = "snapshot-"
        let suffix = ".png"
        guard plainArtifactName(detailName),
              imageArtifact.hasPrefix(prefix),
              imageArtifact.hasSuffix(suffix)
        else { return false }
        let start = imageArtifact.index(imageArtifact.startIndex, offsetBy: prefix.count)
        let end = imageArtifact.index(imageArtifact.endIndex, offsetBy: -suffix.count)
        let token = imageArtifact[start..<end]
        guard token.count == 32,
              token.allSatisfy({ "0123456789abcdef".contains($0) })
        else { return false }
        return detailName == String(imageArtifact.dropLast(4)) + ".ax.json"
    }

    private func validatePlanActionsPayload(
        _ payload: JSONValue,
        fragment: OptionalFragment<ForegroundFragmentPlanRequest>
    ) -> Bool {
        guard case let .object(values) = payload else { return false }
        var keys: Set<String> = ["interaction_mode", "snapshot_id", "actions"]
        if let request = fragment.value {
            keys.formUnion(["fragment_hash", "stage_index", "stage_hash"])
            if request.takeoverRef != nil { keys.insert("takeover_ref") }
        }
        guard Set(values.keys) == keys,
              interactionMode(values["interaction_mode"]) != nil,
              boundedReference(values["snapshot_id"]) != nil
        else { return false }
        return validActions(values["actions"])
    }

    private func validateTakeoverBeginPayload(_ payload: JSONValue, hasFragment: Bool) -> Bool {
        var keys: Set<String> = ["snapshot_id", "plan_ref"]
        if hasFragment { keys.insert("fragment") }
        guard let values = exactPayload(payload, keys: keys) else { return false }
        return boundedReference(values["snapshot_id"]) != nil && boundedReference(values["plan_ref"]) != nil
    }

    private func validateCooperativeActPayload(
        _ payload: JSONValue,
        fragmentStage: OptionalFragment<FragmentStageAuthority>
    ) -> Bool {
        guard case let .object(values) = payload,
              let mode = interactionMode(values["interaction_mode"])
        else { return false }
        var required: Set<String> = ["interaction_mode", "snapshot_id", "plan_ref", "actions"]
        if mode == .foregroundTakeover {
            required.insert("takeover_ref")
            if fragmentStage.value != nil {
                required.formUnion(["fragment_hash", "stage_index", "stage_hash"])
            }
        } else if fragmentStage.value != nil {
            return false
        }
        guard Set(values.keys) == required,
              boundedReference(values["snapshot_id"]) != nil,
              boundedReference(values["plan_ref"]) != nil,
              (mode == .background || boundedReference(values["takeover_ref"]) != nil)
        else { return false }
        return validActions(values["actions"])
    }

    private struct OptionalFragment<Value> { let value: Value? }

    private func fragmentPlanRequest(
        _ request: HelperRequest
    ) -> OptionalFragment<ForegroundFragmentPlanRequest>? {
        guard let values = strictPayloadObject(request) else { return nil }
        let fragmentKeys = ["fragment_hash", "stage_index", "stage_hash"]
        let present = fragmentKeys.filter { values[$0] != nil }
        guard !present.isEmpty else { return OptionalFragment(value: nil) }
        guard present.count == fragmentKeys.count,
              let snapshotID = values["snapshot_id"] as? String,
              let authority = decodeStageAuthority(values, inputSnapshotID: snapshotID)
        else { return nil }
        if let takeoverRef = values["takeover_ref"] as? String {
            guard authority.stageIndex > 0, boundedString(takeoverRef) else { return nil }
            return OptionalFragment(value: .continuing(takeoverRef: takeoverRef, authority: authority))
        }
        guard authority.stageIndex == 0 else { return nil }
        return OptionalFragment(value: .initial(authority: authority))
    }

    private func fragmentActAuthority(
        _ request: HelperRequest
    ) -> OptionalFragment<FragmentStageAuthority>? {
        guard let values = strictPayloadObject(request),
              let mode = values["interaction_mode"] as? String
        else { return nil }
        if mode == InteractionMode.background.rawValue {
            guard values["fragment_hash"] == nil,
                  values["stage_index"] == nil,
                  values["stage_hash"] == nil
            else { return nil }
            return OptionalFragment(value: nil)
        }
        guard mode == InteractionMode.foregroundTakeover.rawValue,
              let snapshotID = values["snapshot_id"] as? String
        else { return nil }
        let fragmentKeys = ["fragment_hash", "stage_index", "stage_hash"]
        let present = fragmentKeys.filter { values[$0] != nil }
        guard !present.isEmpty else { return OptionalFragment(value: nil) }
        guard present.count == fragmentKeys.count,
              let stage = decodeStageAuthority(values, inputSnapshotID: snapshotID)
        else { return nil }
        return OptionalFragment(value: stage)
    }

    private func fragmentDeclaration(_ request: HelperRequest) -> ForegroundFragmentDeclaration? {
        guard let values = strictPayloadObject(request),
              Set(values.keys) == ["snapshot_id", "plan_ref", "fragment"],
              let raw = values["fragment"] as? [String: Any],
              Set(raw.keys) == [
                  "fragment_hash", "stages", "max_actions", "wall_clock_limit_ms",
                  "restore_previous_focus",
              ],
              let fragmentHash = raw["fragment_hash"] as? String,
              foregroundFragmentHashIsValid(fragmentHash),
              let maxActions = exactInteger(raw["max_actions"]),
              let wallClockLimitMS = exactInteger(raw["wall_clock_limit_ms"]),
              let restore = raw["restore_previous_focus"] as? Bool,
              let rawStages = raw["stages"] as? [Any]
        else { return nil }
        var stages: [ForegroundFragmentStageDeclaration] = []
        for value in rawStages {
            guard let stage = value as? [String: Any],
                  Set(stage.keys) == ["stage_hash", "expected_action_count", "requirements"],
                  let stageHash = stage["stage_hash"] as? String,
                  let expectedActionCount = exactInteger(stage["expected_action_count"]),
                  let rawRequirements = stage["requirements"] as? [Any]
            else { return nil }
            var requirements: [ForegroundFragmentActionRequirement] = []
            for rawRequirement in rawRequirements {
                guard let requirement = decodeFragmentRequirement(rawRequirement) else { return nil }
                requirements.append(requirement)
            }
            stages.append(.init(
                stageHash: stageHash,
                expectedActionCount: expectedActionCount,
                actions: requirements
            ))
        }
        let declaration = ForegroundFragmentDeclaration(
            fragmentHash: fragmentHash,
            stages: stages,
            maxActions: maxActions,
            wallClockLimitMS: wallClockLimitMS,
            restorePreviousFocus: restore
        )
        return (try? ForegroundFragmentAuthority(declaration: declaration)) == nil ? nil : declaration
    }

    private func fragmentStageCommit(
        _ request: HelperRequest
    ) -> (takeoverRef: String, commit: FragmentStageCommit)? {
        guard let values = strictPayloadObject(request),
              Set(values.keys) == [
                  "takeover_ref", "fragment_hash", "stage_index", "stage_hash", "plan_ref",
                  "fresh_snapshot_id", "postcondition_verified",
              ],
              let takeoverRef = values["takeover_ref"] as? String,
              let fragmentHash = values["fragment_hash"] as? String,
              let stageIndex = exactInteger(values["stage_index"]),
              let stageHash = values["stage_hash"] as? String,
              let planRef = values["plan_ref"] as? String,
              let freshSnapshotID = values["fresh_snapshot_id"] as? String,
              let verified = values["postcondition_verified"] as? Bool,
              boundedString(takeoverRef), boundedString(planRef), boundedString(freshSnapshotID),
              foregroundFragmentHashIsValid(fragmentHash),
              foregroundFragmentHashIsValid(stageHash),
              (0 ..< maximumForegroundFragmentStages).contains(stageIndex)
        else { return nil }
        return (takeoverRef, FragmentStageCommit(
            fragmentHash: fragmentHash,
            stageIndex: stageIndex,
            stageHash: stageHash,
            planRef: planRef,
            freshSnapshotID: freshSnapshotID,
            postconditionVerified: verified
        ))
    }

    private func decodeStageAuthority(
        _ values: [String: Any],
        inputSnapshotID: String
    ) -> FragmentStageAuthority? {
        guard let fragmentHash = values["fragment_hash"] as? String,
              let stageIndex = exactInteger(values["stage_index"]),
              let stageHash = values["stage_hash"] as? String,
              (0 ..< maximumForegroundFragmentStages).contains(stageIndex),
              foregroundFragmentHashIsValid(fragmentHash),
              foregroundFragmentHashIsValid(stageHash),
              boundedString(inputSnapshotID)
        else { return nil }
        return FragmentStageAuthority(
            fragmentHash: fragmentHash,
            stageIndex: stageIndex,
            stageHash: stageHash,
            inputSnapshotID: inputSnapshotID
        )
    }

    private func decodeFragmentRequirement(_ value: Any) -> ForegroundFragmentActionRequirement? {
        guard let raw = value as? [String: Any],
              Set(raw.keys) == ["backend", "action_class", "intent"],
              let backendLabel = raw["backend"] as? String,
              let backend = DispatchBackend(rawValue: backendLabel)
        else { return nil }
        let actionClass: DispatchActionClass?
        if raw["action_class"] is NSNull {
            actionClass = nil
        } else if let label = raw["action_class"] as? String {
            actionClass = DispatchActionClass(rawValue: label)
            guard actionClass != nil else { return nil }
        } else { return nil }
        let intent: SyntheticInputIntent?
        if raw["intent"] is NSNull {
            intent = nil
        } else if let label = raw["intent"] as? String {
            if label == "text_entry" {
                intent = .textEntry
            } else if label.hasPrefix("pointer:"),
                      let action = DispatchActionClass(rawValue: String(label.dropFirst(8))) {
                intent = .pointer(action)
            } else if label.hasPrefix("key_chord:"),
                      let chord = ApprovedKeyChord(rawValue: String(label.dropFirst(10))) {
                intent = .keyChord(chord)
            } else { return nil }
        } else { return nil }
        return ForegroundFragmentActionRequirement(
            backend: backend,
            actionClass: actionClass,
            intent: intent
        )
    }

    private func strictPayloadObject(_ request: HelperRequest) -> [String: Any]? {
        guard let value = try? JSONSerialization.jsonObject(with: request.payloadData),
              let object = value as? [String: Any]
        else { return nil }
        return object
    }

    private func exactInteger(_ value: Any?) -> Int? {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID(),
              !["f", "d"].contains(String(cString: number.objCType))
        else { return nil }
        return number.intValue == Int(number.doubleValue) ? number.intValue : nil
    }

    private func boundedString(_ value: String) -> Bool {
        !value.isEmpty && value.unicodeScalars.count <= 256
    }

    private func validateTakeoverEndPayload(_ payload: JSONValue) -> Bool {
        guard case let .object(values) = payload,
              Set(values.keys).isSubset(of: ["takeover_ref", "restore_previous_focus"]),
              boundedReference(values["takeover_ref"]) != nil else { return false }
        if let restore = values["restore_previous_focus"] {
            guard case .bool = restore else { return false }
        }
        return true
    }

    private func exactPayload(_ payload: JSONValue, keys: Set<String>) -> [String: JSONValue]? {
        guard case let .object(values) = payload, Set(values.keys) == keys else { return nil }
        return values
    }

    private func boundedReference(_ value: JSONValue?) -> String? {
        guard case let .string(reference)? = value,
              !reference.isEmpty,
              reference.unicodeScalars.count <= 256,
              reference.lengthOfBytes(using: .utf8) <= maxRequestLineBytes
        else { return nil }
        return reference
    }

    private func interactionMode(_ value: JSONValue?) -> InteractionMode? {
        guard case let .string(raw)? = value else { return nil }
        return InteractionMode(rawValue: raw)
    }

    private func validActions(_ value: JSONValue?) -> Bool {
        guard case let .array(actionValues)? = value, actionValues.count <= maximumNativeActions else { return false }
        do {
            _ = try actionValues.map(NativeAction.parse)
            return true
        } catch {
            return false
        }
    }

    private func stringPayload(_ payload: JSONValue, _ key: String) -> String? {
        guard case let .object(values) = payload, case let .string(value)? = values[key],
              !value.isEmpty, value.lengthOfBytes(using: .utf8) <= maxRequestLineBytes
        else { return nil }
        return value
    }
}

private func takeoverOutcomeJSON(_ outcome: TakeoverOutcome) -> JSONValue {
    .object([
        "started": .bool(outcome.started),
        "restoration": .string(outcome.restoration.rawValue),
    ])
}

private enum RequestParseOutcome {
    case request(HelperRequest)
    case invalid(requestID: String?)
}

private struct StrictRequestParser {
    private static let allowedFields: Set<String> = ["protocol_version", "request_id", "operation", "payload"]
    private var bytes: [UInt8]
    private var index = 0
    private var recognizedRequestID: String?

    init(data: Data) {
        bytes = [UInt8](data)
    }

    mutating func parse() -> RequestParseOutcome {
        do {
            return .request(try parseStrict())
        } catch {
            return .invalid(requestID: recognizedRequestID)
        }
    }

    private mutating func parseStrict() throws -> HelperRequest {
        skipWhitespace()
        try consume(0x7B)
        skipWhitespace()
        var fields: [String: Data] = [:]
        if peek() != 0x7D {
            while true {
                let keyData = try consumeString()
                let key = try JSONDecoder().decode(String.self, from: keyData)
                guard Self.allowedFields.contains(key) else {
                    throw ParseError.invalidFrame
                }
                guard fields[key] == nil else {
                    if key == "request_id" {
                        recognizedRequestID = nil
                    }
                    throw ParseError.invalidFrame
                }
                skipWhitespace()
                try consume(0x3A)
                skipWhitespace()
                let value = try consumeValue()
                fields[key] = value
                if key == "request_id" {
                    recognizedRequestID = boundedString(from: value, limit: maxRequestIDBytes)
                }
                skipWhitespace()
                if peek() == 0x2C {
                    index += 1
                    skipWhitespace()
                    continue
                }
                break
            }
        }
        try consume(0x7D)
        skipWhitespace()
        guard index == bytes.count,
              Set(fields.keys) == Self.allowedFields,
              let version = strictInteger(from: fields["protocol_version"]!),
              let requestID = boundedString(from: fields["request_id"]!, limit: maxRequestIDBytes),
              let operation = boundedString(from: fields["operation"]!, limit: maxOperationBytes),
              StrictProtocolJSON.validate(fields["payload"]!),
              let payload = try? JSONDecoder().decode(JSONValue.self, from: fields["payload"]!),
              case .object = payload
        else {
            throw ParseError.invalidFrame
        }
        return HelperRequest(
            protocolVersion: version,
            requestID: requestID,
            operation: operation,
            payload: payload,
            payloadData: fields["payload"]!
        )
    }

    private mutating func consumeValue() throws -> Data {
        guard let byte = peek() else {
            throw ParseError.invalidFrame
        }
        switch byte {
        case 0x22:
            return try consumeString()
        case 0x7B, 0x5B:
            return try consumeComposite()
        default:
            let start = index
            while let current = peek(), current != 0x2C, current != 0x7D {
                index += 1
            }
            guard index > start else {
                throw ParseError.invalidFrame
            }
            return Data(bytes[start..<index])
        }
    }

    private mutating func consumeComposite() throws -> Data {
        let start = index
        var expectedClosures: [UInt8] = []
        while let byte = peek() {
            if byte == 0x22 {
                _ = try consumeString()
                continue
            }
            switch byte {
            case 0x7B:
                expectedClosures.append(0x7D)
            case 0x5B:
                expectedClosures.append(0x5D)
            case 0x7D, 0x5D:
                guard expectedClosures.popLast() == byte else {
                    throw ParseError.invalidFrame
                }
                index += 1
                if expectedClosures.isEmpty {
                    return Data(bytes[start..<index])
                }
                continue
            default:
                break
            }
            index += 1
        }
        throw ParseError.invalidFrame
    }

    private mutating func consumeString() throws -> Data {
        let start = index
        try consume(0x22)
        while let byte = peek() {
            switch byte {
            case 0x22:
                index += 1
                return Data(bytes[start..<index])
            case 0x5C:
                index += 1
                guard peek() != nil else {
                    throw ParseError.invalidFrame
                }
                index += 1
            case 0x00...0x1F:
                throw ParseError.invalidFrame
            default:
                index += 1
            }
        }
        throw ParseError.invalidFrame
    }

    private mutating func consume(_ expected: UInt8) throws {
        guard peek() == expected else {
            throw ParseError.invalidFrame
        }
        index += 1
    }

    private mutating func skipWhitespace() {
        while let byte = peek(), byte == 0x20 || byte == 0x09 || byte == 0x0A || byte == 0x0D {
            index += 1
        }
    }

    private func peek() -> UInt8? {
        index < bytes.count ? bytes[index] : nil
    }

    private func strictInteger(from data: Data) -> Int? {
        let value = [UInt8](trimmed(data))
        guard !value.isEmpty else {
            return nil
        }
        var start = 0
        if value[start] == 0x2D {
            start += 1
        }
        guard start < value.count else {
            return nil
        }
        if value[start] == 0x30 {
            guard start + 1 == value.count else {
                return nil
            }
        } else {
            guard value[start] >= 0x31, value[start] <= 0x39 else {
                return nil
            }
            if start + 1 < value.count,
               !value[(start + 1)...].allSatisfy({ $0 >= 0x30 && $0 <= 0x39 })
            {
                return nil
            }
        }
        return Int(String(decoding: value, as: UTF8.self))
    }

    private func boundedString(from data: Data, limit: Int) -> String? {
        guard let value = try? JSONDecoder().decode(String.self, from: data),
              !value.isEmpty,
              value.lengthOfBytes(using: .utf8) <= limit
        else {
            return nil
        }
        return value
    }

    private func trimmed(_ data: Data) -> Data {
        let value = [UInt8](data)
        var start = 0
        var end = value.count
        while start < end && isWhitespace(value[start]) {
            start += 1
        }
        while end > start && isWhitespace(value[end - 1]) {
            end -= 1
        }
        return Data(value[start..<end])
    }

    private func isWhitespace(_ byte: UInt8) -> Bool {
        byte == 0x20 || byte == 0x09 || byte == 0x0A || byte == 0x0D
    }

    private enum ParseError: Error {
        case invalidFrame
    }
}

private struct StrictProtocolJSON {
    private static let maximumNestingDepth = 32
    private let bytes: [UInt8]
    private var index = 0

    static func validate(_ data: Data) -> Bool {
        var parser = Self(bytes: Array(data))
        do {
            try parser.parseValue(depth: 0)
            parser.skipWhitespace()
            return parser.index == parser.bytes.count
        } catch {
            return false
        }
    }

    private init(bytes: [UInt8]) { self.bytes = bytes }

    private mutating func parseValue(depth: Int) throws {
        guard depth <= Self.maximumNestingDepth else { throw ParseError.invalid }
        skipWhitespace()
        guard let byte = current else { throw ParseError.invalid }
        switch byte {
        case 0x7B: try parseObject(depth: depth)
        case 0x5B: try parseArray(depth: depth)
        case 0x22: _ = try parseString()
        case 0x74: try consume("true")
        case 0x66: try consume("false")
        case 0x6E: try consume("null")
        case 0x2D, 0x30 ... 0x39: try parseNumber()
        default: throw ParseError.invalid
        }
    }

    private mutating func parseObject(depth: Int) throws {
        index += 1
        skipWhitespace()
        if consumeIf(0x7D) { return }
        var keys = Set<String>()
        while true {
            skipWhitespace()
            let key = try parseString()
            guard keys.insert(key).inserted else { throw ParseError.invalid }
            skipWhitespace()
            guard consumeIf(0x3A) else { throw ParseError.invalid }
            try parseValue(depth: depth + 1)
            skipWhitespace()
            if consumeIf(0x7D) { return }
            guard consumeIf(0x2C) else { throw ParseError.invalid }
        }
    }

    private mutating func parseArray(depth: Int) throws {
        index += 1
        skipWhitespace()
        if consumeIf(0x5D) { return }
        while true {
            try parseValue(depth: depth + 1)
            skipWhitespace()
            if consumeIf(0x5D) { return }
            guard consumeIf(0x2C) else { throw ParseError.invalid }
        }
    }

    private mutating func parseString() throws -> String {
        guard current == 0x22 else { throw ParseError.invalid }
        let start = index
        index += 1
        while let byte = current {
            if byte == 0x22 {
                index += 1
                let data = Data(bytes[start..<index])
                guard let value = try? JSONDecoder().decode(String.self, from: data) else {
                    throw ParseError.invalid
                }
                return value
            }
            if byte == 0x5C {
                index += 1
                guard current != nil else { throw ParseError.invalid }
            } else if byte < 0x20 {
                throw ParseError.invalid
            }
            index += 1
        }
        throw ParseError.invalid
    }

    private mutating func parseNumber() throws {
        if consumeIf(0x2D), current == nil { throw ParseError.invalid }
        if consumeIf(0x30) {
            if let byte = current, (0x30 ... 0x39).contains(byte) { throw ParseError.invalid }
        } else {
            guard current.map({ (0x31 ... 0x39).contains($0) }) == true else { throw ParseError.invalid }
            repeat { index += 1 } while current.map { (0x30 ... 0x39).contains($0) } == true
        }
        if consumeIf(0x2E) {
            guard current.map({ (0x30 ... 0x39).contains($0) }) == true else { throw ParseError.invalid }
            repeat { index += 1 } while current.map { (0x30 ... 0x39).contains($0) } == true
        }
        if current == 0x65 || current == 0x45 {
            index += 1
            if current == 0x2B || current == 0x2D { index += 1 }
            guard current.map({ (0x30 ... 0x39).contains($0) }) == true else { throw ParseError.invalid }
            repeat { index += 1 } while current.map { (0x30 ... 0x39).contains($0) } == true
        }
    }

    private mutating func consume(_ literal: String) throws {
        let value = Array(literal.utf8)
        guard index + value.count <= bytes.count,
              Array(bytes[index..<(index + value.count)]) == value
        else { throw ParseError.invalid }
        index += value.count
    }

    private mutating func consumeIf(_ byte: UInt8) -> Bool {
        guard current == byte else { return false }
        index += 1
        return true
    }

    private mutating func skipWhitespace() {
        while current.map({ [0x20, 0x09, 0x0A, 0x0D].contains($0) }) == true { index += 1 }
    }

    private var current: UInt8? { index < bytes.count ? bytes[index] : nil }
    private enum ParseError: Error { case invalid }
}
