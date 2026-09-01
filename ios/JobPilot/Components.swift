import SwiftUI
import UIKit

// MARK: - Ambient background (soft mesh — unique without loudness)

struct AmbientBackground: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        TimelineView(.animation(minimumInterval: reduceMotion ? 120 : 1.0 / 20.0,
                                paused: reduceMotion)) { context in
            let t = reduceMotion ? 0 : context.date.timeIntervalSinceReferenceDate
            let a = sin(t / 8.0) * 20
            let b = cos(t / 11.0) * 16
            let c = sin(t / 13.0) * 14
            ZStack {
                Theme.fog
                Circle()
                    .fill(Theme.cockpit.opacity(0.12))
                    .frame(width: 320, height: 320)
                    .blur(radius: 60)
                    .offset(x: -120 + a, y: -180 + b)
                Circle()
                    .fill(Theme.horizon.opacity(0.12))
                    .frame(width: 280, height: 280)
                    .blur(radius: 50)
                    .offset(x: 140 - b, y: 80 + a)
                Circle()
                    .fill(Theme.cloud.opacity(0.5))
                    .frame(width: 220, height: 220)
                    .blur(radius: 40)
                    .offset(x: 40 + c, y: 320 - a)
            }
        }
        .ignoresSafeArea()
        .allowsHitTesting(false)
    }
}

extension View {
    func ambientScreen() -> some View {
        self
            .foregroundStyle(Theme.ink)
            .background { AmbientBackground() }
            .toolbarBackground(.hidden, for: .navigationBar)
    }

    /// Stock `Form` on grouped gray. Sit it on fog so sheets match the rest of the app.
    func fogFormChrome() -> some View {
        self
            .scrollContentBackground(.hidden)
            .background(Theme.fog)
            .toolbarBackground(Theme.fog, for: .navigationBar)
            .toolbarBackground(.visible, for: .navigationBar)
    }
}

// MARK: - Floating dock tab bar

struct FloatingTabBar: View {
    @Binding var selection: Int
    var readyBadge: Int = 0
    @Namespace private var dockNS
    @State private var propNudge = false
    @State private var badgeBump = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private let tabs: [(label: String, icon: String?)] = [
        ("Apply", nil), // propeller
        ("You", "person"),
        ("Ask", "bubble.left"),
        ("Settings", "slider.horizontal.3"),
    ]

    var body: some View {
        HStack(spacing: 0) {
            ForEach(tabs.indices, id: \.self) { i in
                Button {
                    withAnimation(reduceMotion ? nil : Theme.springSoft) { selection = i }
                    Theme.selection()
                    if i == 0 { nudgeProp() }
                } label: {
                    VStack(spacing: 4) {
                        ZStack(alignment: .topTrailing) {
                            tabIcon(i)
                                .frame(width: 28, height: 28)
                            if i == 0, readyBadge > 0 {
                                dockBadge(readyBadge)
                            }
                        }
                        Text(tabs[i].label)
                            .font(.system(size: 10, weight: .medium, design: .rounded))
                    }
                    .foregroundStyle(selection == i ? Theme.accent : Theme.trail)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 8)
                    .background {
                        if selection == i {
                            Capsule()
                                .fill(Theme.accent.opacity(0.14))
                                .padding(.horizontal, 4)
                                .padding(.vertical, 2)
                                .matchedGeometryEffect(id: "dock-pill", in: dockNS)
                        }
                    }
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 3)
        .paperCapsule()
        .padding(.horizontal, Theme.spaceL)
        .padding(.bottom, 8)
        .animation(reduceMotion ? nil : Theme.springSoft, value: selection)
        .onChange(of: readyBadge) { _, _ in bumpBadge() }
    }

    @ViewBuilder
    private func tabIcon(_ i: Int) -> some View {
        if i == 0 {
            PropellerIcon(speed: .still, size: 18, nudge: propNudge)
        } else if let icon = tabs[i].icon {
            Image(systemName: icon)
                .font(.system(size: 18, weight: .medium))
        }
    }

    private func nudgeProp() {
        guard !reduceMotion else { return }
        propNudge = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) {
            propNudge = false
        }
    }

    private func bumpBadge() {
        guard !reduceMotion, readyBadge > 0 else { return }
        badgeBump = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.22) {
            badgeBump = false
        }
    }

    private func dockBadge(_ n: Int) -> some View {
        Text("\(min(n, 9))")
            .font(.system(size: 9, weight: .bold, design: .rounded))
            .foregroundStyle(.white)
            .padding(.horizontal, 4)
            .padding(.vertical, 1)
            .background(Theme.accent, in: Capsule())
            .scaleEffect(badgeBump ? 1.18 : 1)
            .animation(reduceMotion ? nil : Theme.spring, value: badgeBump)
            .offset(x: 8, y: -4)
    }
}

/// Frosted paper for floating chrome. Bare `ultraThinMaterial` on recent iOS
/// samples the navy mesh and the dock goes unreadable.
struct PaperCapsule: ViewModifier {
    func body(content: Content) -> some View {
        content
            .background {
                ZStack {
                    Capsule().fill(.regularMaterial)
                    Capsule().fill(Theme.fog.opacity(0.86))
                }
            }
            .overlay {
                Capsule().strokeBorder(Theme.cloud.opacity(0.9), lineWidth: 1)
            }
            .shadow(color: Theme.ink.opacity(0.07), radius: 16, y: 6)
    }
}

extension View {
    func paperCapsule() -> some View { modifier(PaperCapsule()) }

    func paperBar() -> some View {
        self.background {
            ZStack {
                Rectangle().fill(.regularMaterial)
                Rectangle().fill(Theme.fog.opacity(0.9))
            }
        }
    }
}

// MARK: - Editorial page header

struct PageHeader<Accessory: View>: View {
    let eyebrow: String
    let title: String
    var subtitle: String? = nil
    @ViewBuilder var accessory: Accessory

    init(eyebrow: String, title: String, subtitle: String? = nil,
         @ViewBuilder accessory: () -> Accessory = { EmptyView() }) {
        self.eyebrow = eyebrow
        self.title = title
        self.subtitle = subtitle
        self.accessory = accessory()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(eyebrow)
                .font(.caption.weight(.semibold))
                .foregroundStyle(Theme.horizon)
                .textCase(.uppercase)
                .tracking(1.2)
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Text(title)
                    .font(Theme.title(34))
                    .foregroundStyle(Theme.ink)
                    .contentTransition(.opacity)
                    .frame(maxWidth: .infinity, alignment: .leading)
                accessory
            }
            if let subtitle {
                Text(subtitle)
                    .font(.subheadline)
                    .foregroundStyle(Theme.soft)
                    .fixedSize(horizontal: false, vertical: true)
                    .contentTransition(.opacity)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, Theme.spaceL)
        .padding(.top, 4)
        .padding(.bottom, 2)
    }
}

