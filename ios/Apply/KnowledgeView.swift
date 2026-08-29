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
            .refreshable { await load() }
            .ambientScreen()
            .task { await load() }
            .onAppear { consumeHorizonHop() }
            .onChange(of: push.hop) { _, _ in consumeHorizonHop() }
        }
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
    @State private var saving = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("New grad SWE, backend intern", text: $roles, axis: .vertical)
                        .lineLimit(2...4)
                } header: { Text("Roles") }
                Section {
                    TextField("NYC, remote, Chicago", text: $locations, axis: .vertical)
                        .lineLimit(2...4)
                } header: { Text("Locations") }
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
                    .disabled(saving || roles.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            .onAppear {
                let p = setup.status?.profile ?? [:]
                if roles.isEmpty { roles = p["roles"] ?? "" }
                if locations.isEmpty { locations = p["locations"] ?? "" }
            }
            .tint(Theme.accent)
        }
    }

    private func save() async {
        defer { saving = false }
        do {
            try await APIClient(config: config).saveProfile(
                roles: roles,
                locations: locations,
                seniority: setup.status?.profile["seniority"] ?? "",
                keywords: setup.status?.profile["keywords"] ?? ""
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
                Section("Education") {
                    TextField("School", text: $identity.school)
                    TextField("Degree", text: $identity.degree)
                    TextField("Major", text: $identity.discipline)
                    TextField("Graduation year", text: $identity.gradYear)
                        .keyboardType(.numberPad)
                    TextField("GPA", text: $identity.gpa)
                        .keyboardType(.decimalPad)
                }
                Section("Work") {
                    TextField("Years of experience", text: $identity.years)
                        .keyboardType(.numberPad)
                    TextField("Current company", text: $identity.currentCompany)
                    TextField("Current title", text: $identity.currentTitle)
                    TextField("Start date", text: $identity.startDate)
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
                    .disabled(saving)
                }
            }
            .onAppear { identity.load(from: setup.status?.identity ?? [:]) }
            .tint(Theme.accent)
        }
    }

    private func save() async {
        defer { saving = false }
        do {
            try await APIClient(config: config).saveIdentity(fields: identity.fullPayload())
            await setup.refresh(config: config)
            Theme.impact(.soft)
            dismiss()
        } catch {
            self.error = APIClient.userMessage(for: error)
        }
    }
}
