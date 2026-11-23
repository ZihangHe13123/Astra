import Foundation

struct PIDTargetApplication: Equatable {
    let bundleIdentifier: String
    let version: String
}

struct PIDPointerDeliveryCapability: Equatable {
    let isAvailable: Bool

    static let unavailable = Self(isAvailable: false)
    static let experimentalAvailable = Self(isAvailable: true)
    static let available = Self(isAvailable: true)
}

struct ForegroundKeyboardDeliveryCapability: Equatable {
    let isAvailable: Bool

    static let unavailable = Self(isAvailable: false)
    static let available = Self(isAvailable: true)
}

func canonicalComputerKey(_ value: String) -> String {
    switch value.lowercased() {
    case "arrowleft": "left"
    case "arrowright": "right"
    case "arrowup": "up"
    case "arrowdown": "down"
    case "enter": "return"
    case "esc": "escape"
    default: value.lowercased()
    }
}

struct ApprovedKeyChord: RawRepresentable, Hashable, Equatable {
    private static let maximumScalars = 128
    private static let modifierOrder = ["command", "control", "option", "shift", "function", "caps_lock"]
    private static let allowedKeys: Set<String> = [
        "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m", "n", "o", "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z",
        "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
        "=", "-", "]", "[", "'", ";", "\\", ",", "/", ".",
        "return", "tab", "space", "delete", "escape", "left", "right", "down", "up",
    ]

    let rawValue: String

    static func supportsKeyName(_ value: String) -> Bool {
        allowedKeys.contains(canonicalComputerKey(value))
    }

    init?(rawValue: String) {
        guard !rawValue.isEmpty,
              rawValue.unicodeScalars.count <= Self.maximumScalars,
              rawValue == rawValue.lowercased()
        else { return nil }
        let components = rawValue.split(separator: "+", omittingEmptySubsequences: false).map(String.init)
        guard let key = components.last, Self.allowedKeys.contains(key) else { return nil }
        let modifiers = Array(components.dropLast())
        guard Set(modifiers).count == modifiers.count,
              modifiers.allSatisfy(Self.modifierOrder.contains),
              modifiers == Self.modifierOrder.filter(Set(modifiers).contains),
              !(key == "v" && modifiers.contains("command"))
        else { return nil }
        self.rawValue = rawValue
    }

    init?(action: NativeAction) {
        guard action.kind == .keypress, let rawKey = action.key else { return nil }
        let key = canonicalComputerKey(rawKey)
        guard Self.allowedKeys.contains(key),
              Set(action.modifiers).count == action.modifiers.count,
              action.modifiers.allSatisfy(Self.modifierOrder.contains),
              !(key == "v" && action.modifiers.contains("command"))
        else { return nil }
        let modifierSet = Set(action.modifiers)
        let canonical = (Self.modifierOrder.filter(modifierSet.contains) + [key]).joined(separator: "+")
        self.init(rawValue: canonical)
    }
}

enum SyntheticInputIntent: Equatable {
    case pointer(DispatchActionClass)
    case keyChord(ApprovedKeyChord)
    case textEntry
}

struct SyntheticInputPlanningPolicy {
    let pointerCapability: PIDPointerDeliveryCapability
    let keyboardCapability: ForegroundKeyboardDeliveryCapability
    let registry: PIDInputCompatibilityRegistry
    /// Explicit opt-in for the background delivery family. Off by default so
    /// the existing takeover contracts keep their behavior until the
    /// capability is deliberately enabled (per manifest review).
    let backgroundDeliveryEnabled: Bool
    let genericForegroundEnabled: Bool

    init(
        pointerCapability: PIDPointerDeliveryCapability,
        keyboardCapability: ForegroundKeyboardDeliveryCapability,
        registry: PIDInputCompatibilityRegistry,
        backgroundDeliveryEnabled: Bool = false,
        genericForegroundEnabled: Bool = false
    ) {
        self.pointerCapability = pointerCapability
        self.keyboardCapability = keyboardCapability
        self.registry = registry
        self.backgroundDeliveryEnabled = backgroundDeliveryEnabled
        self.genericForegroundEnabled = genericForegroundEnabled
    }

