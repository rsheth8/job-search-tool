import SwiftUI
import UniformTypeIdentifiers

/// Resume / GitHub / LinkedIn shortcuts used by the first-run quiz and You.
struct ProfileImportPanel: View {
    var demo: Bool = false
    var compact: Bool = false
    var startsCollapsed: Bool = false
    var onRetakeQuiz: (() -> Void)? = nil
    var onImported: (ImportResult) async -> Void

    @EnvironmentObject var config: Config
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var pickingResume = false
    @State private var githubOpen = false
    @State private var linkedinOpen = false
    @State private var busy: String?
    @State private var error: String?
    @State private var lastResult: ImportResult?
    @State private var expanded = true

    private var api: APIClient { APIClient(config: config) }

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.spaceM) {
            if compact {
                Button {
                    withAnimation(reduceMotion ? nil : Theme.springSoft) { expanded.toggle() }
                } label: {
                    Text(expanded ? "Hide import" : "Import résumé, GitHub, or LinkedIn")
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(Theme.accent)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())
                }
                .buttonStyle(PressableButtonStyle())
                .accessibilityLabel(expanded ? "Hide import" : "Import résumé, GitHub, or LinkedIn")
            } else {
                Text("Start from something you already have")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Theme.horizon)
                    .textCase(.uppercase)
                    .tracking(0.8)
            }

            if let lastResult {
                ImportSuccessCard(result: lastResult)
                    .id("\(lastResult.source)-\(lastResult.filled.joined())-\(lastResult.knowledge_added)")
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }

            if !compact || expanded {
                importList
            }

            if let error, !error.isEmpty {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(Theme.note)
                    .fixedSize(horizontal: false, vertical: true)
                    .transition(.opacity)
            }
        }
        .onAppear {
            if startsCollapsed { expanded = false }
        }
        .onChange(of: lastResult?.source) { _, src in
            if src != nil { expanded = true }
        }
        .animation(reduceMotion ? nil : Theme.springSoft, value: lastResult.map { "\($0.source)-\($0.filled.count)-\($0.knowledge_added)" } ?? "")
        .animation(reduceMotion ? nil : Theme.quick, value: busy)
        .animation(reduceMotion ? nil : Theme.quick, value: error)
        .animation(reduceMotion ? nil : Theme.springSoft, value: expanded)
        .fileImporter(
            isPresented: $pickingResume,
            allowedContentTypes: [.pdf, .plainText, .utf8PlainText],
            allowsMultipleSelection: false
        ) { result in
            switch result {
            case .success(let urls):
                guard let url = urls.first else { return }
                Task { await uploadResume(url) }
            case .failure:
                error = "Couldn't open that file."
            }
        }
        .sheet(isPresented: $githubOpen) {
            HandleSheet(
                title: "GitHub",
                prompt: "Username or github.com/you — we pull public repos when GitHub is reachable.",
                placeholder: "octocat",
                keyboard: .URL
            ) { handle in
                try await api.importGitHub(handle: handle)
            } onDone: { result in
                githubOpen = false
                Task { await finish(result) }
            }
        }
        .sheet(isPresented: $linkedinOpen) {
            LinkedInImportSheet { url, file in
                if let file {
                    return try await api.importLinkedIn(
                        url: url,
                        filename: file.filename,
                        data: file.data,
                        mime: file.mime
                    )
                }
                return try await api.importLinkedIn(url: url)
            } onDone: { result in
                linkedinOpen = false
                Task { await finish(result) }
            }
        }
    }

    private var importList: some View {
        GroupedSurface {
            importRow(
                title: "Upload a resume",
                subtitle: demo ? "Sample résumé — nothing is uploaded." : "PDF or text. A LinkedIn PDF works too.",
                icon: "doc.badge.arrow.up",
                key: "resume",
                index: 0
            ) {
                if demo { Task { await fakeImport("resume") } }
                else { pickingResume = true }
            }

            rowDivider

            importRow(
                title: "GitHub",
                subtitle: demo ? "Sample profile — nothing is fetched." : "Public profile and your top repos.",
                icon: "chevron.left.forwardslash.chevron.right",
                key: "github",
                index: 1
            ) {
                if demo { Task { await fakeImport("github") } }
                else { githubOpen = true }
            }

            rowDivider

            importRow(
                title: "LinkedIn",
                subtitle: demo
                    ? "Sample link — nothing is saved."
                    : "Paste the profile URL. LinkedIn blocks scraping — add a PDF to fill school and jobs.",
                icon: "link",
                key: "linkedin",
                index: 2
            ) {
                if demo { Task { await fakeImport("linkedin") } }
                else { linkedinOpen = true }
            }

            if onRetakeQuiz != nil {
                rowDivider
                quizRow
            }
        }
    }

    private var rowDivider: some View {
        Rectangle()
            .fill(Theme.cloud.opacity(0.45))
            .frame(height: 1)
            .padding(.leading, 64)
    }

    private var quizRow: some View {
        Button {
            onRetakeQuiz?()
        } label: {
            importRowLabel(
                title: "Profile quiz",
                subtitle: "Walk through remaining fields by hand.",
                icon: "list.bullet.clipboard",
                spinning: false
            )
        }
        .buttonStyle(PressableButtonStyle())
        .disabled(busy != nil)
        .opacity(busy != nil ? 0.45 : 1)
        .accessibilityLabel("Retake the profile quiz")
        .staggerAppear(3)
    }

    private func importRow(title: String, subtitle: String, icon: String,
                           key: String, index: Int, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            importRowLabel(
                title: title,
                subtitle: subtitle,
                icon: icon,
                spinning: busy == key
            )
        }
        .buttonStyle(PressableButtonStyle())
        .disabled(busy != nil)
        .opacity(busy != nil && busy != key ? 0.45 : 1)
        .staggerAppear(index)
    }

    private func importRowLabel(title: String, subtitle: String,
                                icon: String, spinning: Bool) -> some View {
        HStack(spacing: 12) {
            ZStack {
                Circle()
                    .fill(Theme.accent.opacity(spinning ? 0.18 : 0.12))
                if spinning {
                    PropellerIcon(speed: .medium, size: 14)
                        .foregroundStyle(Theme.accent)
                } else {
                    Image(systemName: icon)
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundStyle(Theme.accent)
                }
            }
            .frame(width: 36, height: 36)

            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(.body.weight(.medium))
                    .foregroundStyle(Theme.ink)
                Text(subtitle)
                    .font(.caption)
                    .foregroundStyle(Theme.soft)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            Image(systemName: "chevron.right")
                .font(.caption.weight(.semibold))
                .foregroundStyle(Theme.soft)
                .opacity(spinning ? 0 : 1)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .contentShape(Rectangle())
    }

    private func fakeImport(_ key: String) async {
        error = nil
        busy = key
        try? await Task.sleep(for: .milliseconds(420))
        busy = nil
        await finish(QuizDemo.importResult(source: key))
    }

    private func uploadResume(_ url: URL) async {
        error = nil
        busy = "resume"
        defer { busy = nil }
        let access = url.startAccessingSecurityScopedResource()
        defer { if access { url.stopAccessingSecurityScopedResource() } }
        do {
            let data = try Data(contentsOf: url)
            let ext = url.pathExtension.lowercased()
            let mime = ext == "pdf" ? "application/pdf" : "text/plain"
            let result = try await api.importResume(
                filename: url.lastPathComponent, data: data, mime: mime
            )
            await finish(result)
        } catch {
            self.error = APIClient.userMessage(for: error)
            if (self.error ?? "").isEmpty {
                self.error = "Couldn't read that resume."
            }
        }
    }

    @MainActor
    private func finish(_ result: ImportResult) async {
        error = nil
        lastResult = result
        Theme.notify(.success)
        await onImported(result)
    }
}

