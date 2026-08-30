import Foundation

/// Last-known-good field-matching rules, persisted across launches.
///
/// Three sources of rules, best first:
///   1. freshly fetched from `GET /apply/rules`
///   2. this cache — the last set that actually arrived
///   3. the copy bundled in `Autofill.lib`, generated from `app/fieldmatch.py`
///
/// The cache matters because the alternative to a live fetch isn't "no autofill",
/// it's "autofill with older rules" — and older rules are exactly what let this app
/// fill demographic fields the backend had already learned to refuse.
enum RulesCache {
    private static let key = "apply.rules.payload"

    static func save(_ payload: RulesPayload) {
        guard let data = try? JSONEncoder().encode(payload) else { return }
        UserDefaults.standard.set(data, forKey: key)
    }

    static func load() -> RulesPayload? {
        guard let data = UserDefaults.standard.data(forKey: key) else { return nil }
        return try? JSONDecoder().decode(RulesPayload.self, from: data)
    }

    /// Refresh in the background; a failure is fine, the cache or the bundled copy
    /// carries the page. Returns whatever we should use right now.
    @discardableResult
    static func refresh(using api: APIClient) async -> RulesPayload? {
        if let fresh = try? await api.fetchRules() { return fresh }
        return load()
    }
}
