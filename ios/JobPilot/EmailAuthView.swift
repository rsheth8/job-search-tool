import SwiftUI

/// Email + password, as a sheet over the welcome screen.
///
/// One view for both modes so the two paths can't drift apart — the only
/// differences are the copy, whether a name field shows, and which endpoint the
/// button calls. Validation is client-side for the obvious mistakes only; the
/// server is the authority on everything that matters (allowlist, duplicates,
/// password floor) and its sentences are shown verbatim rather than remapped.
struct EmailAuthView: View {
    enum Mode: String, Identifiable {
        case signIn, signUp
        var id: String { rawValue }

        var title: String { self == .signIn ? "Welcome back" : "Create your account" }
        var blurb: String {
            self == .signIn
                ? "Sign in to pick up where you left off."
                : "Invite-only for now — use the email you were invited with."
        }
        var callToAction: String { self == .signIn ? "Sign in" : "Create account" }
        var switchPrompt: String {
            self == .signIn ? "New here? Create an account" : "Already have an account? Sign in"
        }
        var flipped: Mode { self == .signIn ? .signUp : .signIn }
    }

    @State var mode: Mode

    @EnvironmentObject var auth: AuthManager
    @EnvironmentObject var config: Config
    @Environment(\.dismiss) private var dismiss

    @State private var email = ""
    @State private var password = ""
    @State private var displayName = ""
    @State private var revealPassword = false
    @FocusState private var focus: Field?

    private enum Field: Hashable { case name, email, password }

    /// Mirrors the server's floor. Kept as a constant rather than a literal so
    /// the hint under the field and the check above it can never disagree.
    private let minPasswordLength = 8