/// Quiet word-pair toggle (Matches / Filed) — a sliding needle, not UIKit.
struct InstrumentToggle: View {
    let options: [String]
    @Binding var selection: Int
    @Namespace private var needle
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        HStack(spacing: options.count > 2 ? 12 : 16) {
            ForEach(options.indices, id: \.self) { i in
                Button {
                    withAnimation(reduceMotion ? nil : Theme.springSoft) { selection = i }
                    Theme.selection()
                } label: {
                    VStack(spacing: 5) {
                        Text(options[i])
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(selection == i ? Theme.accent : Theme.soft)
                        ZStack {
                            Color.clear.frame(height: 2)
                            if selection == i {
                                Capsule()
                                    .fill(Theme.accent)
                                    .frame(width: 18, height: 2)
                                    .matchedGeometryEffect(id: "needle", in: needle)
                            }
                        }
                    }
                }
                .buttonStyle(.plain)
            }
        }
        .accessibilityElement(children: .contain)
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
    var ticks: Bool = false
    var color: Color = Theme.accent
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Group {
            if ticks {
                InstrumentDial(progress: progress, size: size, color: color)
            } else {
                let clamped = min(max(progress, 0), 1)
                ZStack {
                    Circle()
                        .stroke(Theme.accent.opacity(0.14), lineWidth: lineWidth)
                    Circle()
                        .trim(from: 0, to: clamped)
                        .stroke(color, style: StrokeStyle(lineWidth: lineWidth, lineCap: .round))
                        .rotationEffect(.degrees(-90))
                }
                .frame(width: size, height: size)
            }
        }
        .animation(reduceMotion ? nil : Theme.tick, value: progress)
        .accessibilityHidden(true)
    }
}

/// Paper face under a crystal. Frost stays in the plate; the shine lives on the cover.
struct GlassDisc: View {
    var size: CGFloat

    var body: some View {
        Circle()
            .fill(.ultraThinMaterial)
            .overlay { Circle().fill(Color.white.opacity(0.78)) }
            .overlay {
                Circle()
                    .fill(
                        RadialGradient(
                            stops: [
                                .init(color: Color.white.opacity(0.28), location: 0),
                                .init(color: Color.clear, location: 0.65)
                            ],
                            center: UnitPoint(x: 0.38, y: 0.30),
                            startRadius: 0,
                            endRadius: size * 0.52
                        )
                    )
                    .allowsHitTesting(false)
            }
            .shadow(color: Theme.ink.opacity(0.08), radius: 24, y: 10)
            .frame(width: size, height: size)
            .allowsHitTesting(false)
    }
}

/// Cover glass — sharp. A rim catch and a light sheen, no blur over the dial.
struct CrystalCover: View {
    var size: CGFloat

    var body: some View {
        ZStack {
            Circle()
                .fill(
                    LinearGradient(
                        stops: [
                            .init(color: Color.white.opacity(0.18), location: 0),
                            .init(color: Color.clear, location: 0.34),
                            .init(color: Theme.ink.opacity(0.04), location: 1)
                        ],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    )
                )

            Circle()
                .strokeBorder(
                    LinearGradient(
                        colors: [Color.white.opacity(0.7), Theme.cloud.opacity(0.4)],
                        startPoint: .top,
                        endPoint: .bottom
                    ),
                    lineWidth: 1
                )

            Circle()
                .trim(from: 0.60, to: 0.86)
                .stroke(
                    Color.white.opacity(0.65),
                    style: StrokeStyle(lineWidth: 2, lineCap: .round)
                )
                .padding(2.5)
        }
        .frame(width: size, height: size)
        .allowsHitTesting(false)
    }
}

/// Point on a circle: `center` offset by `radius` at `radians`.
///
/// Every dial call site used to inline `cos(a) * radius` with a `Double` angle
/// and a `CGFloat` radius. Swift bridges CGFloat and Double implicitly, so in an
/// expression that mixes them the compiler has more than one valid reading of
/// `cos` — some toolchains pick one, others call it ambiguous and refuse. An
/// arm64-only local build was happy; CI was not. Doing the trig once, in a
/// CGFloat-typed place, removes the ambiguity instead of relying on the
/// compiler's mood.
private func dialPoint(_ center: CGPoint, _ radians: Double, _ radius: CGFloat) -> CGPoint {
    CGPoint(x: center.x + CGFloat(cos(radians)) * radius,
            y: center.y + CGFloat(sin(radians)) * radius)
}

/// Unit vector at `radians`, as CGFloat — same reason as `dialPoint`.
private func dialVector(_ radians: Double) -> CGVector {
    CGVector(dx: CGFloat(cos(radians)), dy: CGFloat(sin(radians)))
}

/// 270° HUD airspeed dial. Annular radar fill, needle lives in the band so the score stays clear.
struct InstrumentDial: View {
    var progress: Double
    var size: CGFloat
    var color: Color = Theme.accent

    /// 0 at 7:30, 100 at 4:30 — ASI sweep, not a fitness ring.
    private let startDeg: Double = 135
    private let sweepDeg: Double = 270

