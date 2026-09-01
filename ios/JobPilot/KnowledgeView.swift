import SwiftUI

/// Dossier layout: coverage hero, split Looking for / On forms, knowledge filter.
struct KnowledgeView: View {
    @EnvironmentObject var config: Config
    @EnvironmentObject var setup: SetupGate
    @EnvironmentObject var push: PushManager
    @State private var items: [KnowledgeItem] = []
    @State private var audit: KnowledgeAudit?
    @State private var loading = false
    @State private var showAdd = false
    @State private var showSearch = false
    @State private var showIdentity = false
    @State private var showDocuments = false
    @State private var error: String?
    @State private var addPrefillHint: String?
    @State private var addPrefillCategory: String?
    @State private var segment = 0
    @State private var pendingImportScroll = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var api: APIClient { APIClient(config: config) }

    private var experience: [KnowledgeItem] {
        items.filter { $0.category == "experience" }
    }
    private var projects: [KnowledgeItem] {
        items.filter { $0.category == "project" }
    }
    private var other: [KnowledgeItem] {
        items.filter { !["experience", "project"].contains($0.category) }
    }

    var body: some View {
        NavigationStack {
            Group {
                if loading && items.isEmpty && audit == nil {
                    PreparingView(message: "Loading…")
                } else if let error, items.isEmpty && audit == nil {
                    EmptyStateView(
                        title: "Couldn't load",
                        description: error,
                        retryTitle: "Try again",
                        retry: { Task { await load() } },
                        secondaryTitle: "Send feedback",
                        secondary: { push.openDeepLink("settings:feedback", fromHorizon: true) }
                    )
                    .instrumentEnter()
                } else {
                    dossier
                }
            }
            .toolbar(.hidden, for: .navigationBar)
            .sheet(isPresented: $showAdd) {
                AddFactView(
                    initialCategory: addPrefillCategory ?? "project",
                    initialHint: addPrefillHint
                ) { category, text, label in
                    try? await api.addKnowledge(category: category, text: text, label: label)
                    Theme.impact(.soft)
                    await load()
                }
            }
            .sheet(isPresented: $showSearch) {
                SearchEditorView()
            }
            .sheet(isPresented: $showIdentity) {
                IdentityEditorView()
            }
            .sheet(isPresented: $showDocuments) {
                DocumentsView()
            }
            .refreshable { await load() }
            .ambientScreen()
            .task { await load() }
            .onAppear { consumeHorizonHop() }
            .onChange(of: push.hop) { _, _ in consumeHorizonHop() }
        }
    }

    /// Counted fresh rather than cached: the folder is also reachable from
    /// Files, so the app is not the only thing that can change it.
    private var documentsLine: String {
        let docs = LocalDocuments.all()
        if docs.isEmpty { return "Transcript and anything else forms ask to attach" }
        let transcripts = docs.filter { $0.kind == .transcript }.count
        let noun = docs.count == 1 ? "file" : "files"
        return transcripts > 0
            ? "\(docs.count) \(noun) on this phone, transcript included"
            : "\(docs.count) \(noun) on this phone"
    }

