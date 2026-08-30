import SwiftUI
import UIKit

/// Sparse config with the same editorial shell as the other tabs.
struct SettingsView: View {
    @EnvironmentObject var config: Config
    @EnvironmentObject var push: PushManager
    @EnvironmentObject var auth: AuthManager
    @EnvironmentObject var setup: SetupGate

    @State private var testing = false
    @State private var connectionLabel = "Not checked yet"
    @State private var toast: String?
    @State private var feedbackText = ""
    @State private var sendingFeedback = false
    @State private var showQuizDemo = false
    @ObservedObject private var diagnostics = Diagnostics.shared

    private var diagSummary: String {
        let d = diagnostics
        let rid = d.lastRequestId.isEmpty ? "—" : d.lastRequestId
        let path = d.lastPath.isEmpty ? "—" : d.lastPath
        let err = d.lastError.isEmpty ? "none" : d.lastError
        return "\(Diagnostics.appVersion)\n\(path) · \(d.lastStatus) · \(rid)\n\(err)"
    }

    var body: some View {
        NavigationStack {
            ScrollViewReader { proxy in
            ScrollView(showsIndicators: false) {
                VStack(alignment: .leading, spacing: Theme.spaceM) {
                    PageHeader(
                        eyebrow: "Settings",
                        title: "Preferences",
                        subtitle: "Account, notifications, and feedback."
                    )

                    FocusCard {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("Account")
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(Theme.horizon)
                                .textCase(.uppercase)
                                .tracking(0.8)
                            Text(auth.displayName.isEmpty ? "Signed in with Apple" : auth.displayName)
                                .font(.subheadline.weight(.medium))
                                .foregroundStyle(Theme.ink)
                            Button(role: .destructive) {
                                Task { await auth.signOut() }
                            } label: {
                                Text("Sign out")
                                    .font(.subheadline.weight(.medium))
                                    .foregroundStyle(Theme.note)
                            }
                            .padding(.top, 4)
                        }
                    }
                    .padding(.horizontal, Theme.spaceL)

                    FocusCard {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("Notifications")
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(Theme.horizon)
                                .textCase(.uppercase)
                                .tracking(0.8)
                            if push.authorized {
                                Text("On — we’ll ping you when new matches land, and when a follow-up is due.")
                                    .font(.subheadline)
                                    .foregroundStyle(Theme.ink.opacity(0.85))
                                if !push.serverConfigured {
                                    Text("Server isn’t set up for push yet.")
                                        .font(.caption)
                                        .foregroundStyle(Theme.note)
                                }
                            } else {
                                Button {
                                    Task { await push.enable() }
                                } label: {
                                    Text("Turn on notifications")
                                        .font(.subheadline.weight(.medium))
                                        .foregroundStyle(Theme.accent)
                                }
                                Text("Optional. We’ll ping you for new matches and follow-ups.")
                                    .font(.caption)
                                    .foregroundStyle(Theme.soft)
                            }
                            if let error = push.lastError {
                                Text(error).font(.caption2).foregroundStyle(Theme.soft)
                            }
                        }
                    }
                    .padding(.horizontal, Theme.spaceL)
                    .id("notifications")

                    FocusCard {
                        VStack(alignment: .leading, spacing: 10) {
                            Text("Feedback")
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(Theme.horizon)
                                .textCase(.uppercase)
                                .tracking(0.8)
                            Text("A bug, a confusing screen, or a form that didn’t fill.")
                                .font(.caption)
                                .foregroundStyle(Theme.soft)
                            TextField("A sentence or two", text: $feedbackText, axis: .vertical)
                                .lineLimit(3...6)
                                .font(.subheadline)
                                .padding(12)
                                .background(Theme.fog, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                            PrimaryButton(
                                title: "Send",
                                busy: sendingFeedback,
                                busyTitle: "Sending…"
                            ) {
                                Task { await sendFeedback() }
                            }
                            .disabled(sendingFeedback
                                      || feedbackText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                        }
                    }
                    .padding(.horizontal, Theme.spaceL)
                    .id("feedback")

                    GroupedSurface {
                        Button {
                            showQuizDemo = true
                        } label: {
                            HStack {
                                Text("Quiz preview")
                                    .font(.subheadline.weight(.medium))
                                    .foregroundStyle(Theme.ink)
                                Spacer()
                                Image(systemName: "chevron.right")
                                    .font(.caption.weight(.semibold))
                                    .foregroundStyle(Theme.soft.opacity(0.7))
                            }
                            .padding(.horizontal, 16)
                            .padding(.vertical, 14)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(PressableButtonStyle())
                        .accessibilityLabel("Open quiz preview")
                        settingsDivider
                        settingsExpand("How it works") {
                            VStack(alignment: .leading, spacing: 10) {
                                howStep("Matches — JobPilot finds fits on Greenhouse, Lever, and Ashby.")
                                howStep("You — roles, form details, and facts used to fill.")
                                howStep("Preflight — resume and answers are prepared for the role.")
                                howStep("Fill — tap Fill on the real form. Attach a résumé or cover letter from the documents menu if the form asks.")
                                howStep("You submit, then mark Filed. Filed applications live on Apply.")
                                howStep("Horizon — find jobs, edit details, and how Autofill works.")
                            }
                        }
                        settingsDivider
                        settingsExpand("For testers") {
                            VStack(alignment: .leading, spacing: 10) {
                                howStep("Sign in with Apple (invite-only). Hide My Email is fine — send that relay address to the host first.")
                                howStep("Finish the profile quiz so Autofill has your details.")
                                howStep("Matches start searching as soon as you save roles. Pull to refresh to search again.")
                                howStep("Preflight, then Fill. Public forms get Autofill. Sign in when a site asks — logins are saved. Attach the résumé or cover letter yourself (Files).")
                                howStep("LinkedIn Easy Apply is still you in LinkedIn. Workday widgets often need a hand.")
                                howStep("Send feedback when something’s off. Settings → Diagnostics copies a report if you email instead.")
                            }
                        }
                        settingsDivider
                        settingsExpand("Diagnostics") {
                            VStack(alignment: .leading, spacing: 10) {
                                Text("Paste this if something’s off — Send already attaches it.")
                                    .font(.caption)
                                    .foregroundStyle(Theme.soft)
                                    .fixedSize(horizontal: false, vertical: true)
                                if !config.user.isEmpty {
                                    Text(config.user)
                                        .font(.caption2.monospaced())
                                        .foregroundStyle(Theme.soft)
                                        .textSelection(.enabled)
                                }
                                Text(diagSummary)
                                    .font(.caption2.monospaced())
                                    .foregroundStyle(Theme.ink.opacity(0.8))
                                    .textSelection(.enabled)
                                Button {
                                    UIPasteboard.general.string = Diagnostics.shared.report
                                    flashToast("Copied diagnostics")
                                } label: {
                                    Text("Copy report")
                                        .font(.subheadline.weight(.semibold))
                                        .foregroundStyle(Theme.accent)
                                }
                                .buttonStyle(PressableButtonStyle())
                            }
                        }
                        settingsDivider
                        settingsExpand("Advanced") {
                            VStack(alignment: .leading, spacing: 12) {
                                fieldLabel("Base URL")
                                TextField("https://…", text: $config.baseURL)
                                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                                    .keyboardType(.URL)
                                    .padding(12)
                                    .background(Theme.fog, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                                fieldLabel("API token")
                                SecureField("Optional (extension)", text: $config.token)
                                    .padding(12)
                                    .background(Theme.fog, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                                HStack {
                                    Text(connectionLabel)
                                        .font(.subheadline)
                                        .foregroundStyle(Theme.soft)
                                    Spacer()
                                    Button {
                                        Task { await testConnection() }
                                    } label: {
                                        if testing {
                                            PropellerIcon(speed: .medium, size: 14)
                                                .foregroundStyle(Theme.accent)
                                        } else {
                                            Text("Test").fontWeight(.semibold)
                                        }
                                    }
                                    .foregroundStyle(Theme.accent)
                                    .disabled(testing)
                                }
                            }
                        }
                    }
                    .padding(.horizontal, Theme.spaceL)

                    Color.clear.frame(height: Theme.dockClearance)
                }
                .padding(.top, 4)
            }
            .onAppear { consumeHorizonHop(proxy: proxy) }
            .onChange(of: push.hop) { _, _ in consumeHorizonHop(proxy: proxy) }
            }
            .toolbar(.hidden, for: .navigationBar)
            .ambientScreen()
            .instrumentEnter()
            .appToast($toast, bottomPadding: Theme.toastClearance)
            .onAppear { push.reregister(config: config) }
            .onChange(of: config.baseURL) { _, _ in push.reregister(config: config) }
            .fullScreenCover(isPresented: $showQuizDemo) {
                SetupView(mode: .demo)
                    .environmentObject(config)
                    .environmentObject(setup)
            }
        }
    }

    private func consumeHorizonHop(proxy: ScrollViewProxy) {
        guard let hop = push.hop else { return }
        switch hop {
        case .settingsNotifications, .settingsFeedback, .settingsQuiz:
            push.hop = nil
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: 280_000_000)
                switch hop {
                case .settingsNotifications:
                    withAnimation(Theme.springSoft) {
                        proxy.scrollTo("notifications", anchor: .top)
                    }
                case .settingsFeedback:
                    withAnimation(Theme.springSoft) {
                        proxy.scrollTo("feedback", anchor: .top)
                    }
                case .settingsQuiz:
                    showQuizDemo = true
                default:
                    break
                }
            }
        default:
            break
        }
    }

    private func settingsExpand<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        ExpandRow(title: title) { content() }
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
    }

