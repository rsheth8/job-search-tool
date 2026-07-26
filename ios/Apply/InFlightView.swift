import SwiftUI

/// Applications the submit worker is handling, and the approval gate.
///
/// The worker fills a form, screenshots it, and waits. Approving used to mean
/// opening Slack or the web page; this puts the gate on the phone where the rest of
/// the flow already lives.
///
/// **Nothing is ever submitted without an explicit tap here** — same rule as every
/// other surface. The server enforces it too; this is the human, not a shortcut.
struct InFlightView: View {
    @EnvironmentObject var config: Config
    @State private var rows: [InFlightRow] = []
    @State private var loading = false
    @State private var busy: Set<Int> = []
    @State private var error: String?

    private var api: APIClient { APIClient(config: config) }

    var body: some View {
        NavigationStack {
            Group {
                if loading && rows.isEmpty {
                    ProgressView("Checking…")
                } else if let error {
                    ContentUnavailableView("Couldn't load", systemImage: "wifi.slash",
                                           description: Text(error))
                } else if rows.isEmpty {
                    ContentUnavailableView("Nothing in flight", systemImage: "checkmark.circle",
                        description: Text("Prepare a match, then tap Auto-fill & submit to start one."))
                } else {
                    List(rows) { row in card(row) }.listStyle(.insetGrouped)
                }
            }
            .navigationTitle("In flight")
            .refreshable { await load() }
            .task { await load() }
        }
    }

    @ViewBuilder private func card(_ row: InFlightRow) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(row.label).font(.headline)
            Label(row.state, systemImage: row.awaiting ? "hand.raised.fill" : "clock")
                .font(.subheadline)
                .foregroundStyle(row.awaiting ? .orange : .secondary)

            if let preview = row.preview {
                if let filled = preview.filled, !filled.isEmpty {
                    Text("Filled \(filled.count) field\(filled.count == 1 ? "" : "s")")
                        .font(.caption).foregroundStyle(.secondary)
                    ForEach(filled.prefix(6), id: \.self) { f in
                        Text("✓ \(f.label): \(f.value)")
                            .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
                if let skipped = preview.skipped, !skipped.isEmpty {
                    Text("Left for you: \(skipped.prefix(5).joined(separator: ", "))")
                        .font(.caption2).foregroundStyle(.orange).lineLimit(2)
                }
                // The screenshot is a JPEG data: URL, so you approve against the real
                // form rather than a list of field names.
                if let shot = preview.screenshot_url, let image = dataImage(shot) {
                    Image(uiImage: image)
                        .resizable().scaledToFit()
                        .frame(maxHeight: 260)
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                }
            }

            if row.awaiting, let rid = row.request_id {
                HStack(spacing: 10) {
                    Button {
                        Task { await act(rid) { try await api.approve(requestId: $0) } }
                    } label: {
                        // Short label on purpose: "Approve & submit" wraps to two
                        // lines beside Cancel on a phone. The caption below carries
                        // the meaning.
                        Label("Approve", systemImage: "paperplane.fill")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)

                    Button(role: .destructive) {
                        Task { await act(rid) { try await api.cancelRequest(requestId: $0) } }
                    } label: {
                        Label("Cancel", systemImage: "xmark").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)
                }
                .disabled(busy.contains(rid))
                Text("It submits the form for you. Nothing is sent until you tap Approve.")
                    .font(.caption2).foregroundStyle(.tertiary)
            }
        }
        .padding(.vertical, 4)
    }

    private func dataImage(_ url: String) -> UIImage? {
        guard let comma = url.firstIndex(of: ","),
              let data = Data(base64Encoded: String(url[url.index(after: comma)...]))
        else { return nil }
        return UIImage(data: data)
    }

    private func act(_ requestId: Int, _ call: @escaping (Int) async throws -> Void) async {
        busy.insert(requestId)
        defer { busy.remove(requestId) }
        try? await call(requestId)
        await load()
    }

    private func load() async {
        loading = true; error = nil
        defer { loading = false }
        do { rows = try await api.fetchInFlight() }
        catch { self.error = "\(error)" }
    }
}
