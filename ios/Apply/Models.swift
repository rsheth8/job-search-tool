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

    var id: Int { posting_id }

    /// True when this is a first-party ATS form (Greenhouse/Lever/Ashby) the autofill
    /// can actually drive — vs. an aggregator (Built In, RSS, etc.) that's login-walled.
    /// Mirrors the backend's `app/ats.py`: trust the backend flag when present, else
    /// sniff the URL host. (The live deploy may not send `auto_fillable` yet.)
    var isFirstParty: Bool {
        if let f = auto_fillable { return f }
        let host = (url.flatMap { URL(string: $0) }?.host ?? "").lowercased()
        let atsHosts = ["greenhouse.io", "lever.co", "ashbyhq.com"]
        if atsHosts.contains(where: { host == $0 || host.hasSuffix("." + $0) }) { return true }
        // Greenhouse on a custom domain still carries a gh_jid query param.
        if (url ?? "").contains("gh_jid=") { return true }
        return false
    }
}

struct QueueResponse: Codable {
    let queued: [QueueItem]?   // top matches not yet staged
    let queue: [QueueItem]?    // staged, ready to apply
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
