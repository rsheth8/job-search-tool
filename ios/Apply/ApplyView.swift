import SwiftUI

/// Loads the application package for a posting, then hands off to the in-app browser.
struct ApplyView: View {
    let item: QueueItem
    @EnvironmentObject var config: Config
    @State private var package: Package?
    @State private var error: String?

    var body: some View {
        Group {
            if let package {
                ApplyBrowser(item: item, package: package)
            } else if let error {
                ContentUnavailableView("Couldn't prepare this one", systemImage: "exclamationmark.triangle",
                                       description: Text(error))
            } else {
                ProgressView("Preparing your application…")
            }
        }
        .task {
            do { package = try await APIClient(config: config).fetchPackage(postingId: item.posting_id) }
            catch { self.error = "\(error)" }
        }
    }
}

/// The in-app apply browser: the real form, a one-tap Autofill, and "I applied".
private struct ApplyBrowser: View {
    let item: QueueItem
    let package: Package
    @EnvironmentObject var config: Config
    @Environment(\.dismiss) private var dismiss
    @StateObject private var model: WebViewModel
    @State private var toast: String?
    @State private var marking = false
    @State private var resumeDoc: ResumeDoc?
    @State private var fetchingResume = false

    init(item: QueueItem, package: Package) {
        self.item = item
        self.package = package
        _model = StateObject(wrappedValue: WebViewModel(
            identity: package.identity ?? [:], answers: package.questions ?? []))
    }

    var body: some View {
        ZStack(alignment: .bottom) {
            WebViewContainer(model: model).ignoresSafeArea(edges: .bottom)

            if !item.isFirstParty {
                VStack {
                    Label("Aggregator page — may require login. Autofill works best on the "
                          + "company's own Greenhouse/Lever/Ashby form.", systemImage: "person.badge.key")
                        .font(.caption).padding(8)
                        .frame(maxWidth: .infinity)
                        .background(.orange.opacity(0.15))
                    Spacer()
                }
            }

            if let toast {
                Text(toast)
                    .font(.subheadline.weight(.medium))
                    .padding(.horizontal, 14).padding(.vertical, 8)
                    .background(.ultraThinMaterial, in: Capsule())
                    .padding(.bottom, 76)
                    .transition(.opacity)
            }
            controls
        }
        .navigationTitle(item.company ?? "Apply")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear { model.load(package.url) }
        .onChange(of: model.lastFill?.filled) { showFillToast() }
        .sheet(item: $resumeDoc) { doc in ShareSheet(items: [doc.url]) }
    }

    private var controls: some View {
        HStack(spacing: 12) {
            Button { model.autofill() } label: {
                Label("Autofill", systemImage: "bolt.fill").frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)

            Button { fetchResume() } label: {
                Label(fetchingResume ? "…" : "Resume", systemImage: "doc.text")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.bordered)
            .disabled(fetchingResume)

            Button {
                marking = true
                Task {
                    try? await APIClient(config: config).markApplied(postingId: item.posting_id)
                    dismiss()
                }
            } label: {
                Label("I applied", systemImage: "checkmark").frame(maxWidth: .infinity)
            }
            .buttonStyle(.bordered)
            .disabled(marking)
        }
        .padding(12)
        .background(.ultraThinMaterial)
    }

    private func showFillToast() {
        guard let f = model.lastFill else { return }
        let more = f.essays > 0 ? " · \(f.essays) need you" : ""
        flashToast("Filled \(f.filled)\(more)")
    }

    /// Pull the tailored resume PDF and open the share sheet so it can be saved to
    /// Files; from there it's one tap to attach in the form's upload field.
    private func fetchResume() {
        fetchingResume = true
        Task {
            defer { fetchingResume = false }
            do { resumeDoc = ResumeDoc(url: try await APIClient(config: config).downloadResume(postingId: item.posting_id)) }
            catch APIClient.APIError.http(404) { flashToast("No tailored resume yet") }
            catch { flashToast("Couldn't fetch resume") }
        }
    }

    private func flashToast(_ text: String) {
        withAnimation { toast = text }
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
            withAnimation { toast = nil }
        }
    }
}

/// UIKit share sheet (Save to Files / AirDrop / …) for the tailored resume PDF.
struct ShareSheet: UIViewControllerRepresentable {
    let items: [Any]
    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: items, applicationActivities: nil)
    }
    func updateUIViewController(_ vc: UIActivityViewController, context: Context) {}
}

/// Wraps the downloaded resume URL so it can drive `.sheet(item:)`.
struct ResumeDoc: Identifiable { let id = UUID(); let url: URL }
