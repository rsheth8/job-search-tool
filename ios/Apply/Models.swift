import Foundation

/// One staged/queued match, from `GET /apply/data` (the `queue` array).
struct QueueItem: Codable, Identifiable, Hashable {
    let posting_id: Int
    let title: String?
    let company: String?
    let url: String?
    let score: Double?
    let source: String?
    let status: String?
    let auto_fillable: Bool?
    /// Why this surfaced, in words — a bare percentage can't be argued with.
    /// Computed server-side by `app/fit.py`.
    let why: String?
    let reasons: [String]?
    let concerns: [String]?

    var id: Int { posting_id }

    /// True when this is a first-party ATS form (Greenhouse/Lever/Ashby) the autofill
    /// can actually drive — vs. an aggregator (Built In, RSS, etc.) that's login-walled.
    /// Mirrors the backend's `app/ats.py`: trust the backend flag when present, else
    /// sniff the URL host. (The live deploy may not send `auto_fillable` yet.)
    var isFirstParty: Bool {
        if let f = auto_fillable { return f }
        return Self.looksLikeATS(url)
    }

    /// Sniff a URL (posting link *or* the page after redirects) for a known ATS host.
    /// Careers pages often redirect `careers.company.com` → `job-boards.greenhouse.io`,
    /// so the live WebView URL is more trustworthy than the stored posting URL.
    static func looksLikeATS(_ urlString: String?) -> Bool {
        let raw = urlString ?? ""
        let host = (URL(string: raw)?.host ?? "").lowercased()
        let atsHosts = ["greenhouse.io", "lever.co", "ashbyhq.com"]
        if atsHosts.contains(where: { host == $0 || host.hasSuffix("." + $0) }) { return true }
        // Greenhouse on a custom domain still carries a gh_jid query param.
        if raw.contains("gh_jid=") { return true }
        return false
    }
}

struct QueueResponse: Codable {
    let queued: [QueueItem]?   // top matches not yet staged
    let queue: [QueueItem]?    // staged, ready to apply
}

/// One application the submit worker is handling, from `GET /apply/inflight`.
struct InFlightRow: Codable, Identifiable, Hashable {
    let id: Int                  // posting id
    let label: String
    let state: String            // human-readable ("waiting on your approval")
    let awaiting: Bool           // true when it's the human's turn
    let request_id: Int?
    let status: String?          // pending | filling | preview | approved | submitting
    let preview: FillPreview?
}

/// What the worker filled, and what it left for you.
struct FillPreview: Codable, Hashable {
    let filled: [FilledField]?
    let skipped: [String]?
    let screenshot_url: String?
}

struct FilledField: Codable, Hashable {
    let label: String
    let value: String
}

/// One stored fact about you, from `GET /apply/knowledge`.
struct KnowledgeItem: Codable, Identifiable, Hashable {
    let id: Int
    let category: String
    let label: String?
    let text: String
}

/// How completely the assistant knows you — the lever on how much it can fill
/// without asking.
struct KnowledgeAudit: Codable, Hashable {
    let identity_have: [String]
    let identity_missing: [String]
    let suggestions: [String]
    let score: Double
}

struct KnowledgeResponse: Codable {
    let items: [KnowledgeItem]
    let audit: KnowledgeAudit
}

/// The field-matching rules, from `GET /apply/rules`.
///
/// This app used to carry its own hand-ported copy of these, which drifted behind
/// `app/fieldmatch.py` — including a narrower EEO list, so the phone would fill
/// demographic questions the backend refuses. Fetching them keeps every autofill
/// surface on one brain; `Autofill.lib` keeps a bundled copy only for offline.
struct RulesPayload: Codable, Equatable {
    let rules: [[String]]        // [[identity_key, regex_source], …], in priority order
    let never_fill: String       // labels never auto-filled (EEO / demographic)
    let flags: String?
    let version: String?
    /// Optional probe patterns from the backend; ignored by older app builds.
    let formprobe: FormProbePayload?
}

struct FormProbePayload: Codable, Equatable {
    let version: String?
    let advance: String?
    let submit: String?
}

/// One tailored question + drafted answer.
struct Question: Codable, Hashable {
    let question: String
    let answer: String
}

/// The full application package for one posting, from `POST /apply/package`.
struct Package: Codable {
    let posting_id: Int
    let url: String
    let company: String?
    let title: String?
    let questions: [Question]?
    let identity: [String: String]?   // applicant.autofill_map — label-fillable facts
}

// MARK: - Auth + chat

struct AuthUser: Codable {
    let id: String
    let email: String?
    let display_name: String?
}

struct AuthSession: Codable {
    let token: String
    let user: AuthUser
}

struct ChatMessage: Codable, Identifiable, Hashable {
    let id: Int
    let role: String
    let body: String
    let created_at: String?
}

struct ChatSendResult: Codable {
    let reply: String
    let user_message: ChatMessage?
    let assistant_message: ChatMessage?
}

struct SetupStatus: Codable {
    let complete: Bool
    let needs_setup: Bool
    let has_profile: Bool
    let identity_score: Double
    let identity_missing: [String]?
    let profile: [String: String]
    let identity: [String: String]
}