    private var dossier: some View {
        ScrollViewReader { proxy in
        ScrollView(showsIndicators: false) {
            VStack(alignment: .leading, spacing: Theme.spaceM) {
                PageHeader(
                    eyebrow: "You",
                    title: profileTitle,
                    subtitle: "Matches and fills use this."
                )

                if let error, !error.isEmpty {
                    InlineError(text: error) { Task { await load() } }
                        .padding(.horizontal, Theme.spaceL)
                }

                if let audit {
                    CoverageMeter(
                        score: audit.score,
                        missing: audit.identity_missing,
                        suggestion: audit.suggestions.first
                    ) {
                        if !(audit.identity_missing.isEmpty) {
                            showIdentity = true
                        } else if let tip = audit.suggestions.first {
                            addPrefillHint = tip
                            addPrefillCategory = suggestedCategory(for: tip)
                            showAdd = true
                        }
                    }
                    .padding(.horizontal, Theme.spaceL)
                }

                SplitInstrumentCard(
                    leftTitle: "Looking for",
                    leftBody: lookingForLine,
                    leftCaption: "Roles and places used to find matches.",
                    rightTitle: "On forms",
                    rightBody: identityLine,
                    rightCaption: identityCaption,
                    onLeft: { showSearch = true },
                    onRight: { showIdentity = true }
                )
                .padding(.horizontal, Theme.spaceL)

                if audit != nil {
                    ProfileImportPanel(
                        compact: true,
                        startsCollapsed: (audit?.score ?? 1) >= 0.5,
                        onRetakeQuiz: { setup.reopen() }
                    ) { _ in
                        await load()
                    }
                    .id("import")
                    .padding(.horizontal, Theme.spaceL)
                }

                // A transcript is asked for by most university-recruiting forms
                // and is the one thing here we can't generate.
                Button { showDocuments = true } label: {
                    HStack(spacing: 12) {
                        Image(systemName: "graduationcap")
                            .font(.body)
                            .foregroundStyle(Theme.accent)
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Documents")
                                .font(.subheadline.weight(.semibold))
                                .foregroundStyle(Theme.ink)
                            Text(documentsLine)
                                .font(.caption)
                                .foregroundStyle(Theme.soft)
                        }
                        Spacer(minLength: 8)
                        Image(systemName: "chevron.right")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(Theme.cloud)
                    }
                    .padding(Theme.spaceM)
                    .background(Theme.cardFill, in: RoundedRectangle(cornerRadius: 16))
                    .contentShape(Rectangle())
                }
                .buttonStyle(PressableButtonStyle())
                .padding(.horizontal, Theme.spaceL)

                HStack {
                    Text("Knowledge")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Theme.soft)
                        .textCase(.uppercase)
                        .tracking(0.8)
                    Spacer()
                    Button {
                        addPrefillHint = nil
                        addPrefillCategory = nil
                        showAdd = true
                    } label: {
                        Image(systemName: "plus")
                            .font(.body.weight(.semibold))
                            .foregroundStyle(Theme.accent)
                            .frame(width: 36, height: 36)
                            .contentShape(Rectangle())
                    }
                    .accessibilityLabel("Add a fact")
                }
                .padding(.horizontal, Theme.spaceL)
                .padding(.top, 4)

                InstrumentToggle(
                    options: ["Experience", "Projects", "More"],
                    selection: $segment
                )
                .padding(.horizontal, Theme.spaceL)

                let shown: [KnowledgeItem] = {
                    switch segment {
                    case 0: return experience
                    case 1: return projects
                    default: return other
                    }
                }()

                if shown.isEmpty {
                    EmptyStateView(
                        title: emptyTitle,
                        description: emptyCopy,
                        compact: true
                    )
                    .padding(.horizontal, Theme.spaceL)
                } else {
                    LazyVStack(spacing: 12) {
                        ForEach(Array(shown.enumerated()), id: \.element.id) { i, fact in
                            factCard(fact)
                                .staggerAppear(min(i, 8))
                                .contextMenu {
                                    Button(role: .destructive) {
                                        Theme.impact(.soft)
                                        Task { await remove([fact.id]) }
                                    } label: {
                                        Label("Remove", systemImage: "trash")
                                    }
                                }
                        }
                    }
                    .padding(.horizontal, Theme.spaceL)
                    .animation(reduceMotion ? nil : Theme.springSoft, value: segment)
                }

                Color.clear.frame(height: Theme.dockClearance)
            }
            .padding(.top, 4)
        }
        .onAppear {
            if pendingImportScroll {
                pendingImportScroll = false
                proxy.scrollTo("import", anchor: .center)
            }
        }
        .onChange(of: pendingImportScroll) { _, go in
            guard go else { return }
            pendingImportScroll = false
            withAnimation(reduceMotion ? nil : Theme.springSoft) {
                proxy.scrollTo("import", anchor: .center)
            }
        }
        .instrumentEnter()
        }
    }

    private func consumeHorizonHop() {
        guard let hop = push.hop else { return }
        switch hop {
        case .youIdentity, .youSearch, .youAdd, .youProjects, .youExperience, .youImport:
            push.hop = nil
            Task { @MainActor in
                if !reduceMotion {
                    try? await Task.sleep(nanoseconds: 280_000_000)
                }
                applyYouHop(hop)
            }
        default:
            break
        }
    }

    private func applyYouHop(_ hop: HorizonHop) {
        switch hop {
        case .youIdentity:
            showIdentity = true
        case .youSearch:
            showSearch = true
        case .youAdd:
            addPrefillHint = nil
            addPrefillCategory = nil
            showAdd = true
        case .youProjects:
            segment = 1
        case .youExperience:
            segment = 0
        case .youImport:
            pendingImportScroll = true
        default:
            break
        }
    }

    private var profileTitle: String {
        let name = Voice.firstName(identity: setup.status?.identity, displayName: config.displayName)
        return name.isEmpty ? "Profile" : name
    }

    private var lookingForLine: String {
        let p = setup.status?.profile ?? [:]
        let roles = p["roles"] ?? ""
        let locs = p["locations"] ?? ""
        let parts = [roles, locs].map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        return parts.isEmpty ? "Not set yet" : parts.joined(separator: " · ")
    }

    private var identityLine: String {
        let id = setup.status?.identity ?? [:]
        let name = [id["first_name"], id["last_name"]].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " ")
        let email = id["email"] ?? ""
        let parts = [name, email].filter { !$0.isEmpty }
        return parts.isEmpty ? "Name and contact for forms" : parts.joined(separator: " · ")
    }

    private var identityCaption: String {
        "Used to fill Greenhouse, Lever, and Ashby."
    }

    private var emptyTitle: String {
        switch segment {
        case 0: return "No experience yet"
        case 1: return "No projects yet"
        default: return "Nothing more yet"
        }
    }

    private var emptyCopy: String {
        switch segment {
        case 0: return "Facts about jobs you’ve held — each one has a city."
        case 1: return "Personal and GitHub projects. No employer location."
        default: return "Strengths, preferences, and saved answers land here. JobPilot reuses them when filling forms."
        }
    }

    private func factCard(_ fact: KnowledgeItem) -> some View {
        FocusCard {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text((fact.label ?? KnowledgeCategoryStyle.sectionTitle(for: fact.category)))
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Theme.horizon)
                        .textCase(.uppercase)
                        .tracking(0.8)
                    Spacer()
                    Image(systemName: KnowledgeCategoryStyle.symbol(for: fact.category))
                        .font(.caption)
                        .foregroundStyle(Theme.soft)
                }
                Text(fact.text)
                    .font(.subheadline)
                    .foregroundStyle(Theme.ink)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func suggestedCategory(for tip: String) -> String {
        let t = tip.lowercased()
        if t.contains("intern") || t.contains("experience") || t.contains("employer") || t.contains("job") {
            return "experience"
        }
        if t.contains("project") { return "project" }
        if t.contains("achievement") || t.contains("impact") { return "achievement" }
        if t.contains("prefer") || t.contains("want") { return "preference" }
        if t.contains("strength") || t.contains("skill") { return "strength" }
        return "project"
    }

    private func remove(_ ids: [Int]) async {
        for id in ids { try? await api.removeKnowledge(id: id) }
        await load()
    }

    private func load() async {
        loading = true
        error = nil
        defer { loading = false }
        do {
            let response = try await api.fetchKnowledge()
            withAnimation(Theme.springSoft) {
                items = response.items
                audit = response.audit
            }
            await setup.refresh(config: config)
        } catch {
            if APIClient.isCancellation(error) { return }
            let msg = APIClient.userMessage(for: error)
            if !msg.isEmpty { self.error = msg }
        }
    }
}

