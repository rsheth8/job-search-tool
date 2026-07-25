import SwiftUI

/// Point the app at your backend and identify yourself. Defaults already match the
/// live deploy + your Slack id, so usually there's nothing to change.
struct SettingsView: View {
    @EnvironmentObject var config: Config
    @EnvironmentObject var push: PushManager

    var body: some View {
        NavigationStack {
            Form {
                Section("Backend") {
                    TextField("Base URL", text: $config.baseURL)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                        .keyboardType(.URL)
                    TextField("User id", text: $config.user)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                    SecureField("APPLY_API_TOKEN (optional)", text: $config.token)
                }

                Section("Notifications") {
                    if push.authorized {
                        Label("On — new matches and approval requests",
                              systemImage: "bell.badge.fill")
                            .font(.subheadline)
                        // Registering into a server with no APNs key would look like
                        // success and then never deliver anything. Say so instead.
                        if !push.serverConfigured {
                            Label("The server has no APNs key yet, so nothing will "
                                  + "arrive until that's configured.",
                                  systemImage: "exclamationmark.triangle")
                                .font(.caption).foregroundStyle(.orange)
                        }
                    } else {
                        // Asked on demand, not at launch: a prompt before the app has
                        // shown you anything is the fastest way to a permanent no.
                        Button {
                            Task { await push.enable() }
                        } label: {
                            Label("Turn on notifications", systemImage: "bell")
                        }
                        Text("Get told when new matches land, and when a filled "
                             + "application is waiting on your approval.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    if let error = push.lastError {
                        Text(error).font(.caption2).foregroundStyle(.secondary)
                    }
                }
                Section {
                    Text("Tap a match → it opens in an in-app browser. Hit **Autofill** to "
                         + "fill your details and answers, attach your resume, solve any "
                         + "captcha yourself, then submit on the site. Tap **I applied** to log it.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Settings")
        }
    }
}
