import CoreGraphics

struct KeyboardTextContinuationObservation {
    let focus: KeyboardFocusObservation
    let observationRequired: Bool
}

func provenContinuingTextFocus<Element>(
    expectedPreference: FocusedRootPreference, observedPreference: FocusedRootPreference,
    wanted: KeyboardFocusAuthority?, windowBounds: CGRect,
    readFocused: () -> Element?, observe: (Element) -> KeyboardFocusObservation,
    belongs: (Element) -> Bool, same: (Element, Element) -> Bool
) -> KeyboardFocusObservation? {
    guard let pinned = readFocused(),
          continuingTextRootIsProven(expectedPreference: expectedPreference,
            observedPreference: observedPreference, wanted: wanted, observedFocus: observe(pinned),
            belongsToSelectedRoot: belongs(pinned), windowBounds: windowBounds),
          let current = readFocused(), same(pinned, current)
    else { return nil }
    // Both identity and ancestry are about this retained object, never another
    // sample of the application's mutable focused-element attribute.
    let finalObservation = observe(pinned)
    guard continuingTextRootIsProven(expectedPreference: expectedPreference,
        observedPreference: observedPreference, wanted: wanted, observedFocus: finalObservation,
        belongsToSelectedRoot: belongs(pinned), windowBounds: windowBounds)
    else { return nil }
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
    guard expectedPreference == .selectedWindow, observedPreference == .containedOverlay,
          belongsToSelectedRoot, let wanted, !wanted.isSecure,
          ["AXTextField", "AXTextArea"].contains(wanted.role),
          observedFocus == .authority(wanted)
    else { return false }
    return keyboardFocusIdentityAndGeometryAreTrusted(
        identityToken: wanted.identityToken, bounds: wanted.bounds, containerBounds: windowBounds
    )
}