private struct AddFactView: View {
    var initialCategory: String = "project"
    var initialHint: String? = nil
    let onSave: (String, String, String?) async -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var category = "project"
    @State private var text = ""
    @State private var question = ""
    @State private var saving = false

    private let categories = ["experience", "project", "achievement", "strength", "preference", "answer"]

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Picker("Kind", selection: $category) {
                        ForEach(categories, id: \.self) { cat in
                            Text(KnowledgeCategoryStyle.sectionTitle(for: cat)).tag(cat)
                        }
                    }
                }
                if category == "answer" {
                    Section("The question") {
                        TextField("Why do you want to work here?", text: $question)
                    }
                }
                Section(category == "answer" ? "Your answer" : "The fact") {
                    TextField(placeholder, text: $text, axis: .vertical)
                        .lineLimit(3...8)
                }
                if let initialHint, !initialHint.isEmpty, category != "answer" {
                    Text(initialHint)
                        .font(.caption)
                        .foregroundStyle(Theme.soft)
                }
            }
            .navigationTitle("Add a fact")
            .navigationBarTitleDisplayMode(.inline)
            .fogFormChrome()
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        saving = true
                        Task {
                            await onSave(category, text,
                                         category == "answer" ? question : nil)
                            dismiss()
                        }
                    }
                    .fontWeight(.semibold)
                    .disabled(saving || text.isEmpty
                              || (category == "answer" && question.isEmpty))
                }
            }
            .onAppear { category = initialCategory }
            .tint(Theme.accent)
        }
    }

    private var placeholder: String {
        switch category {
        case "experience": return "Software intern at Acme — Austin, TX (Summer 2025)"
        case "project": return "I built a real-time pricing service in Go"
        case "achievement": return "I cut p99 latency 40%"
        case "strength": return "Systems debugging"
        case "preference": return "I want a role with real ownership"
        default: return "I care about infrastructure at scale."
        }
    }
}

