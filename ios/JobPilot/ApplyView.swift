import SwiftUI
import UIKit

/// Loads the application package for a posting, then hands off to the in-app browser.
struct ApplyView: View {
    let item: QueueItem
    @EnvironmentObject var config: Config
    @State private var package: Package?
    @State private var rules: RulesPayload?
    @State private var error: String?

    @EnvironmentObject var chrome: AppChrome

    var body: some View {
        Group {
            if let package {
                ApplyBrowser(item: item, package: package, rules: rules)
            } else if let error {
                EmptyStateView(
                    title: "Couldn't prepare this application",
                    description: error,
                    retryTitle: "Try again",
                    retry: { Task { await load() } },
                    secondaryTitle: "Send feedback",
                    secondary: { PushManager.shared.openDeepLink("settings:feedback", fromHorizon: true) }
                )
            } else {
                PreparingView(
                    message: "Preparing application…",
                    notes: ["Reading the posting", "Drafting your answers",
                            "Tailoring your résumé to one page"]
                )
            }
        }
        // Hide the floating dock for the whole apply flow (including loading),
        // not only after the package arrives — otherwise dock + Autofill stack.
        .onAppear { chrome.dockHidden = true }
        .task { await load() }
    }

    private func load() async {
        error = nil
        let api = APIClient(config: config)
        rules = await RulesCache.refresh(using: api)
        do { package = try await api.fetchPackage(postingId: item.posting_id) }
        catch {
            if APIClient.isCancellation(error) { return }
            let msg = APIClient.userMessage(for: error)
            if !msg.isEmpty { self.error = msg }
        }
    }
}

/// Soft chrome over the real form: Autofill, documents, mark applied.
private struct ApplyBrowser: View {
    let item: QueueItem
    let package: Package
    let rules: RulesPayload?
    @EnvironmentObject var config: Config
    @EnvironmentObject var chrome: AppChrome
    @Environment(\.dismiss) private var dismiss
    @StateObject private var model: WebViewModel
    @State private var toast: String?
    @State private var marking = false
    @State private var confirmApplied = false
    @State private var confirmPass = false
    @State private var applyDoc: ApplyDoc?
    @State private var fetchingDoc = false
    @State private var resumeReadyFlash = false
    @State private var prefetchedResume: URL?
    @State private var confirmLeave = false
    @State private var confirmUnstage = false

    private var remaining: RemainingWork { RemainingWork(skips: model.lastSkips) }

    private var formDirty: Bool {
        if model.driveState.isRunning { return true }
        if case .ready = model.driveState { return true }
        if let fill = model.lastFill, fill.filled > 0 { return true }
        return remaining.isEmpty == false
    }

    init(item: QueueItem, package: Package, rules: RulesPayload?) {
        self.item = item
        self.package = package
        self.rules = rules
        _model = StateObject(wrappedValue: WebViewModel(
            identity: package.identity ?? [:], answers: package.questions ?? [],
            rules: rules))
    }

