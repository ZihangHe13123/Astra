import Foundation

enum InteractionMode: String, Codable, Equatable {
    case background
    case foregroundTakeover = "foreground_takeover"
}

enum DispatchBackend: String, Codable, Equatable {
    case axPress = "ax_press"
    case axIncrement = "ax_increment"
    case axDecrement = "ax_decrement"
    case axSelectedText = "ax_selected_text"
    case pidPointer = "pid_pointer"
    case foregroundPointer = "foreground_pointer"
    case pidKeyboard = "pid_keyboard"
    case foregroundKeyboard = "foreground_keyboard"
    case wait
}

enum DispatchActionClass: String, Codable, Equatable {
    case press
    case text
    case click
    case doubleClick = "double_click"
    case scroll
    case drag
}

struct PlannedSyntheticRequirement: Equatable {
    let backend: DispatchBackend
    let actionClass: DispatchActionClass
    let intent: SyntheticInputIntent
}

enum TakeoverRestoration: String, Codable, Equatable {
    case notStarted = "not_started"
    case restored
    case preservedUserFocus = "preserved_user_focus"
    case restoreFailed = "restore_failed"
}

enum CooperativeErrorCode: String, Codable, Equatable {
    case foregroundTakeoverRequired = "foreground_takeover_required"
    case requiresActiveForegroundTakeover = "requires_active_foreground_takeover"
    case backgroundActionUnsupported = "background_action_unsupported"
    case userActivityPaused = "user_activity_paused"
    case observationRequired = "observation_required"
    case sidecarFailed = "sidecar_failed"
}

struct DispatchPlanSummary: Codable, Equatable {
    let planRef: String
    let interactionMode: InteractionMode
    let requiresTakeover: Bool
    let reason: String
    let actionClasses: [DispatchActionClass]
    let lastAcknowledgedAction: Int
    var plannedPIDActionClasses: [DispatchActionClass]? = nil

    enum CodingKeys: String, CodingKey {
        case planRef = "plan_ref"
        case interactionMode = "interaction_mode"
        case requiresTakeover = "requires_takeover"
        case reason
        case actionClasses = "action_classes"
        case lastAcknowledgedAction = "last_acknowledged_action"
    }

    func asJSON() -> JSONValue {
        .object([
            "plan_ref": .string(planRef),
            "interaction_mode": .string(interactionMode.rawValue),
            "requires_takeover": .bool(requiresTakeover),
            "reason": .string(reason),
            "action_classes": .array(actionClasses.map { .string($0.rawValue) }),
            "last_acknowledged_action": .number(Double(lastAcknowledgedAction)),
        ])
    }
}

struct TakeoverOutcome: Codable, Equatable {
    let started: Bool
    let restoration: TakeoverRestoration
    let cleanupFailed: Bool
    let presented: Bool

    init(
        started: Bool,
        restoration: TakeoverRestoration,
        cleanupFailed: Bool = false,
        presented: Bool = false
    ) {
        self.started = started
        self.restoration = restoration
        self.cleanupFailed = cleanupFailed
        self.presented = presented
    }

    var isValid: Bool {
        started || restoration == .notStarted
    }
}

struct FragmentStageCommitOutcome: Equatable {
    let terminal: Bool
    let takeover: TakeoverOutcome?
}