private struct SearchEditorView: View {
    @EnvironmentObject var config: Config
    @EnvironmentObject var setup: SetupGate
    @Environment(\.dismiss) private var dismiss
    @State private var roles = ""
    @State private var locations = ""
    @State private var keywords = ""
    @State private var seniority = ""
    @State private var saving = false
    @State private var loaded = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TagEditor(
                        text: $roles,
                        suggestions: [
                            "Software engineer", "Software intern", "Backend",
                            "Full-stack", "ML engineer", "Data scientist",
                        ],
                        placeholder: "Add a role",
                        caption: "Tap every role you want matches for.",
                        field: "roles"
                    )
                } header: { Text("Roles") }
                Section {
                    TagEditor(
                        text: $locations,
                        suggestions: ["Remote", "Chicago", "Minneapolis", "San Francisco", "NYC"],
                        placeholder: "Add a city or Remote",
                        field: "locations"
                    )
                } header: { Text("Locations") }
                Section {
                    TagEditor(
                        text: $keywords,
                        suggestions: ["Python", "JavaScript", "TypeScript", "React", "Java", "SQL"],
                        placeholder: "Add a skill",
                        field: "skills"
                    )
                } header: { Text("Skills") }
                Section {
                    TagEditor(
                        text: $seniority,
                        suggestions: [
                            "Internship", "New grad", "Entry-level",
                            "Junior", "Mid-level", "Senior",
                        ],
                        placeholder: "Add a level",
                        caption: "You can pick more than one."
                    )
                } header: { Text("Seniority") }
                if let error {
                    Text(error).font(.caption).foregroundStyle(Theme.note)
                }
            }
            .navigationTitle("Looking for")
            .navigationBarTitleDisplayMode(.inline)
            .fogFormChrome()
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        saving = true
                        Task { await save() }
                    }
                    .fontWeight(.semibold)
                    .disabled(saving || !loaded
                              || roles.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            // Save posts every field, so the editor must know what it's editing
            // first. Opening on a nil status and saving used to blank out the
            // locations, skills and seniority it had never been shown.
            .task { await loadCurrent() }
            .tint(Theme.accent)
        }
    }

    private func loadCurrent() async {
        if setup.status == nil { await setup.refresh(config: config) }
        guard let p = setup.status?.profile else {
            error = "Couldn't load what you have saved, so Save is off — "
                  + "otherwise it would overwrite it with blanks. Pull to refresh You."
            return
        }
        if roles.isEmpty { roles = p["roles"] ?? "" }
        if locations.isEmpty { locations = p["locations"] ?? "" }
        if keywords.isEmpty { keywords = p["keywords"] ?? "" }
        if seniority.isEmpty { seniority = p["seniority"] ?? "" }
        loaded = true
    }

    private func save() async {
        defer { saving = false }
        guard loaded else { return }
        do {
            try await APIClient(config: config).saveProfile(
                roles: roles,
                locations: locations,
                seniority: seniority,
                keywords: keywords
            )
            await setup.refresh(config: config)
            Theme.impact(.soft)
            dismiss()
        } catch {
            self.error = APIClient.userMessage(for: error)
        }
    }
}

