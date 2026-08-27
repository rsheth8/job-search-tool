import SwiftUI

/// One calm approval at a time — never a stack of alarms.
struct InFlightView: View {
    @EnvironmentObject var config: Config
    @EnvironmentObject var chrome: AppChrome
    @Environment(\.scenePhase) private var scenePhase
    @State private var rows: [InFlightRow] = []
    @State private var loading = false
    @State private var busy: Set<Int> = []
    @State private var error: String?
    @State private var confirmApprove: Int?
    @State private var confirmCancel: Int?
    @State private var expandedShot: IdentifiedImage?

    private var api: APIClient { APIClient(config: config) }
    private var focus: InFlightRow? { rows.first(where: \.awaiting) }
    private var others: [InFlightRow] {
        guard let focus else { return rows }
        return rows.filter { $0.id != focus.id }
    }

    var body: some View {
        NavigationStack {
            Group {
                if loading && rows.isEmpty {
                    PreparingView(message: "Checking…")
                } else if let error, rows.isEmpty {
                    EmptyStateView(
                        title: "Couldn't load",
                        description: error,
                        retryTitle: "Try again"
                    ) { Task { await load() } }
                } else if rows.isEmpty {
                    EmptyStateView(title: "You're clear.")
                } else {
                    ScrollView(showsIndicators: false) {
                        VStack(alignment: .leading, spacing: Theme.spaceXL) {
                            PageHeader(
                                eyebrow: "In flight",
                                title: focus != nil ? "Needs a look" : "In progress",
                                subtitle: focus != nil
                                    ? "One approval at a time."
                                    : "Nothing waiting on you right now."
                            )

                            if let focus {
                                ApprovalFocus(
                                    row: focus,
                                    busy: focus.request_id.map { busy.contains($0) } ?? false,
                                    onApprove: { if let rid = focus.request_id { confirmApprove = rid } },
                                    onCancel: { if let rid = focus.request_id { confirmCancel = rid } },
                                    onExpandShot: { expandedShot = IdentifiedImage(image: $0) }
                                )
                                .padding(.horizontal, Theme.spaceL)
                            }

                            if !others.isEmpty {
                                VStack(alignment: .leading, spacing: Theme.spaceS) {
                                    Text(focus == nil ? "Working" : "Also in progress")
                                        .font(.caption.weight(.semibold))
                                        .foregroundStyle(Theme.soft)
                                        .textCase(.uppercase)
                                        .tracking(0.8)
                                        .padding(.horizontal, Theme.spaceL)

                                    VStack(spacing: 0) {
                                        ForEach(others) { row in
                                            QuietRow(
                                                title: row.label,
                                                subtitle: row.state
                                            )
                                            .padding(.horizontal, Theme.spaceL)
                                            .padding(.vertical, 10)
                                            if row.id != others.last?.id {
                                                Divider()
                                                    .background(Theme.accent.opacity(0.08))
                                                    .padding(.leading, Theme.spaceL)
                                            }
                                        }
                                    }
                                    .background(
                                        RoundedRectangle(cornerRadius: 20, style: .continuous)
                                            .fill(Color.white.opacity(0.65))
                                    )
                                    .padding(.horizontal, Theme.spaceL)
                                }
                            }

                            Color.clear.frame(height: 88)
                        }
                        .padding(.top, 8)
                    }
                }
            }
            .toolbar(.hidden, for: .navigationBar)
            .ambientScreen()
            .refreshable { await load() }
            .task {
                await load()
                while !Task.isCancelled {
                    try? await Task.sleep(nanoseconds: 4_000_000_000)
                    if Task.isCancelled { break }
                    guard scenePhase == .active, rows.contains(where: \.awaiting) else { continue }
                    await load(quiet: true)
                }
            }
            .confirmationDialog("Submit this application?",
                                isPresented: Binding(
                                    get: { confirmApprove != nil },
                                    set: { if !$0 { confirmApprove = nil } }
                                ),
                                titleVisibility: .visible) {
                Button("Approve & submit") {
                    if let rid = confirmApprove {
                        Task { await act(rid) { try await api.approve(requestId: $0) } }
                    }
                    confirmApprove = nil
                }
                Button("Not now", role: .cancel) { confirmApprove = nil }
            } message: {
                Text("Nothing is sent until you confirm.")
            }
            .confirmationDialog("Set this aside?",
                                isPresented: Binding(
                                    get: { confirmCancel != nil },
                                    set: { if !$0 { confirmCancel = nil } }
                                ),
                                titleVisibility: .visible) {
                Button("Cancel fill", role: .destructive) {
                    if let rid = confirmCancel {
                        Task { await act(rid) { try await api.cancelRequest(requestId: $0) } }
                    }
                    confirmCancel = nil
                }
                Button("Keep waiting", role: .cancel) { confirmCancel = nil }
            }
            .fullScreenCover(item: $expandedShot) { shot in
                ScreenshotViewer(image: shot.image) { expandedShot = nil }
            }
        }
    }

