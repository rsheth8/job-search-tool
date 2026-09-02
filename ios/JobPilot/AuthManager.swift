import AuthenticationServices
import Foundation
import SwiftUI

/// Sign in with Apple + session persistence.
///
/// The backend verifies the Apple identity token and returns an opaque session
/// token. We store that (and the app user id) in the Keychain-backed Config;
/// every API call then sends `Authorization: Bearer …`.
@MainActor
final class AuthManager: NSObject, ObservableObject {
    static let shared = AuthManager()

    @Published var isSignedIn: Bool = false
    @Published var displayName: String = ""
    @Published var lastError: String?
    @Published var busy = false

    private var continuity: ASAuthorizationController?

    override init() {
        super.init()
        let cfg = Config.shared
        isSignedIn = !cfg.sessionToken.isEmpty && !cfg.user.isEmpty
        displayName = cfg.displayName
    }

    /// Restore session from disk; confirm with `/auth/me` when online.
    func refresh() async {
        let cfg = Config.shared
        guard !cfg.sessionToken.isEmpty else {
            isSignedIn = false
            return
        }
        do {
            let user = try await APIClient(config: cfg).authMe()
            cfg.user = user.id
            if let name = user.display_name, !name.isEmpty {
                cfg.displayName = name
                displayName = name
            }
            isSignedIn = true
        } catch {
            if let api = error as? APIClient.APIError, case .http(401) = api {
                signOutLocal()
                lastError = "Session expired. Sign in again."
                return
            }
            // Network blips: keep the local session; the next call will 401 if dead.
        }
    }

    func signInWithApple() {
        lastError = nil
        busy = true
        let request = ASAuthorizationAppleIDProvider().createRequest()
        request.requestedScopes = [.fullName, .email]
        let controller = ASAuthorizationController(authorizationRequests: [request])
        controller.delegate = self
        controller.presentationContextProvider = self
        continuity = controller
        controller.performRequests()
    }

    // MARK: Email + password
    //
    // The second door. Sign in with Apple needs the Apple ID signed into the
    // device; on a shared test iPhone, or a simulator where the Apple sheet
    // stalls, that is a wall with nothing behind it.

    /// Create an account. Errors carry the server's own sentence.
    func signUp(email: String, password: String, displayName: String) async -> Bool {
        await run {
            try await APIClient(config: Config.shared).authSignUp(
                email: email.trimmingCharacters(in: .whitespacesAndNewlines),
                password: password,
                displayName: displayName.trimmingCharacters(in: .whitespacesAndNewlines)
            )
        }
    }

    func logIn(email: String, password: String) async -> Bool {
        await run {
            try await APIClient(config: Config.shared).authLogIn(
                email: email.trimmingCharacters(in: .whitespacesAndNewlines),
                password: password
            )
        }
    }

    /// Shared shape for every credential path: busy on, error cleared, session
    /// applied on success. Returns whether it worked so the caller can dismiss.
    private func run(_ work: () async throws -> AuthSession) async -> Bool {
        busy = true
        lastError = nil
        defer { busy = false }
        do {
            apply(try await work())
            return true
        } catch {
            if APIClient.isCancellation(error) { return false }
            lastError = APIClient.userMessage(for: error)
            return false
        }
    }

    /// Local-only escape hatch when the backend has AUTH_ALLOW_DEV_LOGIN (simulator).
    /// Always reuses ``Config.simulatorDevUserId`` so queue/identity/knowledge stay
    /// on one account across relaunches and Dev sign-ins (a bare /auth/dev mints a
    /// fresh empty user every time).
    func signInDev() async {
        busy = true
        lastError = nil
        defer { busy = false }
        #if targetEnvironment(simulator)
        // Prod Fly returns 404 for /auth/dev — always hit local uvicorn here.
        let cfg = Config.shared
        if cfg.baseURL.contains("fly.dev") || cfg.baseURL.isEmpty {
            cfg.baseURL = "http://127.0.0.1:8000"
        }
        #endif
        do {
            let session = try await APIClient(config: Config.shared).authDev(
                displayName: "Rahil",
                userId: Config.simulatorDevUserId
            )
            apply(session)
        } catch {
            if let api = error as? APIClient.APIError, case .http(404) = api {
                lastError = "Dev login 404 — start local API: AUTH_ALLOW_DEV_LOGIN=true uvicorn on :8000"
            } else {
                lastError = APIClient.userMessage(for: error)
            }
        }
    }