    var body: some View {
        ZStack(alignment: .top) {
            WebViewContainer(model: model)

            VStack(spacing: 0) {
                if model.loading {
                    ProgressView()
                        .progressViewStyle(.linear)
                        .tint(Theme.accent)
                        .padding(.horizontal, Theme.spaceL)
                        .padding(.top, 2)
                        .transition(.opacity)
                }

                driveBanner
                    .transition(.move(edge: .top).combined(with: .opacity))
                remainingBanner
                    .transition(.move(edge: .top).combined(with: .opacity))
                Spacer(minLength: 0)
            }
            .animation(Theme.springSoft, value: model.driveState)
            .animation(Theme.quick, value: model.loading)
            .animation(Theme.springSoft, value: remaining.isEmpty)
        }
        .safeAreaInset(edge: .bottom, spacing: 0) { controls }
        .appToast($toast, bottomPadding: 96)
        .navigationTitle(item.company ?? "Apply")
        .navigationBarTitleDisplayMode(.inline)
        .navigationBarBackButtonHidden(formDirty)
        .background(PopGuard(locked: formDirty))
        .toolbarBackground(Theme.fog, for: .navigationBar)
        .toolbarBackground(.visible, for: .navigationBar)
        .toolbarColorScheme(.light, for: .navigationBar)
        .toolbar {
            // Nav "Back" pops Apply. Site history lives in the bottom bar so we
            // don't show two competing chevrons under the title. When the form
            // has been filled, a custom back asks first — popping dumps the
            // WebView and they start the Greenhouse over.
            if formDirty {
                ToolbarItem(placement: .topBarLeading) {
                    Button { confirmLeave = true } label: {
                        Image(systemName: "chevron.backward")
                            .font(.body.weight(.semibold))
                    }
                    .accessibilityLabel("Back")
                }
            }
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    Button {
                        if formDirty { confirmUnstage = true }
                        else { Task { await skipAndClose() } }
                    } label: {
                        Label("Back to matches", systemImage: "clock")
                    }
                    Button(role: .destructive) {
                        confirmPass = true
                    } label: {
                        Label("Pass on this job", systemImage: "xmark")
                    }
                } label: {
                    Image(systemName: "ellipsis.circle")
                }
            }
        }
        .onAppear {
            chrome.dockHidden = true
            model.load(package.url)
            prefetchResume()
        }
        .onChange(of: model.driveState) { _, state in
            if case .ready = state { flashToast("Ready — you submit on the site") }
            if case .failed(let r) = state { flashToast(r) }
        }
        .onChange(of: model.fillSeq) { _, _ in
            showFillToast()
            reportFillSkips()
        }
        .confirmationDialog("Mark this as filed?", isPresented: $confirmApplied,
                            titleVisibility: .visible) {
            Button("Filed") {
                marking = true
                Task {
                    let snap = try? await APIClient(config: config).markApplied(postingId: item.posting_id)
                    if let toast = snap?.toast { SittingCue.toast = toast }
                    Theme.notify(.success)
                    dismiss()
                }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Adds this to your tracker. Submit on the company’s site first.")
        }
        .confirmationDialog("Pass on this job?", isPresented: $confirmPass,
                            titleVisibility: .visible) {
            Button("Pass", role: .destructive) {
                Task { await passAndClose() }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Removes this listing. It won’t appear again.")
        }
        .confirmationDialog("Leave this form?", isPresented: $confirmLeave,
                            titleVisibility: .visible) {
            Button("Leave", role: .destructive) { dismiss() }
            Button("Stay", role: .cancel) {}
        } message: {
            Text("What you filled on this page isn’t saved. Coming back starts the form over.")
        }
        .confirmationDialog("Back to matches?", isPresented: $confirmUnstage,
                            titleVisibility: .visible) {
            Button("Leave", role: .destructive) {
                Task { await skipAndClose() }
            }
            Button("Stay", role: .cancel) {}
        } message: {
            Text("This listing goes back to matches. The form itself isn’t saved.")
        }
        .sheet(item: $applyDoc) { doc in ShareSheet(items: [doc.url]) }
    }

    @ViewBuilder
    private var driveBanner: some View {
        switch model.driveState {
        case .needsHuman(let reason):
            blockerBanner(
                title: reason,
                detail: model.statusLine.isEmpty
                    ? "Handle this here, then tap Resume."
                    : model.statusLine,
                icon: model.lastProbeKind == "captcha"
                    ? "checkmark.shield.fill"
                    : "lock.fill"
            )
        case .watchingClear:
            handoffBanner(
                title: "Continue",
                detail: "Tap Resume to fill, or finish the form yourself.",
                tint: Theme.accent
            )
        case .paused:
            handoffBanner(
                title: "Paused",
                detail: "Fill anything you like. Resume when you want JobPilot to continue.",
                tint: Theme.soft
            )
        case .filling, .advancing, .probing:
            HStack(spacing: 10) {
                AutopilotOrb(active: true)
                Text(model.statusLine.isEmpty ? "Filling…" : model.statusLine)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(Theme.ink)
                    .lineLimit(2)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.white.opacity(0.96))
            .overlay(alignment: .bottom) {
                Rectangle().fill(Theme.cloud.opacity(0.5)).frame(height: 1)
            }
        case .ready:
            handoffBanner(
                title: "Ready to submit",
                detail: "Review the form and submit yourself — JobPilot never sends.",
                tint: Theme.accent
            )
        default:
            EmptyView()
        }
    }

    @ViewBuilder
    private var remainingBanner: some View {
        if model.driveState.isRunning || remaining.isEmpty {
            EmptyView()
        } else {
            VStack(alignment: .leading, spacing: 8) {
                Text("Still you")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Theme.ink)
                ForEach(remaining.lines, id: \.self) { line in
                    Text(line)
                        .font(.caption)
                        .foregroundStyle(Theme.ink.opacity(0.72))
                }
                if remaining.wantsResume || remaining.wantsCover {
                    HStack(spacing: 8) {
                        if remaining.wantsResume {
                            Button(action: fetchResume) {
                                Text("Attach résumé")
                                    .font(.caption.weight(.semibold))
                            }
                            .disabled(fetchingDoc)
                        }
                        if remaining.wantsCover {
                            Button(action: fetchCover) {
                                Text("Attach cover letter")
                                    .font(.caption.weight(.semibold))
                            }
                            .disabled(fetchingDoc)
                        }
                    }
                    .foregroundStyle(Theme.accent)
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.white.opacity(0.96))
            .overlay(alignment: .bottom) {
                Rectangle().fill(Theme.cloud.opacity(0.5)).frame(height: 1)
            }
            .accessibilityElement(children: .combine)
        }
    }

    private func blockerBanner(title: String, detail: String, icon: String) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: icon)
                .font(.body.weight(.semibold))
                .foregroundStyle(Theme.accent)
                .frame(width: 28, height: 28)
                .background(Theme.accent.opacity(0.12), in: Circle())
            VStack(alignment: .leading, spacing: 4) {
                Text(title)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Theme.ink)
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(Theme.ink.opacity(0.72))
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.white.opacity(0.96))
        .overlay(alignment: .leading) {
            Rectangle()
                .fill(Theme.accent)
                .frame(width: 3)
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
    }

    private func handoffBanner(title: String, detail: String, tint: Color) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.caption.weight(.semibold))
                .foregroundStyle(Theme.ink)
            Text(detail)
                .font(.caption2)
                .foregroundStyle(Theme.note)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.white.opacity(0.96))
        .overlay(alignment: .bottom) {
            Rectangle().fill(Theme.cloud.opacity(0.5)).frame(height: 1)
        }
        .overlay(alignment: .leading) {
            if tint == Theme.accent {
                Rectangle()
                    .fill(Theme.accent)
                    .frame(width: 3)
            }
        }
    }

    private var controls: some View {
        VStack(spacing: 8) {
            if !model.statusLine.isEmpty, model.driveState == .idle {
                Text(model.statusLine)
                    .font(.caption2)
                    .foregroundStyle(Theme.soft)
            }

            HStack(spacing: 10) {
                if model.canGoBack {
                    Button { model.goBack() } label: {
                        Image(systemName: "chevron.backward")
                            .font(.body.weight(.semibold))
                            .foregroundStyle(Theme.ink.opacity(0.75))
                            .frame(width: 48, height: 44)
                            .background(Color.white.opacity(0.7), in: Circle())
                    }
                    .accessibilityLabel("Previous page")
                }

                primaryDriveButton

                Menu {
                    Button { fetchResume() } label: {
                        Label("Resume", systemImage: "doc.text")
                    }
                    Button { fetchCover() } label: {
                        Label("Cover letter", systemImage: "envelope")
                    }
                } label: {
                    Image(systemName: resumeReadyFlash || prefetchedResume != nil
                          ? "doc.text.fill" : "doc.text")
                        .font(.body.weight(.medium))
                        .foregroundStyle(Theme.ink.opacity(0.75))
                        .frame(width: 48, height: 44)
                        .background(Color.white.opacity(0.7), in: Circle())
                }
                .disabled(fetchingDoc)
                .overlay {
                    if fetchingDoc { ProgressView().controlSize(.small) }
                }
                .accessibilityLabel("Documents")

                Button { confirmApplied = true } label: {
                    Image(systemName: "checkmark")
                        .font(.body.weight(.semibold))
                        .foregroundStyle(Theme.accent)
                        .frame(width: 48, height: 44)
                        .background(Color.white.opacity(0.7), in: Circle())
                }
                .disabled(marking)
                .accessibilityLabel("Filed")
            }
        }
        .padding(.horizontal, 14)
        .padding(.top, 10)
        .padding(.bottom, 10)
        .paperBar()
    }

    @ViewBuilder
    private var primaryDriveButton: some View {
        switch model.driveState {
        case .filling, .advancing, .probing:
            Button { model.pauseAutopilot() } label: {
                HStack(spacing: 8) {
                    PropellerIcon(speed: .fast, size: 14)
                    Text("Pause")
                        .font(.body.weight(.semibold))
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 12)
                .foregroundStyle(Theme.ink)
                .background(Color.white.opacity(0.85), in: Capsule())
            }
            .buttonStyle(PressableButtonStyle())
        case .needsHuman, .watchingClear, .paused, .ready:
            Button { model.resumeAutopilot() } label: {
                HStack(spacing: 8) {
                    PropellerIcon(speed: .still, size: 14)
                    Text("Resume")
                        .font(.body.weight(.semibold))
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 12)
                .foregroundStyle(.white)
                .background(Theme.accent, in: Capsule())
            }
            .buttonStyle(PressableButtonStyle())
        case .failed:
            Button { model.startAutopilot() } label: {
                HStack(spacing: 8) {
                    PropellerIcon(speed: .still, size: 14)
                    Text("Try Fill")
                        .font(.body.weight(.semibold))
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 12)
                .foregroundStyle(.white)
                .background(Theme.accent, in: Capsule())
            }
            .buttonStyle(PressableButtonStyle())
        case .idle:
            Button { model.autofill() } label: {
                HStack(spacing: 8) {
                    PropellerIcon(speed: model.oneShotFilling ? .fast : .still, size: 16)
                    Text("Fill")
                        .font(.body.weight(.semibold))
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 12)
                .foregroundStyle(.white)
                .background(Theme.accent, in: Capsule())
            }
            .buttonStyle(PressableButtonStyle())
            .disabled(model.oneShotFilling)
            .simultaneousGesture(LongPressGesture(minimumDuration: 0.6).onEnded { _ in
                model.startAutopilot()
            })
        }
    }

    private func skipAndClose() async {
        try? await APIClient(config: config).skipQueueItem(postingId: item.posting_id)
        Theme.impact(.soft)
        dismiss()
    }

    private func passAndClose() async {
        let snap = try? await APIClient(config: config).passPosting(postingId: item.posting_id)
        if let line = snap?.ranker_line { SittingCue.toast = line }
        Theme.impact(.soft)
        dismiss()
    }

    private func showFillToast() {
        guard let f = model.lastFill else { return }
        // Drive loop posts its own status; avoid noisy toasts while autopilot runs.
        if model.driveState.isRunning { return }
        if f.filled == 0 && f.essays == 0 {
            flashToast("No fields matched. The form may still be loading, or name and email are missing in You.")
            return
        }
        let remain = RemainingWork(skips: model.lastSkips)
        let more: String
        if remain.isEmpty {
            more = f.essays > 0 ? " · \(f.essays) need you" : ""
        } else {
            more = " · \(remain.count) still you"
        }
        let stale = f.rules.hasPrefix("bundled") ? " · offline rules" : ""
        flashToast("Filled \(f.filled)\(more)\(stale)")
        if f.filled > 0 { Theme.notify(.success) }
    }

    private func reportFillSkips() {
        let skips = model.lastSkips
        guard !skips.isEmpty else { return }
        Task {
            try? await APIClient(config: config).reportFillSkips(
                postingId: item.posting_id,
                url: model.currentURL,
                skips: skips)
        }
    }

    private func prefetchResume() {
        guard applyDoc == nil, !fetchingDoc else { return }
        Task {
            if let url = try? await APIClient(config: config)
                .downloadResume(postingId: item.posting_id) {
                prefetchedResume = url
                withAnimation(Theme.springSoft) { resumeReadyFlash = true }
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.4) {
                    withAnimation(Theme.quick) { resumeReadyFlash = false }
                }
            }
        }
    }

    private func fetchResume() {
        if let ready = prefetchedResume {
            applyDoc = ApplyDoc(url: ready)
            return
        }
        fetchingDoc = true
        Task {
            defer { fetchingDoc = false }
            do {
                applyDoc = ApplyDoc(url: try await APIClient(config: config)
                    .downloadResume(postingId: item.posting_id))
            }
            catch APIClient.APIError.http(404) { flashToast("No tailored resume yet") }
            catch {
                flashToast(APIClient.userMessage(for: error))
            }
        }
    }

    private func fetchCover() {
        fetchingDoc = true
        Task {
            defer { fetchingDoc = false }
            do {
                applyDoc = ApplyDoc(url: try await APIClient(config: config)
                    .downloadCover(postingId: item.posting_id))
            }
            catch APIClient.APIError.http(404) { flashToast("Couldn't build a cover letter") }
            catch {
                flashToast(APIClient.userMessage(for: error))
            }
        }
    }

    private func flashToast(_ text: String) {
        withAnimation(Theme.springSoft) { toast = text }
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.8) {
            withAnimation(Theme.quick) { toast = nil }
        }
    }
}

