import SwiftUI

/// Horizon — prose + action chips, not an SMS command bot.
/// On Apple Intelligence devices, phrasing is classified on-device; everything
/// else uses the free heuristic router via POST /chat.
struct ChatView: View {
    @EnvironmentObject var config: Config
    @EnvironmentObject var chrome: AppChrome
    @EnvironmentObject var push: PushManager
    @EnvironmentObject var setup: SetupGate
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    @State private var messages: [ChatMessage] = []
    @State private var draft = ""
    @State private var loading = false
    @State private var sending = false
    @State private var errorLabel: String?
    @State private var failedSend: String?
    @State private var suggestions: [String] = Self.defaultSuggestions
    @State private var confirmReset = false
    @State private var hopTask: Task<Void, Never>?
    @FocusState private var focused: Bool

    private static let defaultSuggestions = [
        "Show new jobs",
        "How do I autofill?",
        "What's missing?",
    ]

    private static let fallbackReply =
        "I didn’t fully catch that. Try “show new jobs”, “how do I autofill?”, or “change my phone to …”."

    /// Chip labels the user sees → what the engine actually understands.
    private static let chipUtterance: [String: String] = [
        "Skip": "skip",
        "Queue this": "apply",
        "Stop": "stop",
        "Yes": "yes",
        "Cancel": "cancel",
        "Open Apply": "open Apply",
        "Open You": "open You",
        "Open form details": "open form details",
        "Walk me through them": "review jobs",
        "Take me there": "take me there",
        "Not now": "not now",
    ]

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                header

                ScrollViewReader { proxy in
                    ScrollView(showsIndicators: false) {
                        LazyVStack(alignment: .leading, spacing: 0) {
                            if messages.isEmpty && !sending {
                                emptyIntro
                            }
                            ForEach(messages) { msg in
                                turn(msg)
                                    .padding(.bottom, Theme.spaceL)
                                    .id(msg.id)
                                    .transition(msg.role == "user" ? userInsert : horizonInsert)
                            }
                            if sending {
                                thinking
                                    .padding(.bottom, Theme.spaceM)
                                    .id("thinking")
                                    .transition(thinkingInsert)
                            }
                            if !suggestions.isEmpty && !sending {
                                chips
                                    .padding(.top, messages.isEmpty ? Theme.spaceS : Theme.spaceXS)
                                    .id("chips")
                                    .transition(chipClusterInsert)
                            }
                        }
                        .padding(.horizontal, Theme.spaceL)
                        .padding(.top, Theme.spaceS)
                        .padding(.bottom, Theme.spaceL)
                    }
                    .scrollDismissesKeyboard(.interactively)
                    .onChange(of: messages.count) { _, _ in
                        scrollToEnd(proxy)
                    }
                    .onChange(of: suggestions) { _, _ in
                        scrollToEnd(proxy)
                    }
                    .onChange(of: sending) { _, _ in
                        scrollToEnd(proxy)
                    }
                }

                if let errorLabel, !errorLabel.isEmpty {
                    InlineError(text: errorLabel, retryTitle: failedSend == nil ? "Try again" : "Retry send") {
                        Task {
                            if let failedSend {
                                await send(failedSend, echoUser: true)
                            } else {
                                await load()
                            }
                        }
                    }
                    .padding(.horizontal, Theme.spaceL)
                    .padding(.bottom, Theme.spaceXS)
                    .transition(.opacity)
                }

