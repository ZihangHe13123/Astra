import CoreGraphics
import Testing
@testable import AstraMacComputerHelperCore

private let continuityBounds = CGRect(x: -500, y: 20, width: 500, height: 400)
private let continuityField = KeyboardFocusAuthority(identityToken: "ax:field",
    bounds: CGRect(x: -480, y: 40, width: 200, height: 30), role: "AXTextField", subrole: nil)

@Test func KeyboardTextContinuityPinsIdentityAndAncestryToOneElement() {
    for scenario in [(true, ["wanted", "wanted"]), (true, ["wanted", "other", "wanted"]),
                     (false, ["wanted", "other", "wanted"])] {
        var samples = scenario.1
        var ownershipSamples: [String] = []
        let result = provenContinuingTextFocus(expectedPreference: .selectedWindow,
            observedPreference: .containedOverlay, wanted: continuityField, windowBounds: continuityBounds,
            readFocused: { samples.isEmpty ? nil : samples.removeFirst() },
            observe: { _ in .authority(continuityField) },
            belongs: { element in
                ownershipSamples.append(element)
                return element == "other" || scenario.0
            }, same: { $0 == $1 })
        let valid = scenario.0 && !scenario.1.contains("other")
        #expect(result == (valid ? .authority(continuityField) : nil))
        #expect(ownershipSamples.allSatisfy { $0 == "wanted" })
    }
}

@Test func KeyboardTextContinuityRechecksTheRetainedElementsSafety() {
    var reads = 0
    let result = provenContinuingTextFocus(expectedPreference: .selectedWindow,
        observedPreference: .containedOverlay, wanted: continuityField, windowBounds: continuityBounds,
        readFocused: { "wanted" }, observe: { _ in
            reads += 1
            return reads == 1 ? .authority(continuityField) : .secure
        }, belongs: { _ in true }, same: { $0 == $1 })
    #expect(result == nil)
    #expect(reads == 2)
}

@Test func KeyboardTextContinuityRequiresExactFieldAndOwnedRoot() {
    #expect(continuingTextRootIsProven(expectedPreference: .selectedWindow,
        observedPreference: .containedOverlay, wanted: continuityField,
        observedFocus: .authority(continuityField), belongsToSelectedRoot: true,
        windowBounds: continuityBounds))
    #expect(!continuingTextRootIsProven(expectedPreference: .selectedWindow,
        observedPreference: .containedOverlay, wanted: continuityField,
        observedFocus: .authority(continuityField), belongsToSelectedRoot: false,
        windowBounds: continuityBounds))
    #expect(!continuingTextRootIsProven(expectedPreference: .selectedWindow,
        observedPreference: .containedOverlay, wanted: nil,
        observedFocus: .authority(continuityField), belongsToSelectedRoot: true,
        windowBounds: continuityBounds))
}

@Test func KeyboardTextContinuityAcceptsTheSameFieldRestyledByItsAutocomplete() {
    // Live Edge 153: the omnibox field shifts and widens while its suggestion window opens.
    let restyled = KeyboardFocusAuthority(identityToken: continuityField.identityToken,
        bounds: continuityField.bounds.offsetBy(dx: 1, dy: 0).insetBy(dx: -4, dy: 0), role: "AXTextField", subrole: nil)
    #expect(continuingTextRootIsProven(expectedPreference: .selectedWindow,
        observedPreference: .containedOverlay, wanted: continuityField,
        observedFocus: .authority(restyled), belongsToSelectedRoot: true, windowBounds: continuityBounds))
    #expect(continuingTextRejectionReason(expectedPreference: .selectedWindow,
        observedPreference: .containedOverlay, wanted: continuityField,
        observedFocus: .authority(restyled), belongsToSelectedRoot: true, windowBounds: continuityBounds) == nil)
    // The same identity outside the validated window is still no proof.
    let escaped = KeyboardFocusAuthority(identityToken: continuityField.identityToken,
        bounds: CGRect(x: 100, y: 40, width: 200, height: 30), role: "AXTextField", subrole: nil)
    #expect(!continuingTextRootIsProven(expectedPreference: .selectedWindow,
        observedPreference: .containedOverlay, wanted: continuityField,
        observedFocus: .authority(escaped), belongsToSelectedRoot: true, windowBounds: continuityBounds))
    #expect(continuingTextRejectionReason(expectedPreference: .selectedWindow,
        observedPreference: .containedOverlay, wanted: continuityField,
        observedFocus: .authority(escaped), belongsToSelectedRoot: true, windowBounds: continuityBounds)
        == "focus_geometry_untrusted")
}