    func allowsForeground(application: PIDTargetApplication?, intents: [SyntheticInputIntent]) -> Bool {
        guard genericForegroundEnabled, let application,
              !application.bundleIdentifier.isEmpty, !application.version.isEmpty else { return false }
        return intents.allSatisfy {
            switch $0 {
            case let .pointer(action):
                return pointerCapability.isAvailable && [.click, .doubleClick, .scroll, .drag].contains(action)
            case .textEntry, .keyChord:
                return keyboardCapability.isAvailable
            }
        }
    }

    func allows(application: PIDTargetApplication?, intents: [SyntheticInputIntent]) -> Bool {
        guard !intents.isEmpty else { return true }
        guard let application else {
            logSippDenied("no application")
            return false
        }
        return intents.allSatisfy { intent in
            switch intent {
            case let .pointer(action):
                return pointerCapability.isAvailable && registry.allows(
                    application: application,
                    backend: .pidPointer,
                    action: action
                )
            case let .keyChord(chord):
                let allowed = keyboardCapability.isAvailable &&
                    (registry.allows(application: application, backend: .foregroundKeyboard, action: .text) ||
                        registry.allows(application: application, backend: .pidKeyboard, action: .text)) &&
                    registry.allows(application: application, keyChord: chord)
                if !allowed {
                    logSippDenied("keyChord \(chord.rawValue) app=\(application.bundleIdentifier) v=\(application.version) kbAvail=\(keyboardCapability.isAvailable) fg=\(registry.allows(application: application, backend: .foregroundKeyboard, action: .text)) pidKb=\(registry.allows(application: application, backend: .pidKeyboard, action: .text))")
                }
                return allowed
            case .textEntry:
                let allowed = keyboardCapability.isAvailable &&
                    (registry.allows(application: application, backend: .foregroundKeyboard, action: .text) ||
                        registry.allows(application: application, backend: .pidKeyboard, action: .text)) &&
                    registry.allowsTextEntry(application: application)
                if !allowed {
                    logSippDenied("textEntry app=\(application.bundleIdentifier) v=\(application.version)")
                }
                return allowed
            }
        }
    }

    /// Manifest gate for the background delivery family: only applications
    /// whose compatibility cell explicitly declares the backend+action may
    /// receive a no-takeover targeted PID delivery. Unlisted applications
    /// keep the existing requiresTakeover=true behavior.
    func allowsBackgroundDelivery(
        application: PIDTargetApplication?,
        backend: DispatchBackend,
        action: DispatchActionClass
    ) -> Bool {
        guard backgroundDeliveryEnabled else { return false }
        guard let application else { return false }
        switch backend {
        case .pidPointer:
            // requires_active 的 app 仅在 active 前台才处理合成指针事件:
            // 计划阶段强制走 foreground takeover 激活路径, 不做 background 直投
            // (see docs/macos-computer-use.md#input-delivery-contracts)
            if pointerRequiresActive(application: application) {
                return false
            }
            return pointerCapability.isAvailable &&
                registry.allows(application: application, backend: .pidPointer, action: action)
        case .pidKeyboard:
            // The background executor currently supports pointer families
            // only; keyboard background delivery stays on the takeover path
            // so a plan never exists that cannot be executed.
            return false
        default:
            return false
        }
    }

    /// App-level declaration: pointer events only take effect while the
    /// application is the active foreground app (plan-stage routing to the
    /// foreground takeover activation path).
    func pointerRequiresActive(application: PIDTargetApplication?) -> Bool {
        guard let application else { return false }
        return registry.pointerRequiresActive(
            bundleIdentifier: application.bundleIdentifier,
            version: application.version
        )
    }
}

private func logSippDenied(_ detail: String) {
    let line = "[sipp_denied] \(detail)\n"
    if let handle = fopen("/tmp/astra-sipp-diagnostics.log", "a") {
        _ = fputs(line, handle)
        fclose(handle)
    }
}

struct PIDPointerPlanningPolicy {
    let capability: PIDPointerDeliveryCapability
    let registry: PIDInputCompatibilityRegistry