                composer
            }
            .toolbar(.hidden, for: .navigationBar)
            .ambientScreen()
            .instrumentEnter()
            .task { await load() }
            .onChange(of: focused) { _, on in
                chrome.dockHidden = on
            }
            .confirmationDialog("Clear this chat?", isPresented: $confirmReset,
                                titleVisibility: .visible) {
                Button("Clear chat", role: .destructive) {
                    Task { await resetChat() }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Removes the transcript. Jobs, identity, and the apply queue stay.")
            }
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 0) {
            PageHeader(
                eyebrow: "Horizon",
                title: "Ask",
                subtitle: "Find jobs, edit details, learn the app."
            ) {
                Button("Reset") { confirmReset = true }
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(Theme.horizon)
                    .buttonStyle(PressableButtonStyle())
                    .disabled(sending || loading)
            }
            Rectangle()
                .fill(Theme.cloud.opacity(0.55))
                .frame(height: 1)
                .padding(.horizontal, Theme.spaceL)
                .padding(.top, Theme.spaceS)
        }
        .padding(.bottom, Theme.spaceS)
    }

    private var emptyIntro: some View {
        Text(introCopy)
            .font(.body)
            .foregroundStyle(Theme.soft)
            .lineSpacing(3)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.top, Theme.spaceS)
            .padding(.bottom, Theme.spaceS)
    }

    private var greetingName: String {
        Voice.firstName(identity: setup.status?.identity, displayName: config.displayName)
    }

    private var introCopy: String {
        let hi = Voice.timeGreeting(name: greetingName)
        return "\(hi). Find jobs, change form details, or ask me to open any screen. You always tap Submit."
    }

    private var chips: some View {
        WrapHStack(spacing: 10, lineSpacing: 10) {
            ForEach(suggestions, id: \.self) { text in
                suggestion(text)
                    .transition(chipInsert)
            }
        }
        .animation(reduceMotion ? nil : Theme.springChip, value: suggestions)
    }

    private func scrollToEnd(_ proxy: ScrollViewProxy) {
        withAnimation(reduceMotion ? nil : Theme.springSoft) {
            if sending {
                proxy.scrollTo("thinking", anchor: .bottom)
            } else if !suggestions.isEmpty {
                proxy.scrollTo("chips", anchor: .bottom)
            } else if let last = messages.last {
                proxy.scrollTo(last.id, anchor: .bottom)
            }
        }
    }

    private var thinking: some View {
        HStack(spacing: 10) {
            PropellerIcon(speed: .medium, size: 15)
                .foregroundStyle(Theme.horizon)
            Text("Horizon is on it")
                .font(.subheadline)
                .foregroundStyle(Theme.soft)
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Horizon is thinking")
    }

    private var composer: some View {
        VStack(spacing: 0) {
            HStack(alignment: .bottom, spacing: 12) {
                TextField("Ask Horizon…", text: $draft, axis: .vertical)
                    .lineLimit(1...5)
                    .font(.body)
                    .padding(.horizontal, 16)
                    .padding(.vertical, 12)
                    .background(
                        RoundedRectangle(cornerRadius: 20, style: .continuous)
                            .fill(Theme.cardFill)
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: 20, style: .continuous)
                            .stroke(Theme.cloud.opacity(focused ? 0.9 : 0.45), lineWidth: 1)
                    )
                    .focused($focused)

                sendControl
            }
            .padding(.horizontal, Theme.spaceL)
            .padding(.top, 12)
            .padding(.bottom, 12)
            .paperBar()
            .overlay(alignment: .top) {
                Rectangle()
                    .fill(Theme.cloud.opacity(0.65))
                    .frame(height: 1)
            }

            if !focused {
                Color.clear.frame(height: Theme.toastClearance - 12)
            }
        }
        .animation(reduceMotion ? nil : Theme.springSoft, value: focused)
    }

    private var sendControl: some View {
        Button {
            Task { await send(draft, echoUser: true) }
        } label: {
            ZStack {
                Circle()
                    .fill(canSend || sending ? Theme.accent : Theme.cloud)
                    .frame(width: 44, height: 44)
                PropellerIcon(speed: sending ? .medium : .still, size: 16)
                    .foregroundStyle(canSend || sending ? Color.white : Theme.trail)
            }
            .scaleEffect(reduceMotion ? 1 : (canSend || sending ? 1 : 0.94))
        }
        .buttonStyle(PressableButtonStyle())
        .disabled(!canSend || sending)
        .accessibilityLabel("Send")
        .animation(reduceMotion ? nil : Theme.springSoft, value: canSend)
        .animation(reduceMotion ? nil : Theme.springSoft, value: sending)
    }

    private func suggestion(_ text: String) -> some View {
        Button {
            Task { await send(Self.chipUtterance[text] ?? text, echoUser: false) }
        } label: {
            Text(text)
                .font(.subheadline.weight(.medium))
                .foregroundStyle(Theme.accent)
                .padding(.horizontal, 16)
                .padding(.vertical, 11)
                .background(Theme.accent.opacity(0.10), in: Capsule())
                .overlay(
                    Capsule()
                        .stroke(Theme.accent.opacity(0.16), lineWidth: 1)
                )
        }
        .buttonStyle(PressableButtonStyle())
        .disabled(sending)
    }

    private var canSend: Bool {
        !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    @ViewBuilder
    private func turn(_ msg: ChatMessage) -> some View {
        if msg.role == "user" {
            HStack {
                Spacer(minLength: 56)
                Text(msg.body)
                    .font(.body)
                    .foregroundStyle(.white)
                    .padding(.horizontal, 16)
                    .padding(.vertical, 11)
                    .background(
                        Theme.accent,
                        in: RoundedRectangle(cornerRadius: 20, style: .continuous)
                    )
            }
        } else {
            assistantBody(msg.body)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.trailing, 20)
        }
    }

    private func assistantBody(_ body: String) -> some View {
        let blocks = body
            .components(separatedBy: "\n\n")
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        return VStack(alignment: .leading, spacing: 12) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, para in
                Text(para)
                    .font(.body)
                    .foregroundStyle(Theme.ink)
                    .lineSpacing(5)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // MARK: - Motion

    private var userInsert: AnyTransition {
        if reduceMotion { return .opacity }
        return .asymmetric(
            insertion: .scale(scale: 0.9, anchor: .bottomTrailing).combined(with: .opacity),
            removal: .opacity
        )
    }

    private var horizonInsert: AnyTransition {
        if reduceMotion { return .opacity }
        return .asymmetric(
            insertion: .opacity
                .combined(with: .offset(y: 12))
                .combined(with: .scale(scale: 0.98, anchor: .bottomLeading)),
            removal: .opacity.combined(with: .offset(y: -4))
        )
    }

    private var thinkingInsert: AnyTransition {
        if reduceMotion { return .opacity }
        return .asymmetric(
            insertion: .opacity.combined(with: .offset(y: 8)),
            removal: .opacity.combined(with: .offset(y: -6))
        )
    }

    private var chipClusterInsert: AnyTransition {
        if reduceMotion { return .opacity }
        return .asymmetric(
            insertion: .opacity.combined(with: .offset(y: 10)),
            removal: .opacity.combined(with: .offset(y: 6))
        )
    }

    private var chipInsert: AnyTransition {
        if reduceMotion { return .opacity }
        return .asymmetric(
            insertion: .scale(scale: 0.92, anchor: .leading).combined(with: .opacity),
            removal: .scale(scale: 0.96).combined(with: .opacity)
        )
    }

    private var motion: Animation? {
        reduceMotion ? nil : Theme.springHorizon
    }

    // MARK: - Network

    private func load() async {
        loading = true
        errorLabel = nil
        failedSend = nil
        defer { loading = false }
        do {
            let history = try await APIClient(config: config).chatHistory()
            var transaction = Transaction()
            transaction.disablesAnimations = true
            withTransaction(transaction) {
                messages = history
                suggestions = Self.defaultSuggestions
            }
        } catch {
            if APIClient.isCancellation(error) { return }
            errorLabel = APIClient.userMessage(for: error)
        }
    }

    private func resetChat() async {
        hopTask?.cancel()
        errorLabel = nil
        do {
            try await APIClient(config: config).chatClear()
            withAnimation(motion) {
                messages = []
                suggestions = Self.defaultSuggestions
                draft = ""
            }
            Theme.selection()
        } catch {
            if APIClient.isCancellation(error) { return }
            errorLabel = APIClient.userMessage(for: error)
        }
    }

    private func send(_ raw: String, echoUser: Bool) async {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        if echoUser { draft = "" }
        hopTask?.cancel()
        errorLabel = nil
        failedSend = nil
        let temp = ChatMessage(id: -Int(Date().timeIntervalSince1970),
                               role: "user", body: text, created_at: nil)
        if echoUser {
            withAnimation(motion) { messages.append(temp) }
        }
        withAnimation(reduceMotion ? nil : Theme.springChip) {
            sending = true
        }
        defer {
            if sending {
                sending = false
            }
        }
        do {
            let client = APIClient(config: config)
            let result: ChatSendResult
            if let turn = await AgentClassifier.classify(text), turn.shouldSendToAgent {
                result = try await client.agentSend(
                    text: text, action: turn.action, slots: turn.jsonSlots
                )
            } else {
                result = try await client.chatSend(text)
            }
            apply(result, replacing: echoUser ? temp : nil)
            followIfAccepted(result.deep_link)
            Theme.selection()
        } catch {
            if APIClient.isCancellation(error) { return }
            errorLabel = APIClient.userMessage(for: error)
            failedSend = text
            withAnimation(motion) { sending = false }
            if echoUser {
                withAnimation(Theme.quick) { messages.removeAll { $0.id == temp.id } }
                draft = text
            }
        }
    }

    private func apply(_ result: ChatSendResult, replacing temp: ChatMessage?) {
        let chips = result.suggestions ?? []
        withAnimation(motion) {
            sending = false
            if let temp, let user = result.user_message,
               let idx = messages.firstIndex(where: { $0.id == temp.id }) {
                messages[idx] = ChatMessage(
                    id: temp.id,
                    role: user.role,
                    body: user.body,
                    created_at: user.created_at
                )
            }
            if let assistant = result.assistant_message, !assistant.body.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                messages.append(assistant)
            } else {
                let body = result.reply.trimmingCharacters(in: .whitespacesAndNewlines)
                let nid = result.assistant_message?.id
                    ?? (temp?.id ?? -Int(Date().timeIntervalSince1970)) - 1
                messages.append(ChatMessage(
                    id: nid,
                    role: "assistant",
                    body: body.isEmpty ? Self.fallbackReply : body,
                    created_at: result.assistant_message?.created_at
                ))
            }
            suggestions = chips.isEmpty ? Self.defaultSuggestions : chips
        }
    }

    private func followIfAccepted(_ link: String?) {
        hopTask?.cancel()
        guard let link, !link.isEmpty else { return }
        let dest = link.split(separator: ":").first.map(String.init)?.lowercased() ?? link
        if dest == "chat" || dest == "ask" || dest == "assistant" { return }
        hopTask = Task { @MainActor in
            let wait: UInt64 = reduceMotion ? 40_000_000 : 380_000_000
            try? await Task.sleep(nanoseconds: wait)
            guard !Task.isCancelled else { return }
            AgentLocal.followDeepLink(link, push: push)
        }
    }
}