private struct IdentityEditorView: View {
    @EnvironmentObject var config: Config
    @EnvironmentObject var setup: SetupGate
    @Environment(\.dismiss) private var dismiss
    @State private var identity = IdentityDraft()
    @State private var saving = false
    @State private var loaded = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                Section("Name") {
                    TextField("First name", text: $identity.firstName)
                    TextField("Last name", text: $identity.lastName)
                    TextField("Preferred name", text: $identity.preferredName)
                    TextField("Pronouns", text: $identity.pronouns)
                }
                Section("Contact") {
                    TextField("Email", text: $identity.email)
                        .keyboardType(.emailAddress)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    TextField("Phone", text: $identity.phone).keyboardType(.phonePad)
                }
                Section("Location") {
                    TextField("Street address", text: $identity.address)
                    TextField("City", text: $identity.city)
                    TextField("State", text: $identity.state)
                    TextField("ZIP", text: $identity.zip)
                        .keyboardType(.numbersAndPunctuation)
                    TextField("Country", text: $identity.country)
                }
                Section("Links") {
                    TextField("LinkedIn URL", text: $identity.linkedin)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    TextField("GitHub URL", text: $identity.github)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    TextField("Portfolio / website", text: $identity.portfolio)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                }
                Section {
                    ForEach($identity.education) { $entry in
                        EducationRowEditor(entry: $entry) {
                            identity.education.removeAll { $0.id == entry.id }
                        }
                    }
                    Button {
                        withAnimation { identity.education.append(EducationEntry()) }
                    } label: {
                        Label("Add a degree", systemImage: "plus.circle")
                    }
                } header: {
                    Text("Education")
                } footer: {
                    Text("Add each degree separately. A degree you are still "
                         + "reading for is the one forms get asked about, and "
                         + "the rest still go on applications that want your "
                         + "full history.")
                }
                Section("Work") {
                    TextField("Years of experience", text: $identity.years)
                        .keyboardType(.numberPad)
                    TextField("Current company", text: $identity.currentCompany)
                    TextField("Current title", text: $identity.currentTitle)
                    TextField("Start date", text: $identity.startDate)
                    TextField("Internship term (e.g. Summer 2027)",
                              text: $identity.internSeason)
                    TextField("Work arrangement", text: $identity.workArrangement)
                    TextField("Salary expectation", text: $identity.salary)
                    Toggle("Authorized to work in the US", isOn: $identity.workAuthorized)
                    Toggle("Need visa sponsorship", isOn: $identity.needsSponsorship)
                    Toggle("I am 18 or older", isOn: $identity.over18)
                    Toggle("Willing to relocate", isOn: $identity.willingToRelocate)
                    Toggle("Willing to travel", isOn: $identity.canTravel)
                }
                Section("Application defaults") {
                    TextField("How you heard about the role", text: $identity.howHeard)
                    Toggle("Okay with a background check", isOn: $identity.backgroundCheck)
                    Toggle("Okay with a drug test", isOn: $identity.drugTest)
                    Toggle("Previously applied here", isOn: $identity.previouslyApplied)
                    Toggle("Related to an employee", isOn: $identity.relatedToEmployee)
                }
                Section("Demographics (optional)") {
                    TextField("Gender", text: $identity.gender)
                    TextField("Race", text: $identity.race)
                    TextField("Ethnicity", text: $identity.ethnicity)
                    TextField("Veteran status", text: $identity.veteranStatus)
                    TextField("Disability status", text: $identity.disabilityStatus)
                }
                if let error {
                    Text(error).font(.caption).foregroundStyle(Theme.note)
                }
            }
            .navigationTitle("On forms")
            .navigationBarTitleDisplayMode(.inline)
            .fogFormChrome()
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        saving = true
                        Task { await save() }
                    }
                    .fontWeight(.semibold)
                    .disabled(saving || !loaded)
                }
            }
            // `fullPayload()` posts every field including the blanks, so clearing
            // one really clears it. That means the editor must not be savable
            // before it has loaded: opening on a nil status and hitting Save
            // erased the whole saved identity and reset every Yes/No to default.
            .task { await loadCurrent() }
            .tint(Theme.accent)
        }
    }

    private func loadCurrent() async {
        if setup.status == nil { await setup.refresh(config: config) }
        guard let saved = setup.status?.identity else {
            error = "Couldn't load what you have saved, so Save is off — "
                  + "otherwise it would overwrite it with blanks. Pull to refresh You."
            return
        }
        identity.load(from: saved)
        // Separate from the flat map, which is stringified single-degree fields.
        identity.education = setup.status?.education ?? []
        loaded = true
    }

    private func save() async {
        defer { saving = false }
        guard loaded else { return }
        do {
            try await APIClient(config: config)
                .saveIdentity(fields: identity.fullPayload(includeEducation: true))
            await setup.refresh(config: config)
            Theme.impact(.soft)
            dismiss()
        } catch {
            self.error = APIClient.userMessage(for: error)
        }
    }
}

