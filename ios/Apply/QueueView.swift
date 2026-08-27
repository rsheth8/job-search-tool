import SwiftUI

/// Editorial apply surface: one Up next, a Ready strip, then more matches.
struct QueueView: View {
    @EnvironmentObject var config: Config
    @EnvironmentObject var chrome: AppChrome
    @EnvironmentObject var setup: SetupGate
    @State private var queue: [QueueItem] = []
    @State private var matches: [QueueItem] = []
    @State private var loading = false
    @State private var staging: Set<Int> = []
    @State private var error: String?
    @State private var path = NavigationPath()
    @State private var appeared = false

    private var api: APIClient { APIClient(config: config) }

    private var upNext: QueueItem? { queue.first ?? matches.first }
    private var upNextIsReady: Bool {
        guard let upNext else { return false }
        return queue.contains(where: { $0.posting_id == upNext.posting_id })
    }
    private var readyRest: [QueueItem] {
        guard let upNext, upNextIsReady else { return queue }
        return queue.filter { $0.posting_id != upNext.posting_id }
    }
    private var matchRest: [QueueItem] {
        guard let upNext, !upNextIsReady else { return matches }
        return matches.filter { $0.posting_id != upNext.posting_id }
    }

    var body: some View {
        NavigationStack(path: $path) {
            Group {
                if loading && queue.isEmpty && matches.isEmpty && error == nil {
                    PreparingView(message: "Gathering matches…")
                } else if let error, queue.isEmpty && matches.isEmpty {
                    EmptyStateView(
                        title: "Couldn't load",
                        description: error,
                        retryTitle: "Try again"
                    ) { Task { await load() } }
                } else if queue.isEmpty && matches.isEmpty {
                    if setup.status?.has_profile == false {
                        EmptyStateView(
                            title: "Finish setup",
                            description: "Tell Apply what roles you want so it can find matches.",
                            retryTitle: "Set up profile"
                        ) { setup.reopen() }
                    } else {
                        EmptyStateView(
                            title: "Nothing waiting",
                            description: "Discovery is looking. Pull to refresh — first matches can take a few minutes. Greenhouse, Lever, and Ashby forms autofill; Workday and LinkedIn Easy Apply do not."
                        )
                    }
                } else {
                    content
                        .opacity(appeared ? 1 : 0)
                        .offset(y: appeared ? 0 : 12)
                        .onAppear {
                            withAnimation(Theme.springSoft) { appeared = true }
                        }
                }
            }
            .toolbar(.hidden, for: .navigationBar)
            .navigationDestination(for: QueueItem.self) { ApplyView(item: $0) }
            .refreshable { await load() }
            .ambientScreen()
            .onChange(of: path.count) { _, count in
                chrome.dockHidden = count > 0
            }
            .onAppear { chrome.dockHidden = !path.isEmpty }
            // Do NOT reset dockHidden in onDisappear — NavigationStack fires that
            // when a detail is pushed, which raced Autofill and left the floating
            // dock painted over the apply controls.
            .task {
                let uid = config.user
                if queue.isEmpty && matches.isEmpty, let cached = QueueCache.load(user: uid) {
                    queue = cached.queue
                    matches = cached.matches
                    chrome.readyCount = cached.queue.count
                }
                await load()
            }
        }
    }