    var body: some View {
        let clamped = min(max(progress, 0), 1)
        let s = size
        let rOuter = s / 2 - 3
        let rKeep = s * (s >= 200 ? 0.36 : 0.32)

        Canvas { ctx, _ in
            let c = CGPoint(x: s / 2, y: s / 2)
            let start = Angle.degrees(startDeg)
            let end = Angle.degrees(startDeg + sweepDeg)
            let valueEnd = Angle.degrees(startDeg + sweepDeg * clamped)
            let a = startDeg * .pi / 180 + clamped * sweepDeg * .pi / 180
            let tip = dialPoint(c, a, rOuter - 2)

            var bezel = Path()
            bezel.addEllipse(in: CGRect(x: 1, y: 1, width: s - 2, height: s - 2))
            ctx.stroke(bezel, with: .color(Theme.cloud), lineWidth: 1)

            var rest = Path()
            rest.addArc(center: c, radius: rOuter - 4,
                         startAngle: start, endAngle: end, clockwise: false)
            ctx.stroke(rest, with: .color(Theme.accent.opacity(0.10)),
                       style: StrokeStyle(lineWidth: 4, lineCap: .butt))

            if clamped > 0.004 {
                var band = Path()
                band.addArc(center: c, radius: rOuter - 3,
                            startAngle: start, endAngle: valueEnd, clockwise: false)
                band.addArc(center: c, radius: rKeep,
                            startAngle: valueEnd, endAngle: start, clockwise: true)
                band.closeSubpath()
                ctx.fill(band, with: .linearGradient(
                    Gradient(stops: [
                        .init(color: color.opacity(0.16), location: 0),
                        .init(color: color.opacity(0.36), location: 1)
                    ]),
                    startPoint: dialPoint(c, startDeg * .pi / 180, rKeep),
                    endPoint: tip
                ))

                var rim = Path()
                rim.addArc(center: c, radius: rOuter - 4,
                            startAngle: start, endAngle: valueEnd, clockwise: false)
                ctx.stroke(rim, with: .color(color),
                           style: StrokeStyle(lineWidth: 4, lineCap: .butt))
            }

            let majors: [Double] = s >= 200 ? [0, 25, 50, 75, 100] : [0, 50, 100]
            let minorStep: Double = s >= 200 ? 5 : 10
            for v in stride(from: 0.0, through: 100, by: minorStep) {
                tick(ctx: ctx, center: c, value: v, rOuter: rOuter,
                     major: majors.contains(v))
            }
            if s >= 140 {
                for v in majors {
                    numeral(ctx: ctx, center: c, value: v,
                            radius: rOuter - (s >= 200 ? 22 : 14), size: s)
                }
            }

            needle(ctx: ctx, center: c, value: clamped, rKeep: rKeep, rOuter: rOuter)
        }
        .frame(width: size, height: size)
        .allowsHitTesting(false)
    }

    private func angle(for value: Double) -> Double {
        (startDeg + (value / 100) * sweepDeg) * .pi / 180
    }

    private func tick(ctx: GraphicsContext, center: CGPoint, value: Double,
                       rOuter: CGFloat, major: Bool) {
        let a = angle(for: value)
        let n = dialVector(a)
        let len: CGFloat = major ? 10 : 4
        var p = Path()
        p.move(to: CGPoint(x: center.x + n.dx * rOuter, y: center.y + n.dy * rOuter))
        p.addLine(to: CGPoint(x: center.x + n.dx * (rOuter - len),
                             y: center.y + n.dy * (rOuter - len)))
        ctx.stroke(p, with: .color(major ? Theme.ink.opacity(0.55) : Theme.cloud),
                   style: StrokeStyle(lineWidth: major ? 1.5 : 1, lineCap: .butt))
    }

    private func numeral(ctx: GraphicsContext, center: CGPoint, value: Double,
                          radius: CGFloat, size: CGFloat) {
        let a = angle(for: value)
        let pt = dialPoint(center, a, radius)
        let fontSize: CGFloat = size >= 200 ? 10 : 8
        let text = Text("\(Int(value))")
            .font(.system(size: fontSize, weight: .medium, design: .rounded).monospacedDigit())
            .foregroundStyle(Theme.ink.opacity(0.45))
        ctx.draw(ctx.resolve(text), at: pt, anchor: .center)
    }

    private func needle(ctx: GraphicsContext, center: CGPoint, value: Double,
                         rKeep: CGFloat, rOuter: CGFloat) {
        let a = startDeg * .pi / 180 + value * sweepDeg * .pi / 180
        let n = dialVector(a)
        let t = CGVector(dx: -n.dy, dy: n.dx)
        let shaft: CGFloat = 1.8
        let tipLen: CGFloat = max(12, size * 0.055)
        let tipR = rOuter - 2
        let startR = rKeep + 2
        let shaftEnd = max(startR + 8, tipR - tipLen)
        let origin = CGPoint(x: center.x + n.dx * startR, y: center.y + n.dy * startR)
        let tip = CGPoint(x: center.x + n.dx * tipR, y: center.y + n.dy * tipR)
        let end = CGPoint(x: center.x + n.dx * shaftEnd, y: center.y + n.dy * shaftEnd)

        var blade = Path()
        blade.move(to: CGPoint(x: origin.x + t.dx * shaft, y: origin.y + t.dy * shaft))
        blade.addLine(to: CGPoint(x: end.x + t.dx * shaft, y: end.y + t.dy * shaft))
        blade.addLine(to: tip)
        blade.addLine(to: CGPoint(x: end.x - t.dx * shaft, y: end.y - t.dy * shaft))
        blade.addLine(to: CGPoint(x: origin.x - t.dx * shaft, y: origin.y - t.dy * shaft))
        blade.closeSubpath()
        ctx.fill(blade, with: .color(color))

        var root = Path()
        let rr: CGFloat = 2.2
        root.addEllipse(in: CGRect(x: origin.x - rr, y: origin.y - rr, width: rr * 2, height: rr * 2))
        ctx.fill(root, with: .color(color))

        var contact = Path()
        let cr: CGFloat = 2.4
        contact.addEllipse(in: CGRect(x: tip.x - cr, y: tip.y - cr, width: cr * 2, height: cr * 2))
        ctx.fill(contact, with: .color(color))
    }
}

/// Four corner brackets — HUD box, no fill. Locks on once.
struct HUDReticle<Content: View>: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var locked = false
    @ViewBuilder var content: Content

    var body: some View {
        content
            .padding(.horizontal, 4)
            .padding(.vertical, 1)
            .overlay { HUDCorners() }
            .scaleEffect(reduceMotion || locked ? 1 : 1.12)
            .opacity(reduceMotion || locked ? 1 : 0)
            .compositingGroup()
            .onAppear {
                guard !reduceMotion else { return }
                withAnimation(Theme.spring.delay(0.12)) { locked = true }
            }
    }
}

private struct HUDCorners: View {
    var arm: CGFloat = 11

    var body: some View {
        Canvas { ctx, size in
            let w = size.width
            let h = size.height
            let a = min(arm, min(w, h) / 3)
            var p = Path()
            p.move(to: CGPoint(x: 0, y: a)); p.addLine(to: .zero); p.addLine(to: CGPoint(x: a, y: 0))
            p.move(to: CGPoint(x: w - a, y: 0)); p.addLine(to: CGPoint(x: w, y: 0)); p.addLine(to: CGPoint(x: w, y: a))
            p.move(to: CGPoint(x: 0, y: h - a)); p.addLine(to: CGPoint(x: 0, y: h)); p.addLine(to: CGPoint(x: a, y: h))
            p.move(to: CGPoint(x: w - a, y: h)); p.addLine(to: CGPoint(x: w, y: h)); p.addLine(to: CGPoint(x: w, y: h - a))
            ctx.stroke(p, with: .color(Theme.horizon),
                       style: StrokeStyle(lineWidth: 1, lineCap: .square, lineJoin: .miter))
        }
        .allowsHitTesting(false)
    }
}

