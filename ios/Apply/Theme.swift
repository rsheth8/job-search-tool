//  Theme.swift — the app's colours, matching the web surfaces.
//
//  This app previously had no styling at all: no asset catalog, no custom colours,
//  every view on SwiftUI defaults. That made it the one surface with no relationship
//  to the rest of the product.
//
//  Colours are defined in code rather than an .xcassets catalog on purpose. The
//  project is generated from ios/project.yml via XcodeGen, so a hand-maintained
//  catalog is one more thing to keep in sync — and, more usefully, hex literals here
//  can be pinned against app/theme.py by tests/test_theme_parity.py. A catalog is
//  opaque to that check.
//
//  Light/dark comes free: `Palette` resolves through UIColor's dynamic provider, so
//  a colour re-reads itself whenever the trait collection changes. Nothing in the
//  views needs to know which mode is active, and there's no toggle to wire up — the
//  system's setting is the switch, which is what iOS users expect.
//
//  Values must match app/theme.py. The parity test fails the build if they don't.

import SwiftUI

/// A colour with a light and a dark value, resolved per trait collection.
struct Palette {
    let light: UInt32
    let dark: UInt32

    init(light: UInt32, dark: UInt32) {
        self.light = light
        self.dark = dark
    }

    var color: Color {
        Color(UIColor { traits in
            traits.userInterfaceStyle == .dark
                ? UIColor(rgb: self.dark) : UIColor(rgb: self.light)
        })
    }
}

private extension UIColor {
    convenience init(rgb: UInt32) {
        self.init(
            red: CGFloat((rgb >> 16) & 0xFF) / 255,
            green: CGFloat((rgb >> 8) & 0xFF) / 255,
            blue: CGFloat(rgb & 0xFF) / 255,
            alpha: 1
        )
    }
}

enum Theme {
    // Names mirror the CSS custom properties one-for-one so a colour can be traced
    // across surfaces without a translation table.
    static let background = Palette(light: 0xF7F8FA, dark: 0x0C0F16)
    static let panel      = Palette(light: 0xFFFFFF, dark: 0x141924)
    static let panel2     = Palette(light: 0xF1F3F7, dark: 0x1B2130)
    static let line       = Palette(light: 0xE2E6EE, dark: 0x263041)
    static let ink        = Palette(light: 0x0E1420, dark: 0xE9EDF5)
    static let dim        = Palette(light: 0x5C6779, dark: 0x98A3B8)
    static let accent     = Palette(light: 0x0B7373, dark: 0x2ED9D9)
    static let accentInk  = Palette(light: 0xFFFFFF, dark: 0x06202A)
    static let accentSoft = Palette(light: 0xE3F4F4, dark: 0x0E2A2E)
    static let ok         = Palette(light: 0x1F8A4C, dark: 0x3DD68C)
    static let warn       = Palette(light: 0x9A6600, dark: 0xFFC861)
    static let bad        = Palette(light: 0xC0392B, dark: 0xFF6B6B)

    // Convenience accessors — views read `Theme.inkColor`, never a raw hex.
    static var backgroundColor: Color { background.color }
    static var panelColor: Color      { panel.color }
    static var panel2Color: Color     { panel2.color }
    static var lineColor: Color       { line.color }
    static var inkColor: Color        { ink.color }
    static var dimColor: Color        { dim.color }
    static var accentColor: Color     { accent.color }
    static var accentInkColor: Color  { accentInk.color }
    static var accentSoftColor: Color { accentSoft.color }
    static var okColor: Color         { ok.color }
    static var warnColor: Color       { warn.color }
    static var badColor: Color        { bad.color }

    static let corner: CGFloat = 14
    static let cornerSmall: CGFloat = 10
}

// MARK: - Shared view treatments

/// The card surface used for every list row across the four tabs, so a posting on
/// Apply and a preview on In-flight read as the same kind of object.
struct ThemedCard: ViewModifier {
    func body(content: Content) -> some View {
        content
            .padding(14)
            .background(Theme.panelColor)
            .clipShape(RoundedRectangle(cornerRadius: Theme.corner, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: Theme.corner, style: .continuous)
                    .stroke(Theme.lineColor, lineWidth: 1)
            )
    }
}

/// Small uppercase status label — the SwiftUI counterpart of `.pill` on the web.
struct ThemedPill: View {
    let text: String
    var tint: Color = Theme.dimColor

    var body: some View {
        Text(text.uppercased())
            .font(.system(size: 10, weight: .semibold))
            .tracking(0.4)
            .foregroundStyle(tint)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(tint.opacity(0.14))
            .clipShape(Capsule())
    }
}

extension View {
    func themedCard() -> some View { modifier(ThemedCard()) }

    /// Applies the app background and tint to a whole screen. Used once per tab so
    /// the four of them can't drift apart.
    func themedScreen() -> some View {
        self
            .background(Theme.backgroundColor.ignoresSafeArea())
            .tint(Theme.accentColor)
    }
}
