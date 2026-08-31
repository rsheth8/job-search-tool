import AuthenticationServices
import SwiftUI

/// The screen behind the door. No session yet, so this is the only thing a new
/// tester sees — it has to say what the app does before it asks for anything.
///
/// Two doors, deliberately: Sign in with Apple is the fast one, email is the one
/// that works on a device whose Apple ID isn't the tester's (a shared beta
/// iPhone, a simulator where the Apple sheet stalls). Both mint the same
/// session and both pass the same invite allowlist.
struct SignInView: View {
    @EnvironmentObject var auth: AuthManager
    @EnvironmentObject var config: Config
    @EnvironmentObject var setup: SetupGate

    @State private var showQuizDemo = false
    @State private var emailMode: EmailAuthView.Mode?
    @State private var appeared = false

    /// What the app is, in the order a person needs it.
    private let promises: [(symbol: String, title: String, detail: String)] = [
        ("scope", "Finds roles that fit",
         "Scans real job boards against your profile — no company list required."),
        ("sparkles", "Prepares the application",
         "Tailored one-page résumé and answers drafted from your own history."),
        ("bolt.fill", "Fills the form for you",
         "⚡ Autofill completes Greenhouse, Lever and Ashby. You always tap Submit."),
    ]

    var body: some View {
        ZStack {
            AmbientBackground()

            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    header
                    promiseList.padding(.top, Theme.spaceXL)
                    Spacer(minLength: Theme.spaceXL)
                    actions.padding(.top, Theme.spaceXL)
                    footer.padding(.top, Theme.spaceL)
                }
                .padding(.horizontal, Theme.spaceL)
                .padding(.top, Theme.spaceXL)
                .padding(.bottom, Theme.spaceXL)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .scrollBounceBehavior(.basedOnSize)
        }
        .onAppear { withAnimation(Theme.springSoft.delay(0.05)) { appeared = true } }
        .sheet(item: $emailMode) { mode in
            EmailAuthView(mode: mode)
                .environmentObject(auth)
                .environmentObject(config)
        }
        .fullScreenCover(isPresented: $showQuizDemo) {
            SetupView(mode: .demo)
                .environmentObject(config)
                .environmentObject(setup)
        }
    }

    // MARK: Header

    private var header: some View {
        VStack(alignment: .leading, spacing: 10) {
            PropellerIcon(speed: auth.busy ? .medium : .slow, size: 46)
                .foregroundStyle(Theme.accent)
                .padding(.bottom, 2)

            Text("JOBPILOT")
                .font(.caption.weight(.semibold))
                .foregroundStyle(Theme.horizon)
                .tracking(1.4)

            Text("Your job search,\nflown for you.")
                .font(Theme.title(34))
                .foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)

            Text("Find the roles, prepare the application, autofill the form. You stay in the seat.")
                .font(.body)
                .foregroundStyle(Theme.soft)
                .fixedSize(horizontal: false, vertical: true)
        }
        .staggerAppear(0)
    }

    private var promiseList: some View {
        VStack(alignment: .leading, spacing: Theme.spaceM) {
            ForEach(Array(promises.enumerated()), id: \.offset) { index, promise in
                HStack(alignment: .top, spacing: Theme.spaceM) {
                    ZStack {
                        Circle()
                            .fill(Theme.cockpit.opacity(0.10))
                            .frame(width: 38, height: 38)
                        Image(systemName: promise.symbol)
                            .font(.system(size: 15, weight: .semibold))
                            .foregroundStyle(Theme.cockpit)
                    }
                    VStack(alignment: .leading, spacing: 3) {
                        Text(promise.title)
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(Theme.ink)
                        Text(promise.detail)
                            .font(.footnote)
                            .foregroundStyle(Theme.soft)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: 0)
                }
                .staggerAppear(index + 1)
            }
        }
    }

    // MARK: Actions

    private var actions: some View {
        VStack(spacing: Theme.spaceM) {
            SignInWithAppleButton(.signIn) { request in
                request.requestedScopes = [.fullName, .email]
            } onCompletion: { result in
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
            .frame(height: 50)
            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
            .disabled(auth.busy)
            .accessibilityIdentifier("signInWithApple")

            HStack(spacing: Theme.spaceM) {
                divider
                Text("or")
                    .font(.caption)
                    .foregroundStyle(Theme.soft)
                divider
            }
            .padding(.vertical, 2)

            Button {
                auth.lastError = nil
                emailMode = .signIn
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "envelope.fill").font(.system(size: 14, weight: .semibold))
                    Text("Continue with email").font(.subheadline.weight(.semibold))
                }
                .foregroundStyle(Theme.cockpit)
                .frame(maxWidth: .infinity)
                .frame(height: 50)
                .background(
                    RoundedRectangle(cornerRadius: 14, style: .continuous)
                        .fill(Color.white.opacity(0.75))
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 14, style: .continuous)
                        .strokeBorder(Theme.cloud, lineWidth: 1)
                )
            }
            .buttonStyle(PressableButtonStyle())
            .disabled(auth.busy)
            .accessibilityIdentifier("continueWithEmail")

            Button {
                auth.lastError = nil
                emailMode = .signUp
            } label: {
                Text("New here? Create an account")
                    .font(.footnote.weight(.medium))
                    .foregroundStyle(Theme.horizon)
            }
            .buttonStyle(PressableButtonStyle())
            .disabled(auth.busy)
            .accessibilityIdentifier("createAccount")

            if auth.busy {
                HStack(spacing: 8) {
                    PropellerIcon(speed: .fast, size: 14).foregroundStyle(Theme.accent)
                    Text("Signing in…").font(.caption).foregroundStyle(Theme.soft)
                }
                .transition(.opacity)
            }

            if let err = auth.lastError, !err.isEmpty {
                InlineError(text: err)
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .animation(Theme.quick, value: auth.busy)
        .animation(Theme.quick, value: auth.lastError)
        .staggerAppear(promises.count + 1)
    }

    private var divider: some View {
        Rectangle().fill(Theme.cloud).frame(height: 1)
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: Theme.spaceM) {
            Text("Invite-only beta. JobPilot never submits an application for you.")
                .font(.caption)
                .foregroundStyle(Theme.soft)
                .fixedSize(horizontal: false, vertical: true)

            Button {
                showQuizDemo = true
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "play.circle").font(.system(size: 13, weight: .semibold))
                    Text("Preview the profile quiz").font(.subheadline.weight(.medium))
                }
                .foregroundStyle(Theme.accent)
            }
            .buttonStyle(PressableButtonStyle())
            .accessibilityLabel("Preview the profile quiz")

            #if targetEnvironment(simulator)
            Button {
                Task { await auth.signInDev() }
            } label: {
                Text("Dev sign-in (simulator)")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(Theme.trail)
            }
            .buttonStyle(PressableButtonStyle())
            .disabled(auth.busy)
            .accessibilityIdentifier("devSignIn")
            #endif
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .staggerAppear(promises.count + 2)
    }

    // MARK: Apple

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