struct QuietStatus: View {
    let text: String
    var emphasize: Bool = false

    var body: some View {
        Text(text)
            .font(.caption.weight(.medium))
            .foregroundStyle(emphasize ? Theme.accent : Theme.soft)
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
            .transition(.asymmetric(
                insertion: .opacity.combined(with: .move(edge: .bottom)).combined(with: .scale(scale: 0.96)),
                removal: .opacity.combined(with: .scale(scale: 0.98))
            ))
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
    var secondaryTitle: String? = nil
    var secondary: (() -> Void)? = nil
    var systemImage: String = ""
    /// When true, sits inside a scroll instead of filling the screen.
    var compact: Bool = false

    var body: some View {
        VStack(spacing: Theme.spaceM) {
            if !compact { Spacer(minLength: 60) }
            Text(title)
                .font(Theme.title(compact ? 22 : 26))
                .foregroundStyle(Theme.ink)
                .multilineTextAlignment(.center)
            if let description {
                Text(description)
                    .font(.body)
                    .foregroundStyle(Theme.soft)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, compact ? 0 : Theme.spaceXL)
            }
            if let retryTitle, let retry {
                Button(retryTitle, action: retry)
                    .font(.body.weight(.medium))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 22)
                    .padding(.vertical, 12)
                    .background(Theme.accent, in: Capsule())
                    .padding(.top, Theme.spaceS)
                    .buttonStyle(PressableButtonStyle(haptic: true))
            }
            if let secondaryTitle, let secondary {
                Button(secondaryTitle, action: secondary)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(Theme.accent)
                    .buttonStyle(PressableButtonStyle())
            }
            if !compact { Spacer() }
        }
        .frame(maxWidth: .infinity, maxHeight: compact ? nil : .infinity)
        .padding(.vertical, compact ? Theme.spaceXL : 0)
        .background { if !compact { AmbientBackground() } }
        .foregroundStyle(Theme.ink)
    }
}

struct InlineError: View {
    let text: String
    var retryTitle: String = "Try again"
    var retry: (() -> Void)? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(text)
                .font(.caption)
                .foregroundStyle(Theme.note)
                .fixedSize(horizontal: false, vertical: true)
            if let retry {
                Button(retryTitle, action: retry)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Theme.accent)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}

struct InstrumentEnter: ViewModifier {
    @State private var appeared = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func body(content: Content) -> some View {
        content
            .opacity(appeared || reduceMotion ? 1 : 0)
            .offset(y: appeared || reduceMotion ? 0 : 8)
            .onAppear {
                if reduceMotion {
                    appeared = true
                    return
                }
                withAnimation(Theme.springSoft) { appeared = true }
            }
    }
}

extension View {
    func instrumentEnter() -> some View { modifier(InstrumentEnter()) }

    func staggerAppear(_ index: Int) -> some View {
        modifier(StaggerAppear(index: index))
    }
}

struct StaggerAppear: ViewModifier {
    let index: Int
    @State private var on = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func body(content: Content) -> some View {
        content
            .opacity(on || reduceMotion ? 1 : 0)
            .offset(y: on || reduceMotion ? 0 : 12)
            .onAppear {
                if reduceMotion || index > 12 {
                    on = true
                    return
                }
                let delay = 0.16 + Double(index) * 0.05
                withAnimation(Theme.springSoft.delay(delay)) { on = true }
            }
    }
}

struct GaugePressStyle: ButtonStyle {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(reduceMotion ? 1 : (configuration.isPressed ? 0.97 : 1))
            .rotation3DEffect(
                .degrees(reduceMotion ? 0 : (configuration.isPressed ? 5 : 0)),
                axis: (x: 1, y: -0.15, z: 0)
            )
            .animation(reduceMotion ? nil : Theme.quick, value: configuration.isPressed)
    }
}

struct PressableButtonStyle: ButtonStyle {
    var haptic: Bool = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(reduceMotion ? 1 : (configuration.isPressed ? 0.98 : 1))
            .opacity(configuration.isPressed ? 0.9 : 1)
            .animation(reduceMotion ? nil : Theme.quick, value: configuration.isPressed)
            .onChange(of: configuration.isPressed) { _, pressed in
                if pressed && haptic { Theme.impact(.soft) }
            }
    }
}

struct PrimaryButton: View {
    let title: String
    var systemImage: String? = nil
    var busy: Bool = false
    var busyTitle: String = "Preparing…"
    let action: () -> Void

    @State private var shine = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.isEnabled) private var isEnabled

    private var live: Bool { isEnabled || busy }

    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                if busy {
                    PropellerIcon(speed: .medium, size: 16)
                        .foregroundStyle(.white)
                } else if let systemImage {
                    Image(systemName: systemImage)
                }
                Text(busy ? busyTitle : title)
                    .font(.body.weight(.semibold))
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 14)
            .foregroundStyle(live ? Color.white : Theme.trail)
            .background((live ? Theme.accent : Theme.cloud), in: Capsule())
            .overlay {
                if live && !reduceMotion {
                    Capsule()
                        .fill(
                            LinearGradient(
                                colors: [.clear, Color.white.opacity(0.38), .clear],
                                startPoint: .leading,
                                endPoint: .trailing
                            )
                        )
                        .offset(x: shine ? 240 : -240)
                        .allowsHitTesting(false)
                }
            }
            .clipShape(Capsule())
        }
        .buttonStyle(PressableButtonStyle(haptic: true))
        .disabled(busy)
        .onAppear {
            guard !reduceMotion, live else { return }
            shine = false
            withAnimation(Theme.sheen.delay(0.35)) { shine = true }
        }
    }
}

struct FocusCard<Content: View>: View {
    var prominent: Bool = false
    var padded: Bool = true
    @ViewBuilder var content: Content

    @State private var gauge: CGFloat = 0
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        content
            .padding(padded ? Theme.spaceL : 0)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous)
                    .fill(Theme.cardFill)
            )
            .overlay {
                if prominent {
                    RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous)
                        .fill(
                            LinearGradient(
                                colors: [Color.white.opacity(0.55), Color.white.opacity(0)],
                                startPoint: .topLeading,
                                endPoint: UnitPoint(x: 0.55, y: 0.5)
                            )
                        )
                        .allowsHitTesting(false)
                }
            }
            .overlay(alignment: .top) {
                if prominent {
                    Capsule()
                        .fill(Theme.accent.opacity(0.85))
                        .frame(height: 2.5)
                        .padding(.horizontal, 28)
                        .scaleEffect(x: gauge, y: 1, anchor: .leading)
                        .padding(.top, 10)
                        .allowsHitTesting(false)
                }
            }
            .overlay(
                RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous)
                    .strokeBorder(Theme.accent.opacity(prominent ? 0.22 : 0.08), lineWidth: 1)
            )
            .shadow(
                color: Theme.ink.opacity(prominent ? 0.08 : 0.035),
                radius: prominent ? 24 : 14,
                y: prominent ? 10 : 6
            )
            .onAppear {
                gauge = reduceMotion ? 1 : 0
                withAnimation(Theme.spring.delay(0.08)) { gauge = 1 }
            }
    }
}

