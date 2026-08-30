import SwiftUI

/// Three-blade propeller. Still in chrome; spinning only while work is in flight.
/// Respects Reduce Motion: never spins, stays at rest.
struct PropellerIcon: View {
    enum Speed {
        case still, slow, medium, fast

        var period: Double {
            switch self {
            case .still: return 0
            case .slow: return Theme.propSlow
            case .medium: return Theme.propMedium
            case .fast: return Theme.propFast
            }
        }
    }

    var speed: Speed = .still
    var size: CGFloat = 18
    /// One springy blade-tick (tab land). Not idle spin.
    var nudge: Bool = false

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var spinning: Bool { speed != .still && !reduceMotion }

    var body: some View {
        TimelineView(.animation(minimumInterval: spinning ? 1.0 / 30.0 : 120,
                                paused: !spinning)) { context in
            let degrees: Double = {
                guard spinning, speed.period > 0 else { return 0 }
                let t = context.date.timeIntervalSinceReferenceDate
                return (t.truncatingRemainder(dividingBy: speed.period) / speed.period) * 360
            }()
            PropellerGlyph()
                .frame(width: size, height: size)
                .rotationEffect(.degrees(degrees + (nudge && !reduceMotion ? 50 : 0)))
        }
        .frame(width: size, height: size)
        .animation(Theme.spring, value: nudge)
        .accessibilityHidden(true)
    }
}

/// Thin VTOL tri-blade: high aspect ratio, clipped tips, small hub.
struct PropellerGlyph: View {
    static let parkedDegrees: Double = 18

    var body: some View {
        GeometryReader { geo in
            let s = min(geo.size.width, geo.size.height)
            ZStack {
                ForEach(0..<3, id: \.self) { i in
                    blade(s)
                        .offset(y: -s * 0.305)
                        .rotationEffect(.degrees(Double(i) * 120))
                }
                spinner(s)
            }
            .compositingGroup()
            .overlay {
                if s >= 32 {
                    Circle()
                        .fill(Color.black)
                        .frame(width: s * 0.10 * 0.36, height: s * 0.10 * 0.36)
                        .blendMode(.destinationOut)
                }
            }
            .compositingGroup()
            .rotationEffect(.degrees(Self.parkedDegrees))
            .frame(width: s, height: s)
        }
    }

    private func blade(_ s: CGFloat) -> some View {
        ZStack {
            PropellerBlade()
            PropellerBlade()
                .fill(
                    LinearGradient(
                        stops: [
                            .init(color: .black.opacity(0.22), location: 0),
                            .init(color: .white.opacity(0.7), location: 0.28),
                            .init(color: .white.opacity(0.1), location: 0.55),
                            .init(color: .black.opacity(0.48), location: 1)
                        ],
                        startPoint: .leading,
                        endPoint: .trailing
                    )
                )
                .blendMode(.overlay)
        }
        .frame(width: s * 0.16, height: s * 0.50)
    }

    private func spinner(_ s: CGFloat) -> some View {
        let d = s * 0.10
        return ZStack {
            Circle()
                .frame(width: d, height: d)
            Circle()
                .fill(
                    RadialGradient(
                        colors: [
                            .white.opacity(0.55),
                            .white.opacity(0.04),
                            .black.opacity(0.35)
                        ],
                        center: UnitPoint(x: 0.34, y: 0.30),
                        startRadius: 0,
                        endRadius: d * 0.62
                    )
                )
                .blendMode(.overlay)
                .frame(width: d, height: d)
        }
    }
}

/// Skinny FPV/VTOL blade, pointing up. Slight scimitar, clipped tip.
struct PropellerBlade: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        func pt(_ xn: CGFloat, _ yn: CGFloat) -> CGPoint {
            CGPoint(
                x: rect.midX + xn * rect.width,
                y: rect.minY + yn * rect.height
            )
        }

        path.move(to: pt(-0.18, 0.995))
        path.addCurve(
            to: pt(-0.42, 0.48),
            control1: pt(-0.20, 0.86),
            control2: pt(-0.36, 0.64)
        )
        path.addCurve(
            to: pt(-0.16, 0.05),
            control1: pt(-0.44, 0.28),
            control2: pt(-0.28, 0.12)
        )
        path.addLine(to: pt(0.20, 0.14))
        path.addCurve(
            to: pt(0.26, 0.50),
            control1: pt(0.24, 0.24),
            control2: pt(0.28, 0.36)
        )
        path.addCurve(
            to: pt(0.14, 0.995),
            control1: pt(0.22, 0.70),
            control2: pt(0.16, 0.88)
        )
        path.closeSubpath()
        return path
    }
}
