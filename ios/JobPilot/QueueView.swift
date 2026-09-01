import SwiftUI
import UIKit

/// Apply home: one scored next as a gauge, a quiet tape of the rest, one Preflight.
struct QueueView: View {
    @EnvironmentObject var config: Config
    @EnvironmentObject var chrome: AppChrome
    @EnvironmentObject var setup: SetupGate
    @EnvironmentObject var push: PushManager
    @State private var queue: [QueueItem] = []
    @State private var matches: [QueueItem] = []
    @State private var loading = false
    @State private var staging: Set<Int> = []
    @State private var error: String?
    @State private var path = NavigationPath()
    @State private var toast: String?
    @State private var pendingPass: QueueItem?
    @State private var pane = 0
    @State private var filed: [FiledApplication] = []
    @State private var stages: [String] = FiledApplication.defaultStages
    @State private var editingApp: FiledApplication?
    @State private var pendingDelete: FiledApplication?
    @State private var showAll = false
    @State private var tapeFocus: Int?
    @State private var searching = false
    @State private var pendingJobId: Int?
    @State private var linkDraft = ""
    @State private var importingLink = false
    @State private var momentum: Momentum?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var api: APIClient { APIClient(config: config) }

    private var upNext: QueueItem? { queue.first ?? matches.first }
    private var upNextIsReady: Bool {
        guard let upNext else { return false }
        return queue.contains(where: { $0.posting_id == upNext.posting_id })
    }
    private var readyRest: [QueueItem] {
        Array(queue.dropFirst())
    }
    private var matchRest: [QueueItem] {
        if upNextIsReady { return matches }
        return Array(matches.dropFirst())
    }
    private var tapeItems: [QueueItem] {
        Array((readyRest + matchRest).prefix(12))
    }

    private func isReady(_ item: QueueItem) -> Bool {
        queue.contains(where: { $0.posting_id == item.posting_id })
    }

    var body: some View {
        NavigationStack(path: $path) {
            Group {
                if loading && queue.isEmpty && matches.isEmpty && filed.isEmpty && error == nil {
                    PreparingView(
                        message: "Finding matches…",
                        notes: ["Scanning job boards", "Filtering out dead listings",
                                "Scoring against your profile", "Ranking what fits best"]
                    )
                } else if let error, queue.isEmpty && matches.isEmpty && filed.isEmpty {
                    EmptyStateView(
                        title: "Couldn't load matches",
                        description: error,
                        retryTitle: "Try again",
                        retry: { Task { await load(refresh: true); await pollWhileSearching() } },
                        secondaryTitle: "Send feedback",
                        secondary: { push.openDeepLink("settings:feedback", fromHorizon: true) }
                    )
                    .instrumentEnter()
                } else if searching && queue.isEmpty && matches.isEmpty && filed.isEmpty {
                    PreparingView(
                        message: "Finding matches…",
                        notes: ["Scanning job boards", "Filtering out dead listings",
                                "Scoring against your profile", "Ranking what fits best"]
                    )
                        .instrumentEnter()
                } else if queue.isEmpty && matches.isEmpty && filed.isEmpty {
                    emptyHome
                        .instrumentEnter()
                } else {
                    content
                        .instrumentEnter()
                }
            }
            .toolbar(.hidden, for: .navigationBar)
            .navigationDestination(for: QueueItem.self) { ApplyView(item: $0) }
            .ambientScreen()
            .appToast($toast, bottomPadding: Theme.toastClearance)
            .confirmationDialog(
                "Pass on \(pendingPass?.company ?? "this role")?",
                isPresented: Binding(
                    get: { pendingPass != nil },
                    set: { if !$0 { pendingPass = nil } }
                ),
                titleVisibility: .visible
            ) {
                Button("Pass", role: .destructive) {
                    if let item = pendingPass {
                        Task { await pass(item) }
                    }
                    pendingPass = nil
                }
                Button("Cancel", role: .cancel) { pendingPass = nil }
            } message: {
                Text("It won’t appear in matches again.")
            }
            .confirmationDialog(
                "Delete this application?",
                isPresented: Binding(
                    get: { pendingDelete != nil },
                    set: { if !$0 { pendingDelete = nil } }
                ),
                titleVisibility: .visible
            ) {
                Button("Delete", role: .destructive) {
                    if let app = pendingDelete {
                        Task { await deleteFiled(app) }
                    }
                    pendingDelete = nil
                }
                Button("Cancel", role: .cancel) { pendingDelete = nil }
            } message: {
                Text("Removes it from your tracker. The job stays applied, so it will not come back as a match.")
            }
            .sheet(item: $editingApp) { app in
                EditApplicationSheet(app: app) { company, role in
                    await saveEdit(app, company: company, role: role)
                }
            }
            .onChange(of: path.count) { _, count in
                chrome.dockHidden = count > 0
                if count == 0 {
                    Task {
                        await load()
                        consumeSittingCue()
                    }
                }
            }
            .onAppear { chrome.dockHidden = !path.isEmpty; consumeHorizonHop() }
            .onChange(of: push.hop) { _, _ in consumeHorizonHop() }
            .task {
                #if DEBUG
                if ProcessInfo.processInfo.arguments.contains("-JobPilotFiled") { pane = 1 }
                #endif
                let uid = config.user
                if queue.isEmpty && matches.isEmpty, let cached = QueueCache.load(user: uid) {
                    queue = cached.queue
                    matches = cached.matches
                    chrome.readyCount = cached.queue.count
                }
                await load(refresh: queue.isEmpty && matches.isEmpty)
                await pollWhileSearching()
            }
        }
    }

