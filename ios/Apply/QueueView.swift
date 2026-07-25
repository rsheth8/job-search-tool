import SwiftUI

/// Your staged matches, plus the top matches you could stage.
///
/// Staging used to require Slack — the app fetched `/apply/data` and threw away the
/// half listing un-staged matches. Both halves are here now, so the whole
/// browse → stage → apply loop happens on the phone.
struct QueueView: View {
    @EnvironmentObject var config: Config
    @State private var queue: [QueueItem] = []
    @State private var matches: [QueueItem] = []
    @State private var loading = false
    @State private var staging: Set<Int> = []
    @State private var error: String?

    private var api: APIClient { APIClient(config: config) }

    var body: some View {
        NavigationStack {
            Group {
                if loading && queue.isEmpty && matches.isEmpty {
                    ProgressView("Loading your matches…")
                } else if let error {
                    ContentUnavailableView("Couldn't load", systemImage: "wifi.slash",
                                           description: Text(error))
                } else if queue.isEmpty && matches.isEmpty {
                    ContentUnavailableView("Nothing yet", systemImage: "tray",
                        description: Text("New matches land here as discovery finds them."))
                } else {
                    list
                }
            }
            .navigationTitle("Apply")
            .navigationDestination(for: QueueItem.self) { ApplyView(item: $0) }
            .refreshable { await load() }
            .task { await load() }
        }
    }

    private var list: some View {
        List {
            if !queue.isEmpty {
                Section("Ready to apply") {
                    ForEach(queue) { item in
                        NavigationLink(value: item) { row(item) }
                    }
                }
            }
            if !matches.isEmpty {
                Section("Top matches") {
                    ForEach(matches) { item in
                        VStack(alignment: .leading, spacing: 8) {
                            row(item)
                            Button {
                                Task { await stage(item) }
                            } label: {
                                Label(staging.contains(item.posting_id)
                                      ? "Preparing…" : "Prepare application",
                                      systemImage: "tray.and.arrow.down")
                            }
                            .buttonStyle(.bordered)
                            .disabled(staging.contains(item.posting_id))
                        }
                        .padding(.vertical, 2)
                    }
                }
            }
        }
        .listStyle(.insetGrouped)
    }

    private func row(_ item: QueueItem) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(item.title ?? "Role").font(.headline)
            HStack(spacing: 6) {
                Text(item.company ?? "—").foregroundStyle(.secondary)
                if let s = item.source { Text("· \(s)").font(.caption).foregroundStyle(.tertiary) }
                Spacer()
                if let sc = item.score { Text("\(Int(sc * 100))%").font(.caption).foregroundStyle(.secondary) }
            }.font(.subheadline)

            // Why this one surfaced. A percentage on its own can't be argued with.
            if let reasons = item.reasons, !reasons.isEmpty {
                Label(reasons.joined(separator: " · "), systemImage: "checkmark.seal")
                    .font(.caption).foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            ForEach(item.concerns ?? [], id: \.self) { concern in
                Label(concern, systemImage: "exclamationmark.triangle")
                    .font(.caption2).foregroundStyle(.orange).lineLimit(1)
            }
            fillBadge(item)
        }.padding(.vertical, 2)
    }

    @ViewBuilder private func fillBadge(_ item: QueueItem) -> some View {
        if item.isFirstParty {
            Label("Auto-fill ready", systemImage: "bolt.fill")
                .font(.caption2.weight(.medium)).foregroundStyle(.green)
        } else {
            Label("Aggregator · may need login", systemImage: "person.badge.key")
                .font(.caption2.weight(.medium)).foregroundStyle(.orange)
        }
    }

    private func stage(_ item: QueueItem) async {
        staging.insert(item.posting_id)
        defer { staging.remove(item.posting_id) }
        try? await api.stage(postingId: item.posting_id)
        await load()
    }

    private func load() async {
        loading = true; error = nil
        defer { loading = false }
        do {
            let data = try await api.fetchData()
            queue = data.queue
            matches = data.matches
        } catch { self.error = "\(error)" }
    }
}
