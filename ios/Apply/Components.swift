import SwiftUI

// MARK: - Ambient background (soft mesh — unique without loudness)

struct AmbientBackground: View {
    var body: some View {
        ZStack {
            Theme.fog
            Circle()
                .fill(Theme.accent.opacity(0.10))
                .frame(width: 320, height: 320)
                .blur(radius: 60)
                .offset(x: -120, y: -180)
            Circle()
                .fill(Theme.note.opacity(0.08))
                .frame(width: 280, height: 280)
                .blur(radius: 50)
                .offset(x: 140, y: 80)
            Circle()
                .fill(Theme.accent.opacity(0.06))
                .frame(width: 220, height: 220)
                .blur(radius: 40)
                .offset(x: 40, y: 320)
        }
        .ignoresSafeArea()
    }
}

extension View {
    func ambientScreen() -> some View {
        self
            .background { AmbientBackground() }
            .foregroundStyle(Theme.ink)
    }
}

// MARK: - Floating dock tab bar

struct FloatingTabBar: View {
    @Binding var selection: Int
    var readyBadge: Int = 0
    var awaitingBadge: Int = 0

    private let tabs: [(label: String, icon: String)] = [
        ("Apply", "leaf"),
        ("In flight", "hourglass"),
        ("About", "person"),
        ("Chat", "bubble.left"),
        ("Settings", "slider.horizontal.3"),
    ]

    var body: some View {
        HStack(spacing: 0) {
            ForEach(tabs.indices, id: \.self) { i in
                Button {
                    withAnimation(Theme.springSoft) { selection = i }
                    Theme.selection()
                } label: {
                    VStack(spacing: 4) {
                        ZStack(alignment: .topTrailing) {
                            Image(systemName: tabs[i].icon)
                                .font(.system(size: 18, weight: .medium))
                                .frame(width: 28, height: 28)
                            if i == 0, readyBadge > 0 {
                                dockBadge(readyBadge)
                            }
                            if i == 1, awaitingBadge > 0 {
                                dockBadge(awaitingBadge)
                            }
                        }
                        Text(tabs[i].label)
                            .font(.system(size: 10, weight: .medium, design: .rounded))
                    }
                    .foregroundStyle(selection == i ? Theme.accent : Theme.soft)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 10)
                    .background {
                        if selection == i {
                            Capsule()
                                .fill(Theme.accent.opacity(0.12))
                                .padding(.horizontal, 4)
                                .padding(.vertical, 2)
                                .transition(.scale.combined(with: .opacity))
                        }
                    }
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(.ultraThinMaterial, in: Capsule())
        .overlay(Capsule().strokeBorder(Theme.accent.opacity(0.1), lineWidth: 1))
        .shadow(color: Theme.ink.opacity(0.06), radius: 16, y: 6)
        .padding(.horizontal, Theme.spaceL)
        .padding(.bottom, 8)
    }

    private func dockBadge(_ n: Int) -> some View {
        Text("\(min(n, 9))")
            .font(.system(size: 9, weight: .bold, design: .rounded))
            .foregroundStyle(.white)
            .padding(.horizontal, 4)
            .padding(.vertical, 1)
            .background(Theme.accent, in: Capsule())
            .offset(x: 8, y: -4)
    }
}

// MARK: - Editorial page header

struct PageHeader: View {
    let eyebrow: String
    let title: String
    var subtitle: String? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(eyebrow)
                .font(.caption.weight(.semibold))
                .foregroundStyle(Theme.accent)
                .textCase(.uppercase)
                .tracking(1.2)
            Text(title)
                .font(Theme.title(34))
                .foregroundStyle(Theme.ink)
            if let subtitle {
                Text(subtitle)
                    .font(.body)
                    .foregroundStyle(Theme.soft)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, Theme.spaceL)
        .padding(.top, Theme.spaceS)
        .padding(.bottom, Theme.spaceM)
    }
}

// MARK: - Score mark

struct ScoreMark: View {
    let score: Double
    var size: CGFloat = 15