/// One grouped surface for a stack of match / filed rows.
struct GroupedSurface<Content: View>: View {
    @ViewBuilder var content: Content

    var body: some View {
        VStack(spacing: 0) { content }
            .background(
                RoundedRectangle(cornerRadius: 20, style: .continuous)
                    .fill(Theme.rowFill)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 20, style: .continuous)
                    .strokeBorder(Theme.accent.opacity(0.08), lineWidth: 1)
            )
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

/// Three fitted files is a sitting. Hitting the goal is permission to stop.
struct SittingStrip: View {
    let momentum: Momentum

    private var fraction: CGFloat {
        let goal = max(1, momentum.sitting_goal)
        return min(1, CGFloat(momentum.filed_today) / CGFloat(goal))
    }

    private var countLabel: String {
        if momentum.sitting_done {
            return momentum.filed_today > momentum.sitting_goal
                ? "\(momentum.filed_today) today"
                : "Done"
        }
        return "\(momentum.filed_today)/\(momentum.sitting_goal)"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(momentum.sitting_line)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(Theme.ink)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 8)
                Text(countLabel)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Theme.horizon)
                    .monospacedDigit()
            }
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Theme.cloud.opacity(0.7))
                    Capsule()
                        .fill(Theme.accent)
                        .frame(width: max(6, geo.size.width * fraction))
                }
            }
            .frame(height: 4)
            if let ranker = momentum.ranker_line {
                Text(ranker)
                    .font(.caption)
                    .foregroundStyle(Theme.soft)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(14)
        .background(Theme.cardFill, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .padding(.horizontal, Theme.spaceL)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityText)
    }

    private var accessibilityText: String {
        if let ranker = momentum.ranker_line {
            return "\(momentum.sitting_line). \(ranker)"
        }
        return momentum.sitting_line
    }
}

// MARK: - Up-next hero (editorial)

/// Leading hit target for reorder — no visible handle.
struct GrabStrip: View {
    var body: some View {
        Color.clear
            .frame(width: 14, height: 48)
            .contentShape(Rectangle())
            .accessibilityLabel("Reorder")
    }
}

// MARK: - Next role as a gauge

struct UpNextCard: View {
    let item: QueueItem
    var kicker: String = "Next"
    var actionTitle: String
    var busy: Bool = false
    var showTriage: Bool = false
    var onLater: (() -> Void)? = nil
    var onPass: (() -> Void)? = nil
    let action: () -> Void

    @State private var shownScore: Double = 0
    @State private var copyReady = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var reasonLine: String? {
        if let r = item.reasons?.first, !r.isEmpty { return r }
        if let w = item.why, !w.isEmpty { return w }
        return nil
    }

    private var targetScore: Double {
        (item.score ?? 0) * 100
    }

    private let gaugeSize: CGFloat = 260

    var body: some View {
        VStack(spacing: 12) {
            Button(action: action) {
                ZStack {
                    GlassDisc(size: gaugeSize)

                    SoftRing(
                        progress: shownScore / 100,
                        size: gaugeSize,
                        lineWidth: 8,
                        ticks: true
                    )

                    CrystalCover(size: gaugeSize)

                    HUDReticle {
                        Text("\(Int(shownScore.rounded()))")
                            .font(.system(size: 88, weight: .bold, design: .rounded).monospacedDigit())
                            .foregroundStyle(Theme.accent)
                            .padding(.horizontal, -6)
                            .padding(.vertical, -12)
                            .contentTransition(reduceMotion ? .identity : .numericText())
                            .accessibilityLabel("Match score \(Int(shownScore.rounded())) percent")
                    }
                    .id(item.posting_id)
                }
                .frame(width: gaugeSize, height: gaugeSize)
            }
            .buttonStyle(GaugePressStyle())
            .disabled(busy)
            .frame(maxWidth: .infinity)

            // The same dial face also shows identity Coverage on You and on the
            // quiz's last step — and the quiz hands straight over to this screen,
            // so an unlabelled number here read as the coverage figure carried
            // forward. Both faces are captioned now; neither is bare.
            Text("Match")
                .font(.caption.weight(.semibold))
                .foregroundStyle(Theme.horizon)
                .textCase(.uppercase)
                .tracking(0.8)
                .accessibilityHidden(true)

            VStack(spacing: 4) {
                Text(item.company ?? "Company")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(Theme.ink)
                    .multilineTextAlignment(.center)
                    .lineLimit(1)
                Text(item.title ?? "Role")
                    .font(.subheadline)
                    .foregroundStyle(Theme.soft)
                    .multilineTextAlignment(.center)
                    .lineLimit(2)
                HStack(spacing: 6) {
                    Text(item.applyKindLabel)
                        .font(.caption.weight(.medium))
                        .foregroundStyle(Theme.horizon)
                    if let reasonLine {
                        Text("·")
                            .foregroundStyle(Theme.soft.opacity(0.6))
                        Text(reasonLine)
                            .font(.caption)
                            .foregroundStyle(Theme.soft.opacity(0.85))
                            .lineLimit(1)
                    }
                }
                .multilineTextAlignment(.center)
            }
            .padding(.horizontal, Theme.spaceL)
            .padding(.top, 4)
            .opacity(copyReady || reduceMotion ? 1 : 0)
            .offset(y: copyReady || reduceMotion ? 0 : 6)

            VStack(spacing: 8) {
                PrimaryButton(title: actionTitle, busy: busy, action: action)
                    .frame(maxWidth: 280)

                if showTriage {
                    HStack(spacing: 28) {
                        Button("Later") { onLater?() }
                            .font(.subheadline.weight(.medium))
                            .foregroundStyle(Theme.soft)
                        Button("Pass") { onPass?() }
                            .font(.subheadline.weight(.medium))
                            .foregroundStyle(Theme.note)
                    }
                    .buttonStyle(PressableButtonStyle())
                }
            }
        }
        .onAppear { present() }
        .onChange(of: item.posting_id) { _, _ in present() }
        .animation(reduceMotion ? nil : Theme.springSoft, value: item.posting_id)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(kicker). \(item.company ?? "Company"). \(item.title ?? "Role")")
    }

