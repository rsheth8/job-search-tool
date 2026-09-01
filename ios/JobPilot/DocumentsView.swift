import QuickLook
import SwiftUI

/// The files you keep for applications. A transcript is the one nearly every
/// university-recruiting form asks for and nothing here could produce.
///
/// Nothing on this screen uploads. The copy says so plainly, because a promise
/// the user can't see is a promise they can't rely on.
struct DocumentsView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var docs: [LocalDocuments.StoredDocument] = []
    @State private var picking = false
    @State private var pickingKind: LocalDocuments.Kind = .transcript
    @State private var preview: URL?
    @State private var sharing: URL?
    @State private var error: String?

    var body: some View {
        NavigationStack {
            ScrollView(showsIndicators: false) {
                VStack(alignment: .leading, spacing: Theme.spaceM) {
                    Text("Kept on this phone. Never uploaded to JobPilot — "
                         + "they're in Files under On My iPhone → JobPilot, which "
                         + "is where a form's file picker can reach them.")
                        .font(.caption)
                        .foregroundStyle(Theme.soft)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.horizontal, Theme.spaceL)

                    if let error, !error.isEmpty {
                        InlineError(text: error) { self.error = nil }
                            .padding(.horizontal, Theme.spaceL)
                    }

                    if docs.isEmpty {
                        emptyState
                    } else {
                        GroupedSurface {
                            ForEach(docs) { doc in
                                row(doc)
                                if doc.id != docs.last?.id {
                                    Divider().background(Theme.accent.opacity(0.08))
                                }
                            }
                        }
                        .padding(.horizontal, Theme.spaceL)
                    }

                    addButtons
                        .padding(.horizontal, Theme.spaceL)
                }
                .padding(.vertical, Theme.spaceL)
            }
            .background(Theme.fog.ignoresSafeArea())
            .navigationTitle("Documents")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
        .onAppear { reload() }
        .fileImporter(isPresented: $picking,
                      allowedContentTypes: LocalDocuments.allowedTypes,
                      allowsMultipleSelection: false) { result in
            switch result {
            case .success(let urls):
                guard let url = urls.first else { return }
                add(url)
            case .failure:
                error = "Couldn't open that file."
            }
        }
        .quickLookPreview($preview)
        .sheet(item: $sharing) { url in
            ShareLink(item: url) { Text("Share \(url.lastPathComponent)") }
                .presentationDetents([.medium])
        }
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("No documents yet")
                .font(.subheadline.weight(.semibold))
            Text("Add your transcript once and it's there for every application "
                 + "that asks — most university-recruiting forms do.")
                .font(.caption)
                .foregroundStyle(Theme.soft)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(Theme.spaceL)
        .background(Theme.cardFill, in: RoundedRectangle(cornerRadius: 16))
        .padding(.horizontal, Theme.spaceL)
    }

    private var addButtons: some View {
        VStack(spacing: 8) {
            ForEach([LocalDocuments.Kind.transcript, .resume, .other], id: \.self) { kind in
                Button {
                    pickingKind = kind
                    picking = true
                } label: {
                    Label("Add a \(kind.label.lowercased())", systemImage: kind.icon)
                        .font(.subheadline.weight(.medium))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.vertical, 12)
                        .padding(.horizontal, 16)
                        .background(Theme.cardFill, in: RoundedRectangle(cornerRadius: 14))
                }
                .buttonStyle(PressableButtonStyle())
            }
        }
    }

    private func row(_ doc: LocalDocuments.StoredDocument) -> some View {
        Button {
            preview = LocalDocuments.url(for: doc)
        } label: {
            HStack(spacing: 12) {
                Image(systemName: doc.kind.icon)
                    .font(.body)
                    .foregroundStyle(Theme.accent)
                    .frame(width: 28)
                VStack(alignment: .leading, spacing: 2) {
                    Text(doc.displayName)
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(Theme.ink)
                        .lineLimit(1)
                    Text("\(doc.kind.label) · \(doc.sizeText)")
                        .font(.caption)
                        .foregroundStyle(Theme.soft)
                }
                Spacer(minLength: 8)
                Image(systemName: "chevron.right")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Theme.cloud)
            }
            .padding(.vertical, 10)
            .contentShape(Rectangle())
        }
        .buttonStyle(PressableButtonStyle())
        .contextMenu {
            Button {
                sharing = LocalDocuments.url(for: doc)
            } label: { Label("Share", systemImage: "square.and.arrow.up") }
            ForEach(LocalDocuments.Kind.allCases, id: \.self) { kind in
                if kind != doc.kind {
                    Button {
                        LocalDocuments.setKind(kind, for: doc)
                        reload()
                    } label: { Label("Mark as \(kind.label.lowercased())", systemImage: kind.icon) }
                }
            }
            Button(role: .destructive) {
                remove(doc)
            } label: { Label("Delete", systemImage: "trash") }
        }
        .accessibilityLabel("\(doc.displayName), \(doc.kind.label), \(doc.sizeText)")
    }

    private func reload() { docs = LocalDocuments.all() }

    private func add(_ url: URL) {
        // A picked file is security-scoped; the read fails silently without this.
        let access = url.startAccessingSecurityScopedResource()
        defer { if access { url.stopAccessingSecurityScopedResource() } }
        do {
            try LocalDocuments.importFile(from: url, kind: pickingKind)
            error = nil
            Theme.notify(.success)
            reload()
        } catch {
            self.error = "Couldn't save that file."
        }
    }

    private func remove(_ doc: LocalDocuments.StoredDocument) {
        do {
            try LocalDocuments.delete(doc)
            Theme.selection()
            reload()
        } catch {
            self.error = "Couldn't delete that file."
        }
    }
}

extension URL: @retroactive Identifiable {
    public var id: String { absoluteString }
}
