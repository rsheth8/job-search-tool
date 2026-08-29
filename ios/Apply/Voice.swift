import Foundation

/// Conversational first name + time-of-day greetings for the iOS chrome.
///
/// Legal `first_name` is what Autofill puts on forms. Greetings prefer
/// `preferred_name`, then first name, then the first token of the Apple
/// display name. Time of day uses the phone clock — that's the user's time.
enum Voice {
    static func firstName(identity: [String: String]?, displayName: String) -> String {
        let preferred = token(identity?["preferred_name"])
        if !preferred.isEmpty { return preferred }
        let first = token(identity?["first_name"])
        if !first.isEmpty { return first }
        return token(displayName)
    }

    /// "Good morning, Ada" / "Hey Ada" / "Good evening" depending on local hour.
    static func timeGreeting(name: String = "", now: Date = Date(),
                             calendar: Calendar = .current) -> String {
        let hour = calendar.component(.hour, from: now)
        let stem: String
        switch hour {
        case 5..<12: stem = "Good morning"
        case 12..<17: stem = "Good afternoon"
        case 17..<22: stem = "Good evening"
        default: stem = "Hey"
        }
        let who = name.trimmingCharacters(in: .whitespacesAndNewlines)
        if who.isEmpty { return stem }
        if stem == "Hey" { return "Hey \(who)" }
        return "\(stem), \(who)"
    }

    private static func token(_ raw: String?) -> String {
        let text = (raw ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if text.isEmpty || text.contains("@") { return "" }
        guard let piece = text.split(whereSeparator: { $0.isWhitespace }).first else {
            return ""
        }
        let cleaned = String(piece).trimmingCharacters(in: CharacterSet(charactersIn: ".,!?"))
        if cleaned.isEmpty || cleaned.count > 24 { return "" }
        return cleaned
    }
}
