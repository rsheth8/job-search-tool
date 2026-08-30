import SwiftUI
import UIKit

/// One hue. Mid navy that still reads as blue on fog — not electric, not black.
enum Theme {
    // MARK: Colors

    /// Cool paper haze (#F2F5F8).
    static let fog = Color(red: 242 / 255, green: 245 / 255, blue: 248 / 255)
    /// Body text, same hue, darker (#13233D).
    static let ink = Color(red: 19 / 255, green: 35 / 255, blue: 61 / 255)
    /// The live color — CTAs, score, ring, dock (#1A508C).
    static let cockpit = Color(red: 26 / 255, green: 80 / 255, blue: 140 / 255)
    /// Pressed CTA (#143E6C).
    static let cockpitDeep = Color(red: 20 / 255, green: 62 / 255, blue: 108 / 255)
    /// Eyebrows, links — same hue, lighter (#4A7FB8).
    static let horizon = Color(red: 74 / 255, green: 127 / 255, blue: 184 / 255)
    /// Inactive icons, secondary (#8A9BB8).
    static let trail = Color(red: 138 / 255, green: 155 / 255, blue: 184 / 255)
    /// Dividers, ambient haze (#C9D4E6).
    static let cloud = Color(red: 201 / 255, green: 212 / 255, blue: 230 / 255)
    /// Primary accent alias — one CTA per screen.
    static let accent = cockpit
    /// Secondary text / idle chrome.
    static let soft = trail.opacity(0.92)
    /// Warnings, pass, skip — cool slate (#7A8494).
    static let note = Color(red: 122 / 255, green: 132 / 255, blue: 148 / 255)
    static let success = cockpit
    static let warning = note
    static let muted = trail.opacity(0.85)

    // MARK: Spacing

    static let spaceXS: CGFloat = 4
    static let spaceS: CGFloat = 8
    static let spaceM: CGFloat = 14
    static let spaceL: CGFloat = 20
    static let spaceXL: CGFloat = 28
    /// Scroll padding so the last row clears the floating dock.
    static let dockClearance: CGFloat = 160
    /// Toast sits just above the dock.
    static let toastClearance: CGFloat = 112
    static let cardRadius: CGFloat = 24
    static let cardFill = Color.white.opacity(0.92)
    /// Opaque fill for swipe rows so actions stay hidden at rest.
    static let rowFill = Color.white

    // MARK: Type

    static func title(_ size: CGFloat = 28) -> Font {
        .system(size: size, weight: .semibold, design: .rounded)
    }

    static func headline() -> Font {
        .system(.headline, design: .rounded)
    }

    // MARK: Motion — soft, slow, high damping

    static let spring = Animation.spring(response: 0.5, dampingFraction: 0.88)
    static let springSoft = Animation.spring(response: 0.6, dampingFraction: 0.9)
    /// Horizon replies: rise into place, no bounce.
    static let springHorizon = Animation.spring(response: 0.52, dampingFraction: 0.92)
    /// Suggestion chips swapping under a turn.
    static let springChip = Animation.spring(response: 0.4, dampingFraction: 0.88)
    /// Score / coverage count-up — a little overshoot, like a needle settling.
    static let tick = Animation.spring(response: 0.95, dampingFraction: 0.78)
    static let quick = Animation.easeOut(duration: 0.28)
    static let breathe = Animation.easeInOut(duration: 2.4).repeatForever(autoreverses: true)
    static let sheen = Animation.easeInOut(duration: 1.15)

    /// Propeller periods (seconds per revolution).
    static let propSlow: Double = 3.5
    static let propMedium: Double = 1.2
    static let propFast: Double = 0.6

    // MARK: Haptics — only on meaningful commits

    static func impact(_ style: UIImpactFeedbackGenerator.FeedbackStyle = .soft) {
        UIImpactFeedbackGenerator(style: style).impactOccurred()
    }

    static func notify(_ type: UINotificationFeedbackGenerator.FeedbackType) {
        UINotificationFeedbackGenerator().notificationOccurred(type)
    }

    static func selection() {
        UISelectionFeedbackGenerator().selectionChanged()
    }
}

/// Tab badge counts + shared chrome state updated by each tab’s load.
@MainActor
final class AppChrome: ObservableObject {
    static let shared = AppChrome()

    @Published var readyCount: Int = 0
    /// Hide the floating dock while the in-app browser (or any pushed detail) is up.
    @Published var dockHidden: Bool = false
}
