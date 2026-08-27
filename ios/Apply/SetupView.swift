import SwiftUI

/// One-time setup after Sign in with Apple: search profile, identity, dossier.
struct SetupView: View {
    @EnvironmentObject var config: Config
    @EnvironmentObject var setup: SetupGate

    @State private var step = 0
    @State private var roles = ""
    @State private var locations = ""
    @State private var firstName = ""
    @State private var lastName = ""
    @State private var email = ""
    @State private var phone = ""
    @State private var city = ""
    @State private var state = ""
    @State private var school = ""
    @State private var degree = ""
    @State private var gradYear = ""
    @State private var linkedin = ""
    @State private var years = ""
    @State private var workAuthorized = true
    @State private var needsSponsorship = false
    @State private var project = ""
    @State private var about = ""
    @State private var busy = false
    @State private var error: String?

    private var api: APIClient { APIClient(config: config) }

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: Theme.spaceL) {
                PageHeader(
                    eyebrow: "Setup",
                    title: titles[step],
                    subtitle: subtitles[step]
                )
                .padding(.horizontal, Theme.spaceL)

                ScrollView(showsIndicators: false) {
                    VStack(alignment: .leading, spacing: Theme.spaceM) {
                        switch step {
                        case 0: profileFields
                        case 1: identityFields
                        default: knowledgeFields
                        }
                        if let error, !error.isEmpty {
                            Text(error)
                                .font(.caption)
                                .foregroundStyle(Theme.note)
                        }
                    }
                    .padding(.horizontal, Theme.spaceL)
                    .padding(.bottom, 120)
                }
            }
            .safeAreaInset(edge: .bottom) { bottomBar }
            .toolbar(.hidden, for: .navigationBar)
            .ambientScreen()
            .onAppear { prefill() }
        }
    }

    private var titles: [String] {
        ["What are you looking for?", "Who are you on a form?", "One thing they can cite"]
    }

    private var subtitles: [String] {
        [
            "Roles and places — this is how matches show up.",
            "Name, contact, school. Autofill uses these; skip anything you don't want stored.",
            "A project and a short “about me.” You can add more later in About.",
        ]
    }

    private var profileFields: some View {
        card {
            labeled("Roles") {
                TextField("New grad SWE, backend intern", text: $roles)
            }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("Locations") {
                TextField("NYC, remote, Chicago", text: $locations)
            }
        }
    }

    private var identityFields: some View {
        card {
            labeled("First name") { TextField("Ada", text: $firstName) }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("Last name") { TextField("Lovelace", text: $lastName) }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("Email") {
                TextField("you@school.edu", text: $email)
                    .keyboardType(.emailAddress)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
            }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("Phone") {
                TextField("555-0100", text: $phone).keyboardType(.phonePad)
            }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("City") { TextField("Chicago", text: $city) }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("State") { TextField("IL", text: $state) }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("School") { TextField("University", text: $school) }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("Degree") { TextField("B.S. Computer Science", text: $degree) }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("Graduation year") {
                TextField("2026", text: $gradYear).keyboardType(.numberPad)
            }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("LinkedIn URL") {
                TextField("https://linkedin.com/in/…", text: $linkedin)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
            }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("Years of experience") {
                TextField("0", text: $years).keyboardType(.numberPad)
            }
            Toggle("Authorized to work in the US", isOn: $workAuthorized)
                .font(.subheadline)
                .padding(.top, 4)
            Toggle("Need visa sponsorship", isOn: $needsSponsorship)
                .font(.subheadline)
        }
    }

    private var knowledgeFields: some View {
        card {
            labeled("A project worth citing") {
                TextField("I built …", text: $project, axis: .vertical)
                    .lineLimit(3...6)
            }
            Divider().background(Theme.accent.opacity(0.08))
            labeled("Tell us about yourself") {
                TextField("A short paragraph you reuse on applications", text: $about, axis: .vertical)
                    .lineLimit(4...8)
            }
        }
    }

    private var bottomBar: some View {
        HStack(spacing: Theme.spaceM) {
            if step > 0 {
                Button("Back") { withAnimation(Theme.quick) { step -= 1 } }
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Theme.accent)
            }
            Spacer()
            if step == 1 || step == 2 {
                Button("Skip") { Task { await skipOrNext() } }
                    .font(.subheadline)
                    .foregroundStyle(Theme.soft)
                    .disabled(busy)
            }
            Button {
                Task { await advance() }
            } label: {
                if busy {
                    ProgressView().controlSize(.small).tint(.white)
                } else {
                    Text(step < 2 ? "Continue" : "Done")
                        .font(.subheadline.weight(.semibold))
                }
            }
            .foregroundStyle(.white)
            .padding(.horizontal, 22)
            .padding(.vertical, 12)
            .background(canContinue ? Theme.accent : Theme.accent.opacity(0.4), in: Capsule())
            .disabled(busy || !canContinue)
        }
        .padding(.horizontal, Theme.spaceL)
        .padding(.vertical, Theme.spaceM)
        .background(.ultraThinMaterial)
    }

    private var canContinue: Bool {
        if step == 0 {
            return !roles.trimmingCharacters(in: .whitespaces).isEmpty
        }
        return true
    }

    private func labeled(_ title: String, @ViewBuilder field: () -> some View) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(.caption.weight(.semibold))
                .foregroundStyle(Theme.soft)
                .textCase(.uppercase)
                .tracking(0.5)
            field()
                .font(.subheadline)
                .foregroundStyle(Theme.ink)
        }
    }

    private func card<Content: View>(@ViewBuilder content: () -> Content) -> some View {
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
    }

    private func prefill() {
        guard let s = setup.status else { return }
        if roles.isEmpty { roles = s.profile["roles"] ?? "" }
        if locations.isEmpty { locations = s.profile["locations"] ?? "" }
        let id = s.identity
        if firstName.isEmpty { firstName = id["first_name"] ?? "" }
        if lastName.isEmpty { lastName = id["last_name"] ?? "" }
        if email.isEmpty { email = id["email"] ?? "" }
        if phone.isEmpty { phone = id["phone"] ?? "" }
        if city.isEmpty { city = id["city"] ?? "" }
        if state.isEmpty { state = id["state"] ?? "" }
        if school.isEmpty { school = id["school"] ?? "" }
        if degree.isEmpty { degree = id["degree"] ?? "" }
        if gradYear.isEmpty { gradYear = id["grad_year"] ?? "" }
        if linkedin.isEmpty { linkedin = id["linkedin"] ?? "" }
        if years.isEmpty { years = id["years_experience"] ?? "" }
    }

    private func skipOrNext() async {
        if step < 2 {
            withAnimation(Theme.quick) { step += 1 }
            return
        }
        await finish(skipKnowledge: true)
    }

    private func advance() async {
        error = nil
        busy = true
        defer { busy = false }
        do {
            if step == 0 {
                try await api.saveProfile(roles: roles, locations: locations)
                withAnimation(Theme.quick) { step = 1 }
                return
            }
            if step == 1 {
                try await api.saveIdentity(fields: identityPayload())
                withAnimation(Theme.quick) { step = 2 }
                return
            }
            await finish(skipKnowledge: false)
        } catch {
            self.error = APIClient.userMessage(for: error)
        }
    }

    private func finish(skipKnowledge: Bool) async {
        busy = true
        defer { busy = false }
        do {
            if !skipKnowledge {
                let p = project.trimmingCharacters(in: .whitespacesAndNewlines)
                if !p.isEmpty {
                    try await api.addKnowledge(category: "project", text: p)
                }
                let a = about.trimmingCharacters(in: .whitespacesAndNewlines)
                if !a.isEmpty {
                    try await api.addKnowledge(
                        category: "answer",
                        text: a,
                        label: "Tell us about yourself"
                    )
                }
            }
            Theme.notify(.success)
            await setup.refresh(config: config)
            setup.needsSetup = false
        } catch {
            self.error = APIClient.userMessage(for: error)
        }
    }

    private func identityPayload() -> [String: Any] {
        [
            "first_name": firstName,
            "last_name": lastName,
            "email": email,
            "phone": phone,
            "city": city,
            "state": state,
            "school": school,
            "degree": degree,
            "grad_year": gradYear,
            "linkedin": linkedin,
            "years_experience": years,
            "work_authorized": workAuthorized,
            "needs_sponsorship": needsSponsorship,
        ]
    }
}