    private var content: some View {
        ScrollView(showsIndicators: false) {
            VStack(alignment: .leading, spacing: Theme.spaceXL) {
                PageHeader(
                    eyebrow: "Apply",
                    title: greeting,
                    subtitle: subtitleLine
                )

                if let item = upNext {
                    UpNextCard(
                        item: item,
                        actionTitle: upNextIsReady ? "Open application" : "Prepare application",
                        busy: staging.contains(item.posting_id),
                        showPassActions: upNextIsReady,
                        onPass: { Task { await pass(item) } },
                        onSkip: { Task { await skip(item) } }
                    ) {
                        if upNextIsReady {
                            path.append(item)
                        } else {
                            Task { await stage(item) }
                        }
                    }
                    .padding(.horizontal, Theme.spaceL)
                }

                if !readyRest.isEmpty {
                    VStack(alignment: .leading, spacing: Theme.spaceS) {
                        sectionLabel("Also ready")
                        ScrollView(.horizontal, showsIndicators: false) {
                            HStack(spacing: 12) {
                                ForEach(readyRest) { item in
                                    Button { path.append(item) } label: {
                                        ReadyChipCard(item: item)
                                    }
                                    .buttonStyle(.plain)
                                    .contextMenu {
                                        Button { path.append(item) } label: {
                                            Label("Open", systemImage: "arrow.up.right")
                                        }
                                        Button { Task { await skip(item) } } label: {
                                            Label("Skip for now", systemImage: "clock")
                                        }
                                        Button(role: .destructive) {
                                            Task { await pass(item) }
                                        } label: {
                                            Label("Pass", systemImage: "xmark")
                                        }
                                    }
                                }
                            }
                            .padding(.horizontal, Theme.spaceL)
                        }
                    }
                }

                if !matchRest.isEmpty {
                    VStack(alignment: .leading, spacing: Theme.spaceS) {
                        sectionLabel("More matches")
                        VStack(spacing: 0) {
                            ForEach(Array(matchRest.enumerated()), id: \.element.id) { idx, item in
                                HStack(alignment: .center, spacing: 12) {
                                    QuietRow(
                                        title: item.title ?? "Role",
                                        subtitle: item.company,
                                        score: item.score
                                    )
                                    Button {
                                        Task { await stage(item) }
                                    } label: {
                                        Text(staging.contains(item.posting_id) ? "…" : "Prepare")
                                            .font(.subheadline.weight(.semibold))
                                            .foregroundStyle(Theme.accent)
                                    }
                                    .disabled(staging.contains(item.posting_id))
                                }
                                .padding(.horizontal, Theme.spaceL)
                                .padding(.vertical, 10)
                                .opacity(appeared ? 1 : 0)
                                .animation(Theme.springSoft.delay(0.04 * Double(idx)),
                                           value: appeared)

                                if idx < matchRest.count - 1 {
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

                Color.clear.frame(height: 88) // dock clearance
            }
            .padding(.top, 8)
        }
    }

    private var greeting: String {
        let hour = Calendar.current.component(.hour, from: Date())
        if hour < 12 { return "Good morning" }
        if hour < 17 { return "Good afternoon" }
        return "Good evening"
    }

    private var subtitleLine: String? {
        if !queue.isEmpty {
            return "\(queue.count) ready · one clear next step"
        }
        if !matches.isEmpty {
            return "\(matches.count) matches worth a look"
        }
        return nil
    }

    private func sectionLabel(_ text: String) -> some View {
        Text(text)
            .font(.caption.weight(.semibold))
            .foregroundStyle(Theme.soft)
            .textCase(.uppercase)
            .tracking(0.8)
            .padding(.horizontal, Theme.spaceL)
    }

    private func stage(_ item: QueueItem) async {
        guard !staging.contains(item.posting_id) else { return }
        staging.insert(item.posting_id)
        defer { staging.remove(item.posting_id) }
        do {
            try await api.stage(postingId: item.posting_id)
            Theme.impact(.soft)
            withAnimation(Theme.springSoft) {
                matches.removeAll { $0.posting_id == item.posting_id }
                if !queue.contains(where: { $0.posting_id == item.posting_id }) {
                    queue.insert(item, at: 0)
                }
                chrome.readyCount = queue.count
            }
            await load()
        } catch {
            Theme.notify(.error)
        }
    }

    /// Unstage — may reappear under More matches later.
    private func skip(_ item: QueueItem) async {
        do {
            try await api.skipQueueItem(postingId: item.posting_id)
            Theme.impact(.soft)
            withAnimation(Theme.springSoft) {
                queue.removeAll { $0.posting_id == item.posting_id }
                chrome.readyCount = queue.count
            }
            await load()
        } catch {
            Theme.notify(.error)
        }
    }

    /// Pass for good — dismissed, won't surface again.
    private func pass(_ item: QueueItem) async {
        do {
            try await api.passPosting(postingId: item.posting_id)
            Theme.impact(.soft)
            withAnimation(Theme.springSoft) {
                queue.removeAll { $0.posting_id == item.posting_id }
                matches.removeAll { $0.posting_id == item.posting_id }
                chrome.readyCount = queue.count
            }
            await load()
        } catch {
            Theme.notify(.error)
        }
    }

    private func load() async {
        loading = true
        error = nil
        defer { loading = false }
        do {
            let data = try await api.fetchData()
            withAnimation(Theme.springSoft) {
                queue = data.queue
                matches = data.matches
            }
            chrome.readyCount = data.queue.count
            QueueCache.save(queue: data.queue, matches: data.matches, user: config.user)
        } catch {
            if APIClient.isCancellation(error) { return }
            if queue.isEmpty && matches.isEmpty {
                let msg = APIClient.userMessage(for: error)
                if !msg.isEmpty { self.error = msg }
            }
        }
    }
}
