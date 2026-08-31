import Foundation

/// Horizon as an onboarding coach.
///
/// `OnDeviceSession` only ever *classified* a turn into an action, so Horizon
/// could route you but never explain anything. Asked "what do I do first?" the
/// bare on-device model answered with generic job-board advice — it told testers
/// to upload a cover letter, which this app never asks for during onboarding.
///
/// So the guidance here is **grounded**: the prompt carries what JobPilot really
/// does, which quiz step you're on, and which fields are still empty. The model
/// gets facts instead of guessing them.
///
/// Availability is not assumed. Apple Intelligence needs iOS 26 and a capable,
/// opted-in device, so every step also has a written tip. `guidance` always
/// returns something useful; `onDevice` says whether the model or the fallback
/// produced it.
enum HorizonCoach {

    struct Context {
        var stepTitle: String
        var stepSubtitle: String
        var stepIndex: Int          // 0-based
        var stepCount: Int
        var missing: [String]       // identity_missing from GET /apply/setup
        var score: Double           // identity coverage 0…1
        var alreadyFilled: [String] // what import already read, for "don't retype this"
        /// From `QuizStep.skippable`. Told to the model explicitly: left to infer
        /// it, Horizon advised skipping the search-criteria step, which is the one
        /// step the app requires.
        var isSkippable: Bool = true

        var stepLabel: String { "\(stepIndex + 1) of \(stepCount)" }
    }

