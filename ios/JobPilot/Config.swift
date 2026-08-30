import SwiftUI

/// App configuration, persisted in UserDefaults.
///
/// After Sign in with Apple, ``sessionToken`` + ``user`` come from the backend.
/// ``token`` (APPLY_API_TOKEN) remains for the browser extension / legacy gate.
final class Config: ObservableObject {
    static let shared = Config()

    #if targetEnvironment(simulator)
    /// Simulator defaults to local uvicorn so Dev sign-in works (prod has no /auth/dev).
    @AppStorage("baseURL") var baseURL: String = "http://127.0.0.1:8000"
    #else
    @AppStorage("baseURL") var baseURL: String = "https://job-search-tool.fly.dev"
    #endif
    /// Opaque app user id from `/auth/apple` (e.g. `usr_…`).
    @AppStorage("user") var user: String = ""
    /// Stable simulator Dev-sign-in account — matches the locally seeded queue /
    /// identity. Must stay fixed or every Dev sign-in looks like a blank install.
    static let simulatorDevUserId = "usr_5caeab164e844480"
    /// Bearer session from `/auth/apple`. Preferred over ``token``.
    @AppStorage("sessionToken") var sessionToken: String = ""
    @AppStorage("displayName") var displayName: String = ""
    /// Legacy shared APPLY_API_TOKEN — still sent when set (extension parity).
    @AppStorage("token") var token: String = ""

    var base: URL? { URL(string: baseURL.trimmingCharacters(in: .whitespaces)) }

    var isSignedIn: Bool { !sessionToken.isEmpty && !user.isEmpty }
}
