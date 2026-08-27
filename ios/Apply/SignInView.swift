import AuthenticationServices
import SwiftUI

/// Shown when there’s no session. Apply stays the product; Chat (and the rest)
/// need an account first.
struct SignInView: View {
    @EnvironmentObject var auth: AuthManager
    @EnvironmentObject var config: Config

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: Theme.spaceL) {
                PageHeader(
                    eyebrow: "Apply",
                    title: "Sign in",
                    subtitle: "Your account keeps matches, answers, and chat in sync."
                )

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
                        auth.lastError = error.localizedDescription
                    }
                }
                .signInWithAppleButtonStyle(.black)
                .frame(height: 48)
                .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                .disabled(auth.busy)

                #if targetEnvironment(simulator)
                // SIWA's Apple Account password sheet often hangs on Simulator.
                // Backend must have AUTH_ALLOW_DEV_LOGIN=true (local uvicorn).
                Button {
                    Task { await auth.signInDev() }
                } label: {
                    Text("Dev sign-in (simulator)")
                        .font(.subheadline.weight(.semibold))
                        .frame(maxWidth: .infinity)
                        .frame(height: 48)
                }
                .buttonStyle(.bordered)
                .disabled(auth.busy)
                #endif

                if auth.busy {
                    ProgressView().controlSize(.small)
                }

                if let err = auth.lastError, !err.isEmpty {
                    Text(err)
                        .font(.caption)
                        .foregroundStyle(Theme.note)
                }

                Text("Uses Sign in with Apple. We never see your password.")
                    .font(.caption)
                    .foregroundStyle(Theme.soft)

                #if targetEnvironment(simulator)
                Text("Simulator tip: Sign in with Apple often sticks on the password sheet. Use Dev sign-in against a local backend with AUTH_ALLOW_DEV_LOGIN=true, or sign into Settings → Apple Account first.")
                    .font(.caption)
                    .foregroundStyle(Theme.soft)
                #endif

                Spacer()
            }
            .padding(.horizontal, Theme.spaceL)
            .padding(.top, Theme.spaceM)
            .ambientScreen()
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