    /// True when the on-device model can answer. False falls back to written tips.
    static var isOnDevice: Bool {
        if #available(iOS 26, *) {
            #if canImport(FoundationModels)
            return OnDeviceSession.isAvailable
            #else
            return false
            #endif
        }
        return false
    }

    /// Grounded guidance for the step the person is looking at.
    static func guidance(_ ctx: Context) async -> (text: String, onDevice: Bool) {
        if #available(iOS 26, *) {
            #if canImport(FoundationModels)
            if OnDeviceSession.isAvailable,
               let answer = await OnDeviceSession.coach(prompt: prompt(ctx)) {
                let cleaned = answer.trimmingCharacters(in: .whitespacesAndNewlines)
                if !cleaned.isEmpty { return (cleaned, true) }
            }
            #endif
        }
        return (fallback(ctx), false)
    }

    /// Free-form question about onboarding, answered in the same grounded frame.
    static func answer(question: String, ctx: Context) async -> (text: String, onDevice: Bool) {
        if #available(iOS 26, *) {
            #if canImport(FoundationModels)
            if OnDeviceSession.isAvailable {
                let p = prompt(ctx) + """

                    The person asks: "\(question)"
                    Answer just that, in two or three sentences.
                    """
                if let a = await OnDeviceSession.coach(prompt: p) {
                    let cleaned = a.trimmingCharacters(in: .whitespacesAndNewlines)
                    if !cleaned.isEmpty { return (cleaned, true) }
                }
            }
            #endif
        }
        return (fallback(ctx), false)
    }

    // MARK: - Grounding

    /// What the model is allowed to assume. Every claim here is true of the app,
    /// so Horizon stops inventing steps that don't exist.
    private static let appFacts = """
        Facts about JobPilot you must not contradict:
        - It finds job matches for the person and opens the real application form
          in an in-app browser. Tapping Autofill fills the form from their profile.
        - The person always taps Submit themselves. Autofill never submits.
        - They attach the résumé file themselves; iOS will not let an app do it.
        - Onboarding is a quiz. It never asks for a cover letter.
        - Autofill skips demographic / EEO questions on purpose.
        - Answers can be imported from a résumé PDF, GitHub, or LinkedIn, and
          anything imported is already filled in — no need to retype it.
        - Most steps can be skipped and finished later in the You tab, but not
          all of them; trust the "this step" line below over this general rule.
        """

    private static func prompt(_ ctx: Context) -> String {
        var lines = [
            "You are Horizon, JobPilot's in-app copilot, helping someone through",
            "first-run setup. Be warm, specific and brief: at most three short",
            "sentences, no lists, no headings, no emoji.",
            "",
            appFacts,
            "",
            "Where they are right now:",
            "- Step \(ctx.stepLabel): \"\(ctx.stepTitle)\" — \(ctx.stepSubtitle)",
            "- Profile completeness: \(Int((ctx.score * 100).rounded()))%",
        ]
        if !ctx.alreadyFilled.isEmpty {
            lines.append("- Already filled from their import: "
                         + ctx.alreadyFilled.prefix(8).joined(separator: ", "))
        }
        if !ctx.missing.isEmpty {
            lines.append("- Still empty: " + ctx.missing.prefix(8).joined(separator: ", "))
        }
        lines.append(ctx.isSkippable
            ? "- This step is optional: they can skip it and finish later in You."
            : "- This step is REQUIRED and cannot be skipped. Never suggest skipping it.")
        lines += [
            "",
            "Explain what this step is asking for and why it helps Autofill on real",
            "applications. If something on this step is already filled, say they can",
            "move on. Do not invent features. Do not claim to submit anything.",
        ]
        return lines.joined(separator: "\n")
    }

    // MARK: - Written fallback (no Apple Intelligence needed)

    /// Keyed on the step title so it stays readable next to `QuizStep.title`.
    static func fallback(_ ctx: Context) -> String {
        let tip: String
        switch ctx.stepTitle {
        case "Let’s get Autofill ready", "Let's get Autofill ready":
            tip = "Start from a résumé PDF if you have one — it fills your name, "
                + "contact, school and skills in one shot, so most of the remaining "
                + "steps arrive already answered."
        case "What are you looking for?":
            tip = "These roles, locations and skills are what job search actually "
                + "matches on, so this is the one step worth not skipping. Add the "
                + "title you'd accept today, not your dream title."
        case "Your name and contact":
            tip = "Use your legal name and an email you check — these go straight "
                + "onto applications. Anything already filled came from your import."
        case "Where you live":
            tip = "City and state unlock most location questions on forms. ZIP and "
                + "country are asked surprisingly often, so they're worth 10 seconds."
        case "Links forms ask for":
            tip = "LinkedIn and GitHub appear on nearly every engineering "
                + "application. Paste full URLs so Autofill can drop them in as-is."
        case "School":
            tip = "Degree, major and graduation year cover the education block that "
                + "most forms require before they'll let you submit."
        case "Work and authorization":
            tip = "Work authorization and sponsorship show up on almost every "
                + "application. Answering once here means Autofill can handle them "
                + "everywhere instead of stopping to ask you."
        case "Start date and setup":
            tip = "Availability and work arrangement are common required fields. "
                + "Salary is optional — skip it if you'd rather not store a number."
        case "Usual application answers":
            tip = "These are the yes/no questions every form repeats. Set them once "
                + "and Autofill answers them for you; you can override per company."
        case "What they can cite":
            tip = "A project and an achievement give Autofill real material for "
                + "free-text questions. Tap Fill from my profile if you imported a "
                + "résumé already."
        case "Answers you’ll reuse", "Answers you'll reuse":
            tip = "Saved answers get reused word-for-word with no model call, so "
                + "the long questions stop costing you time on every application."
        case "Optional demographics":
            tip = "Entirely optional. Autofill deliberately skips EEO and "
                + "demographic questions, so leaving this blank costs you nothing."
        case "You’re set", "You're set":
            tip = "That's it — job search starts now and matches land in the Apply "
                + "tab. You can top up anything you skipped from the You tab."
        default:
            tip = "Fill in what you're comfortable with and skip the rest — every "
                + "field you answer is one Autofill can fill on a real application."
        }
        if !ctx.missing.isEmpty && ctx.score < 0.99 {
            return tip + " Still empty overall: "
                + ctx.missing.prefix(3).joined(separator: ", ") + "."
        }
        return tip
    }
}
