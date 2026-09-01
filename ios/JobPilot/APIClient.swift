import AuthenticationServices
import Foundation

/// Thin async client over the FastAPI backend. All the real work (matching, answers,
/// resume, tracking) lives there; the app is a mobile face over it.
struct APIClient {
    let config: Config

    enum APIError: Error { case badURL, http(Int), decode, message(String) }

    private static let session: URLSession = {
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = 30
        cfg.timeoutIntervalForResource = 60
        cfg.waitsForConnectivity = false
        return URLSession(configuration: cfg)
    }()

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
            case .badURL: return "Check the base URL in Settings → Advanced."
            case .http(401):
                return "Session expired. Sign in again."
            case .http(403):
                return "This beta is invite-only. Ask to have your Apple email added — if you hid it, send the privaterelay.appleid.com address."
            case .http(404):
                return "We couldn't find that. Pull to refresh, or go back and try again."
            case .http(408), .http(504):
                return "The request timed out. Try again."
            case .http(429):
                return "Too many requests. Wait a moment and try again."
            case .http(500), .http(502), .http(503):
                return "The server hit a snag. Try again — if it keeps happening, send feedback from Settings."
            case .http(let code): return "Server returned \(code). Try again, or send feedback from Settings."
            case .decode: return "Couldn't read the server response. Try again."
            case .message(let s): return s
            }
        }
        let ns = error as NSError
        if ns.domain == NSURLErrorDomain {
            switch ns.code {
            case NSURLErrorNotConnectedToInternet, NSURLErrorNetworkConnectionLost:
                return "No network connection. Check Wi-Fi or cellular and try again."
            case NSURLErrorTimedOut:
                return "The request timed out. Try again."
            case NSURLErrorCannotFindHost, NSURLErrorCannotConnectToHost,
                 NSURLErrorDNSLookupFailed:
                return "Couldn't reach the server. Try again in a moment."
            case NSURLErrorSecureConnectionFailed:
                return "Couldn't make a secure connection to the server."
            default:
                return "Couldn't reach the server. Try again."
            }
        }
        return "Something went wrong. Try again."
    }

    static func appleAuthMessage(_ error: Error) -> String {
        let ns = error as NSError
        guard ns.domain == ASAuthorizationError.errorDomain else {
            return userMessage(for: error)
        }
        switch ns.code {
        case ASAuthorizationError.canceled.rawValue: return ""
        case ASAuthorizationError.failed.rawValue:
            return "Apple sign-in failed. Try again."
        case ASAuthorizationError.invalidResponse.rawValue:
            return "Apple sent an invalid response. Try again."
        case ASAuthorizationError.notHandled.rawValue,
             ASAuthorizationError.unknown.rawValue:
            return "Apple sign-in didn't complete. Try again."
        default:
            return "Couldn't sign in with Apple. Try again."
        }
    }

    private func applyHeaders(_ req: inout URLRequest, requestId: String,
                              userAgent: String) {
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        req.setValue(userAgent, forHTTPHeaderField: "User-Agent")
        req.setValue(requestId, forHTTPHeaderField: "X-Request-Id")
        if !config.sessionToken.isEmpty {
            req.setValue("Bearer \(config.sessionToken)", forHTTPHeaderField: "Authorization")
        }
        if !config.token.isEmpty { req.setValue(config.token, forHTTPHeaderField: "X-Apply-Token") }
    }

    private func request(_ method: String, _ path: String,
                         body: [String: Any]? = nil) async throws -> Data {
        try Task.checkCancellation()
        guard let base = config.base,
              let resolved = URL(string: path, relativeTo: base)?.absoluteURL else {
            throw APIError.badURL
        }
        let requestId = Diagnostics.newRequestId()
        let userAgent = await MainActor.run { Diagnostics.userAgent }
        var req = URLRequest(url: resolved)
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        applyHeaders(&req, requestId: requestId, userAgent: userAgent)
        if let body { req.httpBody = try JSONSerialization.data(withJSONObject: body) }
        do {
            let (data, resp) = try await Self.session.data(for: req)
            try Task.checkCancellation()
            let http = resp as? HTTPURLResponse
            let status = http?.statusCode ?? 0
            let rid = http?.value(forHTTPHeaderField: "X-Request-Id") ?? requestId
            await Diagnostics.shared.record(path: path, status: status, requestId: rid)
            if let http, !(200..<300).contains(http.statusCode) {
                throw Self.httpError(http.statusCode, data: data, requestId: rid, path: path)
            }
            return data
        } catch {
            if Self.isCancellation(error) { throw error }
            if error is APIError { throw error }
            let msg = Self.userMessage(for: error)
            await Diagnostics.shared.record(
                path: path, status: 0, requestId: requestId, error: msg)
            throw error
        }
    }

    private static func httpError(_ code: Int, data: Data,
                                   requestId: String, path: String) -> APIError {
        let detail = jsonDetail(data)
        let err: APIError
        // Email sign-in / sign-up answer in sentences meant for the person
        // ("Wrong email or password.", "Try again in 15 minute(s).").
        // The generic table below would replace those with "Session expired"
        // and "Too many requests", which is worse and, for 401, a lie.
        if Self.isEmailAuthPath(path), let detail, !detail.isEmpty {
            err = .message(detail)
            let msg = userMessage(for: err)
            Task { @MainActor in
                Diagnostics.shared.record(
                    path: path, status: code, requestId: requestId, error: msg)
            }
            return err
        }
        switch code {
        case 401:
            // Sign-in 401 is almost never an expired session — it's usually
            // Apple's audience (bundle id) not listed in APPLE_CLIENT_IDS.
            if path.contains("/auth/apple") {
                err = .message(
                    "Couldn't verify Sign in with Apple. Set APPLE_CLIENT_IDS=com.rahil.jobpilot on Fly, wait for the machine to restart, then try again."
                )
            } else {
                err = .http(code)
            }
        case 429, 500, 502, 503, 504:
            err = .http(code)
        default:
            if let detail, !detail.isEmpty { err = .message(detail) }
            else { err = .http(code) }
        }
        let msg = userMessage(for: err)
        Task { @MainActor in
            Diagnostics.shared.record(
                path: path, status: code, requestId: requestId, error: msg)
        }
        return err
    }

    private static func jsonDetail(_ data: Data) -> String? {
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        if let detail = obj["detail"] as? String, !detail.isEmpty { return detail }
        return nil
    }

    private var encodedUser: String {
        config.user.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? config.user
    }

    /// Both halves of `/apply/data`: what's staged and ready to apply, and the top
    /// matches you could stage. Pass `refresh` to kick a discovery pass (quiz /
    /// pull-to-refresh) instead of re-reading an empty queue.
    func fetchData(refresh: Bool = false) async throws -> (
        queue: [QueueItem], matches: [QueueItem], searching: Bool
    ) {
        if refresh {
            _ = try? await request("POST", "/apply/discover", body: [:])
        }
        let data = try await request("GET", "/apply/data?user=\(encodedUser)")
        let decoded = try JSONDecoder().decode(QueueResponse.self, from: data)
        return (decoded.queue ?? [], decoded.queued ?? [], decoded.discovery?.searching ?? false)
    }

    /// Start a discovery pass. Quiz completion uses `force` so the first
    /// search isn't eaten by the cooldown from saving roles.
    func discover(force: Bool = false) async {
        _ = try? await request("POST", "/apply/discover", body: ["force": force])
    }

    func fetchApplications() async throws -> (apps: [FiledApplication], statuses: [String]) {
        let data = try await request("GET", "/apply/applications?user=\(encodedUser)")
        let decoded = try JSONDecoder().decode(ApplicationsResponse.self, from: data)
        return (decoded.applications ?? [], decoded.statuses ?? [])
    }

    /// The next batch of chips for a quiz field, minus what's already picked.
    ///
    /// Called again after every tap so the row refills instead of running out.
    /// Ranked server-side against the user's own profile, which is why this is
    /// a request and not a bundled list.
    func suggestions(field: String, chosen: [String],
                     limit: Int = 12) async throws -> SuggestionBatch {
        var items = [URLQueryItem(name: "user", value: config.user),
                     URLQueryItem(name: "field", value: field),
                     URLQueryItem(name: "limit", value: String(limit))]
        if !chosen.isEmpty {
            items.append(URLQueryItem(name: "chosen", value: chosen.joined(separator: ",")))
        }
        var comps = URLComponents()
        comps.queryItems = items
        // URLComponents leaves a literal "+" alone, and the server reads that
        // as a space — so "C++" arrives as "C" and gets offered right back.
        // Nothing here emits "+" as an encoding, so every one is a real plus.
        let query = (comps.percentEncodedQuery ?? "")
            .replacingOccurrences(of: "+", with: "%2B")
        let data = try await request("GET", "/apply/suggestions?\(query)")
        return try JSONDecoder().decode(SuggestionBatch.self, from: data)
    }

    /// Remove a filed application — the way back from a double-tap on Filed.
    func deleteApplication(id: Int) async throws {
        _ = try await request("POST", "/apply/applications/delete",
                              body: ["user": config.user, "application_id": id])
    }

    /// Move a filed application to another stage.
    @discardableResult
    func setApplicationStatus(id: Int, status: String) async throws -> FiledApplication? {
        let data = try await request("POST", "/apply/applications/status",
                                     body: ["user": config.user,
                                            "application_id": id, "status": status])
        return try? JSONDecoder().decode(ApplicationUpdateResponse.self, from: data).application
    }

    /// Correct the company or role. Omitted fields are left alone.
    @discardableResult
    func editApplication(id: Int, company: String?, role: String?) async throws -> FiledApplication? {
        var body: [String: Any] = ["user": config.user, "application_id": id]
        if let company { body["company"] = company }
        if let role { body["role"] = role }
        let data = try await request("POST", "/apply/applications/edit", body: body)
        return try? JSONDecoder().decode(ApplicationUpdateResponse.self, from: data).application
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

    /// Hide a posting for a while (default a week). It leaves Ready and matches
    /// until the snooze expires.
    func snooze(postingId: Int, days: Int = 7) async throws {
        _ = try await request("POST", "/apply/snooze",
                              body: ["user": config.user, "posting_id": postingId,
                                     "days": days])
    }

    /// Move a ready item to the front of the queue, or stage it if it isn't yet.
    func promote(postingId: Int) async throws {
        _ = try await request("POST", "/apply/promote",
                              body: ["user": config.user, "posting_id": postingId])
    }

    /// Persist drag-to-reorder. Pass only the list that changed.
    func reorder(queue: [Int]? = nil, matches: [Int]? = nil) async throws {
        var body: [String: Any] = ["user": config.user]
        if let queue { body["queue"] = queue }
        if let matches { body["matches"] = matches }
        _ = try await request("POST", "/apply/reorder", body: body)
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
                                            "platform": "ios",
                                            "timezone": TimeZone.current.identifier])
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
        try await downloadApplyPDF(path: "/apply/resume", postingId: postingId,
                                   fallbackName: "resume.pdf")
    }

    /// On-demand one-page cover letter. Not prefetched — built when you ask.
    func downloadCover(postingId: Int) async throws -> URL {
        try await downloadApplyPDF(path: "/apply/cover", postingId: postingId,
                                   fallbackName: "cover_letter.pdf")
    }

    private func downloadApplyPDF(path: String, postingId: Int,
                                  fallbackName: String) async throws -> URL {
        let u = config.user.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? config.user
        guard let base = config.base,
              let url = URL(string: "\(path)?user=\(u)&id=\(postingId)", relativeTo: base) else {
            throw APIError.badURL
        }
        let requestId = Diagnostics.newRequestId()
        let userAgent = await MainActor.run { Diagnostics.userAgent }
        var req = URLRequest(url: url)
        applyHeaders(&req, requestId: requestId, userAgent: userAgent)
        let (data, resp) = try await Self.session.data(for: req)
        guard let http = resp as? HTTPURLResponse else { throw APIError.decode }
        let rid = http.value(forHTTPHeaderField: "X-Request-Id") ?? requestId
        await Diagnostics.shared.record(path: path, status: http.statusCode, requestId: rid)
        guard (200..<300).contains(http.statusCode) else {
            throw Self.httpError(http.statusCode, data: data, requestId: rid, path: path)
        }
        var name = fallbackName
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

    /// Paths whose own error copy is better than the generic status table.
    static func isEmailAuthPath(_ path: String) -> Bool {
        path.hasPrefix("/auth/login")
            || path.hasPrefix("/auth/signup")
            || path.hasPrefix("/auth/password")
    }

    func authSignUp(email: String, password: String,
                    displayName: String?) async throws -> AuthSession {
        var body: [String: Any] = ["email": email, "password": password]
        if let displayName, !displayName.isEmpty { body["display_name"] = displayName }
        let data = try await request("POST", "/auth/signup", body: body)
        return try JSONDecoder().decode(AuthSession.self, from: data)
    }

    func authLogIn(email: String, password: String) async throws -> AuthSession {
        let data = try await request("POST", "/auth/login",
                                     body: ["email": email, "password": password])
        return try JSONDecoder().decode(AuthSession.self, from: data)
    }

    func authChangePassword(current: String, new: String) async throws {
        _ = try await request("POST", "/auth/password",
                              body: ["current_password": current, "new_password": new])
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

    func agentSend(text: String, action: String, slots: [String: Any]) async throws -> ChatSendResult {
        let data = try await request("POST", "/agent", body: [
            "raw_text": text,
            "action": action,
            "slots": slots,
        ])
        return try JSONDecoder().decode(ChatSendResult.self, from: data)
    }

    func chatClear() async throws {
        _ = try await request("POST", "/chat/clear", body: [:])
    }

    func fetchSetup() async throws -> SetupStatus {
        let data = try await request("GET", "/apply/setup")
        return try JSONDecoder().decode(SetupStatus.self, from: data)
    }

    func markSetup(action: String) async throws -> SetupStatus {
        let data = try await request("POST", "/apply/setup", body: ["action": action])
        return try JSONDecoder().decode(SetupStatus.self, from: data)
    }

    /// Partial update. Only the arguments you pass are sent, and the server leaves
    /// absent keys alone — so saving just a résumé summary can't blank out the
    /// locations and seniority a caller happened not to have in hand. Passing an
    /// empty string *does* clear that field; that's what the editors want.
    func saveProfile(
        roles: String? = nil,
        locations: String? = nil,
        seniority: String? = nil,
        keywords: String? = nil,
        resumeSummary: String? = nil
    ) async throws {
        var fields: [String: Any] = [:]
        if let roles { fields["roles"] = roles }
        if let locations { fields["locations"] = locations }
        if let seniority { fields["seniority"] = seniority }
        if let keywords {
            // Matching runs off keywords; an empty skills box falls back to roles
            // rather than leaving discovery with nothing to search on.
            let kw = keywords.trimmingCharacters(in: .whitespacesAndNewlines)
            fields["keywords"] = kw.isEmpty ? (roles ?? kw) : kw
        }
        if let resumeSummary {
            let summary = resumeSummary.trimmingCharacters(in: .whitespacesAndNewlines)
            if !summary.isEmpty { fields["resume_summary"] = summary }
        }
        guard !fields.isEmpty else { return }
        _ = try await request("POST", "/apply/profile", body: ["fields": fields])
    }

    func saveIdentity(fields: [String: Any]) async throws {
        _ = try await request("POST", "/apply/identity", body: ["fields": fields])
    }

    func importResume(filename: String, data: Data, mime: String) async throws -> ImportResult {
        try await multipart("/apply/import/resume", filename: filename, data: data, mime: mime)
    }

    func importGitHub(handle: String) async throws -> ImportResult {
        let data = try await request("POST", "/apply/import/github", body: ["username": handle])
        return try JSONDecoder().decode(ImportResult.self, from: data)
    }

    func importLinkedIn(url: String, filename: String? = nil, data: Data? = nil,
                        mime: String = "application/pdf") async throws -> ImportResult {
        if let data, let filename, !data.isEmpty {
            return try await multipart(
                "/apply/import/linkedin",
                filename: filename,
                data: data,
                mime: mime,
                fields: ["url": url]
            )
        }
        let data = try await request("POST", "/apply/import/linkedin", body: ["url": url])
        return try JSONDecoder().decode(ImportResult.self, from: data)
    }

    func fetchQuizDraft(polish: Bool = false) async throws -> QuizDraft {
        if polish {
            let data = try await request("POST", "/apply/quiz/draft", body: ["polish": true])
            return try JSONDecoder().decode(QuizDraftEnvelope.self, from: data).draft
        }
        let data = try await request("GET", "/apply/quiz/draft")
        return try JSONDecoder().decode(QuizDraftEnvelope.self, from: data).draft
    }

    private func multipart(_ path: String, filename: String, data file: Data,
                           mime: String, fields: [String: String] = [:]) async throws -> ImportResult {
        try Task.checkCancellation()
        guard let base = config.base,
              let resolved = URL(string: path, relativeTo: base)?.absoluteURL else {
            throw APIError.badURL
        }
        let requestId = Diagnostics.newRequestId()
        let userAgent = await MainActor.run { Diagnostics.userAgent }
        let boundary = "Boundary-\(UUID().uuidString)"
        var req = URLRequest(url: resolved)
        req.httpMethod = "POST"
        req.setValue("multipart/form-data; boundary=\(boundary)",
                     forHTTPHeaderField: "Content-Type")
        applyHeaders(&req, requestId: requestId, userAgent: userAgent)
        var body = Data()
        func put(_ s: String) { if let d = s.data(using: .utf8) { body.append(d) } }
        for (name, value) in fields where !value.isEmpty {
            put("--\(boundary)\r\n")
            put("Content-Disposition: form-data; name=\"\(name)\"\r\n\r\n")
            put("\(value)\r\n")
        }
        put("--\(boundary)\r\n")
        put("Content-Disposition: form-data; name=\"file\"; filename=\"\(filename)\"\r\n")
        put("Content-Type: \(mime)\r\n\r\n")
        body.append(file)
        put("\r\n--\(boundary)--\r\n")
        req.httpBody = body
        let (data, resp) = try await Self.session.data(for: req)
        try Task.checkCancellation()
        if let http = resp as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            let rid = http.value(forHTTPHeaderField: "X-Request-Id") ?? requestId
            throw Self.httpError(http.statusCode, data: data, requestId: rid, path: path)
        }
        return try JSONDecoder().decode(ImportResult.self, from: data)
    }

    func sendFeedback(_ body: String) async throws {
        let context = await MainActor.run { Diagnostics.shared.feedbackContext() }
        _ = try await request("POST", "/feedback", body: ["body": body, "context": context])
    }

    func fetchHealth() async throws -> HealthInfo {
        let data = try await request("GET", "/health")
        return try JSONDecoder().decode(HealthInfo.self, from: data)
    }

    /// Labels Fill skipped on a live form — grows the phrasing table on the server.
    func reportFillSkips(postingId: Int, url: String?, skips: [[String: Any]]) async throws {
        guard !skips.isEmpty else { return }
        var body: [String: Any] = [
            "user": config.user, "posting_id": postingId, "skips": skips,
        ]
        if let url, !url.isEmpty { body["url"] = url }
        _ = try await request("POST", "/apply/fill-skips", body: body)
    }
}