    private func act(_ requestId: Int, _ call: @escaping (Int) async throws -> Void) async {
        busy.insert(requestId)
        defer { busy.remove(requestId) }
        do {
            try await call(requestId)
            Theme.impact(.soft)
        } catch {
            Theme.notify(.error)
        }
        await load()
    }

    private func load(quiet: Bool = false) async {
        if !quiet { loading = true; error = nil }
        defer { if !quiet { loading = false } }
        do {
            let next = try await api.fetchInFlight()
            withAnimation(Theme.springSoft) { rows = next }
            chrome.awaitingCount = next.filter(\.awaiting).count
        } catch {
            if APIClient.isCancellation(error) { return }
            if rows.isEmpty {
                let msg = APIClient.userMessage(for: error)
                if !msg.isEmpty { self.error = msg }
            }
        }
    }
}

private struct IdentifiedImage: Identifiable {
    let id = UUID()
    let image: UIImage
}

private struct ApprovalFocus: View {
    let row: InFlightRow
    let busy: Bool
    let onApprove: () -> Void
    let onCancel: () -> Void
    let onExpandShot: (UIImage) -> Void

    var body: some View {
        FocusCard {
            VStack(alignment: .leading, spacing: Theme.spaceM) {
                QuietStatus(text: "Ready for a look", emphasize: true)

                Text(row.label)
                    .font(Theme.title(24))
                    .foregroundStyle(Theme.ink)
                    .fixedSize(horizontal: false, vertical: true)

                Text(row.state)
                    .font(.subheadline)
                    .foregroundStyle(Theme.soft)

                if let preview = row.preview {
                    if let filled = preview.filled, !filled.isEmpty {
                        Text("Filled \(filled.count) field\(filled.count == 1 ? "" : "s")")
                            .font(.caption)
                            .foregroundStyle(Theme.soft)
                        VStack(alignment: .leading, spacing: 4) {
                            ForEach(filled.prefix(4), id: \.self) { f in
                                Text("\(f.label): \(f.value)")
                                    .font(.caption)
                                    .foregroundStyle(Theme.ink.opacity(0.65))
                                    .lineLimit(1)
                            }
                        }
                    }
                    if let skipped = preview.skipped, !skipped.isEmpty {
                        Text("Left for you: \(skipped.prefix(4).joined(separator: ", "))")
                            .font(.caption)
                            .foregroundStyle(Theme.note)
                            .lineLimit(2)
                    }
                    if let shot = preview.screenshot_url, let image = dataImage(shot) {
                        Button { onExpandShot(image) } label: {
                            Image(uiImage: image)
                                .resizable()
                                .scaledToFit()
                                .frame(maxHeight: 220)
                                .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                        }
                        .buttonStyle(.plain)
                    }
                }

                if row.request_id != nil {
                    PrimaryButton(title: "Approve", systemImage: nil, busy: busy, action: onApprove)
                    Button("Not now", action: onCancel)
                        .font(.subheadline)
                        .foregroundStyle(Theme.soft)
                        .frame(maxWidth: .infinity)
                        .disabled(busy)
                    Text("Submits only after you approve.")
                        .font(.caption2)
                        .foregroundStyle(Theme.soft.opacity(0.8))
                        .frame(maxWidth: .infinity)
                }
            }
        }
        .opacity(busy ? 0.7 : 1)
    }

    private func dataImage(_ url: String) -> UIImage? {
        guard let comma = url.firstIndex(of: ","),
              let data = Data(base64Encoded: String(url[url.index(after: comma)...]))
        else { return nil }
        return UIImage(data: data)
    }
}

private struct ScreenshotViewer: View {
    let image: UIImage
    let onClose: () -> Void

    var body: some View {
        NavigationStack {
            ZoomableImage(image: image)
                .background(Theme.ink)
                .ignoresSafeArea()
                .toolbar {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button("Done", action: onClose)
                    }
                }
        }
    }
}

private struct ZoomableImage: View {
    let image: UIImage
    @State private var scale: CGFloat = 1

    var body: some View {
        Image(uiImage: image)
            .resizable()
            .scaledToFit()
            .scaleEffect(scale)
            .gesture(MagnifyGesture().onChanged { scale = max(1, $0.magnification) }
                .onEnded { _ in
                    withAnimation(Theme.springSoft) { if scale < 1.05 { scale = 1 } }
                })
            .onTapGesture(count: 2) {
                withAnimation(Theme.springSoft) { scale = scale > 1.2 ? 1 : 2 }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
