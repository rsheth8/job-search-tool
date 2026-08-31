import SwiftUI

/// The first 1.9 seconds of the app.
///
/// Everything here is driven off one clock — a `TimelineView` reading elapsed
/// time — rather than a pile of `withAnimation` blocks chained on `onAppear`.
/// Chained state animations drift: if a frame is slow, or the view re-renders
/// mid-sequence, the pieces land out of order and the whole thing reads as
/// broken rather than late. A single clock cannot desynchronise from itself.
///
/// The sequence is a spin-up. The propeller accelerates under constant angular
/// acceleration (θ = ½αt², the real curve, which is why it looks right), the
/// horizon draws out beneath it, the wordmark rises through a sheen, and the
/// whole assembly lifts and hands off to the app.
///
/// With Reduce Motion on, this is a still frame held for 0.7s: same layout,
/// same handoff, no movement.
struct LaunchView: View {
    /// Called once when the sequence is done. The caller swaps the root view.
    var onFinish: () -> Void

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var started = Date()
    @State private var handedOff = false

    #if DEBUG
    /// `simctl launch … -JobPilotLaunchFreeze 0.45` pins the sequence at that
    /// second and never hands off. Screenshotting a 1.75s animation by racing
    /// it is guesswork; this makes any frame inspectable on demand.
    private static var frozenAt: Double? {
        let args = ProcessInfo.processInfo.arguments
        guard let i = args.firstIndex(of: "-JobPilotLaunchFreeze"),
              i + 1 < args.count, let v = Double(args[i + 1]) else { return nil }
        return v
    }
    #endif

    // MARK: Choreography (seconds since start)

    private var total: Double { reduceMotion ? 0.7 : 1.75 }

    /// Deliberately front-loaded. The first build spread these over two seconds
    /// and the screen read as empty for most of it — a launch sequence you have
    /// to wait to see is just a delay. Everything is legible by 0.35s; the rest
    /// of the time is the wordmark settling.
    private enum Beat {
        static let horizon = (start: 0.10, end: 0.58)
        static let prop    = (start: 0.02, end: 0.30)
        static let ring    = (start: 0.26, end: 1.05)
        static let word    = (start: 0.34, end: 0.80)
        static let tagline = (start: 0.68, end: 1.05)
        static let exit    = (start: 1.34, end: 1.75)
    }