    private var pct: Int { Int((score * 100).rounded()) }

    var body: some View {
        Text("\(pct)%")
            .font(.system(size: size, weight: .medium, design: .rounded).monospacedDigit())
            .foregroundStyle(Theme.soft)
            .accessibilityLabel("Match score \(pct) percent")
    }
}

struct SoftRing: View {
    let progress: Double
    var size: CGFloat = 72
    var lineWidth: CGFloat = 5

    var body: some View {
        ZStack {
            Circle()
                .stroke(Theme.accent.opacity(0.12), lineWidth: lineWidth)
            Circle()
                .trim(from: 0, to: min(max(progress, 0), 1))
                .stroke(Theme.accent.opacity(0.75),
                        style: StrokeStyle(lineWidth: lineWidth, lineCap: .round))
                .rotationEffect(.degrees(-90))
                .animation(Theme.springSoft, value: progress)
        }
        .frame(width: size, height: size)
    }
}

struct QuietStatus: View {
    let text: String
    var emphasize: Bool = false
    @State private var breathe = false

    var body: some View {
        Text(text)
            .font(.caption.weight(.medium))
            .foregroundStyle(emphasize ? Theme.accent : Theme.soft)
            .opacity(emphasize && breathe ? 0.55 : 1)
            .animation(emphasize ? Theme.breathe : .default, value: breathe)
            .onAppear { if emphasize { breathe = true } }
    }
}

// MARK: - Toast

struct AppToast: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.subheadline)
            .foregroundStyle(Theme.ink)
            .multilineTextAlignment(.center)
            .padding(.horizontal, 18)
            .padding(.vertical, 12)
            .background(.ultraThinMaterial, in: Capsule())
            .overlay(Capsule().strokeBorder(Theme.accent.opacity(0.12), lineWidth: 1))
            .transition(.opacity.combined(with: .move(edge: .bottom)))
    }
}

struct ToastModifier: ViewModifier {
    @Binding var message: String?
    var bottomPadding: CGFloat = 80

    func body(content: Content) -> some View {
        content.overlay(alignment: .bottom) {
            if let message {
                AppToast(text: message)
                    .padding(.bottom, bottomPadding)
                    .zIndex(10)
            }
        }
    }
}

extension View {
    func appToast(_ message: Binding<String?>, bottomPadding: CGFloat = 80) -> some View {
        modifier(ToastModifier(message: message, bottomPadding: bottomPadding))
    }

    func quietScreen() -> some View { ambientScreen() }
}

// MARK: - Empty

struct EmptyStateView: View {
    let title: String
    var description: String? = nil
    var retryTitle: String? = nil
    var retry: (() -> Void)? = nil
    var systemImage: String = ""

    var body: some View {
        VStack(spacing: Theme.spaceM) {
            Spacer(minLength: 60)
            Text(title)
                .font(Theme.title(26))
                .foregroundStyle(Theme.ink)
                .multilineTextAlignment(.center)
            if let description {
                Text(description)
                    .font(.body)
                    .foregroundStyle(Theme.soft)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, Theme.spaceXL)
            }
            if let retryTitle, let retry {
                Button(retryTitle, action: retry)
                    .font(.body.weight(.medium))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 22)
                    .padding(.vertical, 12)
                    .background(Theme.accent, in: Capsule())
                    .padding(.top, Theme.spaceS)
            }
            Spacer()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .ambientScreen()
    }
}

struct PressableButtonStyle: ButtonStyle {
    var haptic: Bool = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
            .opacity(configuration.isPressed ? 0.9 : 1)
            .animation(Theme.quick, value: configuration.isPressed)
            .onChange(of: configuration.isPressed) { _, pressed in
                if pressed && haptic { Theme.impact(.soft) }
            }
    }
}

