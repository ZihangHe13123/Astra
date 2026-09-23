import CoreGraphics

struct KeyboardTextContinuationObservation {
    let focus: KeyboardFocusObservation
    let observationRequired: Bool
}

func provenContinuingTextFocus<Element>(
    expectedPreference: FocusedRootPreference, observedPreference: FocusedRootPreference,
    wanted: KeyboardFocusAuthority?, windowBounds: CGRect,
    readFocused: () -> Element?, observe: (Element) -> KeyboardFocusObservation,
    belongs: (Element) -> Bool, same: (Element, Element) -> Bool,
    reject: (String) -> Void = { _ in }
) -> KeyboardFocusObservation? {
    guard let pinned = readFocused() else {
        reject("focus_unreadable")
        return nil
    }
    if let reason = continuingTextRejectionReason(expectedPreference: expectedPreference,
        observedPreference: observedPreference, wanted: wanted, observedFocus: observe(pinned),
        belongsToSelectedRoot: belongs(pinned), windowBounds: windowBounds) {
        reject(reason)
        return nil
    }
    guard let current = readFocused(), same(pinned, current) else {
        reject("focus_moved_during_proof")
        return nil
    }
    // Both identity and ancestry are about this retained object, never another
    // sample of the application's mutable focused-element attribute.
    let finalObservation = observe(pinned)
    if let reason = continuingTextRejectionReason(expectedPreference: expectedPreference,
        observedPreference: observedPreference, wanted: wanted, observedFocus: finalObservation,
        belongsToSelectedRoot: belongs(pinned), windowBounds: windowBounds) {
        reject(reason)
        return nil
    }
    return finalObservation
}

func continuingTextRootIsProven(
    expectedPreference: FocusedRootPreference,
    observedPreference: FocusedRootPreference,
    wanted: KeyboardFocusAuthority?,
    observedFocus: KeyboardFocusObservation,
    belongsToSelectedRoot: Bool,
    windowBounds: CGRect
) -> Bool {
    continuingTextRejectionReason(expectedPreference: expectedPreference, observedPreference: observedPreference,
        wanted: wanted, observedFocus: observedFocus, belongsToSelectedRoot: belongsToSelectedRoot,
        windowBounds: windowBounds) == nil
}

/// Names the first failed proof, so live rejections are diagnosable without any text content.
func continuingTextRejectionReason(
    expectedPreference: FocusedRootPreference,
    observedPreference: FocusedRootPreference,
    wanted: KeyboardFocusAuthority?,
    observedFocus: KeyboardFocusObservation,
    belongsToSelectedRoot: Bool,
    windowBounds: CGRect
) -> String? {
    guard expectedPreference == .selectedWindow, observedPreference == .containedOverlay else {
        return "root_preference"
    }
    guard let wanted, !wanted.isSecure, ["AXTextField", "AXTextArea"].contains(wanted.role) else {
        return "wanted_not_text_field"
    }
    guard belongsToSelectedRoot else { return "focus_outside_selected_root" }
    guard case let .authority(current) = observedFocus else { return "focus_not_authority" }
    guard current.identityToken == wanted.identityToken, current.role == wanted.role,
          current.subrole == wanted.subrole, !current.isSecure
    else { return "focus_identity_changed" }
    // An autocomplete window can restyle the same field (live: Edge's omnibox shifts while its
    // suggestions open). Same identity and role inside the validated window is the proof the
    // ordinary text path already accepts for geometry transitions.
    guard keyboardFocusIdentityAndGeometryAreTrusted(
        identityToken: current.identityToken, bounds: current.bounds, containerBounds: windowBounds
    ) else { return "focus_geometry_untrusted" }
    return nil
}