    private var settingsDivider: some View {
        Rectangle()
            .fill(Theme.cloud.opacity(0.45))
            .frame(height: 1)
            .padding(.leading, 16)
    }

    private func fieldLabel(_ text: String) -> some View {
        Text(text)
            .font(.caption.weight(.semibold))
            .foregroundStyle(Theme.soft)
            .textCase(.uppercase)
            .tracking(0.8)
    }

    private func howStep(_ text: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Circle()
                .fill(Theme.accent.opacity(0.35))
                .frame(width: 7, height: 7)
                .padding(.top, 6)
            Text(text)
                .font(.subheadline)
                .foregroundStyle(Theme.soft)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func sendFeedback() async {
        let text = feedbackText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        sendingFeedback = true
        defer { sendingFeedback = false }
        do {
            try await APIClient(config: config).sendFeedback(text)
            feedbackText = ""
            flashToast("Sent — thank you")
        } catch {
            flashToast(APIClient.userMessage(for: error))
        }
    }

    private func testConnection() async {
        testing = true
        defer { testing = false }
        do {
            let health = try await APIClient(config: config).fetchHealth()
            if health.db_ok == false {
                connectionLabel = "Server up, database issue"
                flashToast("Server is up, but storage isn’t. Send feedback.")
                return
            }
            _ = try await APIClient(config: config).fetchData()
            connectionLabel = "Connected"
            flashToast("Connected")
        } catch APIClient.APIError.http(let code) where code == 401 || code == 403 {
            connectionLabel = "Reachable, but auth failed"
            flashToast("Auth failed (\(code)) — sign in again")
        } catch {
            connectionLabel = "Can't reach server"
            flashToast(APIClient.userMessage(for: error))
        }
    }

    private func flashToast(_ text: String) {
        withAnimation(Theme.springSoft) { toast = text }
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) {
            withAnimation(Theme.quick) { toast = nil }
        }
    }
}