private struct ImportSuccessCard: View {
    let result: ImportResult
    @State private var shown = 0.0
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var fields: [String] { Array(result.filled.prefix(8)) }
    private var extra: Int { max(0, result.filled.count - fields.count) }

    var body: some View {
        FocusCard(prominent: true) {
            VStack(alignment: .leading, spacing: Theme.spaceM) {
                HStack(alignment: .center, spacing: 14) {
                    meter
                    VStack(alignment: .leading, spacing: 4) {
                        Text(headline)
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(Theme.ink)
                        if let note = result.note, !note.isEmpty {
                            Text(note)
                                .font(.caption)
                                .foregroundStyle(Theme.soft)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    Spacer(minLength: 0)
                }

                if !fields.isEmpty {
                    WrapHStack(spacing: 8, lineSpacing: 8) {
                        ForEach(Array(fields.enumerated()), id: \.offset) { i, key in
                            Text(prettyField(key))
                                .font(.caption.weight(.medium))
                                .foregroundStyle(Theme.accent)
                                .padding(.horizontal, 10)
                                .padding(.vertical, 5)
                                .background(Theme.accent.opacity(0.12), in: Capsule())
                                .staggerAppear(i)
                        }
                        if extra > 0 {
                            Text("+\(extra)")
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(Theme.horizon)
                                .padding(.horizontal, 10)
                                .padding(.vertical, 5)
                                .background(Theme.cloud.opacity(0.45), in: Capsule())
                                .staggerAppear(fields.count)
                        }
                    }
                }
            }
        }
        .onAppear { tick() }
        .onChange(of: result.identity_score) { _, _ in tick() }
    }

    @ViewBuilder
    private var meter: some View {
        if let score = result.identity_score {
            ZStack {
                SoftRing(progress: shown, size: 52, lineWidth: 4)
                Text("\(Int((shown * 100).rounded()))")
                    .font(.system(size: 13, weight: .bold, design: .rounded).monospacedDigit())
                    .foregroundStyle(Theme.accent)
                    .contentTransition(reduceMotion ? .identity : .numericText())
            }
            .accessibilityLabel("Coverage \(Int((score * 100).rounded())) percent")
        } else {
            Image(systemName: "checkmark")
                .font(.body.weight(.semibold))
                .foregroundStyle(Theme.accent)
                .frame(width: 52, height: 52)
                .background(Theme.accent.opacity(0.12), in: Circle())
                .accessibilityHidden(true)
        }
    }

    private var headline: String {
        let n = result.filled.count
        if n == 0 { return "Saved" }
        if n == 1 { return "Filled 1 field" }
        return "Filled \(n) fields"
    }

    private func tick() {
        let target = result.identity_score ?? 0
        if reduceMotion {
            shown = target
            return
        }
        shown = 0
        withAnimation(Theme.tick) { shown = target }
    }
}

private func prettyField(_ key: String) -> String {
    switch key {
    case "github": return "GitHub"
    case "linkedin": return "LinkedIn"
    case "gpa": return "GPA"
    case "zip": return "ZIP"
    case "over_18": return "Over 18"
    case "work_authorized": return "Work authorized"
    case "needs_sponsorship": return "Sponsorship"
    default:
        return key.replacingOccurrences(of: "_", with: " ").capitalized
    }
}

private struct HandleSheet: View {
    let title: String
    let prompt: String
    let placeholder: String
    var keyboard: UIKeyboardType = .default
    let submit: (String) async throws -> ImportResult
    let onDone: (ImportResult) -> Void

    @Environment(\.dismiss) private var dismiss
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @FocusState private var focused: Bool
    @State private var value = ""
    @State private var busy = false
    @State private var error: String?

    private var trimmed: String {
        value.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: Theme.spaceL) {
                PageHeader(
                    eyebrow: "Import",
                    title: title,
                    subtitle: prompt
                )

                FocusCard {
                    TextField(placeholder, text: $value)
                        .font(.body)
                        .foregroundStyle(Theme.ink)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(keyboard)
                        .focused($focused)
                        .submitLabel(.go)
                        .onSubmit { Task { await go() } }
                }
                .padding(.horizontal, Theme.spaceL)
                .instrumentEnter()

                if let error {
                    Text(error)
                        .font(.caption)
                        .foregroundStyle(Theme.note)
                        .padding(.horizontal, Theme.spaceL)
                        .transition(.opacity)
                }

                Spacer(minLength: 0)

                VStack(spacing: Theme.spaceM) {
                    PrimaryButton(
                        title: "Import",
                        busy: busy,
                        busyTitle: "Importing…"
                    ) {
                        Task { await go() }
                    }
                    .disabled(trimmed.isEmpty)
                    .opacity(trimmed.isEmpty ? 0.45 : 1)

                    Button("Cancel") { dismiss() }
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(Theme.soft)
                        .buttonStyle(PressableButtonStyle())
                        .disabled(busy)
                }
                .padding(.horizontal, Theme.spaceL)
                .padding(.bottom, Theme.spaceL)
            }
            .toolbar(.hidden, for: .navigationBar)
            .ambientScreen()
            .interactiveDismissDisabled(busy)
            .onAppear {
                if !reduceMotion {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) {
                        focused = true
                    }
                } else {
                    focused = true
                }
            }
        }
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
        .presentationCornerRadius(28)
        .presentationBackground(Theme.fog)
    }

    private func go() async {
        guard !trimmed.isEmpty, !busy else { return }
        busy = true
        defer { busy = false }
        do {
            let result = try await submit(trimmed)
            onDone(result)
        } catch {
            withAnimation(reduceMotion ? nil : Theme.quick) {
                self.error = APIClient.userMessage(for: error)
                if (self.error ?? "").isEmpty {
                    self.error = "Couldn't import that."
                }
            }
        }
    }
}

