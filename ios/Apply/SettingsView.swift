import SwiftUI

/// Point the app at your backend and identify yourself. Defaults already match the
/// live deploy + your Slack id, so usually there's nothing to change.
struct SettingsView: View {
    @EnvironmentObject var config: Config

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
