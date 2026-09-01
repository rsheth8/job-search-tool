import Foundation
import UniformTypeIdentifiers

/// Files kept for applications — a transcript, mostly — held on this phone and
/// nowhere else.
///
/// Device-only was a deliberate choice, so there is deliberately no upload path
/// in this file: it imports Foundation and nothing else, has no `APIClient`, no
/// `URLSession`, and no reference to `Config`. A transcript carries a school, a
/// student number, a GPA and every grade someone has ever received. The safest
/// place for that is the one place it already has to be.
///
/// Files live in the app's Documents directory, which `UIFileSharingEnabled`
/// and `LSSupportsOpeningDocumentsInPlace` expose in Files under
/// "On My iPhone → JobPilot". That exposure is the point rather than a leak: a
/// web form's file picker cannot reach into our container, so a transcript that
/// Files can't see is a transcript you can't actually attach to an application.
///
/// The directory is the source of truth and is rescanned every time, so a PDF
/// the user drops in from Files shows up here too. UserDefaults holds only the
/// label we can't infer from a filename.
enum LocalDocuments {
    enum Kind: String, Codable, CaseIterable {
        case transcript, resume, coverLetter, other

        var label: String {
            switch self {
            case .transcript: return "Transcript"
            case .resume: return "Résumé"
            case .coverLetter: return "Cover letter"
            case .other: return "Other"
            }
        }

        var icon: String {
            switch self {
            case .transcript: return "graduationcap"
            case .resume: return "doc.text"
            case .coverLetter: return "envelope"
            case .other: return "paperclip"
            }
        }
    }

    struct StoredDocument: Identifiable, Equatable {
        /// The filename on disk — stable, unique within the folder, and what
        /// the user sees in Files. Using it as the id means a file renamed
        /// outside the app is a new row rather than a broken one.
        var id: String { filename }
        let filename: String
        let kind: Kind
        let bytes: Int
        let addedAt: Date

        var displayName: String {
            (filename as NSString).deletingPathExtension
        }

        var sizeText: String {
            ByteCountFormatter.string(fromByteCount: Int64(bytes), countStyle: .file)
        }
    }

    static let allowedTypes: [UTType] = [.pdf, .plainText, .utf8PlainText, .rtf, .image]

    private static let metaKey = "documents.kinds"

    static var folder: URL {
        let base = FileManager.default.urls(for: .documentDirectory,
                                            in: .userDomainMask)[0]
        if !FileManager.default.fileExists(atPath: base.path) {
            try? FileManager.default.createDirectory(at: base,
                                                     withIntermediateDirectories: true)
        }
        return base
    }

    /// Everything in the folder, newest first. Hidden files and our own
    /// bookkeeping are skipped; anything else the user put there is theirs.
    static func all() -> [StoredDocument] {
        let keys: [URLResourceKey] = [.fileSizeKey, .contentModificationDateKey,
                                      .isDirectoryKey]
        let urls = (try? FileManager.default.contentsOfDirectory(
            at: folder, includingPropertiesForKeys: keys,
            options: [.skipsHiddenFiles])) ?? []
        let kinds = storedKinds()
        return urls.compactMap { url -> StoredDocument? in
            let values = try? url.resourceValues(forKeys: Set(keys))
            if values?.isDirectory == true { return nil }
            let name = url.lastPathComponent
            return StoredDocument(
                filename: name,
                kind: kinds[name] ?? inferredKind(for: name),
                bytes: values?.fileSize ?? 0,
                addedAt: values?.contentModificationDate ?? .distantPast
            )
        }
        .sorted { $0.addedAt > $1.addedAt }
    }

    static func url(for doc: StoredDocument) -> URL {
        folder.appendingPathComponent(doc.filename)
    }

    /// Copy a picked file in. The source is security-scoped when it comes from
    /// the document picker, which is the caller's job to open.
    @discardableResult
    static func importFile(from source: URL, kind: Kind) throws -> StoredDocument {
        let target = uniqueURL(for: source.lastPathComponent)
        let data = try Data(contentsOf: source)
        try data.write(to: target, options: [.atomic, .completeFileProtection])
        setKind(kind, forFilename: target.lastPathComponent)
        let values = try? target.resourceValues(
            forKeys: [.fileSizeKey, .contentModificationDateKey])
        return StoredDocument(filename: target.lastPathComponent, kind: kind,
                              bytes: values?.fileSize ?? data.count,
                              addedAt: values?.contentModificationDate ?? Date())
    }

    static func delete(_ doc: StoredDocument) throws {
        try FileManager.default.removeItem(at: url(for: doc))
        var kinds = storedKinds()
        kinds.removeValue(forKey: doc.filename)
        write(kinds)
    }

    static func setKind(_ kind: Kind, for doc: StoredDocument) {
        setKind(kind, forFilename: doc.filename)
    }

    // MARK: - private

    private static func setKind(_ kind: Kind, forFilename name: String) {
        var kinds = storedKinds()
        kinds[name] = kind
        write(kinds)
    }

    private static func storedKinds() -> [String: Kind] {
        guard let raw = UserDefaults.standard.dictionary(forKey: metaKey) as? [String: String]
        else { return [:] }
        return raw.compactMapValues(Kind.init(rawValue:))
    }

    private static func write(_ kinds: [String: Kind]) {
        UserDefaults.standard.set(kinds.mapValues(\.rawValue), forKey: metaKey)
    }

    /// A file dropped in from Files has no label of ours. The name is usually
    /// enough, and a wrong guess is one tap to fix.
    private static func inferredKind(for name: String) -> Kind {
        let low = name.lowercased()
        if low.contains("transcript") || low.contains("grades") { return .transcript }
        if low.contains("resume") || low.contains("résumé") || low.contains("cv") {
            return .resume
        }
        if low.contains("cover") { return .coverLetter }
        return .other
    }

    /// "transcript.pdf" twice gives "transcript.pdf" and "transcript 2.pdf" —
    /// never a silent overwrite of the copy already there.
    private static func uniqueURL(for filename: String) -> URL {
        let safe = filename.replacingOccurrences(of: "/", with: "-")
        let stem = (safe as NSString).deletingPathExtension
        let ext = (safe as NSString).pathExtension
        var candidate = folder.appendingPathComponent(safe)
        var n = 2
        while FileManager.default.fileExists(atPath: candidate.path) {
            let next = ext.isEmpty ? "\(stem) \(n)" : "\(stem) \(n).\(ext)"
            candidate = folder.appendingPathComponent(next)
            n += 1
        }
        return candidate
    }
}
