import SwiftUI

/// First-run quiz after Sign in with Apple. New members stay here until they
/// finish; every step except roles can be skipped so Autofill still gets a chance.
///
/// ``mode: .demo`` walks the same screens with sample answers and writes nothing.
enum SetupMode {
    case live
    case demo
}

struct SetupView: View {
    var mode: SetupMode = .live

    @EnvironmentObject var config: Config
    @EnvironmentObject var setup: SetupGate
    @Environment(\.dismiss) private var dismiss

    @State private var step: QuizStep = .welcome
    // Horizon's onboarding coaching (see Agent/HorizonCoach.swift).
    @State private var showCoach = false
    @State private var coachText = ""
    @State private var coachBusy = false
    @State private var coachOnDevice = false
    @State private var roles = ""
    @State private var locations = ""
    @State private var keywords = ""
    @State private var seniority = ""
    @State private var identity = IdentityDraft()
    @State private var project = ""
    @State private var achievement = ""
    @State private var strength = ""
    @State private var preference = ""
    @State private var about = ""
    @State private var whyRole = ""
    @State private var busy = false
    @State private var error: String?
    @State private var didStart = false
    @State private var previewScore: Double = 0
    @State private var showStepJump = false
    @State private var draftBusy = false
    /// A retake (reopened from You → Profile quiz) can be closed. So can a quiz
    /// whose setup status never loaded — trapping someone behind a network blip
    /// helps nobody, and they can always reopen it from You or Matches.
    @State private var canClose = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private static let roleSuggestions = [
        "Software engineer", "Software intern", "Backend", "Full-stack",
        "Frontend", "Mobile", "ML engineer", "Data scientist", "ML intern",
    ]
    private static let locationSuggestions = [
        "Remote", "Chicago", "Minneapolis", "San Francisco", "NYC",
        "Seattle", "Austin", "Boston",
    ]
    private static let skillSuggestions = [
        "Python", "JavaScript", "TypeScript", "React", "Java", "SQL",
        "AWS", "Docker", "FastAPI", "PyTorch",
    ]
    private static var gradYearOptions: [String] {
        let year = Calendar.current.component(.year, from: Date())
        return (0..<8).map { String(year - 1 + $0) }
    }
    /// The terms an internship posting is likely to be hiring for right now.
    private static var internSeasonOptions: [String] {
        let year = Calendar.current.component(.year, from: Date())
        return ["Summer \(year)", "Fall \(year)",
                "Spring \(year + 1)", "Summer \(year + 1)", "Fall \(year + 1)"]
    }
    /// Full month names — what ATS month dropdowns list.
    private static let monthOptions = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ]

    private var api: APIClient { APIClient(config: config) }
    private var stepMotion: Animation? { reduceMotion ? nil : Theme.springSoft }
    private var isDemo: Bool { mode == .demo }

    private var displayedScore: Double {
        isDemo ? previewScore : (setup.status?.identity_score ?? 0)
    }

    private var displayedMissing: [String] {
        isDemo ? QuizDemo.missing : (setup.status?.identity_missing ?? [])
    }

    private var greetingName: String {
        Voice.firstName(
            identity: [
                "preferred_name": identity.preferredName,
                "first_name": identity.firstName,
            ],
            displayName: config.displayName
        )
    }

    private var headerTitle: String {
        switch step {
        case .welcome:
            return greetingName.isEmpty ? step.title : "Let’s get you set, \(greetingName)"
        case .done:
            return greetingName.isEmpty ? step.title : "You’re set, \(greetingName)"
        default:
            return step.title
        }
    }

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: Theme.spaceL) {
                progress
                PageHeader(
                    eyebrow: isDemo ? "Preview" : (step == .welcome ? "Welcome" : "Profile quiz"),
                    title: headerTitle,
                    subtitle: step.subtitle
                )
                .animation(stepMotion, value: step)

                ScrollView(showsIndicators: false) {
                    VStack(alignment: .leading, spacing: Theme.spaceM) {
                        stepBody
                            .id(step)
                            .instrumentEnter()
                        if let error, !error.isEmpty {
                            InlineError(text: error)
                                .transition(.opacity)
                        }
                    }
                    .padding(.horizontal, Theme.spaceL)
                    .padding(.bottom, 120)
                }
                .scrollDismissesKeyboard(.interactively)
            }
            .safeAreaInset(edge: .bottom) { bottomBar }
            .toolbar(.hidden, for: .navigationBar)
            .ambientScreen()
            .onAppear {
                if isDemo {
                    applyDemoSample()
                } else {
                    Task { await hydrate() }
                }
            }
            .onChange(of: step) { _, _ in
                if isDemo { syncPreviewScore() }
            }
            .sheet(isPresented: $showStepJump) { stepJumpSheet }
            .sheet(isPresented: $showCoach) { coachSheet }
        }
    }

    private var progress: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                stepCounter
                askHorizonButton
                Spacer()
                if isDemo {
                    Button("Exit") { dismiss() }
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Theme.soft)
                        .buttonStyle(PressableButtonStyle())
                        .accessibilityLabel("Exit preview")
                } else if canClose {
                    // A retake replaces the whole app, so without this the only
                    // way out of "I came back to fix one field" is to tap
                    // through all thirteen steps again.
                    Button("Close") { setup.needsSetup = false }
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Theme.soft)
                        .buttonStyle(PressableButtonStyle())
                        .accessibilityLabel("Close the profile quiz")
                }
            }
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Theme.cloud.opacity(0.7))
                    Capsule()
                        .fill(Theme.accent)
                        .frame(width: max(8, geo.size.width * step.progress))
                        .animation(stepMotion, value: step)
                }
            }
            .frame(height: 6)
            if isDemo {
                Text("Sample answers · nothing is saved · tap the step count to jump")
                    .font(.caption)
                    .foregroundStyle(Theme.soft)
            }
        }
        .padding(.horizontal, Theme.spaceL)
        .padding(.top, Theme.spaceM)
    }

    /// Asks Horizon what this step is for. Grounded in the real step and in
    /// what's still empty, so the answer names actual fields instead of giving
    /// generic job-board advice.
    private var askHorizonButton: some View {
        Button {
            showCoach = true
            Task { await loadCoach() }
        } label: {
            HStack(spacing: 3) {
                Image(systemName: "sparkles")
                    .font(.system(size: 9, weight: .bold))
                Text("Ask Horizon")
                    .font(.caption.weight(.semibold))
            }
            .foregroundStyle(Theme.accent)
        }
        .buttonStyle(PressableButtonStyle())
        .padding(.leading, Theme.spaceS)
        .accessibilityLabel("Ask Horizon about this step")
    }

    private var coachSheet: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: Theme.spaceM) {
                    Text(step.title)
                        .font(.headline)
                    if coachBusy {
                        HStack(spacing: 8) {
                            ProgressView()
                            Text("Horizon is thinking\u{2026}")
                                .font(.subheadline)
                                .foregroundStyle(Theme.soft)
                        }
                    } else {
                        Text(coachText)
                            .font(.body)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Text(coachOnDevice
                         ? "Answered on your device by Apple Intelligence."
                         : "Built-in guidance \u{2014} Apple Intelligence isn\u{2019}t available here.")
                        .font(.caption)
                        .foregroundStyle(Theme.note)
                    Spacer(minLength: 0)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(Theme.spaceL)
            }
            .navigationTitle("Horizon")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { showCoach = false }
                        .accessibilityLabel("Close Horizon")
                }
            }
        }
        .presentationDetents([.medium])
    }

    private func coachContext() -> HorizonCoach.Context {
        HorizonCoach.Context(
            stepTitle: step.title,
            stepSubtitle: step.subtitle ?? "",
            stepIndex: step.rawValue,
            stepCount: QuizStep.allCases.count,
            missing: displayedMissing,
            score: displayedScore,
            alreadyFilled: setup.status?.identity_have ?? [],
            isSkippable: canSkip
        )
    }

    private func loadCoach() async {
        coachBusy = true
        let out = await HorizonCoach.guidance(coachContext())
        coachText = out.text
        coachOnDevice = out.onDevice
        coachBusy = false
    }

    @ViewBuilder
    private var stepCounter: some View {
        let label = Text("\(step.rawValue + 1) of \(QuizStep.allCases.count)")
            .font(.caption.weight(.semibold))
            .foregroundStyle(Theme.horizon)
            .textCase(.uppercase)
            .tracking(0.8)
            .contentTransition(reduceMotion ? .identity : .numericText())
            .animation(stepMotion, value: step)

        if isDemo {
            Button {
                showStepJump = true
            } label: {
                HStack(spacing: 4) {
                    label
                    Image(systemName: "chevron.down")
                        .font(.system(size: 9, weight: .bold))
                        .foregroundStyle(Theme.horizon)
                }
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Jump to a step")
        } else {
            label
        }
    }

    @ViewBuilder
    private var stepBody: some View {
        switch step {
        case .welcome: welcomeBody
        case .search: searchFields
        case .you: youFields
        case .home: homeFields
        case .links: linkFields
        case .school: schoolFields
        case .work: workFields
        case .logistics: logisticsFields
        case .formDefaults: formDefaultFields
        case .story: storyFields
        case .answers: answerFields
        case .demographics: demoFields
        case .done: doneBody
        }
    }

    private var welcomeBody: some View {
        VStack(alignment: .leading, spacing: Theme.spaceM) {
            FocusCard(prominent: true) {
                VStack(alignment: .leading, spacing: 12) {
                    Text("Applications ask the same things over and over. Answer them once here and Autofill can put them on public application forms.")
                        .font(.subheadline)
                        .foregroundStyle(Theme.ink)
                        .fixedSize(horizontal: false, vertical: true)
                    Text("Skip anything you don’t want stored. You can finish later in You.")
                        .font(.subheadline)
                        .foregroundStyle(Theme.soft)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            ProfileImportPanel(demo: isDemo) { result in
                if isDemo {
                    previewScore = result.identity_score ?? previewScore
                    applyDraft(QuizDemo.draft)
                    return
                }
                await setup.refresh(config: config)
                if step != .done { setup.needsSetup = true }
                prefill()
                if let draft = result.draft {
                    applyDraft(draft)
                } else {
                    await loadDraft(polish: false)
                }
            }
        }
    }

    private var searchFields: some View {
        VStack(alignment: .leading, spacing: Theme.spaceM) {
            card {
                labeled("Roles") {
                    TagEditor(
                        text: $roles,
                        suggestions: Self.roleSuggestions,
                        placeholder: "Add a role",
                        caption: "Tap all that apply. This is how matches are found."
                    )
                }
                Divider().background(Theme.accent.opacity(0.08))
                labeled("Locations") {
                    TagEditor(
                        text: $locations,
                        suggestions: Self.locationSuggestions,
                        placeholder: "Add a city or Remote"
                    )
                }
                Divider().background(Theme.accent.opacity(0.08))
                labeled("Skills to match on") {
                    TagEditor(
                        text: $keywords,
                        suggestions: Self.skillSuggestions,
                        placeholder: "Add a skill"
                    )
                }
            }
            labeled("Seniority") {
                chipRow(
                    ["Internship", "New grad", "Entry-level", "Junior", "Mid-level", "Senior"],
                    selected: $seniority,
                    multi: true
                )
            }
        }
    }

    private var youFields: some View {
        card {
            labeled("First name") { TextField("Ada", text: $identity.firstName) }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("Last name") { TextField("Lovelace", text: $identity.lastName) }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("Preferred name") {
                TextField("If forms ask what you go by", text: $identity.preferredName)
            }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("Email") {
                TextField("you@school.edu", text: $identity.email)
                    .keyboardType(.emailAddress)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
            }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("Phone") {
                TextField("555-0100", text: $identity.phone).keyboardType(.phonePad)
            }
        }
    }

    private var homeFields: some View {
        VStack(alignment: .leading, spacing: Theme.spaceM) {
            card {
                labeled("City") { TextField("Chicago", text: $identity.city) }
                Divider().background(Theme.accent.opacity(0.08))
                labeled("State") { TextField("IL", text: $identity.state) }
                Divider().background(Theme.accent.opacity(0.08))
                labeled("ZIP") {
                    TextField("60601", text: $identity.zip).keyboardType(.numbersAndPunctuation)
                }
                Divider().background(Theme.accent.opacity(0.08))
                labeled("Street address") {
                    TextField("optional", text: $identity.address)
                }
            }
            // One value, two ways in. The chips and the field are the same
            // binding, so they were previously shown as two separate questions
            // where tapping one silently cleared the other.
            labeled("Country") {
                chipRow(["United States", "Canada"], selected: $identity.country)
            }
            card {
                labeled("Country (tap above or type it)") {
                    TextField("United States", text: $identity.country)
                }
            }
        }
    }

    private var linkFields: some View {
        card {
            labeled("LinkedIn URL") {
                TextField("https://linkedin.com/in/…", text: $identity.linkedin)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
            }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("GitHub URL") {
                TextField("https://github.com/…", text: $identity.github)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
            }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("Portfolio / website") {
                TextField("optional", text: $identity.portfolio)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
            }
        }
    }

    private var schoolFields: some View {
        VStack(alignment: .leading, spacing: Theme.spaceM) {
            card {
                labeled("School") { TextField("University", text: $identity.school) }
                Divider().background(Theme.accent.opacity(0.08))
                labeled("Major / field of study") {
                    TagEditor(
                        text: $identity.discipline,
                        suggestions: ["Computer Science", "Data Science", "Software Engineering", "Electrical Engineering"],
                        placeholder: "Add a major"
                    )
                }
                Divider().background(Theme.accent.opacity(0.08))
                labeled("GPA") {
                    TextField("optional", text: $identity.gpa).keyboardType(.decimalPad)
                }
            }
            labeled("Degree") {
                chipRow(["B.S.", "B.A.", "M.S.", "M.Eng.", "MBA", "Ph.D."],
                        selected: $identity.degree, multi: true)
            }
            labeled("Graduation year") {
                chipRow(Self.gradYearOptions, selected: $identity.gradYear)
            }
            // Greenhouse and Workable split the education end date into a month
            // select and a year select. Without the month, half of every
            // education block stays blank.
            labeled("Graduation month") {
                chipRow(Self.monthOptions, selected: $identity.gradMonth)
            }
        }
    }

    private var workFields: some View {
        card {
            labeled("Years of experience") {
                TextField("0", text: $identity.years).keyboardType(.numberPad)
            }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("Current or most recent company") {
                TextField("optional", text: $identity.currentCompany)
            }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("Current or most recent title") {
                TextField("optional", text: $identity.currentTitle)
            }
            Toggle("Authorized to work in the US", isOn: $identity.workAuthorized)
                .font(.subheadline)
                .padding(.top, 4)
            Toggle("Need visa sponsorship", isOn: $identity.needsSponsorship)
                .font(.subheadline)
            Toggle("I am 18 or older", isOn: $identity.over18)
                .font(.subheadline)
        }
    }

    private var logisticsFields: some View {
        VStack(alignment: .leading, spacing: Theme.spaceM) {
            labeled("Work arrangement") {
                chipRow(["Remote", "Hybrid", "On-site", "Flexible"],
                        selected: $identity.workArrangement, multi: true)
            }
            labeled("When can you start?") {
                chipRow(["Immediately", "2 weeks", "After graduation"],
                        selected: $identity.startDate)
            }
            // Internship postings ask which term you're applying for. The rules
            // and the option picker already handle it; nothing was asking.
            labeled("Internship term (if you're applying for one)") {
                chipRow(Self.internSeasonOptions, selected: $identity.internSeason)
            }
            card {
                // Same binding as the chips above — one answer, not two.
                labeled("Start date (tap above or type it)") {
                    TextField("June 2026", text: $identity.startDate)
                }
                Divider().background(Theme.accent.opacity(0.08))
                labeled("Salary expectation") {
                    TextField("optional — skip if you’d rather not say", text: $identity.salary)
                }
                Toggle("Willing to relocate", isOn: $identity.willingToRelocate)
                    .font(.subheadline)
                    .padding(.top, 4)
                Toggle("Willing to travel", isOn: $identity.canTravel)
                    .font(.subheadline)
            }
        }
    }

    private var formDefaultFields: some View {
        VStack(alignment: .leading, spacing: Theme.spaceM) {
            labeled("How do you usually hear about roles?") {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Pick one — most applications only allow a single source.")
                        .font(.caption)
                        .foregroundStyle(Theme.soft)
                    chipRow(
                        ["LinkedIn", "Company website", "Job board", "Referral", "Recruiter", "Event"],
                        selected: $identity.howHeard
                    )
                }
            }
            card {
                Toggle("Okay with a background check", isOn: $identity.backgroundCheck)
                    .font(.subheadline)
                Toggle("Okay with a drug test", isOn: $identity.drugTest)
                    .font(.subheadline)
                Toggle("I’ve applied to this company before", isOn: $identity.previouslyApplied)
                    .font(.subheadline)
                Toggle("I’m related to someone who works there", isOn: $identity.relatedToEmployee)
                    .font(.subheadline)
            }
            Text("Those last two default to no — that’s what most forms expect. Change them per company later if needed.")
                .font(.caption)
                .foregroundStyle(Theme.soft)
        }
    }

    private var storyFields: some View {
        VStack(alignment: .leading, spacing: Theme.spaceM) {
            draftButton(title: "Fill from my profile") {
                await loadDraft(polish: false)
            }
            card {
                labeled("A project worth citing") {
                    TextField("I built …", text: $project, axis: .vertical)
                        .lineLimit(3...6)
                }
                Divider().background(Theme.accent.opacity(0.08))
                labeled("An achievement") {
                    TextField("I cut latency 40% …", text: $achievement, axis: .vertical)
                        .lineLimit(2...5)
                }
                Divider().background(Theme.accent.opacity(0.08))
                labeled("A strength") {
                    TextField("Systems debugging", text: $strength, axis: .vertical)
                        .lineLimit(2...4)
                }
                Divider().background(Theme.accent.opacity(0.08))
                labeled("What you want in a role") {
                    TextField("Real ownership, strong mentorship …", text: $preference, axis: .vertical)
                        .lineLimit(2...4)
                }
            }
        }
    }

    private var answerFields: some View {
        VStack(alignment: .leading, spacing: Theme.spaceM) {
            draftButton(title: "Write these for me") {
                await loadDraft(polish: true)
            }
            card {
                labeled("Tell us about yourself") {
                    TextField("A short paragraph you reuse on applications", text: $about, axis: .vertical)
                        .lineLimit(4...8)
                }
                Divider().background(Theme.accent.opacity(0.08))
                labeled("Why this kind of work?") {
                    TextField("What you say when they ask why this role or company", text: $whyRole, axis: .vertical)
                        .lineLimit(4...8)
                }
            }
        }
    }

    private var demoFields: some View {
        VStack(alignment: .leading, spacing: Theme.spaceM) {
            Text("Optional. Autofill only uses these when you save them. Skip if you’d rather leave demographic questions blank.")
                .font(.subheadline)
                .foregroundStyle(Theme.soft)
                .fixedSize(horizontal: false, vertical: true)
            labeled("Gender") {
                chipRow(["Woman", "Man", "Non-binary", "Prefer not to say"],
                        selected: $identity.gender)
            }
            labeled("Veteran status") {
                chipRow(["I am a veteran", "I am not a veteran", "Decline to answer"],
                        selected: $identity.veteranStatus)
            }
            labeled("Disability") {
                chipRow(["Yes", "No", "Decline to answer"],
                        selected: $identity.disabilityStatus)
            }
            card {
                labeled("Race / ethnicity") {
                    TextField("optional — as you’d report it on a form", text: $identity.race, axis: .vertical)
                        .lineLimit(2...3)
                }
            }
        }
    }

    /// True when there isn't enough identity for Autofill to do anything useful.
    /// The server decides the bar (`onboarding._IDENTITY_READY`) and reports it
    /// as `complete`; the app doesn't second-guess the number.
    private var tooThinToFill: Bool {
        if isDemo { return false }
        guard let s = setup.status else { return false }
        return !s.complete
    }

    /// Name the fields when the server told us which ones are missing; otherwise
    /// say why coverage matters at all.
    private var thinReason: String {
        let core = setup.status?.identity_core_missing ?? []
        if !core.isEmpty {
            return "Every application form starts with your \(core.joined(separator: ", ")) — "
                 + "without those, ⚡ Autofill has almost nothing to put on the page."
        }
        return "Name, contact, and work authorization are on nearly every form. "
             + "Without them ⚡ Autofill will leave most of the page blank."
    }

    private var doneBody: some View {
        VStack(spacing: Theme.spaceM) {
            CoverageMeter(
                score: displayedScore,
                missing: displayedMissing,
                suggestion: nil
            )
            if tooThinToFill {
                // Letting someone finish here in silence hands them an app whose
                // headline feature does nothing. Say so, and offer the way back.
                FocusCard {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("Autofill can't fill much yet")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(Theme.ink)
                        Text(thinReason)
                            .font(.subheadline)
                            .foregroundStyle(Theme.soft)
                            .fixedSize(horizontal: false, vertical: true)
                        Button("Add my details") {
                            withAnimation(stepMotion) { step = .you }
                        }
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(Theme.accent)
                        .buttonStyle(PressableButtonStyle())
                    }
                }
            } else {
                Text("Skipped fields stay blank on forms. You can add them anytime in You.")
                    .font(.subheadline)
                    .foregroundStyle(Theme.soft)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.top, 8)
    }

    private var continueTitle: String {
        if isDemo && step == .done { return "Close preview" }
        if step == .done { return "Done" }
        if step == .welcome { return "Get started" }
        return "Continue"
    }

    private var canSkip: Bool {
        if isDemo { return step != .welcome && step != .done }
        return step.skippable
    }

    private var hasLeadingActions: Bool {
        step != .welcome || canSkip
    }

    private var bottomBar: some View {
        HStack(spacing: 12) {
            if step != .welcome {
                Button("Back") {
                    if let prev = step.previous {
                        withAnimation(stepMotion) { step = prev }
                    }
                }
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(Theme.accent)
                .buttonStyle(PressableButtonStyle())
                .transition(.opacity)
            }
            if canSkip {
                Button("Skip") { Task { await skip() } }
                    .font(.subheadline)
                    .foregroundStyle(Theme.soft)
                    .buttonStyle(PressableButtonStyle())
                    .disabled(busy)
                    .transition(.opacity)
            }
            if hasLeadingActions { Spacer(minLength: 8) }
            Button {
                Task { await advance() }
            } label: {
                HStack(spacing: 8) {
                    if !hasLeadingActions { Spacer(minLength: 0) }
                    if busy {
                        PropellerIcon(speed: .medium, size: 14)
                            .foregroundStyle(.white)
                    }
                    Text(busy ? "Saving…" : continueTitle)
                        .font(.subheadline.weight(.semibold))
                    if !hasLeadingActions { Spacer(minLength: 0) }
                }
                .foregroundStyle(.white)
                .padding(.horizontal, 20)
                .padding(.vertical, 12)
                .background(canContinue ? Theme.accent : Theme.accent.opacity(0.4), in: Capsule())
            }
            .buttonStyle(PressableButtonStyle(haptic: true))
            .disabled(busy || !canContinue)
        }
        .padding(.horizontal, hasLeadingActions ? 16 : 6)
        .padding(.vertical, 6)
        .paperCapsule()
        .padding(.horizontal, Theme.spaceL)
        .padding(.bottom, 10)
        .animation(stepMotion, value: hasLeadingActions)
        .animation(stepMotion, value: step)
    }

    private var canContinue: Bool {
        if isDemo { return true }
        if step == .search {
            return !roles.trimmingCharacters(in: .whitespaces).isEmpty
        }
        return true
    }

    private func labeled(_ title: String, @ViewBuilder field: () -> some View) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(.caption.weight(.semibold))
                .foregroundStyle(Theme.horizon)
                .textCase(.uppercase)
                .tracking(0.8)
            field()
                .font(.subheadline)
                .foregroundStyle(Theme.ink)
        }
    }

    private func card<Content: View>(@ViewBuilder content: () -> Content) -> some View {
        FocusCard {
            VStack(alignment: .leading, spacing: 12) {
                content()
            }
        }
    }

    private func chipRow(_ options: [String], selected: Binding<String>,
                         multi: Bool = false) -> some View {
        WrapHStack(spacing: 8, lineSpacing: 8) {
            ForEach(Array(options.enumerated()), id: \.element) { i, option in
                SelectChip(
                    label: option,
                    selected: optionIsOn(option, in: selected.wrappedValue, multi: multi)
                ) {
                    toggleChip(option, selected: selected, options: options, multi: multi)
                }
                .staggerAppear(i)
            }
        }
    }

    private func optionIsOn(_ option: String, in stored: String, multi: Bool) -> Bool {
        let s = stored.lowercased()
        let o = option.lowercased()
        if !multi {
            if stored == option || s == o { return true }
            if option.count == 4, option.allSatisfy(\.isNumber) { return s.contains(option) }
            return false
        }
        if QuizList.split(stored).contains(where: { $0.caseInsensitiveCompare(option) == .orderedSame }) {
            return true
        }
        switch o {
        case "on-site": return s.contains("on-site") || s.contains("onsite")
        case "internship": return s.contains("intern")
        case "new grad": return s.contains("new grad") || s.contains("newgrad")
        case "entry-level": return s.contains("entry")
        case "b.s.": return s.contains("b.s") || s.contains("bachelor")
        case "b.a.": return s.contains("b.a") && !s.contains("b.s")
        case "m.s.": return s.contains("m.s") || s.contains("master")
        case "ph.d.": return s.contains("ph.d") || s.contains("phd")
        default:
            return s.contains(o)
        }
    }

    private func toggleChip(_ option: String, selected: Binding<String>,
                            options: [String], multi: Bool) {
        Theme.selection()
        if !multi {
            let on = optionIsOn(option, in: selected.wrappedValue, multi: false)
            selected.wrappedValue = on ? "" : option
            return
        }
        var current = options.filter { optionIsOn($0, in: selected.wrappedValue, multi: true) }
        if current.contains(option) {
            current.removeAll { $0 == option }
        } else {
            current.append(option)
        }
        selected.wrappedValue = current.joined(separator: ", ")
    }

    /// Load before showing anything. A quiz that opened before setup had loaded
    /// (cold launch, or a refresh that failed) showed empty fields, no Apple
    /// name/email, and no draft — and then saved that emptiness over what was
    /// already there.
    private func hydrate() async {
        if setup.status == nil { await setup.refresh(config: config) }
        // A retake has finished the quiz once already; it must be escapable and
        // must not re-pin them with `mark_started`.
        canClose = setup.status.map { !$0.needs_setup } ?? true
        prefill()
        await startQuiz()
    }

    private func prefill() {
        guard let s = setup.status else { return }
        if roles.isEmpty { roles = s.profile["roles"] ?? "" }
        if locations.isEmpty { locations = s.profile["locations"] ?? "" }
        if keywords.isEmpty { keywords = s.profile["keywords"] ?? "" }
        if seniority.isEmpty { seniority = s.profile["seniority"] ?? "" }
        identity.load(from: s.identity)
        Task { await loadDraft(polish: false) }
    }

    private func applyDraft(_ draft: QuizDraft, overwrite: Bool = false) {
        func take(_ current: inout String, _ incoming: String?) {
            let value = (incoming ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            guard !value.isEmpty else { return }
            if overwrite || current.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                current = value
            }
        }
        take(&roles, draft.roles)
        take(&locations, draft.locations)
        take(&keywords, draft.keywords)
        take(&seniority, draft.seniority)
        take(&project, draft.project)
        take(&achievement, draft.achievement)
        take(&strength, draft.strength)
        take(&preference, draft.preference)
        take(&about, draft.about)
        take(&whyRole, draft.why_role)
    }

    private func loadDraft(polish: Bool) async {
        if isDemo {
            applyDraft(QuizDemo.draft, overwrite: polish)
            return
        }
        draftBusy = true
        defer { draftBusy = false }
        do {
            let draft = try await api.fetchQuizDraft(polish: polish)
            applyDraft(draft, overwrite: polish)
        } catch {
            if APIClient.isCancellation(error) { return }
            if polish {
                self.error = APIClient.userMessage(for: error)
            }
        }
    }

    @ViewBuilder
    private func draftButton(title: String, action: @escaping () async -> Void) -> some View {
        Button {
            Task { await action() }
        } label: {
            HStack(spacing: 8) {
                if draftBusy {
                    PropellerIcon(speed: .medium, size: 14)
                        .foregroundStyle(Theme.accent)
                } else {
                    Image(systemName: "sparkles")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(Theme.accent)
                }
                Text(draftBusy ? "Writing…" : title)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Theme.accent)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(Theme.accent.opacity(0.12), in: Capsule())
        }
        .buttonStyle(PressableButtonStyle())
        .disabled(draftBusy)
        .accessibilityLabel(title)
    }

    private func applyDemoSample() {
        roles = QuizDemo.roles
        locations = QuizDemo.locations
        keywords = QuizDemo.keywords
        seniority = QuizDemo.seniority
        identity = QuizDemo.identity
        project = QuizDemo.project
        achievement = QuizDemo.achievement
        strength = QuizDemo.strength
        preference = QuizDemo.preference
        about = QuizDemo.about
        whyRole = QuizDemo.whyRole
        syncPreviewScore()
    }

    private func syncPreviewScore() {
        previewScore = QuizDemo.score(
            forStepIndex: step.rawValue,
            total: QuizStep.allCases.count
        )
    }

    private var stepJumpSheet: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: Theme.spaceL) {
                PageHeader(
                    eyebrow: "Preview",
                    title: "Jump to a step",
                    subtitle: "Same screens as the real quiz. Nothing is saved."
                )
                ScrollView(showsIndicators: false) {
                    GroupedSurface {
                        ForEach(Array(QuizStep.allCases.enumerated()), id: \.element) { i, item in
                            if i > 0 {
                                Rectangle()
                                    .fill(Theme.cloud.opacity(0.45))
                                    .frame(height: 1)
                                    .padding(.leading, 16)
                            }
                            Button {
                                withAnimation(stepMotion) { step = item }
                                showStepJump = false
                            } label: {
                                HStack {
                                    Text(item.title)
                                        .font(.body.weight(.medium))
                                        .foregroundStyle(Theme.ink)
                                    Spacer()
                                    if item == step {
                                        Image(systemName: "checkmark")
                                            .font(.caption.weight(.semibold))
                                            .foregroundStyle(Theme.accent)
                                    }
                                }
                                .padding(.horizontal, 16)
                                .padding(.vertical, 12)
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(PressableButtonStyle())
                        }
                    }
                    .padding(.horizontal, Theme.spaceL)
                    Color.clear.frame(height: Theme.spaceXL)
                }
            }
            .toolbar(.hidden, for: .navigationBar)
            .ambientScreen()
        }
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
        .presentationCornerRadius(28)
        .presentationBackground(Theme.fog)
    }

    private func startQuiz() async {
        guard !didStart else { return }
        didStart = true
        // Retakes (You → Profile quiz) already finished once — don't trap them again.
        if setup.status?.needs_setup == false { return }
        do {
            let s = try await api.markSetup(action: "start")
            setup.status = s
        } catch {
            if APIClient.isCancellation(error) { return }
            didStart = false
        }
    }

    private func skip() async {
        if isDemo {
            guard let next = step.next else {
                dismiss()
                return
            }
            withAnimation(stepMotion) { step = next }
            return
        }
        guard let next = step.next else {
            await finish()
            return
        }
        withAnimation(stepMotion) { step = next }
        if next == .done { await refreshScore() }
    }

    private func advance() async {
        if isDemo {
            if step == .done {
                Theme.notify(.success)
                dismiss()
                return
            }
            if let next = step.next {
                withAnimation(stepMotion) { step = next }
            }
            return
        }
        error = nil
        busy = true
        defer { busy = false }
        do {
            try await saveCurrentStep()
            if step == .done {
                await finish()
                return
            }
            if let next = step.next {
                withAnimation(stepMotion) { step = next }
                if next == .done { await refreshScore() }
            }
        } catch {
            self.error = APIClient.userMessage(for: error)
        }
    }

    private func saveCurrentStep() async throws {
        switch step {
        case .welcome:
            return
        case .search:
            if setup.status?.needs_setup != false {
                let s = try await api.markSetup(action: "start")
                setup.status = s
            }
            try await api.saveProfile(
                roles: roles,
                locations: locations,
                seniority: seniority,
                keywords: keywords
            )
        case .you:
            try await saveIdentity([
                "first_name", "last_name", "preferred_name", "email", "phone",
            ])
        case .home:
            try await saveIdentity(["city", "state", "zip", "country", "address"])
        case .links:
            try await saveIdentity(["linkedin", "github", "portfolio"])
        case .school:
            try await saveIdentity([
                "school", "degree", "discipline", "gpa", "grad_year", "grad_month",
            ])
        case .work:
            try await saveIdentity([
                "years_experience", "current_company", "current_title",
                "work_authorized", "needs_sponsorship", "over_18",
            ])
        case .logistics:
            try await saveIdentity([
                "work_arrangement", "start_date", "intern_season",
                "salary_expectation", "willing_to_relocate", "can_travel",
            ])
        case .formDefaults:
            try await saveIdentity([
                "how_heard", "background_check", "drug_test",
                "previously_applied", "related_to_employee",
            ])
        case .story:
            try await addFact(category: "project", text: project)
            try await addFact(category: "achievement", text: achievement)
            try await addFact(category: "strength", text: strength)
            try await addFact(category: "preference", text: preference)
        case .answers:
            let aboutText = about.trimmingCharacters(in: .whitespacesAndNewlines)
            if !aboutText.isEmpty {
                try await api.addKnowledge(
                    category: "answer", text: aboutText, label: "Tell us about yourself"
                )
                // Summary only. Re-posting the search fields from local state here
                // wiped the locations and seniority of anyone who reached this step
                // without them loaded (a retake, or a setup refresh that failed).
                try await api.saveProfile(resumeSummary: aboutText)
            }
            let why = whyRole.trimmingCharacters(in: .whitespacesAndNewlines)
            if !why.isEmpty {
                try await api.addKnowledge(
                    category: "answer",
                    text: why,
                    label: "Why do you want to work here?"
                )
            }
        case .demographics:
            try await saveIdentity([
                "gender", "race", "ethnicity", "veteran_status", "disability_status",
            ])
        case .done:
            return
        }
        await refreshScore()
    }

    private func saveIdentity(_ keys: Set<String>) async throws {
        let fields = identity.payload(keys: keys)
        if !fields.isEmpty {
            try await api.saveIdentity(fields: fields)
        }
    }

    private func addFact(category: String, text: String) async throws {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty {
            try await api.addKnowledge(category: category, text: trimmed)
        }
    }

    private func refreshScore() async {
        await setup.refresh(config: config)
        // refresh() would clear needsSetup once complete — keep the quiz up until Done.
        if step != .done {
            setup.needsSetup = true
        }
    }

    private func finish() async {
        busy = true
        defer { busy = false }
        do {
            let s = try await api.markSetup(action: "complete")
            setup.status = s
            Theme.notify(.success)
            setup.needsSetup = false
            await api.discover(force: true)
        } catch {
            self.error = APIClient.userMessage(for: error)
        }
    }
}

