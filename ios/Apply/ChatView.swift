import SwiftUI

/// Secondary chat surface — the same assistant that used to live in Slack.
struct ChatView: View {
    @EnvironmentObject var config: Config
    @EnvironmentObject var chrome: AppChrome

    @State private var messages: [ChatMessage] = []
    @State private var draft = ""
    @State private var loading = false
    @State private var sending = false
    @State private var errorLabel: String?
    @FocusState private var focused: Bool

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                ScrollViewReader { proxy in
                    ScrollView(showsIndicators: false) {
                        LazyVStack(alignment: .leading, spacing: 12) {
                            if messages.isEmpty && !loading {
                                Text("Ask about matches, applications, reminders — or just say hi.")
                                    .font(.subheadline)
                                    .foregroundStyle(Theme.soft)
                                    .padding(.top, Theme.spaceL)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                            ForEach(messages) { msg in
                                bubble(msg)
                                    .id(msg.id)
                            }
                        }
                        .padding(.horizontal, Theme.spaceL)
                        .padding(.bottom, 12)
                    }
                    .onChange(of: messages.count) { _, _ in
                        if let last = messages.last {
                            withAnimation(Theme.quick) {
                                proxy.scrollTo(last.id, anchor: .bottom)
                            }
                        }
                    }
                }

                if let errorLabel, !errorLabel.isEmpty {
                    Text(errorLabel)
                        .font(.caption)
                        .foregroundStyle(Theme.note)
                        .padding(.horizontal, Theme.spaceL)
                }

                composer
            }
            .ambientScreen()
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) {
                    Text("Chat")
                        .font(.headline.weight(.semibold))
                        .foregroundStyle(Theme.ink)
                }
            }
            .task { await load() }
            .onChange(of: focused) { _, on in
                chrome.dockHidden = on
            }
        }
    }

    private var composer: some View {
        HStack(alignment: .bottom, spacing: 10) {
            TextField("Message…", text: $draft, axis: .vertical)
                .lineLimit(1...5)
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(
                    RoundedRectangle(cornerRadius: 16, style: .continuous)
                        .fill(Color.white.opacity(0.9))
                )
                .focused($focused)

            Button {
                Task { await send() }
            } label: {
                Image(systemName: "arrow.up.circle.fill")
                    .font(.system(size: 32))
                    .foregroundStyle(canSend ? Theme.accent : Theme.soft)
            }
            .disabled(!canSend || sending)
        }
        .padding(.horizontal, Theme.spaceL)
        .padding(.vertical, 10)
        .padding(.bottom, 72) // clear floating dock
        .background(.ultraThinMaterial)
    }

    private var canSend: Bool {
        !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    @ViewBuilder
    private func bubble(_ msg: ChatMessage) -> some View {
        HStack {
            if msg.role == "user" { Spacer(minLength: 40) }
            Text(msg.body)
                .font(.subheadline)
                .foregroundStyle(msg.role == "user" ? Color.white : Theme.ink)
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background {
                    RoundedRectangle(cornerRadius: 16, style: .continuous)
                        .fill(msg.role == "user" ? Theme.accent : Color.white.opacity(0.92))
                }
            if msg.role != "user" { Spacer(minLength: 40) }
        }
    }

    private func load() async {
        loading = true
        errorLabel = nil
        defer { loading = false }
        do {
            messages = try await APIClient(config: config).chatHistory()
        } catch {
            if APIClient.isCancellation(error) { return }
            errorLabel = APIClient.userMessage(for: error)
        }
    }

    private func send() async {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        draft = ""
        sending = true
        errorLabel = nil
        defer { sending = false }
        // Optimistic user bubble
        let temp = ChatMessage(id: -Int(Date().timeIntervalSince1970),
                               role: "user", body: text, created_at: nil)
        messages.append(temp)
        do {
            let result = try await APIClient(config: config).chatSend(text)
            if let user = result.user_message {
                if let idx = messages.firstIndex(where: { $0.id == temp.id }) {
                    messages[idx] = user
                }
            }
            if let assistant = result.assistant_message {
                messages.append(assistant)
            } else if !result.reply.isEmpty {
                messages.append(ChatMessage(id: temp.id - 1, role: "assistant",
                                            body: result.reply, created_at: nil))
            }
            Theme.selection()
        } catch {
            if APIClient.isCancellation(error) { return }
            errorLabel = APIClient.userMessage(for: error)
            messages.removeAll { $0.id == temp.id }
            draft = text
        }
    }
}