    private func present() {
        if reduceMotion {
            shownScore = targetScore
            copyReady = true
            return
        }
        shownScore = 0
        copyReady = false
        withAnimation(Theme.tick.delay(0.16)) { shownScore = targetScore }
        withAnimation(Theme.springSoft.delay(0.32)) { copyReady = true }
    }
}

/// Horizontal tape chip — company + score, no Preflight.
struct MatchTapeChip: View {
    let item: QueueItem
    var focused: Bool = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(item.company ?? "—")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(Theme.ink)
                .lineLimit(1)
            if let sc = item.score {
                ScoreMark(score: sc, size: 13)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .frame(width: 128, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .fill(Color.white.opacity(focused ? 0.98 : 0.88))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .strokeBorder(Theme.accent.opacity(focused ? 0.28 : 0.08), lineWidth: 1)
        )
        .scaleEffect(reduceMotion ? 1 : (focused ? 1 : 0.94))
        .animation(reduceMotion ? nil : Theme.springSoft, value: focused)
    }
}

/// Mail-style Later / Pass swipe. Later is the full-swipe.
struct MatchTriageRow<Content: View>: View {
    var onLater: () -> Void
    var onPass: () -> Void
    var compact: Bool = false
    @ViewBuilder var content: Content

    @State private var offset: CGFloat = 0
    @GestureState private var drag: CGFloat = 0

    private let reveal: CGFloat = 152
    private var revealed: CGFloat { offset + drag }
    private var showingActions: Bool { revealed < -2 }

    var body: some View {
        content
            .frame(maxWidth: compact ? nil : .infinity, alignment: .leading)
            .background(compact ? Color.clear : Theme.rowFill)
            .offset(x: revealed)
            .background(alignment: .trailing) {
                HStack(spacing: 0) {
                    Button {
                        settle()
                        onLater()
                    } label: {
                        Text("Later")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(.white)
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                            .background(Theme.horizon)
                    }
                    .frame(width: compact ? 64 : 76)
                    Button {
                        settle()
                        onPass()
                    } label: {
                        Text("Pass")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(.white)
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                            .background(Theme.note)
                    }
                    .frame(width: compact ? 64 : 76)
                }
                .opacity(showingActions ? 1 : 0)
                .clipShape(RoundedRectangle(cornerRadius: compact ? 16 : 0, style: .continuous))
            }
            .clipped()
            .simultaneousGesture(
                DragGesture(minimumDistance: 28, coordinateSpace: .local)
                    .updating($drag) { value, state, _ in
                        let t = value.translation
                        guard abs(t.width) > abs(t.height) * 1.15 else { return }
                        if offset == 0 {
                            state = min(0, t.width)
                        } else {
                            state = min(-offset, t.width)
                        }
                    }
                    .onEnded { value in
                        let t = value.translation
                        guard abs(t.width) > abs(t.height) else {
                            withAnimation(Theme.springSoft) { offset = 0 }
                            return
                        }
                        let predicted = offset + value.predictedEndTranslation.width
                        withAnimation(Theme.springSoft) {
                            if predicted < -reveal * 0.85 {
                                offset = 0
                                onLater()
                            } else if offset + t.width < -56 {
                                offset = -reveal
                            } else {
                                offset = 0
                            }
                        }
                    }
            )
    }

    private func settle() {
        withAnimation(Theme.quick) { offset = 0 }
    }
}

/// Horizontal wrap for suggestion chips.
struct WrapHStack: Layout {
    var spacing: CGFloat = 8
    var lineSpacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        arrange(in: proposal.width ?? 0, subviews: subviews).size
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let placed = arrange(in: bounds.width, subviews: subviews)
        for (i, origin) in placed.origins.enumerated() {
            subviews[i].place(
                at: CGPoint(x: bounds.minX + origin.x, y: bounds.minY + origin.y),
                proposal: .unspecified
            )
        }
    }

    private func arrange(in width: CGFloat, subviews: Subviews) -> (size: CGSize, origins: [CGPoint]) {
        var origins: [CGPoint] = []
        var x: CGFloat = 0
        var y: CGFloat = 0
        var rowH: CGFloat = 0
        var maxX: CGFloat = 0
        let limit = max(width, 1)
        for sub in subviews {
            let size = sub.sizeThatFits(.unspecified)
            if x > 0, x + size.width > limit {
                x = 0
                y += rowH + lineSpacing
                rowH = 0
            }
            origins.append(CGPoint(x: x, y: y))
            rowH = max(rowH, size.height)
            x += size.width + spacing
            maxX = max(maxX, x - spacing)
        }
        return (CGSize(width: max(width, maxX), height: y + rowH), origins)
    }
}

/// Two tappable halves of one instrument card (You: Looking for / On forms).
struct SplitInstrumentCard: View {
    let leftTitle: String
    let leftBody: String
    let leftCaption: String
    let rightTitle: String
    let rightBody: String
    let rightCaption: String
    let onLeft: () -> Void
    let onRight: () -> Void

    var body: some View {
        FocusCard(padded: false) {
            HStack(alignment: .top, spacing: 0) {
                half(title: leftTitle, body: leftBody, caption: leftCaption, action: onLeft)
                Rectangle()
                    .fill(Theme.cloud.opacity(0.7))
                    .frame(width: 1)
                    .padding(.vertical, 16)
                half(title: rightTitle, body: rightBody, caption: rightCaption, action: onRight)
            }
        }
    }

    private func half(title: String, body: String, caption: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text(title)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Theme.horizon)
                        .textCase(.uppercase)
                        .tracking(0.8)
                    Spacer(minLength: 4)
                    Image(systemName: "chevron.right")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Theme.soft.opacity(0.7))
                }
                Text(body)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(Theme.ink)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
                    .lineLimit(3)
                Text(caption)
                    .font(.caption)
                    .foregroundStyle(Theme.soft)
                    .fixedSize(horizontal: false, vertical: true)
                    .lineLimit(2)
            }
            .padding(Theme.spaceL)
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityHint("Edit \(title.lowercased())")
    }
}

struct ExpandRow<Content: View>: View {
    let title: String
    @State private var open: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let content: Content