    func allows(application: PIDTargetApplication?, actions: [DispatchActionClass]) -> Bool {
        guard !actions.isEmpty else { return true }
        guard capability.isAvailable, let application else { return false }
        return actions.allSatisfy { registry.allows(application: application, action: $0) }
    }
}

struct PIDInputCompatibilityCell: Equatable {
    let bundleIdentifier: String
    let version: String
    let backend: DispatchBackend
    let action: DispatchActionClass
    let allowedKeyChords: Set<ApprovedKeyChord>
    let allowTextEntry: Bool
    let requiresActive: Bool

    init(
        bundleIdentifier: String,
        version: String,
        backend: DispatchBackend,
        action: DispatchActionClass,
        allowedKeyChords: Set<ApprovedKeyChord> = [],
        allowTextEntry: Bool = false,
        requiresActive: Bool = false
    ) {
        self.bundleIdentifier = bundleIdentifier
        self.version = version
        self.backend = backend
        self.action = action
        self.allowedKeyChords = allowedKeyChords
        self.allowTextEntry = allowTextEntry
        self.requiresActive = requiresActive
    }

    init(bundleIdentifier: String, version: String, action: DispatchActionClass) {
        self.init(bundleIdentifier: bundleIdentifier, version: version, backend: .pidPointer, action: action)
    }
}

struct PIDInputCompatibilityRegistry {
    private static let maximumBytes = 64 * 1024
    private static let maximumApplications = 128
    private static let maximumCapabilitiesPerApplication = 2
    private static let maximumKeyChordsPerCapability = 32
    private static let maximumBundleIdentifierScalars = 255
    private static let maximumVersionScalars = 64
    private let cells: [PIDInputCompatibilityCell]

    init(cells: [PIDInputCompatibilityCell] = []) {
        self.cells = cells
    }

    init(bytes: Data?) {
        cells = Self.decode(bytes) ?? []
    }

    static func bundled() -> Self {
        guard let url = Bundle.main.url(
            forResource: "macos_computer_compatibility",
            withExtension: "json"
        ) else { return Self() }
        return Self(bytes: try? Data(contentsOf: url, options: [.mappedIfSafe]))
    }

    func allows(application: PIDTargetApplication, action: DispatchActionClass) -> Bool {
        allows(application: application, backend: .pidPointer, action: action)
    }

    func allows(
        application: PIDTargetApplication,
        backend: DispatchBackend,
        action: DispatchActionClass
    ) -> Bool {
        guard !application.bundleIdentifier.isEmpty, !application.version.isEmpty else { return false }
        return cells.contains {
            $0.bundleIdentifier == application.bundleIdentifier &&
                $0.version == application.version &&
                $0.backend == backend &&
                $0.action == action
        }
    }

    func allows(application: PIDTargetApplication, keyChord: ApprovedKeyChord) -> Bool {
        guard !application.bundleIdentifier.isEmpty, !application.version.isEmpty else { return false }
        return cells.contains {
            $0.bundleIdentifier == application.bundleIdentifier &&
                $0.version == application.version &&
                ($0.backend == .foregroundKeyboard || $0.backend == .pidKeyboard) &&
                $0.action == .text &&
                $0.allowedKeyChords.contains(keyChord)
        }
    }

    func allowsTextEntry(application: PIDTargetApplication) -> Bool {
        guard !application.bundleIdentifier.isEmpty, !application.version.isEmpty else { return false }
        return cells.contains {
            $0.bundleIdentifier == application.bundleIdentifier &&
                $0.version == application.version &&
                ($0.backend == .foregroundKeyboard || $0.backend == .pidKeyboard) &&
                $0.action == .text &&
                $0.allowTextEntry
        }
    }

    /// True when the exact bundle/version cell declares requires_active —
    /// pointer events only take effect while the app is the active foreground
    /// app, so the planner must route through the takeover activation path.
    func pointerRequiresActive(bundleIdentifier: String, version: String) -> Bool {
        guard !bundleIdentifier.isEmpty, !version.isEmpty else { return false }
        return cells.contains {
            $0.bundleIdentifier == bundleIdentifier
                && $0.version == version
                && $0.backend == .pidPointer
                && $0.requiresActive
        }
    }