    private var content: some View {
        ScrollView(showsIndicators: false) {
            VStack(alignment: .leading, spacing: Theme.spaceM) {
                PageHeader(
                    eyebrow: "JobPilot",
                    title: greeting,
                    subtitle: subtitleLine
                ) {
                    InstrumentToggle(options: ["Matches", "Filed"], selection: $pane)
                        .onChange(of: pane) { _, new in
                            showAll = false
                            if new == 1 { Task { await load() } }
                        }
                }

                if pane == 0 {
                    pasteLinkBar
                    matchesPane
                        .transition(paneTransition(leading: true))
                } else {
                    filedPane
                        .transition(paneTransition(leading: false))
                }

                Color.clear.frame(height: Theme.dockClearance)
            }
            .padding(.top, 4)
            .animation(reduceMotion ? nil : Theme.springSoft, value: pane)
        }
        .refreshable {
            await load(refresh: true)
            await pollWhileSearching()
        }
    }

    private var emptyHome: some View {
        ScrollView(showsIndicators: false) {
            VStack(alignment: .leading, spacing: Theme.spaceM) {
                PageHeader(
                    eyebrow: "JobPilot",
                    title: greeting
                )
                if setup.status?.has_profile == false {
                    EmptyStateView(
                        title: "Finish setup",
                        description: "Add the roles you want. Matches are based on this.",
                        retryTitle: "Set up profile"
                    ) { setup.reopen() }
                    .padding(.horizontal, Theme.spaceL)
                } else {
                    EmptyStateView(
                        title: "No matches yet",
                        description: emptySearchCopy,
                        retryTitle: "Search now"
                    ) { Task { await load(refresh: true); await pollWhileSearching() } }
                    .padding(.horizontal, Theme.spaceL)
                    pasteLinkBar
                }
                Color.clear.frame(height: Theme.dockClearance)
            }
            .padding(.top, 4)
        }
        .refreshable {
            await load(refresh: true)
            await pollWhileSearching()
        }
    }

    private func paneTransition(leading: Bool) -> AnyTransition {
        if reduceMotion { return .opacity }
        let insertX: CGFloat = leading ? -12 : 12
        let removeX: CGFloat = leading ? 12 : -12
        return .asymmetric(
            insertion: .opacity.combined(with: .offset(x: insertX)),
            removal: .opacity.combined(with: .offset(x: removeX))
        )
    }