struct PrimaryButton: View {
    let title: String
    var systemImage: String? = nil
    var busy: Bool = false
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                if busy {
                    ProgressView().controlSize(.small).tint(.white)
                } else if let systemImage {
                    Image(systemName: systemImage)
                }
                Text(busy ? "Working…" : title)
                    .font(.body.weight(.semibold))
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 14)
            .foregroundStyle(.white)
            .background(Theme.accent, in: Capsule())
        }
        .buttonStyle(PressableButtonStyle())
        .disabled(busy)
    }
}

struct FocusCard<Content: View>: View {
    @ViewBuilder var content: Content

    var body: some View {
        content
            .padding(Theme.spaceL)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 24, style: .continuous)
                    .fill(Color.white.opacity(0.78))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 24, style: .continuous)
                    .strokeBorder(Theme.accent.opacity(0.08), lineWidth: 1)
            )
            .shadow(color: Theme.ink.opacity(0.04), radius: 20, y: 8)
    }
}

struct QuietRow: View {
    let title: String
    var subtitle: String? = nil
    var score: Double? = nil

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.spaceM) {
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(.body.weight(.medium))
                    .foregroundStyle(Theme.ink)
                    .lineLimit(1)
                if let subtitle {
                    Text(subtitle)
                        .font(.subheadline)
                        .foregroundStyle(Theme.soft)
                        .lineLimit(1)
                }
            }
            Spacer(minLength: 8)
            if let score { ScoreMark(score: score) }
        }
        .padding(.vertical, 6)
    }
}

// MARK: - Up-next hero (editorial)

struct UpNextCard: View {
    let item: QueueItem
    var actionTitle: String
    var busy: Bool = false
    var showPassActions: Bool = false
    var onPass: (() -> Void)? = nil
    var onSkip: (() -> Void)? = nil
    let action: () -> Void

    private var reasonLine: String? {
        if let r = item.reasons?.first, !r.isEmpty { return r }
        if let w = item.why, !w.isEmpty { return w }
        return nil
    }

    var body: some View {
        FocusCard {
            ZStack(alignment: .topTrailing) {
                if let sc = item.score {
                    Text("\(Int((sc * 100).rounded()))")
                        .font(.system(size: 72, weight: .bold, design: .rounded).monospacedDigit())
                        .foregroundStyle(Theme.accent.opacity(0.08))
                        .offset(x: 8, y: -12)
                        .accessibilityHidden(true)
                }

                VStack(alignment: .leading, spacing: Theme.spaceM) {
                    Text("Up next")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Theme.accent)
                        .textCase(.uppercase)
                        .tracking(1.0)

                    Text(item.company ?? "Company")
                        .font(Theme.title(30))
                        .foregroundStyle(Theme.ink)
                        .fixedSize(horizontal: false, vertical: true)

                    Text(item.title ?? "Role")
                        .font(.title3)
                        .foregroundStyle(Theme.ink.opacity(0.7))
                        .fixedSize(horizontal: false, vertical: true)

                    if let reasonLine {
                        Text(reasonLine)
                            .font(.subheadline)
                            .foregroundStyle(Theme.soft)
                            .lineLimit(2)
                    }

                    PrimaryButton(title: actionTitle, busy: busy, action: action)
                        .padding(.top, 4)

                    if showPassActions {
                        HStack(spacing: 16) {
                            Button {
                                onSkip?()
                            } label: {
                                Text("Skip for now")
                                    .font(.subheadline.weight(.medium))
                                    .foregroundStyle(Theme.soft)
                                    .frame(maxWidth: .infinity)
                                    .padding(.vertical, 10)
                            }
                            Button {
                                onPass?()
                            } label: {
                                Text("Pass")
                                    .font(.subheadline.weight(.semibold))
                                    .foregroundStyle(Theme.note)
                                    .frame(maxWidth: .infinity)
                                    .padding(.vertical, 10)
                            }
                        }
                    }
                }
            }
        }
    }
}