    var body: some View {
        NavigationStack {
            ZStack {
                AmbientBackground()

                ScrollView {
                    VStack(alignment: .leading, spacing: Theme.spaceL) {
                        heading
                        fields
                        submitButton
                        if let err = auth.lastError, !err.isEmpty {
                            InlineError(text: err)
                                .transition(.opacity.combined(with: .move(edge: .top)))
                        }
                        switcher
                        Spacer(minLength: Theme.spaceXL)
                    }
                    .padding(.horizontal, Theme.spaceL)
                    .padding(.top, Theme.spaceL)
                }
                .scrollDismissesKeyboard(.interactively)
            }
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                        .foregroundStyle(Theme.accent)
                        .accessibilityIdentifier("emailAuthCancel")
                }
            }
            .animation(Theme.quick, value: auth.lastError)
            .animation(Theme.springSoft, value: mode)
        }
        .onAppear { focus = mode == .signUp ? .name : .email }
        .onChange(of: auth.isSignedIn) { _, signedIn in
            if signedIn { dismiss() }
        }
        .interactiveDismissDisabled(auth.busy)
    }

    // MARK: Pieces

    private var heading: some View {
        VStack(alignment: .leading, spacing: 8) {
            PropellerIcon(speed: auth.busy ? .fast : .still, size: 34)
                .foregroundStyle(Theme.accent)
            Text(mode.title)
                .font(Theme.title(28))
                .foregroundStyle(Theme.ink)
            Text(mode.blurb)
                .font(.subheadline)
                .foregroundStyle(Theme.soft)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var fields: some View {
        VStack(spacing: Theme.spaceM) {
            if mode == .signUp {
                LabeledField(label: "Name", systemImage: "person") {
                    TextField("Ada Lovelace", text: $displayName)
                        .textContentType(.name)
                        .autocorrectionDisabled()
                        .focused($focus, equals: .name)
                        .submitLabel(.next)
                        .onSubmit { focus = .email }
                        .accessibilityIdentifier("nameField")
                }
                .transition(.opacity.combined(with: .move(edge: .top)))
            }

            LabeledField(label: "Email", systemImage: "envelope") {
                TextField(text: $email, prompt: Text(verbatim: "you@example.com")) {
                    Text("Email")
                }
                    .textContentType(.emailAddress)
                    .keyboardType(.emailAddress)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .focused($focus, equals: .email)
                    .submitLabel(.next)
                    .onSubmit { focus = .password }
                    .accessibilityIdentifier("emailField")
            }

            LabeledField(label: "Password", systemImage: "lock") {
                HStack(spacing: 8) {
                    Group {
                        if revealPassword {
                            TextField("At least \(minPasswordLength) characters", text: $password)
                        } else {
                            SecureField("At least \(minPasswordLength) characters", text: $password)
                        }
                    }
                    // A new-password field asks iOS for a suggestion; on sign-in
                    // that prompt is wrong and suppresses the saved-password fill.
                    .textContentType(mode == .signUp ? .newPassword : .password)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .focused($focus, equals: .password)
                    .submitLabel(.go)
                    .onSubmit { submit() }
                    .accessibilityIdentifier("passwordField")

                    Button {
                        revealPassword.toggle()
                    } label: {
                        Image(systemName: revealPassword ? "eye.slash" : "eye")
                            .font(.system(size: 14))
                            .foregroundStyle(Theme.trail)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(revealPassword ? "Hide password" : "Show password")
                }
            }

            if mode == .signUp {
                Text("Use at least \(minPasswordLength) characters. We store a hash, never the password itself.")
                    .font(.caption)
                    .foregroundStyle(Theme.soft)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var submitButton: some View {
        Button(action: submit) {
            HStack(spacing: 8) {
                if auth.busy {
                    PropellerIcon(speed: .fast, size: 15).foregroundStyle(.white)
                }
                Text(auth.busy ? "Just a moment…" : mode.callToAction)
                    .font(.subheadline.weight(.semibold))
            }
            .foregroundStyle(.white)
            .frame(maxWidth: .infinity)
            .frame(height: 50)
            .background(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .fill(canSubmit ? Theme.cockpit : Theme.trail.opacity(0.55))
            )
        }
        .buttonStyle(PressableButtonStyle())
        .disabled(!canSubmit || auth.busy)
        .accessibilityIdentifier("emailAuthSubmit")
    }

    private var switcher: some View {
        Button {
            auth.lastError = nil
            mode = mode.flipped
            focus = mode == .signUp ? .name : .email
        } label: {
            Text(mode.switchPrompt)
                .font(.footnote.weight(.medium))
                .foregroundStyle(Theme.accent)
        }
        .buttonStyle(PressableButtonStyle())
        .disabled(auth.busy)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityIdentifier("emailAuthSwitchMode")
    }

    // MARK: Behaviour

    /// Deliberately loose: catches an empty box and an obvious typo, and leaves
    /// every real judgement to the server. A client that guesses harder than the
    /// server just greys out the button on addresses that would have worked.
    private var canSubmit: Bool {
        let e = email.trimmingCharacters(in: .whitespacesAndNewlines)
        guard e.contains("@"), e.contains("."), !e.hasPrefix("@"), !e.hasSuffix("@") else {
            return false
        }
        return password.count >= (mode == .signUp ? minPasswordLength : 1)
    }

    private func submit() {
        guard canSubmit, !auth.busy else { return }
        focus = nil
        Task {
            let ok: Bool
            switch mode {
            case .signIn:
                ok = await auth.logIn(email: email, password: password)
            case .signUp:
                ok = await auth.signUp(email: email, password: password,
                                       displayName: displayName)
            }
            if ok {
                Theme.notify(.success)
                dismiss()
            } else {
                Theme.notify(.error)
            }
        }
    }
}

/// A labelled input on the app's paper surface. Field chrome lives here so the
/// three inputs above can't drift into three different shapes.
struct LabeledField<Content: View>: View {
    let label: String
    let systemImage: String
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label)
                .font(.caption.weight(.semibold))
                .foregroundStyle(Theme.horizon)
                .textCase(.uppercase)
                .tracking(0.8)

            HStack(spacing: 10) {
                Image(systemName: systemImage)
                    .font(.system(size: 14))
                    .foregroundStyle(Theme.trail)
                    .frame(width: 18)
                content
                    .font(.body)
                    .foregroundStyle(Theme.ink)
            }
            .padding(.horizontal, Theme.spaceM)
            .frame(height: 50)
            .background(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .fill(Color.white.opacity(0.8))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .strokeBorder(Theme.cloud, lineWidth: 1)
            )
        }
    }
}