    init(title: String, startsOpen: Bool = false, @ViewBuilder content: () -> Content) {
        self.title = title
        _open = State(initialValue: startsOpen)
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Button {
                withAnimation(reduceMotion ? nil : Theme.springSoft) { open.toggle() }
            } label: {
                HStack {
                    Text(title)
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(Theme.ink)
                    Spacer()
                    Image(systemName: "chevron.down")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Theme.soft)
                        .rotationEffect(.degrees(open ? 180 : 0))
                }
                .frame(minHeight: 28)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(title)
            .accessibilityValue(open ? "Expanded" : "Collapsed")
            .accessibilityHint("Shows or hides this section")
            if open {
                content
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
    }
}

/// Compact horizontal card for the Ready strip.
struct ReadyChipCard: View {
    let item: QueueItem
    var body: some View { MatchTapeChip(item: item) }
}

// MARK: - Coverage

struct CoverageMeter: View {
    let score: Double
    var missing: [String] = []
    var suggestion: String? = nil
    var onSuggestion: (() -> Void)? = nil

    @State private var shown = 0.0
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var pct: Int { Int((shown * 100).rounded()) }
    private var line: String {
        if score >= 0.99 { return "Autofill has everything it needs." }
        if score >= 0.7 { return "Enough to fill forms well." }
        if score >= 0.4 { return "A few details still missing." }
        return "Add more about you for better fills."
    }

    var body: some View {
        VStack(spacing: Theme.spaceM) {
            ZStack {
                GlassDisc(size: 176)
                SoftRing(progress: shown, size: 176, lineWidth: 7, ticks: true)
                CrystalCover(size: 176)
                HUDReticle {
                    Text("\(pct)")
                        .font(.system(size: 48, weight: .bold, design: .rounded).monospacedDigit())
                        .foregroundStyle(Theme.accent)
                        .padding(.horizontal, -4)
                        .padding(.vertical, -8)
                        .contentTransition(reduceMotion ? .identity : .numericText())
                }
            }
            .frame(maxWidth: .infinity)

            Text("Coverage")
                .font(.caption.weight(.semibold))
                .foregroundStyle(Theme.horizon)
                .textCase(.uppercase)
                .tracking(0.8)

            Text(line)
                .font(.subheadline)
                .foregroundStyle(Theme.soft)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
            if !missing.isEmpty {
                Text("Still missing: " + missing.joined(separator: ", "))
                    .font(.caption)
                    .foregroundStyle(Theme.soft)
                    .multilineTextAlignment(.center)
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
        .frame(maxWidth: .infinity)
        .onAppear { tick(to: score) }
        .onChange(of: score) { _, new in tick(to: new) }
    }

    private func tick(to value: Double) {
        if reduceMotion {
            shown = value
            return
        }
        shown = 0
        withAnimation(Theme.tick.delay(0.12)) { shown = value }
    }
}

/// Full-screen wait. A spinning propeller inside a sweeping radar arc, over a
/// pulse that leaves the hub on a slow beat.
///
/// `notes` turns a wait into a report: when work takes more than a moment,
/// naming what is happening ("Scanning job boards…", "Scoring matches…") reads
/// as progress, while one frozen line reads as a hang. They rotate on a timer
/// because the backend gives no per-stage signal — so the copy stays honestly
/// generic rather than claiming a step it can't observe.
struct PreparingView: View {
    var message: String = "Just a moment…"
    var notes: [String] = []

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var noteIndex = 0

    private let noteInterval: TimeInterval = 2.6

    var body: some View {
        VStack(spacing: Theme.spaceL) {
            ZStack {
                PulseRings()
                RadarSweep()
                PropellerIcon(speed: .medium, size: 40)
                    .foregroundStyle(Theme.accent)
            }
            .frame(width: 120, height: 120)

            VStack(spacing: 6) {
                Text(message)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(Theme.ink)

                if !notes.isEmpty {
                    Text(notes[noteIndex % notes.count])
                        .font(.caption)
                        .foregroundStyle(Theme.soft)
                        .id(noteIndex)
                        .transition(.opacity)
                        .multilineTextAlignment(.center)
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .ambientScreen()
        .task {
            guard notes.count > 1 else { return }
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(noteInterval))
                if Task.isCancelled { return }
                withAnimation(.easeInOut(duration: 0.35)) { noteIndex += 1 }
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(message)
        .accessibilityAddTraits(.updatesFrequently)
    }
}

/// Two rings leaving the hub, half a cycle apart, fading as they grow.
struct PulseRings: View {
    var color: Color = Theme.cockpit
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private let period: Double = 2.8

    var body: some View {
        TimelineView(.animation(minimumInterval: reduceMotion ? 120 : 1.0 / 30.0,
                                paused: reduceMotion)) { context in
            let t = context.date.timeIntervalSinceReferenceDate
            ZStack {
                ForEach(0..<2, id: \.self) { i in
                    let phase = ((t / period) + Double(i) * 0.5).truncatingRemainder(dividingBy: 1)
                    let eased = 1 - pow(1 - phase, 2)
                    Circle()
                        .strokeBorder(color.opacity(0.28 * (1 - phase)), lineWidth: 1.5)
                        .frame(width: 52 + 66 * eased, height: 52 + 66 * eased)
                }
            }
            .opacity(reduceMotion ? 0 : 1)
        }
        .allowsHitTesting(false)
    }
}

/// A conic wedge orbiting the hub — the "we are looking" cue.
struct RadarSweep: View {
    var color: Color = Theme.horizon
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private let period: Double = 2.2

    var body: some View {
        TimelineView(.animation(minimumInterval: reduceMotion ? 120 : 1.0 / 30.0,
                                paused: reduceMotion)) { context in
            let t = context.date.timeIntervalSinceReferenceDate
            let angle = (t.truncatingRemainder(dividingBy: period) / period) * 360
            Circle()
                .fill(
                    AngularGradient(
                        colors: [color.opacity(0.0), color.opacity(0.0),
                                 color.opacity(0.22), color.opacity(0.0)],
                        center: .center
                    )
                )
                .frame(width: 104, height: 104)
                .rotationEffect(.degrees(angle))
                .opacity(reduceMotion ? 0 : 1)
        }
        .allowsHitTesting(false)
    }
}

/// Placeholder rows with a light sweeping across them. Used where the shape of
/// the content is already known — a list that is about to arrive reads better
/// as its own silhouette than as a spinner in an empty rectangle.
struct SkeletonList: View {
    var rows: Int = 3

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(spacing: Theme.spaceM) {
            ForEach(0..<rows, id: \.self) { i in
                SkeletonRow()
                    .opacity(1 - Double(i) * 0.18)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Loading")
    }
}

struct SkeletonRow: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            bar(width: 0.45, height: 13)
            bar(width: 0.85, height: 11)
            bar(width: 0.62, height: 11)
        }
        .padding(Theme.spaceM)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous)
                .fill(Theme.cardFill)
        )
        .overlay {
            if !reduceMotion {
                GeometryReader { geo in
                    TimelineView(.animation(minimumInterval: 1.0 / 30.0)) { context in
                        let t = context.date.timeIntervalSinceReferenceDate
                        let p = (t / 1.6).truncatingRemainder(dividingBy: 1)
                        LinearGradient(
                            colors: [.clear, Color.white.opacity(0.55), .clear],
                            startPoint: .leading, endPoint: .trailing
                        )
                        .frame(width: geo.size.width * 0.4)
                        .offset(x: -geo.size.width * 0.4 + geo.size.width * 1.8 * p)
                    }
                }
                .clipShape(RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous))
                .allowsHitTesting(false)
            }
        }
    }

