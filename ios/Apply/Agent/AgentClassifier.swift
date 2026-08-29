import Foundation

/// On-device NLU for Horizon.
///
/// Apple Intelligence (Foundation Models, iOS 26+) classifies a turn into an
/// action + slots. The server executes it. If the model is missing, still
/// downloading, or times out, callers fall back to `POST /chat` (heuristic).
enum AgentClassifier {
    static var isOnDeviceAvailable: Bool {
        if #available(iOS 26, *) {
            #if canImport(FoundationModels)
            return OnDeviceSession.isAvailable
            #else
            return false
            #endif
        }
        return false
    }

    /// Returns a structured turn, or `nil` to use the heuristic chat path.
    static func classify(_ text: String) async -> AgentTurnPayload? {
        guard isOnDeviceAvailable else { return nil }
        if #available(iOS 26, *) {
            #if canImport(FoundationModels)
            do {
                return try await withThrowingTaskGroup(of: AgentTurnPayload.self) { group in
                    group.addTask { try await OnDeviceSession.classify(text) }
                    group.addTask {
                        try await Task.sleep(nanoseconds: 6_000_000_000)
                        throw AgentClassifyTimeout()
                    }
                    let first = try await group.next()
                    group.cancelAll()
                    return first
                }
            } catch {
                return nil
            }
            #else
            return nil
            #endif
        }
        return nil
    }
}

private struct AgentClassifyTimeout: Error {}
