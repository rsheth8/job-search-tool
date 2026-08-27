import SwiftUI

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

    var body: some View {
        NavigationStack {
            ScrollView(showsIndicators: false) {
                VStack(alignment: .leading, spacing: Theme.spaceL) {
                    PageHeader(
                        eyebrow: "Settings",
                        title: "Setup",
                        subtitle: "Usually nothing to change."
                    )

                    settingsCard {
                        Text("Account")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(Theme.soft)
                            .textCase(.uppercase)
                            .tracking(0.6)
                        Text(auth.displayName.isEmpty ? config.user : auth.displayName)
                            .font(.subheadline.weight(.medium))
                            .foregroundStyle(Theme.ink)
                        if !config.user.isEmpty {
                            Text(config.user)
                                .font(.caption2.monospaced())
                                .foregroundStyle(Theme.soft)
                        }
                        Button(role: .destructive) {
                            Task { await auth.signOut() }
                        } label: {
                            Text("Sign out")
                                .font(.subheadline.weight(.semibold))
                        }
                        .padding(.top, 4)
                        Button("Redo setup") {
                            setup.reopen()
                        }
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(Theme.accent)
                    }

                    settingsCard {
                        Text("Send feedback")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(Theme.soft)
                            .textCase(.uppercase)
                            .tracking(0.6)
                        Text("What broke, what was confusing, a form that didn't fill.")
                            .font(.caption)
                            .foregroundStyle(Theme.soft)
                        TextField("A sentence or two", text: $feedbackText, axis: .vertical)
                            .lineLimit(3...6)
                            .font(.subheadline)
                        Button {
                            Task { await sendFeedback() }
                        } label: {
                            if sendingFeedback {
                                ProgressView().controlSize(.small)
                            } else {
                                Text("Send")
                                    .font(.subheadline.weight(.semibold))
                            }
                        }
                        .foregroundStyle(Theme.accent)
                        .disabled(sendingFeedback || feedbackText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }

                    settingsCard {
                        DisclosureGroup {
                            VStack(alignment: .leading, spacing: 10) {
                                howStep("Sign in with Apple (invite-only).")
                                howStep("Finish setup: roles, identity, one project.")
                                howStep("Wait for matches or pull to refresh.")
                                howStep("Prepare → Autofill on Greenhouse, Lever, or Ashby. Attach the résumé yourself.")
                                howStep("Workday and LinkedIn Easy Apply are out of scope.")
                                howStep("Use Send feedback when something's off.")
                            }
                            .padding(.top, 8)
                        } label: {
                            Text("For testers")
                                .font(.subheadline.weight(.medium))
                                .foregroundStyle(Theme.ink)
                        }
                        .tint(Theme.accent)
                    }

                    settingsCard {
                        DisclosureGroup {
                            VStack(alignment: .leading, spacing: 12) {
                                fieldLabel("Base URL")
                                TextField("https://…", text: $config.baseURL)
                                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                                    .keyboardType(.URL)
                                Divider().background(Theme.accent.opacity(0.08))
                                fieldLabel("API token")
                                SecureField("Optional (extension)", text: $config.token)
                                Divider().background(Theme.accent.opacity(0.08))
                                HStack {
                                    Text(connectionLabel)
                                        .font(.subheadline)
                                        .foregroundStyle(Theme.soft)
                                    Spacer()
                                    Button {
                                        Task { await testConnection() }
                                    } label: {
                                        if testing {
                                            ProgressView().controlSize(.small)
                                        } else {
                                            Text("Test").fontWeight(.semibold)
                                        }
                                    }
                                    .foregroundStyle(Theme.accent)
                                    .disabled(testing)
                                }
                            }
                            .padding(.top, 8)
                        } label: {
                            Text("Advanced")
                                .font(.subheadline.weight(.medium))
                                .foregroundStyle(Theme.ink)
                        }
                        .tint(Theme.accent)
                    }

                    settingsCard {
                        Text("Notifications")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(Theme.soft)
                            .textCase(.uppercase)
                            .tracking(0.6)
                        if push.authorized {
                            Text("On — new matches and when something needs approval.")
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
                                    .font(.subheadline.weight(.semibold))
                                    .foregroundStyle(Theme.accent)
                            }
                            Text("Optional. Useful when a filled application is waiting on you.")
                                .font(.caption)
                                .foregroundStyle(Theme.soft)
                        }
                        if let error = push.lastError {
                            Text(error).font(.caption2).foregroundStyle(Theme.soft)
                        }
                    }

                    settingsCard {
                        DisclosureGroup {
                            VStack(alignment: .leading, spacing: 12) {
                                howStep("Open a match in the in-app browser.")
                                howStep("Tap Autofill for your details and answers.")
                                howStep("Attach your resume, handle any captcha, submit on the site.")
                                howStep("Mark I applied. Auto-submit from the phone is off during beta.")
                                howStep("Use Chat for reminders, CRM, and questions — Apply stays front and center.")
                            }
                            .padding(.top, 8)
                        } label: {
                            Text("How Apply works")
                                .font(.subheadline.weight(.medium))
                                .foregroundStyle(Theme.ink)
                        }
                        .tint(Theme.accent)
                    }

                    Color.clear.frame(height: 88)
                }
                .padding(.top, 8)
            }
            .toolbar(.hidden, for: .navigationBar)
            .ambientScreen()
            .appToast($toast, bottomPadding: 100)
            .onAppear { push.reregister(config: config) }
            .onChange(of: config.baseURL) { _, _ in push.reregister(config: config) }
        }
    }

    private func settingsCard<Content: View>(@ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            content()
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .fill(Color.white.opacity(0.72))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .strokeBorder(Theme.accent.opacity(0.08), lineWidth: 1)
        )
        .padding(.horizontal, Theme.spaceL)
    }

    private func fieldLabel(_ text: String) -> some View {
        Text(text)
            .font(.caption.weight(.semibold))
            .foregroundStyle(Theme.soft)
            .textCase(.uppercase)
            .tracking(0.5)
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
            _ = try await APIClient(config: config).fetchData()
            connectionLabel = "Connected"
            flashToast("Connected")
        } catch APIClient.APIError.http(let code) where code == 401 || code == 403 {
            connectionLabel = "Reachable, but auth failed"
            flashToast("Auth failed (\(code)) — sign in again")
        } catch {
            connectionLabel = "Can't reach server"
            flashToast("Couldn't reach server")
        }
    }

    private func flashToast(_ text: String) {
        withAnimation(Theme.springSoft) { toast = text }
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) {
            withAnimation(Theme.quick) { toast = nil }
        }
    }
}
