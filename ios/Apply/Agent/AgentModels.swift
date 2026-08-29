import Foundation

/// Structured turn the on-device classifier emits. Same shape POST /agent expects.
struct AgentTurnPayload {
    var action: String
    var slots: [String: Any]
    var confidence: Double

    var shouldSendToAgent: Bool {
        action != "UNKNOWN" && confidence >= 0.4
    }
}

extension AgentTurnPayload {
    /// JSON-friendly slots for `POST /agent` (drop nils).
    var jsonSlots: [String: Any] {
        var out: [String: Any] = ["confidence": confidence]
        for (k, v) in slots {
            if v is NSNull { continue }
            out[k] = v
        }
        return out
    }
}

/// Local-only tools: switch tabs after the server returns a deep link.
/// Mutations always go through FastAPI so undo / confirm / isolation stay in one place.
enum AgentLocal {
    @MainActor
    static func followDeepLink(_ link: String?, push: PushManager) {
        guard let link, !link.isEmpty else { return }
        let dest = link.split(separator: ":").first.map(String.init)?.lowercased() ?? link
        if dest == "chat" || dest == "ask" || dest == "assistant" { return }
        push.openDeepLink(link, fromHorizon: true)
    }
}