    func signOut() async {
        let cfg = Config.shared
        if !cfg.sessionToken.isEmpty {
            _ = try? await APIClient(config: cfg).authLogout()
        }
        signOutLocal()
    }

    private func signOutLocal() {
        let cfg = Config.shared
        cfg.sessionToken = ""
        cfg.user = ""
        cfg.displayName = ""
        displayName = ""
        isSignedIn = false
    }

    private func apply(_ session: AuthSession) {
        let cfg = Config.shared
        cfg.sessionToken = session.token
        cfg.user = session.user.id
        cfg.displayName = session.user.display_name ?? session.user.email ?? ""
        displayName = cfg.displayName
        isSignedIn = true
        busy = false
        PushManager.shared.reregister(config: cfg)
    }

    private func finishApple(credential: ASAuthorizationAppleIDCredential) async {
        defer { busy = false }
        guard let tokenData = credential.identityToken,
              let identityToken = String(data: tokenData, encoding: .utf8) else {
            lastError = "Apple didn’t return an identity token."
            return
        }
        var display: String?
        if let name = credential.fullName {
            let parts = [name.givenName, name.familyName].compactMap { $0 }
            if !parts.isEmpty { display = parts.joined(separator: " ") }
        }
        do {
            let session = try await APIClient(config: Config.shared)
                .authApple(identityToken: identityToken,
                           email: credential.email,
                           displayName: display)
            apply(session)
        } catch {
            lastError = APIClient.userMessage(for: error)
        }
    }
}

extension AuthManager: ASAuthorizationControllerDelegate {
    nonisolated func authorizationController(
        controller: ASAuthorizationController,
        didCompleteWithAuthorization authorization: ASAuthorization
    ) {
        guard let credential = authorization.credential as? ASAuthorizationAppleIDCredential else {
            Task { @MainActor in
                self.lastError = "Unexpected Apple credential."
                self.busy = false
            }
            return
        }
        Task { @MainActor in
            await self.finishApple(credential: credential)
        }
    }

    nonisolated func authorizationController(
        controller: ASAuthorizationController,
        didCompleteWithError error: Error
    ) {
        Task { @MainActor in
            self.busy = false
            let ns = error as NSError
            if ns.domain == ASAuthorizationError.errorDomain,
               ns.code == ASAuthorizationError.canceled.rawValue {
                return
            }
            self.lastError = APIClient.appleAuthMessage(error)
        }
    }
}

extension AuthManager: ASAuthorizationControllerPresentationContextProviding {
    nonisolated func presentationAnchor(
        for controller: ASAuthorizationController
    ) -> ASPresentationAnchor {
        // The protocol requires `nonisolated`, but every line below is UIKit
        // and main-actor isolated — six warnings, two of them already "an
        // error in the Swift 6 language mode".
        //
        // AuthenticationServices calls this on the main thread to place the
        // sign-in sheet, and the method must return an anchor synchronously,
        // so there is nothing to await our way out of. `assumeIsolated` states
        // the guarantee the framework already makes instead of quietly
        // reaching across the actor and hoping.
        MainActor.assumeIsolated {
            let scenes = UIApplication.shared.connectedScenes
                .compactMap { $0 as? UIWindowScene }
            let window = scenes.flatMap(\.windows).first { $0.isKeyWindow }
                ?? scenes.flatMap(\.windows).first
            return window ?? ASPresentationAnchor()
        }
    }
}