@Test func KeyboardTextContinuityNamesTheFirstFailedProofForDiagnostics() {
    func reason(_ observed: KeyboardFocusObservation, belongs: Bool = true,
                observedPreference: FocusedRootPreference = .containedOverlay) -> String? {
        continuingTextRejectionReason(expectedPreference: .selectedWindow, observedPreference: observedPreference,
            wanted: continuityField, observedFocus: observed, belongsToSelectedRoot: belongs,
            windowBounds: continuityBounds)
    }
    let other = KeyboardFocusAuthority(identityToken: "ax:other", bounds: continuityField.bounds,
        role: "AXTextField", subrole: nil)
    #expect(reason(.authority(continuityField), observedPreference: .selectedWindow) == "root_preference")
    #expect(reason(.authority(continuityField), belongs: false) == "focus_outside_selected_root")
    #expect(reason(.authority(other)) == "focus_identity_changed")
    #expect(reason(.stale) == "focus_not_authority")
}

@Test func KeyboardTextContinuityRejectsChangedOrUnknownFocus() {
    let changed = [
        KeyboardFocusAuthority(identityToken: "ax:other", bounds: continuityField.bounds, role: "AXTextField", subrole: nil),
        KeyboardFocusAuthority(identityToken: continuityField.identityToken, bounds: continuityField.bounds, role: "AXTextArea", subrole: nil),
        KeyboardFocusAuthority(identityToken: continuityField.identityToken, bounds: continuityField.bounds, role: "AXTextField", subrole: "AXSearchField"),
    ]
    let observations: [KeyboardFocusObservation] = changed.map { .authority($0) } + [.secure, .secureOrIndeterminate, .stale]
    for observation in observations {
        #expect(!continuingTextRootIsProven(expectedPreference: .selectedWindow,
            observedPreference: .containedOverlay, wanted: continuityField,
            observedFocus: observation, belongsToSelectedRoot: true, windowBounds: continuityBounds))
    }
}

@Test func KeyboardTextContinuityRejectsUnsafeAuthorityAndOtherRootTransitions() {
    for role in ["AXButton", "AXUnknown", "AXSecureTextField"] {
        let field = KeyboardFocusAuthority(identityToken: continuityField.identityToken,
            bounds: continuityField.bounds, role: role, subrole: nil)
        #expect(!continuingTextRootIsProven(expectedPreference: .selectedWindow,
            observedPreference: .containedOverlay, wanted: field, observedFocus: .authority(field),
            belongsToSelectedRoot: true, windowBounds: continuityBounds))
    }
    #expect(!continuingTextRootIsProven(expectedPreference: .containedOverlay,
        observedPreference: .selectedWindow, wanted: continuityField,
        observedFocus: .authority(continuityField), belongsToSelectedRoot: true, windowBounds: continuityBounds))
    #expect(!continuingTextRootIsProven(expectedPreference: .selectedWindow,
        observedPreference: .selectedWindow, wanted: continuityField,
        observedFocus: .authority(continuityField), belongsToSelectedRoot: true, windowBounds: continuityBounds))
    #expect(!continuingTextRootIsProven(expectedPreference: .selectedWindow,
        observedPreference: .containedOverlay, wanted: continuityField,
        observedFocus: .authority(continuityField), belongsToSelectedRoot: true, windowBounds: .zero))
}
