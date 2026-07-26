import Foundation

/// The last queue + matches we successfully loaded, persisted across launches.
///
/// Applying happens on a phone, which means it happens on transit wifi, in a
/// building with one bar, and in the ten seconds before something else grabs your
/// attention. An empty spinner in any of those is the difference between applying
/// and not. Showing the last known list instantly — then quietly replacing it when
/// the fetch lands — costs nothing and removes that failure entirely.
enum QueueCache {
    private static let key = "apply.queue.cache"

    private struct Payload: Codable {
        let queue: [QueueItem]
        let matches: [QueueItem]
        let savedAt: Date
    }

    static func save(queue: [QueueItem], matches: [QueueItem]) {
        let payload = Payload(queue: queue, matches: matches, savedAt: Date())
        guard let data = try? JSONEncoder().encode(payload) else { return }
        UserDefaults.standard.set(data, forKey: key)
    }

    /// The cached lists, or nil. Deliberately has no expiry: a stale list is still
    /// a far better first paint than nothing, and it's replaced within a second.
    static func load() -> (queue: [QueueItem], matches: [QueueItem])? {
        guard let data = UserDefaults.standard.data(forKey: key),
              let payload = try? JSONDecoder().decode(Payload.self, from: data)
        else { return nil }
        return (payload.queue, payload.matches)
    }
}