private enum QuizStep: Int, CaseIterable, Hashable {
    case welcome, search, you, home, links, school, work
    case logistics, formDefaults, story, answers, demographics, done

    var title: String {
        switch self {
        case .welcome: return "Let’s get Autofill ready"
        case .search: return "What are you looking for?"
        case .you: return "Your name and contact"
        case .home: return "Where you live"
        case .links: return "Links forms ask for"
        case .school: return "School"
        case .work: return "Work and authorization"
        case .logistics: return "Start date and setup"
        case .formDefaults: return "Usual application answers"
        case .story: return "What they can cite"
        case .answers: return "Answers you’ll reuse"
        case .demographics: return "Optional demographics"
        case .done: return "You’re set"
        }
    }

    var subtitle: String? {
        switch self {
        case .welcome: return "Start from a resume, GitHub, or LinkedIn — or tap through by hand."
        case .search: return "Tap every role, city, and skill that should match. Seniority can be more than one."
        case .you: return "Legal name and how they reach you."
        case .home: return "City and state unlock most location questions."
        case .links: return "LinkedIn and GitHub show up on almost every SWE form."
        case .school: return "Degree, major, and when you finish. Tap all degrees that apply."
        case .work: return "Authorization questions are on nearly every form."
        case .logistics: return "Skip salary if you’d rather not store it."
        case .formDefaults: return "Pick one source. Yes/no questions Autofill can answer for you."
        case .story: return "Tap Fill from my profile if you already imported a resume."
        case .answers: return "Saved verbatim when a form asks the same thing."
        case .demographics: return "Only filled when you choose to save them. Easy to skip."
        case .done: return "Coverage is how much Autofill can fill without asking."
        }
    }

    var skippable: Bool {
        switch self {
        case .welcome, .search, .done: return false
        default: return true
        }
    }

    var progress: Double {
        Double(rawValue + 1) / Double(QuizStep.allCases.count)
    }

    var next: QuizStep? { QuizStep(rawValue: rawValue + 1) }
    var previous: QuizStep? { QuizStep(rawValue: rawValue - 1) }
}

#Preview("Quiz preview") {
    SetupView(mode: .demo)
        .environmentObject(Config.shared)
        .environmentObject(SetupGate.shared)
}