    private func bar(width: CGFloat, height: CGFloat) -> some View {
        GeometryReader { geo in
            Capsule()
                .fill(Theme.cloud.opacity(0.6))
                .frame(width: geo.size.width * width, height: height)
        }
        .frame(height: height)
    }
}

enum KnowledgeCategoryStyle {
    static func symbol(for category: String) -> String {
        switch category.lowercased() {
        case "experience": return "briefcase"
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
        case "experience": return "Experience"
        case "project": return "Projects"
        case "achievement": return "Achievements"
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

enum QuizList {
    static func split(_ raw: String) -> [String] {
        raw.split { $0 == "," || $0 == ";" || $0 == "\n" }
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }

    static func join(_ items: [String]) -> String {
        var seen: [String] = []
        for item in items {
            let trimmed = item.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty else { continue }
            if !seen.contains(where: { $0.caseInsensitiveCompare(trimmed) == .orderedSame }) {
                seen.append(trimmed)
            }
        }
        return seen.joined(separator: ", ")
    }
}

/// Tappable chips plus an optional custom field. Stores a comma-separated string
/// so search profile / identity payloads stay backend-compatible.
///
/// Give it a `field` and the chips refill: after every tap it asks the server
/// for the next batch, ranked against what was just picked and against the
/// profile we already have. A fixed row of eight runs out on the second tap,
/// which is what sent everyone to the keyboard. `suggestions` stays as the
/// offline list — shown before the first batch lands and if the request fails,
/// so the row is never empty.
struct TagEditor: View {
    @Binding var text: String
    var suggestions: [String] = []
    var placeholder: String = "Add another"
    var allowCustom: Bool = true
    var caption: String? = nil
    /// Catalog to refill from (`skills`, `roles`, `locations`, `disciplines`,
    /// `degrees`, `languages`, `how_heard`). Nil keeps the static list.
    var field: String? = nil

    @State private var draft = ""
    @State private var pool: [String] = []
    @State private var remaining = 0
    @State private var refill: Task<Void, Never>?

    private var tags: [String] { QuizList.split(text) }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let caption, !caption.isEmpty {
                Text(caption)
                    .font(.caption)
                    .foregroundStyle(Theme.soft)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !tags.isEmpty {
                WrapHStack(spacing: 8, lineSpacing: 8) {
                    ForEach(tags, id: \.self) { tag in
                        SelectChip(label: tag, selected: true, removable: true) {
                            remove(tag)
                        }
                    }
                }
            }
            if allowCustom {
                HStack(spacing: 8) {
                    TextField(placeholder, text: $draft)
                        .textInputAutocapitalization(.words)
                        .submitLabel(.done)
                        .onSubmit { add(draft) }
                    if !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        Button("Add") { add(draft) }
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(Theme.accent)
                    }
                }
            }
            let unused = offered.filter { sug in
                !tags.contains { $0.caseInsensitiveCompare(sug) == .orderedSame }
            }
            if !unused.isEmpty {
                WrapHStack(spacing: 8, lineSpacing: 8) {
                    ForEach(unused, id: \.self) { sug in
                        SelectChip(label: sug, selected: false) {
                            add(sug)
                        }
                    }
                    if remaining > 0 {
                        Button {
                            reload(shuffle: true)
                        } label: {
                            Label("More", systemImage: "arrow.clockwise")
                                .font(.subheadline)
                                .foregroundStyle(Theme.accent)
                                .padding(.horizontal, 14)
                                .padding(.vertical, 8)
                        }
                        .buttonStyle(PressableButtonStyle())
                        .accessibilityLabel("Show more suggestions")
                    }
                }
            }
        }
        .task(id: "\(field ?? "")|\(text)") { reload() }
        .onDisappear { refill?.cancel() }
    }

    /// Server batch once we have one; the bundled list until then.
    private var offered: [String] { pool.isEmpty ? suggestions : pool }

    private func add(_ raw: String) {
        let pieces = QuizList.split(raw)
        guard !pieces.isEmpty else { return }
        text = QuizList.join(tags + pieces)
        draft = ""
        Theme.selection()
    }

    private func remove(_ tag: String) {
        text = QuizList.join(tags.filter { $0.caseInsensitiveCompare(tag) != .orderedSame })
        Theme.selection()
    }

    /// Fetch the next batch. `shuffle` is the More button: it asks past the
    /// batch we're already showing so the row actually changes, rather than
    /// re-fetching the same top twelve.
    private func reload(shuffle: Bool = false) {
        guard let field, !field.isEmpty else { return }
        let skip = shuffle ? offered : []
        let chosen = tags + skip
        refill?.cancel()
        refill = Task { @MainActor in
            guard let batch = try? await APIClient(config: Config.shared)
                .suggestions(field: field, chosen: chosen) else { return }
            guard !Task.isCancelled, batch.known != false else { return }
            let next = batch.suggestions ?? []
            // An empty batch means the catalog is spent. Keep what's on screen
            // rather than blanking the row back to the bundled list.
            guard !next.isEmpty else {
                remaining = 0
                return
            }
            pool = next
            remaining = batch.remaining ?? 0
        }
    }
}

struct SelectChip: View {
    let label: String
    let selected: Bool
    var removable: Bool = false
    let action: () -> Void
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Button(action: action) {
            HStack(spacing: 4) {
                Text(label)
                    .font(.subheadline.weight(selected ? .semibold : .regular))
                if selected && removable {
                    Image(systemName: "xmark")
                        .font(.system(size: 9, weight: .bold))
                }
            }
            .foregroundStyle(selected ? Color.white : Theme.ink)
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
            .background(selected ? Theme.accent : Theme.cardFill, in: Capsule())
            .overlay(
                Capsule().strokeBorder(Theme.cloud.opacity(selected ? 0 : 0.9), lineWidth: 1)
            )
            .animation(reduceMotion ? nil : Theme.quick, value: selected)
        }
        .buttonStyle(PressableButtonStyle())
        .accessibilityAddTraits(selected ? .isSelected : [])
        .accessibilityLabel(removable && selected ? "\(label), selected, double tap to remove" : label)
    }
}

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