private struct LinkedInFile {
    let filename: String
    let data: Data
    let mime: String
}

private struct LinkedInImportSheet: View {
    let submit: (String, LinkedInFile?) async throws -> ImportResult
    let onDone: (ImportResult) -> Void

    @Environment(\.dismiss) private var dismiss
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @FocusState private var focused: Bool
    @State private var value = ""
    @State private var busy = false
    @State private var error: String?
    @State private var pickingPDF = false
    @State private var attached: LinkedInFile?

    private var trimmed: String {
        value.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var canImport: Bool {
        !trimmed.isEmpty || attached != nil
    }

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: Theme.spaceL) {
                PageHeader(
                    eyebrow: "Import",
                    title: "LinkedIn",
                    subtitle: "Paste your profile URL so Autofill can fill LinkedIn fields. LinkedIn doesn't let apps read the page — attach a PDF (More → Save to PDF) to fill school, jobs, and skills."
                )

                FocusCard {
                    TextField(text: $value,
                              prompt: Text(verbatim: "https://linkedin.com/in/…")) {
                        Text("LinkedIn URL")
                    }
                        .font(.body)
                        .foregroundStyle(Theme.ink)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                        .focused($focused)
                        .submitLabel(.go)
                        .onSubmit { Task { await go() } }
                }
                .padding(.horizontal, Theme.spaceL)

                Button {
                    pickingPDF = true
                } label: {
                    HStack(spacing: 10) {
                        Image(systemName: attached == nil ? "doc.badge.plus" : "checkmark.circle.fill")
                            .foregroundStyle(Theme.accent)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(attached == nil ? "Attach LinkedIn PDF" : attached!.filename)
                                .font(.subheadline.weight(.semibold))
                                .foregroundStyle(Theme.ink)
                            Text(attached == nil
                                 ? "Optional, but this is how school and jobs get filled."
                                 : "We’ll read this like a resume.")
                                .font(.caption)
                                .foregroundStyle(Theme.soft)
                        }
                        Spacer(minLength: 0)
                    }
                    .padding(.horizontal, 16)
                    .padding(.vertical, 12)
                    .background(Theme.cardFill, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                }
                .buttonStyle(PressableButtonStyle())
                .padding(.horizontal, Theme.spaceL)

                if let error {
                    Text(error)
                        .font(.caption)
                        .foregroundStyle(Theme.note)
                        .padding(.horizontal, Theme.spaceL)
                }

                Spacer(minLength: 0)

                VStack(spacing: Theme.spaceM) {
                    PrimaryButton(
                        title: "Import",
                        busy: busy,
                        busyTitle: "Importing…"
                    ) {
                        Task { await go() }
                    }
                    .disabled(!canImport)
                    .opacity(canImport ? 1 : 0.45)

                    Button("Cancel") { dismiss() }
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(Theme.soft)
                        .buttonStyle(PressableButtonStyle())
                        .disabled(busy)
                }
                .padding(.horizontal, Theme.spaceL)
                .padding(.bottom, Theme.spaceL)
            }
            .toolbar(.hidden, for: .navigationBar)
            .ambientScreen()
            .interactiveDismissDisabled(busy)
            .fileImporter(
                isPresented: $pickingPDF,
                allowedContentTypes: [.pdf, .plainText, .utf8PlainText],
                allowsMultipleSelection: false
            ) { result in
                switch result {
                case .success(let urls):
                    guard let url = urls.first else { return }
                    attach(url)
                case .failure:
                    error = "Couldn't open that file."
                }
            }
        }
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
        .presentationCornerRadius(28)
        .presentationBackground(Theme.fog)
    }

    private func attach(_ url: URL) {
        let access = url.startAccessingSecurityScopedResource()
        defer { if access { url.stopAccessingSecurityScopedResource() } }
        do {
            let data = try Data(contentsOf: url)
            let ext = url.pathExtension.lowercased()
            let mime = ext == "pdf" ? "application/pdf" : "text/plain"
            attached = LinkedInFile(filename: url.lastPathComponent, data: data, mime: mime)
            error = nil
        } catch {
            self.error = "Couldn't read that file."
        }
    }

    private func go() async {
        guard canImport, !busy else { return }
        busy = true
        defer { busy = false }
        do {
            let result = try await submit(trimmed, attached)
            onDone(result)
        } catch {
            withAnimation(reduceMotion ? nil : Theme.quick) {
                self.error = APIClient.userMessage(for: error)
                if (self.error ?? "").isEmpty {
                    self.error = "Couldn't import that."
                }
            }
        }
    }
}