    @ViewBuilder
    private var matchesPane: some View {
        VStack(alignment: .leading, spacing: Theme.spaceM) {
            if let momentum, (upNext != nil || momentum.filed_today > 0) {
                SittingStrip(momentum: momentum)
                    .staggerAppear(0)
            }
            if let item = upNext {
                hero(item)
                    .id(item.posting_id)
                    .transition(reduceMotion
                        ? .opacity
                        : .asymmetric(
                            insertion: .opacity.combined(with: .scale(scale: 0.96)),
                            removal: .opacity
                        ))
                    .padding(.horizontal, Theme.spaceL)
                    .staggerAppear(1)
            } else if !loading {
                EmptyStateView(
                    title: "No open matches",
                    description: "Pull to refresh to search again, or check Filed for what you’ve already sent.",
                    compact: true
                )
                .padding(.horizontal, Theme.spaceL)
            }

            if !tapeItems.isEmpty {
                VStack(alignment: .leading, spacing: 10) {
                    HStack(alignment: .firstTextBaseline) {
                        Text("Apply these today")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(Theme.soft)
                            .textCase(.uppercase)
                            .tracking(0.8)
                        Spacer(minLength: 8)
                        Button(showAll ? "Hide" : "See all") {
                            withAnimation(reduceMotion ? nil : Theme.springSoft) { showAll.toggle() }
                        }
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(Theme.accent)
                    }
                    .padding(.horizontal, Theme.spaceL)
                    matchTape
                }
            }

            if showAll, !(readyRest + matchRest).isEmpty {
                seeAllList
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .animation(reduceMotion ? nil : Theme.springSoft, value: upNext?.posting_id)
        .animation(reduceMotion ? nil : Theme.springSoft, value: showAll)
    }

    private var matchTape: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 10) {
                ForEach(Array(tapeItems.enumerated()), id: \.element.id) { i, item in
                    MatchTriageRow(
                        onLater: { Task { await later(item) } },
                        onPass: { pendingPass = item },
                        compact: true
                    ) {
                        Button {
                            Task { await makeNext(item) }
                        } label: {
                            MatchTapeChip(
                                item: item,
                                focused: tapeFocus == item.posting_id || (tapeFocus == nil && i == 0)
                            )
                        }
                        .buttonStyle(.plain)
                    }
                    .id(item.posting_id)
                    .modifier(TapePhaseEffect(freeze: reduceMotion))
                    .contextMenu { rowMenu(item, ready: isReady(item)) }
                    .staggerAppear(i + 1)
                }
            }
            .scrollTargetLayout()
        }
        .scrollTargetBehavior(.viewAligned)
        .scrollPosition(id: $tapeFocus)
        .safeAreaPadding(.horizontal, Theme.spaceL)
        .onChange(of: tapeFocus) { old, new in
            guard !reduceMotion, old != nil, new != nil, old != new else { return }
            Theme.selection()
        }
    }

    private var seeAllList: some View {
        GroupedSurface {
            ForEach(Array((readyRest + matchRest).enumerated()), id: \.element.id) { i, item in
                if i > 0 { rowDivider }
                MatchTriageRow(
                    onLater: { Task { await later(item) } },
                    onPass: { pendingPass = item }
                ) {
                    Button {
                        Task { await makeNext(item) }
                    } label: {
                        quietMatchRow(item)
                    }
                    .buttonStyle(.plain)
                }
                .contextMenu { rowMenu(item, ready: isReady(item)) }
                .staggerAppear(i)
            }
        }
        .padding(.horizontal, Theme.spaceL)
    }

    @ViewBuilder
    private var filedPane: some View {
        VStack(alignment: .leading, spacing: Theme.spaceL) {
            if filed.isEmpty {
                EmptyStateView(
                    title: "Nothing filed yet",
                    description: "Mark an application Filed after you submit. It will show up here.",
                    compact: true
                )
                .padding(.horizontal, Theme.spaceL)
            } else {
                GroupedSurface {
                    ForEach(Array(filed.enumerated()), id: \.element.id) { i, app in
                        if i > 0 { rowDivider }
                        filedRow(app)
                            .padding(.horizontal, 16)
                            .padding(.vertical, 12)
                            .contentShape(Rectangle())
                            .contextMenu { filedMenu(app) }
                            .staggerAppear(min(i, 8))
                    }
                }
                .padding(.horizontal, Theme.spaceL)
            }
        }
    }

    private var rowDivider: some View {
        Rectangle()
            .fill(Theme.cloud.opacity(0.45))
            .frame(height: 1)
            .padding(.leading, 16)
    }

    private func hero(_ item: QueueItem) -> some View {
        UpNextCard(
            item: item,
            kicker: upNextIsReady ? "Ready" : "Apply today",
            actionTitle: upNextIsReady ? "Open form" : "Preflight",
            busy: staging.contains(item.posting_id),
            showTriage: true,
            onLater: { Task { await later(item) } },
            onPass: { pendingPass = item }
        ) {
            if upNextIsReady {
                path.append(item)
            } else {
                Task { await stage(item) }
            }
        }
    }

    private func quietMatchRow(_ item: QueueItem) -> some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(item.company ?? "Company")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(Theme.ink)
                    .lineLimit(1)
                HStack(spacing: 6) {
                    Text(item.title ?? "Role")
                        .font(.subheadline)
                        .foregroundStyle(Theme.soft)
                        .lineLimit(1)
                    if let sc = item.score {
                        Text("·")
                            .foregroundStyle(Theme.soft)
                        ScoreMark(score: sc, size: 13)
                    }
                }
                Text(item.applyKindLabel)
                    .font(.caption)
                    .foregroundStyle(Theme.soft.opacity(0.85))
                    .lineLimit(1)
            }
            Spacer(minLength: 8)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .contentShape(Rectangle())
    }

    @ViewBuilder
    private func rowMenu(_ item: QueueItem, ready: Bool) -> some View {
        if ready {
            Button { path.append(item) } label: {
                Label("Open form", systemImage: "arrow.up.right")
            }
            Button { Task { await promote(item) } } label: {
                Label("Apply next", systemImage: "arrow.up")
            }
        } else {
            Button { Task { await stage(item) } } label: {
                Label("Preflight", systemImage: "square.and.pencil")
            }
            Button {
                withAnimation(Theme.springSoft) {
                    matches.removeAll { $0.posting_id == item.posting_id }
                    matches.insert(item, at: 0)
                }
                persistMatches()
            } label: {
                Label("Make next", systemImage: "arrow.up")
            }
        }
        Button { Task { await later(item) } } label: {
            Label("Later", systemImage: "clock")
        }
        Button(role: .destructive) { pendingPass = item } label: {
            Label("Pass", systemImage: "xmark")
        }
    }

    private func filedRow(_ app: FiledApplication) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                Text(app.company ?? "Company")
                    .font(.body.weight(.medium))
                    .foregroundStyle(Theme.ink)
                Spacer(minLength: 8)
                Text(app.status ?? "Applied")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Theme.horizon)
            }
            Text(app.role ?? "Role")
                .font(.subheadline)
                .foregroundStyle(Theme.soft)
                .lineLimit(1)
            if let when = filedDate(app.applied_at) {
                Text(when)
                    .font(.caption)
                    .foregroundStyle(Theme.soft)
            }
        }
        .padding(.vertical, 4)
    }

    /// Long-press a filed row. There is no List here (the rows live in a
    /// GroupedSurface), so swipe actions are not available -- the matches rows
    /// use a context menu for the same reason.
    @ViewBuilder
    private func filedMenu(_ app: FiledApplication) -> some View {
        Menu {
            ForEach(stages, id: \.self) { stage in
                Button {
                    Task { await setStage(app, to: stage) }
                } label: {
                    if app.status == stage {
                        Label(stage, systemImage: "checkmark")
                    } else {
                        Text(stage)
                    }
                }
            }
        } label: {
            Label("Change stage", systemImage: "arrow.triangle.branch")
        }
        Button { editingApp = app } label: {
            Label("Edit company or role", systemImage: "pencil")
        }
        Button(role: .destructive) { pendingDelete = app } label: {
            Label("Delete", systemImage: "trash")
        }
    }

    private func setStage(_ app: FiledApplication, to stage: String) async {
        guard app.status != stage else { return }
        do {
            let updated = try await api.setApplicationStatus(id: app.id, status: stage)
            await MainActor.run {
                applyEdit(updated ?? app, fallbackStatus: stage)
                Theme.notify(.success)
                flashToast("Moved to \(stage)")
            }
        } catch {
            await MainActor.run { flashToast(APIClient.userMessage(for: error)) }
        }
    }

    private func saveEdit(_ app: FiledApplication, company: String, role: String) async {
        let newCompany = company == (app.company ?? "") ? nil : company
        let newRole = role == (app.role ?? "") ? nil : role
        guard newCompany != nil || newRole != nil else { return }
        do {
            let updated = try await api.editApplication(
                id: app.id, company: newCompany, role: newRole
            )
            await MainActor.run {
                if let updated { applyEdit(updated) }
                Theme.notify(.success)
                flashToast("Saved")
            }
        } catch {
            await MainActor.run { flashToast(APIClient.userMessage(for: error)) }
        }
    }

    private func deleteFiled(_ app: FiledApplication) async {
        // Optimistic: the row goes now, and comes back if the server refuses.
        await MainActor.run {
            withAnimation(Theme.springSoft) { filed.removeAll { $0.id == app.id } }
        }
        do {
            try await api.deleteApplication(id: app.id)
            await MainActor.run {
                Theme.notify(.success)
                flashToast("Deleted")
            }
        } catch {
            await MainActor.run {
                flashToast(APIClient.userMessage(for: error))
            }
            await load()
        }
    }

    /// Replace one row in place, so editing does not reshuffle the list.
    private func applyEdit(_ updated: FiledApplication, fallbackStatus: String? = nil) {
        guard let i = filed.firstIndex(where: { $0.id == updated.id }) else { return }
        withAnimation(Theme.springSoft) {
            filed[i] = FiledApplication(
                id: updated.id,
                company: updated.company ?? filed[i].company,
                role: updated.role ?? filed[i].role,
                status: updated.status ?? fallbackStatus ?? filed[i].status,
                applied_at: updated.applied_at ?? filed[i].applied_at,
                next_follow_up_at: updated.next_follow_up_at
            )
        }
    }

    private func filedDate(_ iso: String?) -> String? {
        guard let iso, !iso.isEmpty else { return nil }
        let withFrac = ISO8601DateFormatter()
        withFrac.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let date = withFrac.date(from: iso) ?? ISO8601DateFormatter().date(from: iso)
        if let date {
            return date.formatted(.dateTime.month(.abbreviated).day().year())
        }
        return iso.count >= 10 ? String(iso.prefix(10)) : nil
    }

    private func persistMatches() {
        let ids = matches.map(\.posting_id)
        Task {
            try? await api.reorder(matches: ids)
            QueueCache.save(queue: queue, matches: matches, user: config.user)
        }
    }

    private var greetingName: String {
        Voice.firstName(identity: setup.status?.identity, displayName: config.displayName)
    }

    private var greeting: String {
        Voice.timeGreeting(name: greetingName)
    }

    private var emptySearchCopy: String {
        let lead = greetingName.isEmpty
            ? "No matches yet."
            : "No matches yet, \(greetingName)."
        return "\(lead) Pull to refresh, or paste a LinkedIn / Amazon / Workday link below."
    }

    private var pasteLinkBar: some View {
        HStack(spacing: 8) {
            TextField("Paste a job link", text: $linkDraft)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)
                .font(.subheadline)
                .foregroundStyle(Theme.ink)
            Button {
                Task { await importLink() }
            } label: {
                Text(importingLink ? "Adding…" : "Add")
                    .font(.subheadline.weight(.semibold))
            }
            .disabled(importingLink || linkDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            .foregroundStyle(Theme.accent)
        }
        .padding(12)
        .background(Theme.cardFill, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .padding(.horizontal, Theme.spaceL)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Paste a job link from LinkedIn, Amazon, or a company site")
    }

    private func importLink() async {
        let url = linkDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard url.lowercased().hasPrefix("http") else {
            flashToast("Paste a full https:// job link.")
            return
        }
        importingLink = true
        defer { importingLink = false }
        do {
            let result = try await api.importJobURL(url)
            linkDraft = ""
            Theme.impact(.soft)
            let title = result.title?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "Job"
            flashToast("Saved \(title). It’s in Apply.")
            await load()
        } catch {
            Theme.notify(.error)
            let msg = APIClient.userMessage(for: error)
            if !msg.isEmpty { flashToast(msg) }
        }
    }

    private var subtitleLine: String? {
        if pane == 1 {
            let n = filed.count
            return n == 0 ? "Filed after you submit." : (n == 1 ? "1 application" : "\(n) applications")
        }
        if let momentum, momentum.filed_today > 0 {
            return momentum.sitting_line
        }
        if !queue.isEmpty {
            let n = queue.count
            return n == 1 ? "1 ready to apply" : "\(n) ready to apply"
        }
        if !matches.isEmpty {
            let today = matches.filter { $0.apply_today == true }.count
            let n = today > 0 ? today : min(5, matches.count)
            return n == 1 ? "1 to apply today" : "\(n) to apply today"
        }
        return nil
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
            let msg = APIClient.userMessage(for: error)
            if !msg.isEmpty { flashToast(msg) }
        }
    }

    private func later(_ item: QueueItem) async {
        do {
            try await api.snooze(postingId: item.posting_id)
            Theme.impact(.soft)
            withAnimation(Theme.springSoft) {
                queue.removeAll { $0.posting_id == item.posting_id }
                matches.removeAll { $0.posting_id == item.posting_id }
                chrome.readyCount = queue.count
            }
            flashToast("\(item.company ?? "This role") — back in a week")
            await load()
        } catch {
            Theme.notify(.error)
            let msg = APIClient.userMessage(for: error)
            if !msg.isEmpty { flashToast(msg) }
        }
    }

    private func pass(_ item: QueueItem) async {
        do {
            let snap = try await api.passPosting(postingId: item.posting_id)
            Theme.impact(.soft)
            withAnimation(Theme.springSoft) {
                queue.removeAll { $0.posting_id == item.posting_id }
                matches.removeAll { $0.posting_id == item.posting_id }
                chrome.readyCount = queue.count
                if let snap { momentum = snap }
            }
            if let line = snap?.ranker_line {
                flashToast(line)
            } else {
                flashToast("Passed \(item.company ?? "this role")")
            }
            await load()
        } catch {
            Theme.notify(.error)
            let msg = APIClient.userMessage(for: error)
            if !msg.isEmpty { flashToast(msg) }
        }
    }

    private func promote(_ item: QueueItem) async {
        do {
            try await api.promote(postingId: item.posting_id)
            Theme.impact(.soft)
            withAnimation(Theme.springSoft) {
                queue.removeAll { $0.posting_id == item.posting_id }
                queue.insert(item, at: 0)
            }
            flashToast("\(item.company ?? "This role") is next")
            await load()
        } catch {
            Theme.notify(.error)
            let msg = APIClient.userMessage(for: error)
            if !msg.isEmpty { flashToast(msg) }
        }
    }

    private func makeNext(_ item: QueueItem) async {
        if isReady(item) {
            await promote(item)
            return
        }
        Theme.impact(.soft)
        withAnimation(Theme.springSoft) {
            matches.removeAll { $0.posting_id == item.posting_id }
            matches.insert(item, at: 0)
        }
        persistMatches()
        tapeFocus = nil
        flashToast("\(item.company ?? "This role") is next")
    }

    private func consumeHorizonHop() {
        guard let hop = push.hop else { return }
        switch hop {
        case .applyFiled:
            push.hop = nil
            pane = 1
        case .applyJob(let id):
            push.hop = nil
            pane = 0
            openJob(id)
        default:
            break
        }
    }

    private func openJob(_ id: Int) {
        Task { @MainActor in
            if !reduceMotion {
                try? await Task.sleep(nanoseconds: 280_000_000)
            }
            if let item = (queue + matches).first(where: { $0.posting_id == id }) {
                pendingJobId = nil
                path.append(item)
                return
            }
            pendingJobId = id
            await load()
        }
    }

    private func consumeSittingCue() {
        guard let toast = SittingCue.toast, !toast.isEmpty else { return }
        SittingCue.toast = nil
        flashToast(toast)
    }

    private func tryOpenPendingJob() {
        guard let id = pendingJobId else { return }
        if let item = (queue + matches).first(where: { $0.posting_id == id }) {
            pendingJobId = nil
            path.append(item)
        }
    }

    private func load(refresh: Bool = false) async {
        loading = true
        error = nil
        defer { loading = false }
        do {
            let data = try await api.fetchData(refresh: refresh)
            let filedResult = try? await api.fetchApplications()
            let apps = filedResult?.apps ?? []
            withAnimation(Theme.springSoft) {
                queue = data.queue
                matches = data.matches
                filed = apps
                momentum = data.momentum
                if let served = filedResult?.statuses, !served.isEmpty { stages = served }
                searching = data.searching
            }
            chrome.readyCount = data.queue.count
            QueueCache.save(queue: data.queue, matches: data.matches, user: config.user)
            tryOpenPendingJob()
        } catch {
            if APIClient.isCancellation(error) { return }
            let msg = APIClient.userMessage(for: error)
            if queue.isEmpty && matches.isEmpty {
                if !msg.isEmpty { self.error = msg }
            } else if !msg.isEmpty {
                flashToast(msg)
            }
        }
    }

    private func pollWhileSearching() async {
        var n = 0
        while searching && queue.isEmpty && matches.isEmpty && n < 40 {
            try? await Task.sleep(for: .seconds(2))
            if Task.isCancelled { return }
            await load(refresh: false)
            n += 1
        }
    }

    private func flashToast(_ text: String) {
        withAnimation(Theme.springSoft) { toast = text }
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.4) {
            withAnimation(Theme.quick) { toast = nil }
        }
    }
}