    /// True when the manifest declares at least one pid_pointer cell — the
    /// production signal that background delivery may be enabled for the
    /// listed applications.
    var hasPointerClaims: Bool {
        cells.contains { $0.backend == .pidPointer }
    }

    private static func decode(_ bytes: Data?) -> [PIDInputCompatibilityCell]? {
        guard let bytes, bytes.count <= maximumBytes,
              StrictCompatibilityJSON.validate(bytes),
              let document = try? JSONSerialization.jsonObject(with: bytes),
              let root = document as? [String: Any],
              Set(root.keys) == ["schema_version", "applications"],
              exactInteger(root["schema_version"], equals: 2),
              let applications = root["applications"] as? [Any],
              applications.count <= maximumApplications
        else { return nil }

        var identities = Set<String>()
        var decoded: [PIDInputCompatibilityCell] = []
        for value in applications {
            guard let application = value as? [String: Any],
                  Set(application.keys) == ["bundle_id", "app_version", "capabilities"],
                  let bundleIdentifier = application["bundle_id"] as? String,
                  validBundleIdentifier(bundleIdentifier),
                  let version = application["app_version"] as? String,
                  validVersion(version),
                  let capabilities = application["capabilities"] as? [Any],
                  !capabilities.isEmpty,
                  capabilities.count <= maximumCapabilitiesPerApplication
            else { return nil }
            let identity = "\(bundleIdentifier)\u{0}\(version)"
            guard identities.insert(identity).inserted else { return nil }
            var backends = Set<String>()
            for value in capabilities {
                guard let capability = value as? [String: Any],
                      let backendLabel = capability["backend"] as? String, backends.insert(backendLabel).inserted
                else { return nil }
                if backendLabel == "wps_panel_ax" {
                    guard bundleIdentifier == "com.kingsoft.wpsoffice.mac", version == "12.1.26026",
                          Set(capability.keys) == ["backend", "enabled_actions"],
                          let actions = capability["enabled_actions"] as? [String],
                          actions.count == 2, Set(actions) == ["text", "press"] else { return nil }
                    decoded.append(PIDInputCompatibilityCell(bundleIdentifier: bundleIdentifier, version: version,
                                                             backend: .axSelectedText, action: .text))
                    decoded.append(PIDInputCompatibilityCell(bundleIdentifier: bundleIdentifier, version: version,
                                                             backend: .axPress, action: .press))
                    continue
                }
                guard let backend = DispatchBackend(rawValue: backendLabel),
                      [.pidPointer, .pidKeyboard, .foregroundKeyboard].contains(backend) else { return nil }
                switch backend {
                case .pidPointer:
                    guard Set(capability.keys).isSubset(of: ["backend", "enabled_actions", "requires_active"]),
                          let actions = decodePointerActions(capability["enabled_actions"]),
                          let requiresActive = exactOptionalBoolean(capability["requires_active"])
                    else { return nil }
                    for action in actions {
                        let cell = PIDInputCompatibilityCell(
                            bundleIdentifier: bundleIdentifier,
                            version: version,
                            backend: backend,
                            action: action,
                            requiresActive: requiresActive
                        )
                        decoded.append(cell)
                    }
                case .foregroundKeyboard:
                    guard Set(capability.keys) == ["backend", "enabled_actions", "allowed_key_chords", "allow_text_entry"],
                          let actions = capability["enabled_actions"] as? [Any],
                          actions.count == 1,
                          actions.first as? String == DispatchActionClass.text.rawValue,
                          let chordValues = capability["allowed_key_chords"] as? [Any],
                          chordValues.count <= maximumKeyChordsPerCapability,
                          let allowTextEntry = exactBoolean(capability["allow_text_entry"])
                    else { return nil }
                    var chords = Set<ApprovedKeyChord>()
                    for chordValue in chordValues {
                        guard let label = chordValue as? String,
                              let chord = ApprovedKeyChord(rawValue: label),
                              chords.insert(chord).inserted
                        else { return nil }
                    }
                    decoded.append(PIDInputCompatibilityCell(
                        bundleIdentifier: bundleIdentifier,
                        version: version,
                        backend: backend,
                        action: .text,
                        allowedKeyChords: chords,
                        allowTextEntry: allowTextEntry
                    ))
                case .pidKeyboard:
                    guard Set(capability.keys) == ["backend", "enabled_actions", "allowed_key_chords", "allow_text_entry"],
                          let actions = capability["enabled_actions"] as? [Any],
                          actions.count == 1,
                          actions.first as? String == DispatchActionClass.text.rawValue,
                          let chordValues = capability["allowed_key_chords"] as? [Any],
                          chordValues.count <= maximumKeyChordsPerCapability,
                          let allowTextEntry = exactBoolean(capability["allow_text_entry"])
                    else { return nil }
                    var chords = Set<ApprovedKeyChord>()
                    for chordValue in chordValues {
                        guard let label = chordValue as? String,
                              let chord = ApprovedKeyChord(rawValue: label),
                              chords.insert(chord).inserted
                        else { return nil }
                    }
                    decoded.append(PIDInputCompatibilityCell(
                        bundleIdentifier: bundleIdentifier,
                        version: version,
                        backend: backend,
                        action: .text,
                        allowedKeyChords: chords,
                        allowTextEntry: allowTextEntry
                    ))
                case .foregroundPointer, .axPress, .axIncrement, .axDecrement, .axSelectedText, .wait:
                    return nil
                }
            }
        }
        return decoded
    }

