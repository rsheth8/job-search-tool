import Foundation

/// Thin async client over the FastAPI backend. All the real work (matching, answers,
/// resume, tracking) lives there; the app is a mobile face over it.
struct APIClient {
    let config: Config

    enum APIError: Error { case badURL, http(Int), decode }

    private func request(_ method: String, _ path: String,
                         body: [String: Any]? = nil) async throws -> Data {
        guard let base = config.base, let url = URL(string: path, relativeTo: base) else {
            throw APIError.badURL
        }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if !config.token.isEmpty { req.setValue(config.token, forHTTPHeaderField: "X-Apply-Token") }
        if let body { req.httpBody = try JSONSerialization.data(withJSONObject: body) }
        let (data, resp) = try await URLSession.shared.data(for: req)
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
}