    var body: some View {
        TimelineView(.animation(minimumInterval: reduceMotion ? 120 : 1.0 / 60.0,
                                paused: reduceMotion)) { context in
            let live = reduceMotion ? 0.62 : context.date.timeIntervalSince(started)
            #if DEBUG
            let t = Self.frozenAt ?? live
            let frozen = Self.frozenAt != nil
            #else
            let t = live
            let frozen = false
            #endif
            content(at: t)
                .onChange(of: t >= total && !frozen) { _, done in
                    if done { finish() }
                }
        }
        .background(LaunchBackdrop(elapsed: reduceMotion ? 0 : Date().timeIntervalSince(started)))
        .ignoresSafeArea()
        .onAppear {
            started = Date()
            #if DEBUG
            if Self.frozenAt != nil { return }
            #endif
            if reduceMotion {
                // No clock is running, so schedule the handoff directly.
                DispatchQueue.main.asyncAfter(deadline: .now() + total) { finish() }
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("JobPilot")
    }

    private func finish() {
        guard !handedOff else { return }   // TimelineView fires many frames past `total`
        handedOff = true
        onFinish()
    }

    // MARK: Frame

    @ViewBuilder
    private func content(at t: Double) -> some View {
        // The whole assembly lifts and fades on the way out.
        let exit = ramp(t, Beat.exit.start, Beat.exit.end)
        let exitLift = -34.0 * easeIn(exit)

        ZStack {
            VStack(spacing: 0) {
                Spacer(minLength: 0)

                ZStack {
                    departureRing(at: t)
                    propeller(at: t)
                }
                .frame(width: 190, height: 190)

                horizonLine(at: t)
                    .padding(.top, 26)

                wordmark(at: t)
                    .padding(.top, 30)

                tagline(at: t)
                    .padding(.top, 12)

                Spacer(minLength: 0)
            }
            .offset(y: exitLift)
            .opacity(1 - easeIn(exit))
            .scaleEffect(1 + 0.05 * easeIn(exit))
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: Pieces

    /// Constant angular acceleration, then a steady cruise. Real spin-up is
    /// quadratic in time; a linear ramp reads like a video scrubbing forward.
    private func propeller(at t: Double) -> some View {
        let spinUp = 1.05                      // seconds to reach cruise
        let cruiseDegPerSec = 760.0
        let degrees: Double = {
            if reduceMotion { return PropellerGlyph.parkedDegrees }
            if t <= spinUp {
                // θ = ½αt², with α chosen so dθ/dt hits cruise exactly at spinUp.
                let alpha = cruiseDegPerSec / spinUp
                return 0.5 * alpha * t * t
            }
            let atSpinUp = 0.5 * cruiseDegPerSec * spinUp
            return atSpinUp + cruiseDegPerSec * (t - spinUp)
        }()

        let appear = ramp(t, Beat.prop.start, Beat.prop.end)
        let eased = easeOut(appear)

        // No backing plate here. A white disc behind the glyph reads as haze
        // between the blades — they are thin enough that the gap dominates —
        // and the mark goes pale. Fog alone gives a navy glyph plenty of
        // contrast; the glow below is tinted, not white, for the same reason.
        return PropellerGlyph()
            .frame(width: 108, height: 108)
            .foregroundStyle(Theme.cockpit)
            .rotationEffect(.degrees(degrees))
            .scaleEffect(0.78 + 0.22 * easeOutBack(appear))
            .opacity(eased)
            .shadow(color: Theme.cockpit.opacity(0.30 * eased), radius: 26, y: 10)
    }

    /// One ring leaving the hub — the "cleared for departure" beat.
    private func departureRing(at t: Double) -> some View {
        let p = ramp(t, Beat.ring.start, Beat.ring.end)
        let eased = easeOut(p)
        return Circle()
            .strokeBorder(Theme.cockpit.opacity(0.42 * (1 - p)), lineWidth: 2)
            .frame(width: 104 + 104 * eased, height: 104 + 104 * eased)
            .opacity(reduceMotion ? 0 : 1)
    }

    /// A hairline that draws out from the centre in both directions.
    private func horizonLine(at t: Double) -> some View {
        let p = easeOut(ramp(t, Beat.horizon.start, Beat.horizon.end))
        return Rectangle()
            .fill(
                LinearGradient(
                    colors: [.clear, Theme.horizon.opacity(0.85), .clear],
                    startPoint: .leading, endPoint: .trailing
                )
            )
            .frame(width: 260 * p, height: 1.5)
    }

    /// Letters rise in sequence, then one sheen crosses the finished word.
    private func wordmark(at t: Double) -> some View {
        let letters = Array("JobPilot")
        let sheen = ramp(t, Beat.word.end - 0.10, Beat.word.end + 0.55)

        return HStack(spacing: 0) {
            ForEach(letters.indices, id: \.self) { i in
                let stagger = Double(i) * 0.045
                let p = easeOut(ramp(t, Beat.word.start + stagger, Beat.word.end + stagger))
                Text(String(letters[i]))
                    .font(.system(size: 40, weight: .semibold, design: .rounded))
                    .foregroundStyle(Theme.ink)
                    .opacity(p)
                    .offset(y: 16 * (1 - p))
                    .blur(radius: 5 * (1 - p))
            }
        }
        .overlay {
            if !reduceMotion && sheen > 0 && sheen < 1 {
                GeometryReader { geo in
                    LinearGradient(
                        colors: [.clear, Color.white.opacity(0.85), .clear],
                        startPoint: .leading, endPoint: .trailing
                    )
                    .frame(width: geo.size.width * 0.45)
                    .offset(x: -geo.size.width * 0.5 + geo.size.width * 1.6 * sheen)
                    .blendMode(.plusLighter)
                }
                .allowsHitTesting(false)
            }
        }
        .mask {
            // Keeps the sheen inside the glyphs instead of washing the whole row.
            HStack(spacing: 0) {
                ForEach(letters.indices, id: \.self) { i in
                    Text(String(letters[i]))
                        .font(.system(size: 40, weight: .semibold, design: .rounded))
                }
            }
        }
    }

    private func tagline(at t: Double) -> some View {
        let p = easeOut(ramp(t, Beat.tagline.start, Beat.tagline.end))
        return Text("Find it. Prepare it. Send it.")
            .font(.subheadline.weight(.medium))
            .foregroundStyle(Theme.horizon)
            .tracking(0.6)
            .opacity(p * 0.95)
            .offset(y: 8 * (1 - p))
    }
}

// MARK: - Backdrop

/// Slower, heavier cousin of `AmbientBackground`: the orbs start gathered and
/// spread as the sequence runs, so the screen opens up rather than sitting still.
private struct LaunchBackdrop: View {
    var elapsed: Double
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        TimelineView(.animation(minimumInterval: reduceMotion ? 120 : 1.0 / 30.0,
                                paused: reduceMotion)) { context in
            let t = reduceMotion ? 0 : context.date.timeIntervalSinceReferenceDate
            let spread = reduceMotion ? 1.0 : min(1.0, elapsed / 1.4)
            let e = 1 - pow(1 - spread, 3)

            ZStack {
                Theme.fog
                orb(Theme.cockpit.opacity(0.16), 340, x: -110 * e + sin(t / 7) * 12,
                    y: -200 * e + cos(t / 9) * 10, blur: 66)
                orb(Theme.horizon.opacity(0.15), 300, x: 150 * e + cos(t / 8) * 10,
                    y: 120 * e + sin(t / 11) * 12, blur: 54)
                orb(Theme.cloud.opacity(0.55), 260, x: 30 * e + sin(t / 12) * 8,
                    y: 330 * e, blur: 46)

                // Gentle vignette so the centre reads as the focal point.
                RadialGradient(
                    colors: [.clear, Theme.ink.opacity(0.06)],
                    center: .center, startRadius: 120, endRadius: 460
                )
            }
        }
        .ignoresSafeArea()
        .allowsHitTesting(false)
    }

    private func orb(_ color: Color, _ size: CGFloat,
                     x: Double, y: Double, blur: CGFloat) -> some View {
        Circle()
            .fill(color)
            .frame(width: size, height: size)
            .blur(radius: blur)
            .offset(x: x, y: y)
    }
}

// MARK: - Easing

/// 0 before `start`, 1 after `end`, linear between. The building block every
/// beat above is expressed in, so nothing needs its own timing state.
func ramp(_ t: Double, _ start: Double, _ end: Double) -> Double {
    guard end > start else { return t >= end ? 1 : 0 }
    return min(1, max(0, (t - start) / (end - start)))
}

func easeOut(_ p: Double) -> Double { 1 - pow(1 - p, 3) }
func easeIn(_ p: Double) -> Double { p * p }

/// Overshoots slightly and settles — the propeller arriving with a little weight.
func easeOutBack(_ p: Double) -> Double {
    let c1 = 1.70158, c3 = c1 + 1
    return 1 + c3 * pow(p - 1, 3) + c1 * pow(p - 1, 2)
}