/// One degree in the education list.
///
/// Collapsed to a headline until tapped, because most people have two and a
/// form of six fields each would bury everything under it.
private struct EducationRowEditor: View {
    @Binding var entry: EducationEntry
    let onDelete: () -> Void

    @State private var expanded = false

    private static let degrees = ["B.S.", "B.A.", "M.S.", "M.Eng.", "MBA", "Ph.D.",
                                  "Associate", "Certificate"]

    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            TextField("School", text: $entry.school)
            Picker("Degree", selection: $entry.degree) {
                Text("—").tag("")
                ForEach(Self.degrees, id: \.self) { Text($0).tag($0) }
            }
            TextField("Major / field", text: $entry.discipline)
            Picker("Status", selection: $entry.status) {
                // "" lets the server infer it from the dates, which is right
                // far more often than a default guess would be.
                Text("From the dates").tag("")
                Text("In progress").tag("in_progress")
                Text("Completed").tag("completed")
            }
            TextField("Start year", text: $entry.start_year)
                .keyboardType(.numberPad)
            TextField(entry.status == "in_progress" ? "Expected year" : "Graduation year",
                      text: $entry.grad_year)
                .keyboardType(.numberPad)
            TextField("Graduation month (e.g. May)", text: $entry.grad_month)
            TextField("GPA", text: $entry.gpa)
                .keyboardType(.decimalPad)
            Button(role: .destructive, action: onDelete) {
                Label("Remove this degree", systemImage: "trash")
            }
        } label: {
            VStack(alignment: .leading, spacing: 2) {
                Text(entry.headline)
                if !entry.subtitle.isEmpty {
                    Text(entry.subtitle)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .onAppear {
            // A row the user just added has nothing to show, so open it rather
            // than making them tap a blank line to find out it is empty.
            if entry.isBlank { expanded = true }
        }
    }
}