/// Compact horizontal card for the Ready strip.
struct ReadyChipCard: View {
    let item: QueueItem

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(item.company ?? "—")
                .font(.headline)
                .foregroundStyle(Theme.ink)
                .lineLimit(1)
            Text(item.title ?? "Role")
                .font(.caption)
                .foregroundStyle(Theme.soft)
                .lineLimit(2)
            Spacer(minLength: 0)
            if let sc = item.score {
                ScoreMark(score: sc, size: 13)
            }
        }
        .padding(14)
        .frame(width: 150, height: 118, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .fill(Color.white.opacity(0.72))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .strokeBorder(Theme.accent.opacity(0.08), lineWidth: 1)
        )
    }
}

// MARK: - Coverage

struct CoverageMeter: View {
    let score: Double
    var missing: [String] = []
    var suggestion: String? = nil
    var onSuggestion: (() -> Void)? = nil

    private var pct: Int { Int((score * 100).rounded()) }
    private var line: String {
        if score >= 0.7 { return "Enough to draft well." }
        if score >= 0.4 { return "A few gaps left." }
        return "Add a little more about you."
    }

    var body: some View {
        FocusCard {
            HStack(spacing: Theme.spaceL) {
                ZStack {
                    SoftRing(progress: score, size: 88, lineWidth: 6)
                    VStack(spacing: 0) {
                        Text("\(pct)")
                            .font(.system(.title2, design: .rounded).weight(.bold).monospacedDigit())
                        Text("%")
                            .font(.caption2)
                            .foregroundStyle(Theme.soft)
                    }
                }

                VStack(alignment: .leading, spacing: 8) {
                    Text("Coverage")
                        .font(Theme.headline())
                    Text(line)
                        .font(.subheadline)
                        .foregroundStyle(Theme.soft)
                        .fixedSize(horizontal: false, vertical: true)
                    if !missing.isEmpty {
                        Text("Still missing: " + missing.joined(separator: ", "))
                            .font(.caption)
                            .foregroundStyle(Theme.soft)
                    }
                    if let suggestion {
                        Button(action: { onSuggestion?() }) {
                            Text(suggestion)
                                .font(.subheadline.weight(.medium))
                                .foregroundStyle(Theme.accent)
                        }
                        .buttonStyle(.plain)
                    }
                }
                Spacer(minLength: 0)
            }
        }
    }
}

struct PreparingView: View {
    var message: String = "Just a moment…"

    var body: some View {
        VStack(spacing: Theme.spaceM) {
            ProgressView().tint(Theme.accent)
            Text(message)
                .font(.subheadline)
                .foregroundStyle(Theme.soft)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .ambientScreen()
    }
}

enum KnowledgeCategoryStyle {
    static func symbol(for category: String) -> String {
        switch category.lowercased() {
        case "project": return "hammer"
        case "achievement": return "star"
        case "strength": return "sparkles"
        case "preference": return "slider.horizontal.3"
        case "answer": return "text.quote"
        default: return "plus"
        }
    }

    static func sectionTitle(for category: String) -> String {
        switch category.lowercased() {
        case "project": return "Projects"
        case "achievement": return "Experience"
        case "strength": return "Strengths"
        case "preference": return "Preferences"
        case "answer": return "Saved answers"
        default: return category.capitalized + "s"
        }
    }
}

typealias ScoreRing = ScoreMark
typealias StatusChip = QuietStatusCompat
typealias ChipTone = QuietChipTone
typealias JobCard = QuietRowCompat

enum QuietChipTone { case accent, success, warning, muted }

struct QuietStatusCompat: View {
    let title: String
    var systemImage: String? = nil
    var tone: QuietChipTone = .muted
    var pulse: Bool = false
    var body: some View { QuietStatus(text: title, emphasize: pulse || tone == .warning) }
}

struct QuietRowCompat: View {
    let item: QueueItem
    var body: some View {
        QuietRow(
            title: item.title ?? "Role",
            subtitle: [item.company, item.source].compactMap { $0 }.joined(separator: " · "),
            score: item.score
        )
    }
}
