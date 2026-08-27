import Foundation

/// The last queue + matches we successfully loaded, persisted across launches.
///
/// Applying happens on a phone, which means it happens on transit wifi, in a
/// building with one bar, and in the ten seconds before something else grabs your
/// attention. An empty spinner in any of those is the difference between applying
/// and not. Showing the last known list instantly — then quietly replacing it when
/// the fetch lands — costs nothing and removes that failure entirely.
///
/// Cached **per user id** so signing into a different account (or a one-off empty
/// Dev user) cannot wipe another account's last-known list.
enum QueueCache {
    private static func key(for user: String) -> String {
        let id = user.trimmingCharacters(in: .whitespacesAndNewlines)
        return id.isEmpty ? "apply.queue.cache" : "apply.queue.cache.\(id)"
    }

    private struct Payload: Codable {
        let queue: [QueueItem]
        let matches: [QueueItem]
        let savedAt: Date
    }

    static func save(queue: [QueueItem], matches: [QueueItem], user: String) {
        let payload = Payload(queue: queue, matches: matches, savedAt: Date())
        guard let data = try? JSONEncoder().encode(payload) else { return }
        UserDefaults.standard.set(data, forKey: key(for: user))
    }

    /// The cached lists, or nil. Deliberately has no expiry: a stale list is still
    /// a far better first paint than nothing, and it's replaced within a second.
    static func load(user: String) -> (queue: [QueueItem], matches: [QueueItem])? {
        let k = key(for: user)
        if let data = UserDefaults.standard.data(forKey: k),
           let payload = try? JSONDecoder().decode(Payload.self, from: data) {
            return (payload.queue, payload.matches)
        }
        // One-time fallthrough: pre-scoped cache from before per-user keys.
        if !user.isEmpty,
           let data = UserDefaults.standard.data(forKey: "apply.queue.cache"),
           let payload = try? JSONDecoder().decode(Payload.self, from: data),
           !(payload.queue.isEmpty && payload.matches.isEmpty) {
            save(queue: payload.queue, matches: payload.matches, user: user)
            return (payload.queue, payload.matches)
        }
        return nil
    }
}
