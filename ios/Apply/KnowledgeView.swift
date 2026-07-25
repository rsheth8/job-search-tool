import SwiftUI

/// What the assistant knows about you — and what it still needs.
///
/// This is the lever on how much it can fill without asking: an empty store is why
/// drafted answers read generic, and missing identity fields are why autofill leaves
/// gaps. Both are shown here, with the fastest thing to fix listed first.
struct KnowledgeView: View {
    @EnvironmentObject var config: Config
    @State private var items: [KnowledgeItem] = []
    @State private var audit: KnowledgeAudit?
    @State private var loading = false
    @State private var showAdd = false
    @State private var error: String?

    private var api: APIClient { APIClient(config: config) }

    var body: some View {
        NavigationStack {
            Group {
                if loading && items.isEmpty && audit == nil {
                    ProgressView("Loading…")
                } else if let error {
                    ContentUnavailableView("Couldn't load", systemImage: "wifi.slash",
                                           description: Text(error))
                } else {
                    list
                }
            }
            .navigationTitle("About me")
            .toolbar {
                Button { showAdd = true } label: { Label("Add", systemImage: "plus") }
            }
            .sheet(isPresented: $showAdd) {
                AddFactView { category, text, label in
                    try? await api.addKnowledge(category: category, text: text, label: label)
                    await load()
                }
            }
            .refreshable { await load() }
            .task { await load() }
        }
    }

    private var list: some View {
        List {
            if let audit {
                Section("Coverage") {
                    HStack {
                        Text("Your details")
                        Spacer()
                        Text("\(Int(audit.score * 100))% complete")
                            .foregroundStyle(audit.score < 0.7 ? .orange : .secondary)
                    }
                    if !audit.identity_missing.isEmpty {
                        Text("Missing: " + audit.identity_missing.joined(separator: ", "))
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    ForEach(audit.suggestions.prefix(3), id: \.self) { tip in
                        Label(tip, systemImage: "lightbulb")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            }

            if items.isEmpty {
                Section {
                    Text("Nothing stored yet — that's why drafted answers read generic. "
                         + "Add a project or an achievement and they'll cite your real work.")
                        .font(.callout).foregroundStyle(.secondary)
                }
            } else {
                ForEach(grouped, id: \.0) { category, facts in
                    Section(category.capitalized + "s") {
                        ForEach(facts) { fact in
                            VStack(alignment: .leading, spacing: 2) {
                                if let label = fact.label {
                                    Text(label).font(.caption).foregroundStyle(.secondary)
                                }
                                Text(fact.text).font(.subheadline)
                            }
                        }
                        .onDelete { offsets in
                            Task { await remove(offsets.map { facts[$0].id }) }
                        }
                    }
                }
            }
        }
    }

    private var grouped: [(String, [KnowledgeItem])] {
        Dictionary(grouping: items, by: \.category)
            .sorted { $0.key < $1.key }
            .map { ($0.key, $0.value) }
    }

    private func remove(_ ids: [Int]) async {
        for id in ids { try? await api.removeKnowledge(id: id) }
        await load()
    }

    private func load() async {
        loading = true; error = nil
        defer { loading = false }
        do {
            let response = try await api.fetchKnowledge()
            items = response.items
            audit = response.audit
        } catch { self.error = "\(error)" }
    }
}

/// Add one fact. A saved *answer* needs the question it answers, so it can be
/// matched back and reused verbatim next time — that's the whole point of it.
private struct AddFactView: View {
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
                Picker("Kind", selection: $category) {
                    ForEach(categories, id: \.self) { Text($0.capitalized).tag($0) }
                }
                if category == "answer" {
                    Section("The question") {
                        TextField("Why do you want to work here?", text: $question)
                    }
                }
                Section(category == "answer" ? "Your answer" : "The fact") {
                    TextField(placeholder, text: $text, axis: .vertical).lineLimit(3...8)
                }
                if category == "answer" {
                    Text("Saved answers are reused word-for-word when that question comes "
                         + "up again — no redraft, no cost.")
                        .font(.caption).foregroundStyle(.secondary)
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
                    .disabled(saving || text.isEmpty
                              || (category == "answer" && question.isEmpty))
                }
            }
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