private struct TapePhaseEffect: ViewModifier {
    let freeze: Bool

    func body(content: Content) -> some View {
        content.scrollTransition { view, phase in
            view
                .scaleEffect(freeze || phase.isIdentity ? 1 : 0.92)
                .opacity(freeze || phase.isIdentity ? 1 : 0.7)
        }
    }
}

/// Correct the company or role on an application already filed.
///
/// Only what changed is sent: the endpoint leaves omitted fields alone, so a
/// blank box is never mistaken for "clear this".
private struct EditApplicationSheet: View {
    let app: FiledApplication
    let onSave: (String, String) async -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var company: String
    @State private var role: String
    @State private var saving = false

    init(app: FiledApplication, onSave: @escaping (String, String) async -> Void) {
        self.app = app
        self.onSave = onSave
        _company = State(initialValue: app.company ?? "")
        _role = State(initialValue: app.role ?? "")
    }

    private var trimmedCompany: String {
        company.trimmingCharacters(in: .whitespacesAndNewlines)
    }
    private var trimmedRole: String {
        role.trimmingCharacters(in: .whitespacesAndNewlines)
    }
    private var canSave: Bool {
        !saving && !trimmedCompany.isEmpty && !trimmedRole.isEmpty
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Company") {
                    TextField("Company", text: $company)
                        .textInputAutocapitalization(.words)
                        .autocorrectionDisabled()
                }
                Section("Role") {
                    TextField("Role", text: $role)
                        .textInputAutocapitalization(.words)
                }
            }
            .navigationTitle("Edit application")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        saving = true
                        Task {
                            await onSave(trimmedCompany, trimmedRole)
                            dismiss()
                        }
                    }
                    .disabled(!canSave)
                }
            }
        }
    }
}
