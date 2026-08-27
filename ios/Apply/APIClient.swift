import Foundation

/// Thin async client over the FastAPI backend. All the real work (matching, answers,
/// resume, tracking) lives there; the app is a mobile face over it.
struct APIClient {
    let config: Config

    enum APIError: Error { case badURL, http(Int), decode }

    /// SwiftUI `.task` / tab switches cancel in-flight URLSession work as `-999`.
    /// That is not a real failure — callers should ignore it instead of showing UI.
    static func isCancellation(_ error: Error) -> Bool {
        if error is CancellationError { return true }
        let ns = error as NSError
        if ns.domain == NSURLErrorDomain && ns.code == NSURLErrorCancelled { return true }
        if let url = error as? URLError, url.code == .cancelled { return true }
        return false
    }

    /// Short copy for empty/error states — never dump NSURLError UserInfo.
    static func userMessage(for error: Error) -> String {
        if isCancellation(error) { return "" }
        if let api = error as? APIError {
            switch api {
            case .badURL: return "Check the base URL in Settings."
            case .http(401):
                return "Sign in again (or check your API token in Settings)."
            case .http(403):
                return "This beta is invite-only. Ask to have your Apple email added."
            case .http(let code): return "Server returned \(code)."
            case .decode: return "Couldn't read the server response."
            }
        }
        let ns = error as NSError
        if ns.domain == NSURLErrorDomain {
            switch ns.code {
            case NSURLErrorNotConnectedToInternet, NSURLErrorNetworkConnectionLost:
                return "No network connection."
            case NSURLErrorTimedOut:
                return "The request timed out."
            case NSURLErrorCannotFindHost, NSURLErrorCannotConnectToHost,
                 NSURLErrorDNSLookupFailed:
                return "Couldn't reach the server."
            default:
                return "Couldn't reach the server."
            }
        }
        return "Something went wrong."
    }

