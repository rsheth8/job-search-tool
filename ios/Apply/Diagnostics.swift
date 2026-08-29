import Foundation
import UIKit

/// Last-failure breadcrumb for testers. The human message stays on-screen;
/// this is what Settings copies and what **Send feedback** attaches.
@MainActor
final class Diagnostics: ObservableObject {
    static let shared = Diagnostics()

    @Published private(set) var lastRequestId: String = ""
    @Published private(set) var lastPath: String = ""
    @Published private(set) var lastStatus: Int = 0
    @Published private(set) var lastError: String = ""
    @Published private(set) var lastAt: Date?

    private var crumbs: [String] = []

    static var appVersion: String {
        let info = Bundle.main.infoDictionary
        let short = info?["CFBundleShortVersionString"] as? String ?? "0"
        let build = info?["CFBundleVersion"] as? String ?? "0"
        return "\(short) (\(build))"
    }

    static var userAgent: String {
        let os = UIDevice.current.systemVersion
        let model = UIDevice.current.model
        return "JobPilot/\(appVersion) (\(model); iOS \(os))"
    }

    nonisolated static func newRequestId() -> String {
        String(UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(12)).lowercased()
    }

    func record(path: String, status: Int, requestId: String, error: String? = nil) {
        lastPath = path
        lastStatus = status
        if !requestId.isEmpty { lastRequestId = requestId }
        lastAt = Date()
        if let error, !error.isEmpty { lastError = error }
        if status >= 400 || (error != nil && !(error ?? "").isEmpty) {
            let line = "\(status) \(path) \(requestId) \(error ?? "")"
                .trimmingCharacters(in: .whitespaces)
            crumbs.append(line)
            if crumbs.count > 12 { crumbs.removeFirst(crumbs.count - 12) }
        }
    }

    func recordError(_ message: String) {
        guard !message.isEmpty else { return }
        lastError = message
        lastAt = Date()
        crumbs.append(message)
        if crumbs.count > 12 { crumbs.removeFirst(crumbs.count - 12) }
    }

    var report: String {
        let uid = Config.shared.user
        let when = lastAt.map { ISO8601DateFormatter().string(from: $0) } ?? "—"
        var lines = [
            "JobPilot \(Self.appVersion)",
            "iOS \(UIDevice.current.systemVersion) · \(UIDevice.current.model)",
            "user \(uid.isEmpty ? "signed-out" : uid)",
            "base \(Config.shared.baseURL)",
            "last \(lastStatus) \(lastPath) rid=\(lastRequestId.isEmpty ? "—" : lastRequestId) @ \(when)",
        ]
        if !lastError.isEmpty { lines.append("error \(lastError)") }
        if !crumbs.isEmpty {
            lines.append("recent:")
            lines.append(contentsOf: crumbs.suffix(8).map { "  \($0)" })
        }
        return lines.joined(separator: "\n")
    }

    func feedbackContext() -> [String: Any] {
        [
            "app_version": Self.appVersion,
            "ios": UIDevice.current.systemVersion,
            "model": UIDevice.current.model,
            "user_id": Config.shared.user,
            "base_url": Config.shared.baseURL,
            "last_request_id": lastRequestId,
            "last_path": lastPath,
            "last_status": lastStatus,
            "last_error": lastError,
        ]
    }
}