struct ShareSheet: UIViewControllerRepresentable {
    let items: [Any]
    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: items, applicationActivities: nil)
    }
    func updateUIViewController(_ vc: UIActivityViewController, context: Context) {}
}

struct ApplyDoc: Identifiable { let id = UUID(); let url: URL }

/// Groups Fill skips into the remaining-work banner: file, date, then a few others.
private struct RemainingWork {
    let files: [String]
    let dates: [String]
    let others: [String]

    init(skips: [[String: Any]]) {
        var files: [String] = []
        var dates: [String] = []
        var others: [String] = []
        var seen = Set<String>()
        for raw in skips {
            let reason = ((raw["reason"] as? String) ?? "").lowercased()
            let label = ((raw["label"] as? String) ?? "")
                .replacingOccurrences(of: "*", with: "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            guard !label.isEmpty else { continue }
            let key = reason + "\n" + label.lowercased()
            if seen.contains(key) { continue }
            seen.insert(key)
            switch reason {
            case "file": files.append(label)
            case "date": dates.append(label)
            default:
                if others.count < 3 { others.append(label) }
            }
        }
        self.files = files
        self.dates = dates
        self.others = others
    }

    var isEmpty: Bool { files.isEmpty && dates.isEmpty && others.isEmpty }
    var count: Int { files.count + dates.count + others.count }

    var wantsResume: Bool {
        files.contains { !$0.lowercased().contains("cover") }
    }

    var wantsCover: Bool {
        files.contains { $0.lowercased().contains("cover") }
    }

    var lines: [String] {
        var out: [String] = []
        for label in files {
            let t = label.lowercased()
            if t.contains("cover") { out.append("Attach cover letter — \(label)") }
            else { out.append("Attach résumé — \(label)") }
        }
        for label in dates { out.append("Pick a date — \(label)") }
        out.append(contentsOf: others)
        return out
    }
}

/// Blocks the interactive pop gesture while a filled form would be destroyed.
private struct PopGuard: UIViewControllerRepresentable {
    var locked: Bool

    func makeUIViewController(context: Context) -> UIViewController {
        let vc = UIViewController()
        vc.view.isUserInteractionEnabled = false
        vc.view.backgroundColor = .clear
        return vc
    }

    func updateUIViewController(_ vc: UIViewController, context: Context) {
        DispatchQueue.main.async {
            vc.navigationController?.interactivePopGestureRecognizer?.isEnabled = !locked
        }
    }
}

/// Breathing cockpit orb while fill is mid-flight.
private struct AutopilotOrb: View {
    var active: Bool

    var body: some View {
        PropellerIcon(speed: active ? .fast : .still, size: 14)
            .foregroundStyle(Theme.accent)
    }
}
