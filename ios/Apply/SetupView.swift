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
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

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
                    prefill()
                    Task { await startQuiz() }
                }
            }
            .onChange(of: step) { _, _ in
                if isDemo { syncPreviewScore() }
            }
            .sheet(isPresented: $showStepJump) { stepJumpSheet }
        }
    }

    private var progress: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                stepCounter
                Spacer()
                if displayedScore > 0 {
                    Text("\(Int(displayedScore * 100))% ready")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Theme.accent)
                        .monospacedDigit()
                        .contentTransition(reduceMotion ? .identity : .numericText())
                        .animation(reduceMotion ? nil : Theme.tick, value: displayedScore)
                }
                if isDemo {
                    Button("Exit") { dismiss() }
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Theme.soft)
                        .buttonStyle(PressableButtonStyle())
                        .accessibilityLabel("Exit preview")
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
        case .name: nameFields
        case .contact: contactFields
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
                    return
                }
                await setup.refresh(config: config)
                if step != .done { setup.needsSetup = true }
                prefill()
            }
        }
    }

    private var searchFields: some View {
        VStack(alignment: .leading, spacing: Theme.spaceM) {
            card {
                labeled("Roles") {
                    TextField("New grad SWE, backend intern", text: $roles, axis: .vertical)
                        .lineLimit(2...4)
                }
                Divider().background(Theme.accent.opacity(0.08))
                labeled("Locations") {
                    TextField("NYC, remote, Chicago", text: $locations, axis: .vertical)
                        .lineLimit(2...4)
                }
                Divider().background(Theme.accent.opacity(0.08))
                labeled("Skills to match on") {
                    TextField("Python, React, SQL — optional", text: $keywords, axis: .vertical)
                        .lineLimit(2...3)
                }
            }
            labeled("Seniority") {
                chipRow(["Internship", "New grad", "Junior", "Mid-level", "Senior"],
                        selected: $seniority)
            }
        }
    }

    private var nameFields: some View {
        card {
            labeled("First name") { TextField("Ada", text: $identity.firstName) }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("Last name") { TextField("Lovelace", text: $identity.lastName) }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("Preferred name") {
                TextField("If forms ask what you go by", text: $identity.preferredName)
            }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("Pronouns") {
                TextField("optional", text: $identity.pronouns)
            }
        }
    }

    private var contactFields: some View {
        card {
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
            labeled("Country") {
                chipRow(["United States", "Canada"], selected: $identity.country)
            }
            card {
                labeled("Or type another country") {
                    TextField("optional", text: $identity.country)
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
        card {
            labeled("School") { TextField("University", text: $identity.school) }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("Degree") { TextField("B.S. Computer Science", text: $identity.degree) }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("Major / field of study") {
                TextField("Computer Science", text: $identity.discipline)
            }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("Graduation year") {
                TextField("2026", text: $identity.gradYear).keyboardType(.numberPad)
            }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("GPA") {
                TextField("optional", text: $identity.gpa).keyboardType(.decimalPad)
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
                        selected: $identity.workArrangement)
            }
            labeled("When can you start?") {
                chipRow(["Immediately", "2 weeks", "After graduation"],
                        selected: $identity.startDate)
            }
            card {
                labeled("Custom start date") {
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
                chipRow(
                    ["LinkedIn", "Company website", "Job board", "Referral", "Recruiter", "Event"],
                    selected: $identity.howHeard
                )
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

    private var answerFields: some View {
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

    private var doneBody: some View {
        VStack(spacing: Theme.spaceM) {
            CoverageMeter(
                score: displayedScore,
                missing: displayedMissing,
                suggestion: nil
            )
            Text("Skipped fields stay blank on forms. You can add them anytime in You.")
                .font(.subheadline)
                .foregroundStyle(Theme.soft)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
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

    private func chipRow(_ options: [String], selected: Binding<String>) -> some View {
        WrapHStack(spacing: 8, lineSpacing: 8) {
            ForEach(Array(options.enumerated()), id: \.element) { i, option in
                QuizChip(label: option, selected: selected.wrappedValue == option) {
                    selected.wrappedValue = selected.wrappedValue == option ? "" : option
                    Theme.selection()
                }
                .staggerAppear(i)
            }
        }
    }

    private func prefill() {
        guard let s = setup.status else { return }
        if roles.isEmpty { roles = s.profile["roles"] ?? "" }
        if locations.isEmpty { locations = s.profile["locations"] ?? "" }
        if keywords.isEmpty { keywords = s.profile["keywords"] ?? "" }
        if seniority.isEmpty { seniority = s.profile["seniority"] ?? "" }
        identity.load(from: s.identity)
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
        case .name:
            try await saveIdentity([
                "first_name", "last_name", "preferred_name", "pronouns",
            ])
        case .contact:
            try await saveIdentity(["email", "phone"])
        case .home:
            try await saveIdentity(["city", "state", "zip", "country", "address"])
        case .links:
            try await saveIdentity(["linkedin", "github", "portfolio"])
        case .school:
            try await saveIdentity([
                "school", "degree", "discipline", "gpa", "grad_year",
            ])
        case .work:
            try await saveIdentity([
                "years_experience", "current_company", "current_title",
                "work_authorized", "needs_sponsorship", "over_18",
            ])
        case .logistics:
            try await saveIdentity([
                "work_arrangement", "start_date", "salary_expectation",
                "willing_to_relocate", "can_travel",
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
                try await api.saveProfile(
                    roles: roles,
                    locations: locations,
                    seniority: seniority,
                    keywords: keywords,
                    resumeSummary: aboutText
                )
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
    case welcome, search, name, contact, home, links, school, work
    case logistics, formDefaults, story, answers, demographics, done

    var title: String {
        switch self {
        case .welcome: return "Let’s get Autofill ready"
        case .search: return "What are you looking for?"
        case .name: return "Your name"
        case .contact: return "How they reach you"
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
        case .welcome: return "Start from a resume, GitHub, or LinkedIn — or fill it in by hand."
        case .search: return "Roles and locations — this is how matches are found."
        case .name: return "Legal name on applications. Preferred name is optional."
        case .contact: return "Used to fill email and phone fields."
        case .home: return "City and state unlock most location questions."
        case .links: return "LinkedIn and GitHub show up on almost every SWE form."
        case .school: return "Degree, major, and class year."
        case .work: return "Authorization questions are on nearly every form."
        case .logistics: return "Skip salary if you’d rather not store it."
        case .formDefaults: return "Yes/no questions Autofill can answer for you."
        case .story: return "Projects and achievements ground written answers."
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

private struct QuizChip: View {
    let label: String
    let selected: Bool
    let action: () -> Void
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Button(action: action) {
            Text(label)
                .font(.subheadline.weight(selected ? .semibold : .regular))
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
    }
}

#Preview("Quiz preview") {
    SetupView(mode: .demo)
        .environmentObject(Config.shared)
        .environmentObject(SetupGate.shared)
}
