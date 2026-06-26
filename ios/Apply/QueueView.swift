import SwiftUI

/// Your staged matches. Tap one to open it in the in-app apply browser.
struct QueueView: View {
    @EnvironmentObject var config: Config
    @State private var items: [QueueItem] = []
    @State private var loading = false
    @State private var error: String?

    private var api: APIClient { APIClient(config: config) }

    var body: some View {
        NavigationStack {
            Group {
                if loading && items.isEmpty {
                    ProgressView("Loading your matches…")
                } else if let error {
                    ContentUnavailableView("Couldn't load", systemImage: "wifi.slash",
                                           description: Text(error))
                } else if items.isEmpty {
                    ContentUnavailableView("Nothing staged yet", systemImage: "tray",
                        description: Text("Queue matches from Slack, then pull to refresh."))
                } else {
                    List(items) { item in
                        NavigationLink(value: item) { row(item) }
                    }
                    .listStyle(.plain)
                }
            }
            .navigationTitle("Apply")
            .navigationDestination(for: QueueItem.self) { ApplyView(item: $0) }
            .refreshable { await load() }
            .task { await load() }
        }
    }

    private func row(_ item: QueueItem) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(item.title ?? "Role").font(.headline)
            HStack(spacing: 6) {
                Text(item.company ?? "—").foregroundStyle(.secondary)
                if let s = item.source { Text("· \(s)").font(.caption).foregroundStyle(.tertiary) }
                Spacer()
                if let sc = item.score { Text("\(Int(sc * 100))%").font(.caption).foregroundStyle(.secondary) }
            }.font(.subheadline)
        }.padding(.vertical, 2)
    }

    private func load() async {
        loading = true; error = nil
        defer { loading = false }
        do { items = try await api.fetchQueue() }
        catch { self.error = "\(error)" }
    }
}
