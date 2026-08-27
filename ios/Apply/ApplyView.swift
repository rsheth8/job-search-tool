import SwiftUI

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
                    title: "Couldn't prepare this one",
                    description: error
                )
            } else {
                PreparingView(message: "Preparing your application…")
            }
        }
        // Hide the floating dock for the whole apply flow (including loading),
        // not only after the package arrives — otherwise dock + Autofill stack.
        .onAppear { chrome.dockHidden = true }
        .task {
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
}

/// Soft chrome over the real form: Autofill, resume, mark applied.
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
    @State private var resumeDoc: ResumeDoc?
    @State private var fetchingResume = false
    @State private var resumeReadyFlash = false
    @State private var prefetchedResume: URL?

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
                        .transition(.opacity)
                }

                driveBanner
                    .transition(.move(edge: .top).combined(with: .opacity))
                Spacer(minLength: 0)
            }
            .animation(Theme.springSoft, value: model.driveState)
            .animation(Theme.quick, value: model.loading)
        }
        .safeAreaInset(edge: .bottom, spacing: 0) { controls }
        .appToast($toast, bottomPadding: 96)
        .navigationTitle(item.company ?? "Apply")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            // Nav "Back" pops Apply. Site history lives in the bottom bar so we
            // don't show two competing chevrons under the title.
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    Button {
                        Task { await skipAndClose() }
                    } label: {
                        Label("Skip for now", systemImage: "clock")
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
        .onChange(of: model.lastFill?.filled) { _, _ in showFillToast() }
        .confirmationDialog("Mark this as applied?", isPresented: $confirmApplied,
                            titleVisibility: .visible) {
            Button("I applied") {
                marking = true
                Task {
                    try? await APIClient(config: config).markApplied(postingId: item.posting_id)
                    Theme.impact(.soft)
                    dismiss()
                }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Logs it in your tracker. Submit on the site first.")
        }
        .confirmationDialog("Pass on this job?", isPresented: $confirmPass,
                            titleVisibility: .visible) {
            Button("Pass", role: .destructive) {
                Task { await passAndClose() }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Removes it from Ready and won’t show it again.")
        }
        .sheet(item: $resumeDoc) { doc in ShareSheet(items: [doc.url]) }
    }

    @ViewBuilder
    private var driveBanner: some View {
        switch model.driveState {
        case .needsHuman(let reason):
            handoffBanner(
                title: "Autopilot needs you",
                detail: reason + " — solve it here, then tap Resume.",
                tint: Theme.note
            )
        case .watchingClear:
            handoffBanner(
                title: "All clear",
                detail: "Tap Resume Autopilot to keep going — or finish manually.",
                tint: Theme.accent
            )
        case .paused:
            handoffBanner(
                title: "Paused",
                detail: "Fill anything you like. Resume when you want autopilot back.",
                tint: Theme.soft
            )
        case .filling, .advancing, .probing:
            HStack(spacing: 10) {
                AutopilotOrb(active: true)
                Text(model.statusLine.isEmpty ? "Autopilot working…" : model.statusLine)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(Theme.ink)
                    .lineLimit(2)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.fog.opacity(0.94))
        case .ready:
            handoffBanner(
                title: "Ready for you",
                detail: "Review the form and submit yourself — autopilot never sends.",
                tint: Theme.accent
            )
        default:
            EmptyView()
        }
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
        .background(tint.opacity(0.14))
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

                Button { fetchResume() } label: {
                    Image(systemName: resumeReadyFlash || prefetchedResume != nil
                          ? "doc.text.fill" : "doc.text")
                        .font(.body.weight(.medium))
                        .foregroundStyle(Theme.ink.opacity(0.75))
                        .frame(width: 48, height: 44)
                        .background(Color.white.opacity(0.7), in: Circle())
                }
                .disabled(fetchingResume)
                .overlay {
                    if fetchingResume { ProgressView().controlSize(.small) }
                }
                .accessibilityLabel("Resume file")

                Button { confirmApplied = true } label: {
                    Image(systemName: "checkmark")
                        .font(.body.weight(.semibold))
                        .foregroundStyle(Theme.accent)
                        .frame(width: 48, height: 44)
                        .background(Color.white.opacity(0.7), in: Circle())
                }
                .disabled(marking)
                .accessibilityLabel("I applied")
            }
        }
        .padding(.horizontal, 14)
        .padding(.top, 10)
        .padding(.bottom, 10)
        .background(.ultraThinMaterial)
    }

    @ViewBuilder
    private var primaryDriveButton: some View {
        switch model.driveState {
        case .filling, .advancing, .probing:
            Button { model.pauseAutopilot() } label: {
                Label("Pause", systemImage: "pause.fill")
                    .font(.body.weight(.semibold))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
                    .foregroundStyle(Theme.ink)
                    .background(Color.white.opacity(0.85), in: Capsule())
            }
            .buttonStyle(PressableButtonStyle())
        case .needsHuman, .watchingClear, .paused, .ready:
            Button { model.resumeAutopilot() } label: {
                Label("Resume Autopilot", systemImage: "sparkles")
                    .font(.body.weight(.semibold))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
                    .foregroundStyle(.white)
                    .background(Theme.accent, in: Capsule())
            }
            .buttonStyle(PressableButtonStyle())
        case .failed:
            Button { model.startAutopilot() } label: {
                Label("Try Autopilot", systemImage: "sparkles")
                    .font(.body.weight(.semibold))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
                    .foregroundStyle(.white)
                    .background(Theme.accent, in: Capsule())
            }
            .buttonStyle(PressableButtonStyle())
        case .idle:
            Button { model.autofill() } label: {
                Label("Autofill", systemImage: "bolt.fill")
                    .font(.body.weight(.semibold))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
                    .foregroundStyle(.white)
                    .background(Theme.accent, in: Capsule())
            }
            .buttonStyle(PressableButtonStyle())
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
        try? await APIClient(config: config).passPosting(postingId: item.posting_id)
        Theme.impact(.soft)
        dismiss()
    }

    private func showFillToast() {
        guard let f = model.lastFill else { return }
        // Drive loop posts its own status; avoid noisy toasts while autopilot runs.
        if model.driveState.isRunning { return }
        if f.filled == 0 && f.essays == 0 {
            flashToast("No fields matched — form still loading, or name/email missing in About me")
            return
        }
        let more = f.essays > 0 ? " · \(f.essays) need you" : ""
        let stale = f.rules.hasPrefix("bundled") ? " · offline rules" : ""
        flashToast("Filled \(f.filled)\(more)\(stale)")
    }

    private func prefetchResume() {
        guard resumeDoc == nil, !fetchingResume else { return }
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
            resumeDoc = ResumeDoc(url: ready)
            return
        }
        fetchingResume = true
        Task {
            defer { fetchingResume = false }
            do {
                resumeDoc = ResumeDoc(url: try await APIClient(config: config)
                    .downloadResume(postingId: item.posting_id))
            }
            catch APIClient.APIError.http(404) { flashToast("No tailored resume yet") }
            catch { flashToast("Couldn't fetch resume") }
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

struct ResumeDoc: Identifiable { let id = UUID(); let url: URL }

/// Breathing sage orb while autopilot is mid-flight.
private struct AutopilotOrb: View {
    var active: Bool
    @State private var pulse = false

    var body: some View {
        Circle()
            .fill(Theme.accent.opacity(pulse ? 0.95 : 0.45))
            .frame(width: 10, height: 10)
            .shadow(color: Theme.accent.opacity(pulse ? 0.45 : 0.1), radius: pulse ? 6 : 2)
            .onAppear {
                guard active else { return }
                withAnimation(Theme.breathe) { pulse = true }
            }
    }
}