    private static func decodePointerActions(_ value: Any?) -> [DispatchActionClass]? {
        guard let values = value as? [Any], !values.isEmpty, values.count <= 4 else { return nil }
        let allowed: Set<DispatchActionClass> = [.click, .doubleClick, .scroll, .drag]
        var actions = Set<DispatchActionClass>()
        for value in values {
            guard let label = value as? String,
                  let action = DispatchActionClass(rawValue: label),
                  allowed.contains(action),
                  actions.insert(action).inserted
            else { return nil }
        }
        return values.compactMap { ($0 as? String).flatMap(DispatchActionClass.init(rawValue:)) }
    }

    private static func exactInteger(_ value: Any?, equals expected: Int) -> Bool {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID(),
              !["f", "d"].contains(String(cString: number.objCType))
        else { return false }
        return number.intValue == expected && number.doubleValue == Double(expected)
    }

    private static func exactBoolean(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) == CFBooleanGetTypeID()
        else { return nil }
        return number.boolValue
    }

    private static func exactOptionalBoolean(_ value: Any?) -> Bool? {
        guard let value else { return false }
        return exactBoolean(value)
    }

    private static func validBundleIdentifier(_ value: String) -> Bool {
        guard !value.isEmpty, value.unicodeScalars.count <= maximumBundleIdentifierScalars else { return false }
        return value.range(
            of: #"\A[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9][A-Za-z0-9-]*)+\z"#,
            options: .regularExpression
        ) != nil
    }

    private static func validVersion(_ value: String) -> Bool {
        guard !value.isEmpty, value.unicodeScalars.count <= maximumVersionScalars else { return false }
        return value.range(
            of: #"\A[0-9]+(?:\.[0-9]+){0,7}(?:[-+][A-Za-z0-9][A-Za-z0-9.-]{0,31})?\z"#,
            options: .regularExpression
        ) != nil
    }
}

private struct StrictCompatibilityJSON {
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
                let encoded = Data(bytes[start..<index])
                guard let decoded = try? JSONDecoder().decode(String.self, from: encoded) else {
                    throw ParseError.invalid
                }
                return decoded
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
        let start = index
        if consumeIf(0x2D), current == nil { throw ParseError.invalid }
        if consumeIf(0x30) {
            if let byte = current, (0x30 ... 0x39).contains(byte) { throw ParseError.invalid }
        } else {
            guard let byte = current, (0x31 ... 0x39).contains(byte) else { throw ParseError.invalid }
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
        guard index > start else { throw ParseError.invalid }
    }

    private mutating func consume(_ literal: String) throws {
        let literalBytes = Array(literal.utf8)
        guard index + literalBytes.count <= bytes.count,
              Array(bytes[index..<(index + literalBytes.count)]) == literalBytes
        else { throw ParseError.invalid }
        index += literalBytes.count
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
