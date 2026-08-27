import SwiftUI
import UIKit

/// Quiet Focus — calm personal assistant, not a job board.
enum Theme {
    // MARK: Colors

    /// Soft fog background (#F3F5F2).
    static let fog = Color(red: 243 / 255, green: 245 / 255, blue: 242 / 255)
    /// Primary ink (#1C2421).
    static let ink = Color(red: 28 / 255, green: 36 / 255, blue: 33 / 255)
    /// Muted sage accent (#5B7C6E) — one primary CTA per screen.
    static let accent = Color(red: 91 / 255, green: 124 / 255, blue: 110 / 255)
    /// Soft sage-gray for secondary text/icons.
    static let soft = Color(red: 91 / 255, green: 124 / 255, blue: 110 / 255).opacity(0.55)
    /// Gentle note color (not alarm orange).
    static let note = Color(red: 120 / 255, green: 110 / 255, blue: 90 / 255)
    static let success = accent
    static let warning = note
    static let muted = Color.secondary

    // MARK: Spacing

    static let spaceXS: CGFloat = 4
    static let spaceS: CGFloat = 8
    static let spaceM: CGFloat = 14
    static let spaceL: CGFloat = 20
    static let spaceXL: CGFloat = 28

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
    static let quick = Animation.easeOut(duration: 0.28)
    static let breathe = Animation.easeInOut(duration: 2.4).repeatForever(autoreverses: true)

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
    @Published var awaitingCount: Int = 0
    /// Hide the floating dock while the in-app browser (or any pushed detail) is up.
    @Published var dockHidden: Bool = false
}
