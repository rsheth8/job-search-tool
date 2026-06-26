import SwiftUI

/// App configuration, persisted in UserDefaults. Defaults point at the live backend
/// and your Slack user id so the app works out of the box; change in Settings.
final class Config: ObservableObject {
    static let shared = Config()

    @AppStorage("baseURL") var baseURL: String = "https://job-search-tool.fly.dev"
    @AppStorage("user") var user: String = "U07LVJVD4PL"
    @AppStorage("token") var token: String = ""   // APPLY_API_TOKEN, optional

    var base: URL? { URL(string: baseURL.trimmingCharacters(in: .whitespaces)) }
}
