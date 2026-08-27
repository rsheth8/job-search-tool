import SwiftUI

/// Dossier layout: coverage hero + segmented Experience / Projects / More.
struct KnowledgeView: View {
    @EnvironmentObject var config: Config
    @State private var items: [KnowledgeItem] = []
    @State private var audit: KnowledgeAudit?
    @State private var loading = false
    @State private var showAdd = false
    @State private var error: String?
    @State private var addPrefillHint: String?
    @State private var addPrefillCategory: String?
    @State private var segment = 0

    private var api: APIClient { APIClient(config: config) }

    private var experience: [KnowledgeItem] {
        items.filter { $0.category == "achievement" }
    }
    private var projects: [KnowledgeItem] {
        items.filter { $0.category == "project" }
    }
    private var other: [KnowledgeItem] {
        items.filter { !["achievement", "project"].contains($0.category) }
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
                        retryTitle: "Try again"
                    ) { Task { await load() } }
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
            .refreshable { await load() }
            .ambientScreen()
            .task { await load() }
        }
    }

    private var dossier: some View {
        ScrollView(showsIndicators: false) {
            VStack(alignment: .leading, spacing: Theme.spaceL) {
                HStack(alignment: .top) {
                    PageHeader(
                        eyebrow: "About me",
                        title: "Your dossier",
                        subtitle: "What drafted answers can cite."
                    )
                    Button {
                        addPrefillHint = nil
                        addPrefillCategory = nil
                        showAdd = true
                    } label: {
                        Image(systemName: "plus")
                            .font(.body.weight(.semibold))
                            .foregroundStyle(Theme.accent)
                            .frame(width: 40, height: 40)
                            .background(Color.white.opacity(0.7), in: Circle())
                    }
                    .padding(.trailing, Theme.spaceL)
                    .padding(.top, Theme.spaceM)
                }

                if let audit {
                    CoverageMeter(
                        score: audit.score,
                        missing: audit.identity_missing,
                        suggestion: audit.suggestions.first.map { "Add something? \($0)" }
                    ) {
                        if let tip = audit.suggestions.first {
                            addPrefillHint = tip
                            addPrefillCategory = suggestedCategory(for: tip)
                            showAdd = true
                        }
                    }
                    .padding(.horizontal, Theme.spaceL)
                }

                Picker("", selection: $segment) {
                    Text("Experience").tag(0)
                    Text("Projects").tag(1)
                    Text("More").tag(2)
                }
                .pickerStyle(.segmented)
                .padding(.horizontal, Theme.spaceL)
                .onChange(of: segment) { _, _ in Theme.selection() }

                let shown: [KnowledgeItem] = {
                    switch segment {
                    case 0: return experience
                    case 1: return projects
                    default: return other
                    }
                }()

                if shown.isEmpty {
                    Text(emptyCopy)
                        .font(.callout)
                        .foregroundStyle(Theme.soft)
                        .padding(.horizontal, Theme.spaceL)
                        .padding(.top, Theme.spaceS)
                } else {
                    LazyVStack(spacing: 12) {
                        ForEach(shown) { fact in
                            factCard(fact)
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
                    .animation(Theme.springSoft, value: segment)
                }

                Color.clear.frame(height: 88)
            }
            .padding(.top, 8)
        }
    }

    private var emptyCopy: String {
        switch segment {
        case 0: return "No experience saved yet."
        case 1: return "No projects saved yet."
        default: return "Strengths, preferences, and saved answers land here."
        }
    }

    private func factCard(_ fact: KnowledgeItem) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text((fact.label ?? KnowledgeCategoryStyle.sectionTitle(for: fact.category)))
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Theme.accent)
                    .textCase(.uppercase)
                    .tracking(0.6)
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
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .fill(Color.white.opacity(0.72))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .strokeBorder(Theme.accent.opacity(0.08), lineWidth: 1)
        )
    }

    private func suggestedCategory(for tip: String) -> String {
        let t = tip.lowercased()
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

    private let categories = ["project", "achievement", "strength", "preference", "answer"]

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Picker("Kind", selection: $category) {
                        ForEach(categories, id: \.self) { cat in
                            Text(cat.capitalized).tag(cat)
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
        case "project": return "I built a real-time pricing service in Go"
        case "achievement": return "I cut p99 latency 40%"
        case "strength": return "Systems debugging"
        case "preference": return "I want a role with real ownership"
        default: return "I care about infrastructure at scale."
        }
    }
}
