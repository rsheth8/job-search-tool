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
    /// autofill | direct | browser — how this apply link should be labeled.
    let apply_kind: String?
    /// True for the five "apply these today" shortlist items.
    let apply_today: Bool?
    let fresh: Bool?
    /// Why this surfaced, in words — a bare percentage can't be argued with.
    /// Computed server-side by `app/fit.py`.
    let why: String?
    let reasons: [String]?
    let concerns: [String]?

    var id: Int { posting_id }

    /// True when this is a high-confidence Autofill host (Greenhouse/Lever/Ashby).
    /// Other public forms still get Fill after the page probe; this flag is for
    /// ranking and labels, not a hard gate.
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

    /// Honest label: Autofill (known ATS) vs Fill (company site / public form).
    var applyKindLabel: String {
        switch apply_kind {
        case "autofill": return "Autofill"
        case "direct": return "Fill"
        case "browser": return "Open & fill"
        default: return isFirstParty ? "Autofill" : "Open & fill"
        }
    }
}

struct ImportUrlResponse: Codable {
    let ok: Bool?
    let posting_id: Int?
    let title: String?
    let company: String?
    let apply_kind: String?
}

struct QueueResponse: Codable {
    let queued: [QueueItem]?   // top matches not yet staged
    let queue: [QueueItem]?    // staged, ready to apply
    let discovery: DiscoveryStatus?
}

struct DiscoveryStatus: Codable {
    let searching: Bool?
    let started_at: String?
    let last_finished_at: String?
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
    /// name/id patterns; optional so an old cache still decodes.
    let attr_rules: [[String]]?
    let autocomplete: [String: String]?
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

struct AgentConfirm: Codable, Hashable {
    let pending: Bool
}

struct ChatSendResult: Codable {
    let reply: String
    let user_message: ChatMessage?
    let assistant_message: ChatMessage?
    let suggestions: [String]?
    let deep_link: String?
    let confirm: AgentConfirm?
    let intent: String?
}

struct SetupStatus: Codable {
    let complete: Bool
    let needs_setup: Bool
    let has_profile: Bool
    let identity_score: Double
    let identity_missing: [String]?
    /// The handful Autofill cannot work without (name, email). Optional so an
    /// older build still decodes.
    let identity_core_missing: [String]?
    let identity_have: [String]?
    let onboarding: String?
    let profile: [String: String]
    let identity: [String: String]
    /// Optional so a build that predates the list still decodes a new server.
    let education: [EducationEntry]?
}

/// One degree. A bachelor's finishing while a master's is under way needs two
/// of these; the flat `identity` fields can only describe one.
struct EducationEntry: Codable, Identifiable, Hashable {
    var school = ""
    var degree = ""
    var discipline = ""
    var gpa = ""
    var start_year = ""
    var grad_month = ""
    var grad_year = ""
    /// "in_progress" | "completed" | "" (let the server infer it from the dates)
    var status = ""

    /// Local only. The server keys entries by position, and two degrees from
    /// the same school with the same name are a thing, so nothing in the
    /// payload is stable enough to identify a row.
    var id = UUID()

    enum CodingKeys: String, CodingKey {
        case school, degree, discipline, gpa, start_year, grad_month, grad_year, status
    }

    var isBlank: Bool {
        [school, degree, discipline, gpa, start_year, grad_year]
            .allSatisfy { $0.trimmingCharacters(in: .whitespaces).isEmpty }
    }

    var headline: String {
        let head = [degree, discipline].filter { !$0.isEmpty }.joined(separator: " ")
        if !head.isEmpty { return head }
        return school.isEmpty ? "New degree" : school
    }

    var subtitle: String {
        var bits: [String] = []
        if !school.isEmpty, !headline.hasPrefix(school) { bits.append(school) }
        if status == "in_progress" {
            bits.append(grad_year.isEmpty ? "in progress" : "expected \(grad_year)")
        } else if !grad_year.isEmpty {
            bits.append(grad_year)
        }
        if !gpa.isEmpty { bits.append("GPA \(gpa)") }
        return bits.joined(separator: " · ")
    }

    var payload: [String: String] {
        var out: [String: String] = [:]
        for (key, value) in [
            ("school", school), ("degree", degree), ("discipline", discipline),
            ("gpa", gpa), ("start_year", start_year), ("grad_month", grad_month),
            ("grad_year", grad_year), ("status", status),
        ] {
            let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty { out[key] = trimmed }
        }
        return out
    }
}

struct ImportResult: Codable {
    let ok: Bool?
    let source: String
    let filled: [String]
    let knowledge_added: Int
    let identity_score: Double?
    let note: String?
    let draft: QuizDraft?
}

struct QuizDraft: Codable {
    var project: String?
    var achievement: String?
    var strength: String?
    var preference: String?
    var about: String?
    var why_role: String?
    var roles: String?
    var locations: String?
    var keywords: String?
    var seniority: String?
}

struct QuizDraftEnvelope: Codable {
    let draft: QuizDraft
}

struct HealthInfo: Codable {
    let status: String?
    let db_ok: Bool?
    struct AuthFlags: Codable {
        let fail_open: Bool?
        let sentry: Bool?
        let dev_login: Bool?
    }
    struct BetaFlags: Codable {
        let invite_ready: Bool?
    }
    let auth: AuthFlags?
    let beta: BetaFlags?
}

/// One logged application, from `GET /apply/applications`.
struct FiledApplication: Codable, Identifiable, Hashable {
    let id: Int
    let company: String?
    let role: String?
    let status: String?
    let applied_at: String?
    let next_follow_up_at: String?
}

struct ApplicationsResponse: Codable {
    let applications: [FiledApplication]?
    /// Canonical stages, served with the list so the picker is not a second
    /// copy of a vocabulary that lives on the server.
    let statuses: [String]?
}

extension FiledApplication {
    /// Picker vocabulary for a server that predates `statuses` on the list
    /// response. Kept in step with `intents.CANONICAL_STATUSES`; the served
    /// list wins whenever there is one.
    static let defaultStages = ["Applied", "OA received", "Phone screen",
                                "Interview", "Onsite", "Offer", "Rejected",
                                "Ghosted"]
}

/// `{"ok": true, "application": {...}}` from the edit/status endpoints.
struct ApplicationUpdateResponse: Codable {
    let ok: Bool?
    let application: FiledApplication?
}

/// One refill of quiz chips. `remaining` is what's left in the catalog after
/// this batch — the app shows "more" only when there genuinely is more.
struct SuggestionBatch: Codable {
    let field: String?
    let suggestions: [String]?
    let remaining: Int?
    let known: Bool?
}
