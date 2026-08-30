import AuthenticationServices
import SwiftUI

/// Shown when there’s no session. JobPilot is the product; Chat (and the rest)
/// need an account first.
struct SignInView: View {
    @EnvironmentObject var auth: AuthManager
    @EnvironmentObject var config: Config
    @EnvironmentObject var setup: SetupGate
    @State private var showQuizDemo = false

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: Theme.spaceL) {
                VStack(alignment: .leading, spacing: 10) {
                    PropellerIcon(speed: auth.busy ? .medium : .still, size: 48)
                        .foregroundStyle(Theme.accent)
                        .padding(.bottom, 4)

                    Text("JobPilot")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Theme.horizon)
                        .textCase(.uppercase)
                        .tracking(1.2)

                    Text("Sign in")
                        .font(Theme.title(34))
                        .foregroundStyle(Theme.ink)

                    Text("Your account keeps matches, answers, and applications in one place.")
                        .font(.body)
                        .foregroundStyle(Theme.soft)
                        .fixedSize(horizontal: false, vertical: true)
                }

                SignInWithAppleButton(.signIn) { request in
                    request.requestedScopes = [.fullName, .email]
                } onCompletion: { result in
                    // Prefer AuthManager’s controller so we share one code path;
                    // the button’s completion is a fallback if the manager is busy.
                    switch result {
                    case .success(let authorization):
                        if let credential = authorization.credential as? ASAuthorizationAppleIDCredential {
                            Task { await finish(credential) }
                        }
                    case .failure(let error):
                        let ns = error as NSError
                        if ns.domain == ASAuthorizationError.errorDomain,
                           ns.code == ASAuthorizationError.canceled.rawValue { return }
                        auth.lastError = APIClient.appleAuthMessage(error)
                    }
                }
                .signInWithAppleButtonStyle(.black)
                .frame(height: 48)
                .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                .disabled(auth.busy)

                #if targetEnvironment(simulator)
                Button {
                    Task { await auth.signInDev() }
                } label: {
                    Text("Dev sign-in (simulator)")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(Theme.accent)
                        .frame(maxWidth: .infinity)
                        .frame(height: 48)
                        .background(
                            RoundedRectangle(cornerRadius: 12, style: .continuous)
                                .strokeBorder(Theme.cloud, lineWidth: 1)
                        )
                }
                .buttonStyle(PressableButtonStyle())
                .disabled(auth.busy)
                #endif

                if auth.busy {
                    Text("Signing in…")
                        .font(.caption)
                        .foregroundStyle(Theme.soft)
                }

                if let err = auth.lastError, !err.isEmpty {
                    Text(err)
                        .font(.caption)
                        .foregroundStyle(Theme.note)
                }

                Text("Uses Sign in with Apple. We never see your password.")
                    .font(.caption)
                    .foregroundStyle(Theme.soft)

                Button {
                    showQuizDemo = true
                } label: {
                    Text("Preview the profile quiz")
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(Theme.accent)
                }
                .buttonStyle(PressableButtonStyle())
                .padding(.top, 4)
                .accessibilityLabel("Preview the profile quiz")

                #if targetEnvironment(simulator)
                Text("Simulator tip: Sign in with Apple often sticks on the password sheet. Use Dev sign-in against a local backend with AUTH_ALLOW_DEV_LOGIN=true, or sign into Settings → Apple Account first.")
                    .font(.caption)
                    .foregroundStyle(Theme.soft)
                #endif

                Spacer()
            }
            .padding(.horizontal, Theme.spaceL)
            .padding(.top, Theme.spaceXL)
            .toolbar(.hidden, for: .navigationBar)
            .ambientScreen()
            .fullScreenCover(isPresented: $showQuizDemo) {
                SetupView(mode: .demo)
                    .environmentObject(config)
                    .environmentObject(setup)
            }
        }
    }

    private func finish(_ credential: ASAuthorizationAppleIDCredential) async {
        auth.busy = true
        auth.lastError = nil
        defer { auth.busy = false }
        guard let tokenData = credential.identityToken,
              let identityToken = String(data: tokenData, encoding: .utf8) else {
            auth.lastError = "Apple didn’t return an identity token."
            return
        }
        var display: String?
        if let name = credential.fullName {
            let parts = [name.givenName, name.familyName].compactMap { $0 }
            if !parts.isEmpty { display = parts.joined(separator: " ") }
        }
        do {
            let session = try await APIClient(config: config)
                .authApple(identityToken: identityToken,
                           email: credential.email,
                           displayName: display)
            config.sessionToken = session.token
            config.user = session.user.id
            config.displayName = session.user.display_name ?? session.user.email ?? ""
            auth.displayName = config.displayName
            auth.isSignedIn = true
            PushManager.shared.reregister(config: config)
        } catch {
            auth.lastError = APIClient.userMessage(for: error)
        }
    }
}