    private func request(_ method: String, _ path: String,
                         body: [String: Any]? = nil) async throws -> Data {
        try Task.checkCancellation()
        guard let base = config.base,
              let resolved = URL(string: path, relativeTo: base)?.absoluteURL else {
            throw APIError.badURL
        }
        var req = URLRequest(url: resolved)
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if !config.sessionToken.isEmpty {
            req.setValue("Bearer \(config.sessionToken)", forHTTPHeaderField: "Authorization")
        }
        if !config.token.isEmpty { req.setValue(config.token, forHTTPHeaderField: "X-Apply-Token") }
        if let body { req.httpBody = try JSONSerialization.data(withJSONObject: body) }
        let (data, resp) = try await URLSession.shared.data(for: req)
        try Task.checkCancellation()
        if let http = resp as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            throw APIError.http(http.statusCode)
        }
        return data
    }

    private var encodedUser: String {
        config.user.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? config.user
    }

    /// Both halves of `/apply/data`: what's staged and ready to apply, and the top
    /// matches you could stage. The app used to fetch this and discard the second
    /// half, which is why staging had to happen in Slack first.
    func fetchData() async throws -> (queue: [QueueItem], matches: [QueueItem]) {
        let data = try await request("GET", "/apply/data?user=\(encodedUser)")
        let decoded = try JSONDecoder().decode(QueueResponse.self, from: data)
        return (decoded.queue ?? [], decoded.queued ?? [])
    }

    /// Stage a match so its application package gets prepared.
    func stage(postingId: Int) async throws {
        _ = try await request("POST", "/apply/stage",
                              body: ["user": config.user, "posting_id": postingId])
    }

    /// Unstage a ready item — it can show up in matches again later.
    func skipQueueItem(postingId: Int) async throws {
        _ = try await request("POST", "/apply/remove",
                              body: ["user": config.user, "posting_id": postingId])
    }

    /// Pass on a posting for good — leave the apply queue and mark it dismissed.
    func passPosting(postingId: Int) async throws {
        _ = try await request("POST", "/apply/pass",
                              body: ["user": config.user, "posting_id": postingId])
    }

    /// Applications the submit worker is currently handling.
    func fetchInFlight() async throws -> [InFlightRow] {
        struct Response: Codable { let inflight: [InFlightRow] }
        let data = try await request("GET", "/apply/inflight?user=\(encodedUser)")
        return try JSONDecoder().decode(Response.self, from: data).inflight
    }

    /// Approve a filled application so the worker may submit it. This is the human
    /// gate — the same one Slack and the web page use; nothing submits without it.
    func approve(requestId: Int) async throws {
        _ = try await request("POST", "/apply/request/approve",
                              body: ["user": config.user, "request_id": requestId])
    }

    func cancelRequest(requestId: Int) async throws {
        _ = try await request("POST", "/apply/request/cancel",
                              body: ["user": config.user, "request_id": requestId])
    }

    /// What the assistant knows about you, plus the coverage audit.
    func fetchKnowledge() async throws -> KnowledgeResponse {
        let data = try await request("GET", "/apply/knowledge?user=\(encodedUser)")
        return try JSONDecoder().decode(KnowledgeResponse.self, from: data)
    }

    /// Store one durable fact. `label` carries the question for a saved answer.
    func addKnowledge(category: String, text: String, label: String? = nil) async throws {
        var body: [String: Any] = ["user": config.user, "category": category, "text": text]
        if let label, !label.isEmpty { body["label"] = label }
        _ = try await request("POST", "/apply/knowledge", body: body)
    }

    func removeKnowledge(id: Int) async throws {
        _ = try await request("POST", "/apply/knowledge/remove",
                              body: ["user": config.user, "id": id])
    }

    /// Register this device for push. Returns whether the *server* has APNs
    /// credentials — false means notifications won't actually arrive yet, which the
    /// app says out loud rather than pretending it worked.
    @discardableResult
    func registerDevice(token: String) async throws -> Bool {
        struct Response: Codable { let ok: Bool; let configured: Bool }
        let data = try await request("POST", "/apply/device",
                                     body: ["user": config.user, "token": token,
                                            "platform": "ios"])
        return try JSONDecoder().decode(Response.self, from: data).configured
    }

    /// The field-matching rules. Cached on disk so a launch with no connection still
    /// autofills with the last known-good set rather than the older bundled copy.
    func fetchRules() async throws -> RulesPayload {
        let data = try await request("GET", "/apply/rules")
        let payload = try JSONDecoder().decode(RulesPayload.self, from: data)
        RulesCache.save(payload)
        return payload
    }

    /// The full package (url + identity + tailored answers) for one posting.
    func fetchPackage(postingId: Int) async throws -> Package {
        let data = try await request("POST", "/apply/package",
                                     body: ["user": config.user, "posting_id": postingId])
        do { return try JSONDecoder().decode(Package.self, from: data) }
        catch { throw APIError.decode }
    }

    /// Log a finished application (records it + marks the posting applied).
    func markApplied(postingId: Int) async throws {
        _ = try await request("POST", "/apply/applied",
                              body: ["user": config.user, "posting_id": postingId])
    }

    /// Download the tailored resume PDF to a temp file and return its URL, so it can
    /// be shared into Files and then picked in the form's upload field. Throws
    /// `.http(404)` when the backend has no tailored resume for this posting yet.
    func downloadResume(postingId: Int) async throws -> URL {
        let u = config.user.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? config.user
        guard let base = config.base,
              let url = URL(string: "/apply/resume?user=\(u)&id=\(postingId)", relativeTo: base) else {
            throw APIError.badURL
        }
        var req = URLRequest(url: url)
        if !config.sessionToken.isEmpty {
            req.setValue("Bearer \(config.sessionToken)", forHTTPHeaderField: "Authorization")
        }
        if !config.token.isEmpty { req.setValue(config.token, forHTTPHeaderField: "X-Apply-Token") }
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard let http = resp as? HTTPURLResponse else { throw APIError.decode }
        guard (200..<300).contains(http.statusCode) else { throw APIError.http(http.statusCode) }
        var name = "resume.pdf"
        if let cd = http.value(forHTTPHeaderField: "Content-Disposition"),
           let r = cd.range(of: "filename=\"") {
            let rest = cd[r.upperBound...]
            if let end = rest.firstIndex(of: "\"") { name = String(rest[..<end]) }
        }
        let dst = FileManager.default.temporaryDirectory.appendingPathComponent(name)
        try? FileManager.default.removeItem(at: dst)
        try data.write(to: dst)
        return dst
    }

    // MARK: Auth + chat

    func authApple(identityToken: String, email: String?, displayName: String?) async throws -> AuthSession {
        var body: [String: Any] = ["identity_token": identityToken]
        if let email, !email.isEmpty { body["email"] = email }
        if let displayName, !displayName.isEmpty { body["display_name"] = displayName }
        let data = try await request("POST", "/auth/apple", body: body)
        return try JSONDecoder().decode(AuthSession.self, from: data)
    }

    func authDev(displayName: String? = nil, userId: String? = nil) async throws -> AuthSession {
        var body: [String: Any] = [:]
        if let displayName { body["display_name"] = displayName }
        if let userId, !userId.isEmpty { body["user_id"] = userId }
        let data = try await request("POST", "/auth/dev", body: body)
        return try JSONDecoder().decode(AuthSession.self, from: data)
    }

    func authMe() async throws -> AuthUser {
        struct Response: Codable { let user: AuthUser }
        let data = try await request("GET", "/auth/me")
        return try JSONDecoder().decode(Response.self, from: data).user
    }

    func authLogout() async throws {
        _ = try await request("POST", "/auth/logout")
    }

    func chatHistory(limit: Int = 100) async throws -> [ChatMessage] {
        struct Response: Codable { let messages: [ChatMessage] }
        let data = try await request("GET", "/chat/history?limit=\(limit)")
        return try JSONDecoder().decode(Response.self, from: data).messages
    }

    func chatSend(_ text: String) async throws -> ChatSendResult {
        let data = try await request("POST", "/chat", body: ["text": text])
        return try JSONDecoder().decode(ChatSendResult.self, from: data)
    }

    func fetchSetup() async throws -> SetupStatus {
        let data = try await request("GET", "/apply/setup")
        return try JSONDecoder().decode(SetupStatus.self, from: data)
    }

    func saveProfile(roles: String, locations: String, seniority: String = "") async throws {
        _ = try await request("POST", "/apply/profile", body: [
            "fields": [
                "roles": roles,
                "keywords": roles,
                "locations": locations,
                "seniority": seniority,
            ],
        ])
    }

    func saveIdentity(fields: [String: Any]) async throws {
        _ = try await request("POST", "/apply/identity", body: ["fields": fields])
    }

    func sendFeedback(_ body: String) async throws {
        _ = try await request("POST", "/feedback", body: ["body": body])
    }
}
